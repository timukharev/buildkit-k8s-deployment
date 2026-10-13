import http.client
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("gateway", Path(__file__).resolve().parents[1] / "build-service/server.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as output:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data)
            if kind == tarfile.SYMTYPE:
                member.linkname = "../outside"
            output.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


CONTEXT = archive([("Dockerfile", b"FROM scratch\n", tarfile.REGTYPE)])


class ExtractionTests(unittest.TestCase):
    def check_archive(self, content, limit=1024):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "archive.tgz"
            package.write_bytes(content)
            gateway.extract_context(package, root / "source", limit)

    def test_valid_context(self):
        self.check_archive(CONTEXT)

    def test_traversal_absolute_and_links_rejected(self):
        for name, kind in [("../outside", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE), ("link", tarfile.LNKTYPE)]:
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                self.check_archive(archive([(name, b"", kind)]))

    def test_expansion_limit(self):
        with self.assertRaises(ValueError):
            self.check_archive(CONTEXT, limit=1)

    def test_missing_dockerfile(self):
        with self.assertRaises(ValueError):
            self.check_archive(archive([("app.py", b"", tarfile.REGTYPE)]))


class APITests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.environment = patch.dict("os.environ", {"CONTEXT_ROOT": self.root.name})
        self.environment.start()
        # Generated test-only value: no reusable credential is stored in this repository.
        self.token = "x" * 32
        self.server = gateway.BuildServer(("127.0.0.1", 0), self.token,
                                          "registry.example.com/demo/sample-app", concurrency=1)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.environment.stop()
        self.root.cleanup()

    def request(self, path="/build?tag=test", body=CONTEXT, authorized=True, method="POST"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/gzip"}
        if authorized:
            headers["Authorization"] = "Bearer " + self.token
        connection.request(method, path, body if method == "POST" else None, headers)
        response = connection.getresponse()
        status, payload = response.status, response.read()
        connection.close()
        return status, payload

    def test_unauthorized_and_bad_tag(self):
        self.assertEqual(self.request(authorized=False)[0], 401)
        self.assertEqual(self.request(path="/build?tag=a,b")[0], 400)
        self.assertEqual(self.request(path="/build?tag=a&tag=b")[0], 400)

    def test_upload_limits_and_invalid_archive(self):
        self.server.max_upload = 1
        self.assertEqual(self.request()[0], 413)
        self.server.max_upload = 1024
        self.assertEqual(self.request(body=b"invalid")[0], 400)
        self.assertEqual(self.server.active, 0)

    def test_success_and_no_build_output_disclosure(self):
        with patch.object(gateway.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            status, payload = self.request()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)["image"], "registry.example.com/demo/sample-app:test")
            command = run.call_args.args[0]
            self.assertNotIn(self.token, " ".join(command))
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(self.server.success, 1)
        self.assertEqual(list(Path(self.root.name).iterdir()), [])

    def test_failure_and_timeout_release_slot(self):
        with patch.object(gateway.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(self.request()[0], 502)
        with patch.object(gateway.subprocess, "run", side_effect=subprocess.TimeoutExpired("buildctl", 1)):
            self.assertEqual(self.request()[0], 504)
        self.assertEqual(self.server.active, 0)
        self.assertTrue(self.server.slots.acquire(blocking=False))
        self.server.slots.release()
        self.assertEqual(self.server.failed, 2)

    def test_concurrency_and_health_remain_responsive(self):
        entered, release = threading.Event(), threading.Event()

        def slow_build(*_, **__):
            entered.set()
            release.wait(5)
            return subprocess.CompletedProcess([], 0)

        responses = []
        with patch.object(gateway.subprocess, "run", side_effect=slow_build):
            first = threading.Thread(target=lambda: responses.append(self.request()[0]))
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.request()[0], 503)
                self.assertEqual(self.request(path="/health", method="GET")[0], 200)
                status, metrics = self.request(path="/metrics", method="GET")
                self.assertEqual(status, 200)
                self.assertIn(b"build_service_active_builds 1", metrics)
            finally:
                release.set()
                first.join(5)
        self.assertEqual(responses, [200])

    def test_readiness_checks_backend(self):
        with patch.object(gateway.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(self.request(path="/ready", method="GET")[0], 503)


if __name__ == "__main__":
    unittest.main()
