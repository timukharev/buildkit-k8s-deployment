"""Trusted-team BuildKit gateway. No third-party Python dependencies."""
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def extract_context(archive, destination, max_bytes, max_files=10000):
    """Only regular files/directories; validate everything before extraction."""
    with tarfile.open(archive, mode="r:gz") as source:
        members = []
        total = 0
        for member in source:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or
                    not (member.isfile() or member.isdir())):
                raise ValueError("Archive contains an unsafe entry")
            total += member.size
            members.append(member)
            if total > max_bytes or len(members) > max_files:
                raise ValueError("Expanded context exceeds limits")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not (destination / "Dockerfile").is_file():
        raise ValueError("Context must contain a root Dockerfile")


class BuildServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, token, repository, concurrency=2):
        if not token or len(token) < 32:
            raise ValueError("BUILD_API_TOKEN must contain at least 32 characters")
        if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+", repository):
            raise ValueError("IMAGE_REPOSITORY must be a registry/path without a tag")
        if concurrency < 1:
            raise ValueError("MAX_CONCURRENT_BUILDS must be positive")
        self.token = token
        self.repository = repository
        self.concurrency = concurrency
        self.slots = threading.BoundedSemaphore(concurrency)
        self.lock = threading.Lock()
        self.active = self.success = self.failed = 0
        self.duration = 0.0
        self.max_upload = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
        self.max_context = int(os.getenv("MAX_CONTEXT_BYTES", str(100 * 1024 * 1024)))
        self.build_timeout = int(os.getenv("BUILD_TIMEOUT_SECONDS", "900"))
        self.context_root = Path(os.getenv("CONTEXT_ROOT", "/tmp/contexts"))
        self.context_root.mkdir(parents=True, exist_ok=True)
        super().__init__(address, BuildHandler)

    def metrics(self):
        with self.lock:
            values = dict(active=self.active, success=self.success, failed=self.failed,
                          duration=self.duration)
        return (
            "# TYPE build_service_active_builds gauge\n"
            f"build_service_active_builds {values['active']}\n"
            "# TYPE build_service_slot_utilization_percent gauge\n"
            f"build_service_slot_utilization_percent {100 * values['active'] / self.concurrency}\n"
            "# TYPE build_service_builds_total counter\n"
            f"build_service_builds_total{{status=\"success\"}} {values['success']}\n"
            f"build_service_builds_total{{status=\"failed\"}} {values['failed']}\n"
            "# TYPE build_service_build_duration_seconds summary\n"
            f"build_service_build_duration_seconds_sum {values['duration']}\n"
            f"build_service_build_duration_seconds_count {values['success'] + values['failed']}\n"
        )


class BuildHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        # Request headers, query strings, and build output may contain secrets.
        pass

    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok"})
        if self.path == "/ready":
            try:
                result = subprocess.run(["buildctl", "debug", "workers"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=3, check=False)
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ready = False
            return self.reply(200 if ready else 503, {"ready": ready})
        if self.path == "/metrics":
            payload = self.server.metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        self.reply(404, {"error": "Unknown endpoint"})

    def do_POST(self):
        if urlsplit(self.path).path != "/build":
            return self.reply(404, {"error": "Unknown endpoint"})
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            return self.reply(401, {"error": "Unauthorized"})
        parameters = parse_qs(urlsplit(self.path).query)
        tags = parameters.get("tag", [])
        if len(tags) != 1 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tags[0]):
            return self.reply(400, {"error": "Exactly one valid image tag is required"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if self.headers.get("Transfer-Encoding") or not 0 < length <= self.server.max_upload:
            return self.reply(413, {"error": "A bounded Content-Length is required"})
        if self.headers.get("Content-Type") != "application/gzip":
            return self.reply(415, {"error": "Use application/gzip"})
        if not self.server.slots.acquire(blocking=False):
            return self.reply(503, {"error": "Build capacity exhausted; retry later"})
        started = time.monotonic()
        success = False
        with self.server.lock:
            self.server.active += 1
        try:
            with tempfile.TemporaryDirectory(dir=self.server.context_root) as directory:
                root = Path(directory)
                archive = root / "context.tar.gz"
                with archive.open("wb") as output:
                    remaining = length
                    upload_deadline = time.monotonic() + 30
                    while remaining:
                        available_time = upload_deadline - time.monotonic()
                        if available_time <= 0:
                            raise socket.timeout()
                        self.connection.settimeout(available_time)
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            raise ValueError("Incomplete context upload")
                        output.write(chunk)
                        remaining -= len(chunk)
                self.connection.settimeout(30)
                context = root / "source"
                context.mkdir()
                extract_context(archive, context, self.server.max_context)
                image = self.server.repository + ":" + tags[0]
                command = ["buildctl", "build", "--frontend", "dockerfile.v0",
                           "--local", f"context={context}", "--local", f"dockerfile={context}",
                           "--output", f"type=image,name={image},push=true"]
                # Do not expose Dockerfile output or credentials in logs/API responses.
                result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL,
                                        timeout=self.server.build_timeout, check=False)
                success = result.returncode == 0
                status = 200 if success else 502
                payload = {"status": "ok" if success else "failed", "image": image}
        except (ValueError, tarfile.TarError, EOFError):
            status, payload = 400, {"error": "Invalid or unsafe build context"}
        except (subprocess.TimeoutExpired, socket.timeout):
            status, payload = 504, {"error": "Upload or build timed out"}
        except OSError:
            status, payload = 502, {"error": "Build backend unavailable"}
        finally:
            with self.server.lock:
                self.server.active -= 1
                self.server.success += int(success)
                self.server.failed += int(not success)
                self.server.duration += time.monotonic() - started
            self.server.slots.release()
        self.reply(status, payload)


if __name__ == "__main__":
    server = BuildServer(("0.0.0.0", 8080), os.environ["BUILD_API_TOKEN"],
                         os.environ["IMAGE_REPOSITORY"],
                         int(os.getenv("MAX_CONCURRENT_BUILDS", "2")))
    # Stop accepting work, then let bounded in-flight requests finish on SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    server.serve_forever()
    deadline = time.monotonic() + server.build_timeout + 35
    while time.monotonic() < deadline:
        with server.lock:
            if server.active == 0:
                break
        time.sleep(0.1)
    server.server_close()
