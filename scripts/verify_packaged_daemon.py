"""Smoke-test the packaged Windows runtime without downloading optional models."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "apps" / "desktop" / "dist"
UNPACKED = DIST / "win-unpacked"
PYTHON = UNPACKED / "resources" / "python" / "python.exe"
INDEXER = UNPACKED / "resources" / "indexer"


def require_release_files() -> None:
    required = [
        UNPACKED / "Image Tagger.exe",
        PYTHON,
        INDEXER / "indexer" / "daemon.py",
        UNPACKED / "resources" / "samples" / "beach-sunset-kayak.jpg",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if not list(DIST.glob("Image-Tagger-*-win-x64.exe")):
        missing.append("NSIS installer")
    if not list(DIST.glob("Image-Tagger-*-win-x64.zip")):
        missing.append("portable ZIP")
    if missing:
        raise FileNotFoundError("Missing packaged files: " + ", ".join(missing))


def main() -> int:
    require_release_files()
    subprocess.run(
        [str(PYTHON), "-c", "import PIL, watchdog, piexif, onnxruntime, rapidocr_onnxruntime; print('packaged Python dependencies: ok')"],
        check=True,
        cwd=INDEXER,
    )

    with tempfile.TemporaryDirectory(prefix="image-tagger-release-") as test_home:
        env = os.environ.copy()
        env["IMAGE_TAGGER_HOME"] = test_home
        env["PYTHONUNBUFFERED"] = "1"
        process = subprocess.Popen(
            [str(PYTHON), "-m", "indexer.daemon"],
            cwd=INDEXER,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        output: queue.Queue[dict] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    output.put(json.loads(line))
                except ValueError:
                    output.put({"raw": line.rstrip()})

        threading.Thread(target=read_output, daemon=True).start()

        def until(predicate, timeout: float = 60) -> dict:
            deadline = time.time() + timeout
            while time.time() < deadline:
                item = output.get(timeout=max(0.1, deadline - time.time()))
                if predicate(item):
                    return item
            raise TimeoutError("packaged daemon event timed out")

        request_id = 0

        def call(command: str, **arguments) -> dict:
            nonlocal request_id
            request_id += 1
            assert process.stdin is not None
            process.stdin.write(json.dumps({"id": request_id, "cmd": command, **arguments}) + "\n")
            process.stdin.flush()
            return until(lambda item: item.get("id") == request_id, 90)

        try:
            ready = until(lambda item: item.get("event") == "ready", 30)
            ping = call("ping")
            state = call("model_state")
            doctor = call("doctor")
            assert ping.get("ok") and ping.get("result") == "pong", ping
            assert state.get("ok") and len(state["result"]["facets"]) >= 5, state
            assert doctor.get("ok"), doctor
            print(json.dumps({
                "ready": ready.get("event"),
                "ping": ping["result"],
                "facets": len(state["result"]["facets"]),
                "tier": doctor["result"].get("tier"),
            }, ensure_ascii=False))
        finally:
            if process.poll() is None:
                try:
                    call("stop")
                finally:
                    process.wait(timeout=10)

    print("PASS: packaged runtime, daemon, samples, installer and portable ZIP")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
