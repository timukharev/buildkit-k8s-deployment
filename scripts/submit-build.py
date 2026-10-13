"""Upload only the synthetic sample context; credentials stay out of argv/logs."""
import io
import json
import os
from pathlib import Path
import tarfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    body = io.BytesIO()
    with tarfile.open(fileobj=body, mode="w:gz") as archive:
        for name in ("Dockerfile", "app.py", ".dockerignore"):
            archive.add(Path("sample-app") / name, arcname=name, recursive=False)
    url = os.environ["BUILD_SERVICE_URL"].rstrip("/") + "/build?" + urlencode(
        {"tag": os.environ["CI_COMMIT_SHA"]})
    request = Request(url, data=body.getvalue(), headers={
        "Authorization": "Bearer " + os.environ["BUILD_API_TOKEN"],
        "Content-Type": "application/gzip",
    }, method="POST")
    try:
        with urlopen(request, timeout=960) as response:
            result = json.load(response)
        expected = os.environ["IMAGE_REPOSITORY"] + ":" + os.environ["CI_COMMIT_SHA"]
        if result.get("status") != "ok" or result.get("image") != expected:
            raise SystemExit("Build response did not match the requested image")
        print("Sample image built and pushed successfully")
    except HTTPError as error:
        raise SystemExit(f"Build API returned HTTP {error.code}; no deployment performed") from None
    except (URLError, TimeoutError):
        raise SystemExit("Build API unavailable or timed out; no deployment performed") from None


if __name__ == "__main__":
    main()
