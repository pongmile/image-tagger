"""Install and verify optional dependencies through the packaged app daemon.

This intentionally exercises the same RPC path as the Models screen instead of
calling pip directly. It is useful for release audits and repairing a runtime
whose CUDA torch wheel was replaced by a CPU dependency.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "apps/desktop/dist/win-unpacked/resources/python/python.exe"
INDEXER = ROOT / "apps/desktop/dist/win-unpacked/resources/indexer"
FACETS = ("ocr", "wd14", "clip", "faces", "caption", "sklearn")


def main() -> int:
    env = os.environ.copy()
    env.pop("IMAGE_TAGGER_HOME", None)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [str(PYTHON), "-m", "indexer.daemon"],
        cwd=str(INDEXER),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    messages: queue.Queue[dict] = queue.Queue()
    pending: list[dict] = []

    def reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                messages.put({"raw": line.rstrip()})

    threading.Thread(target=reader, daemon=True).start()

    def until(predicate, timeout: float) -> dict:
        deadline = time.time() + timeout
        for item in list(pending):
            if predicate(item):
                pending.remove(item)
                return item
        while time.time() < deadline:
            item = messages.get(timeout=max(0.1, deadline - time.time()))
            if predicate(item):
                return item
            pending.append(item)
        raise TimeoutError("daemon event timed out")

    request_id = 0

    def call(command: str, **arguments) -> dict:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None
        process.stdin.write(json.dumps({"id": request_id, "cmd": command, **arguments}) + "\n")
        process.stdin.flush()
        return until(lambda item: item.get("id") == request_id, 120)

    try:
        until(lambda item: item.get("event") == "ready", 30)
        for facet in FACETS:
            key = f"dep:{facet}"
            response = call("install_dependency", facet=facet)
            if not response.get("ok") or not response.get("result", {}).get("started"):
                raise RuntimeError(f"could not start {key}: {response}")
            print(f"START {key}", flush=True)
            finished = until(
                lambda item: item.get("event") == "download_done" and item.get("model") == key,
                3600,
            )
            print(json.dumps(finished, ensure_ascii=False), flush=True)
            if not finished.get("ok"):
                raise RuntimeError(f"{key} failed: {finished.get('error')}")
        state = call("model_state")["result"]
        doctor = call("doctor")["result"]
        print(json.dumps({"doctor": doctor, "facets": state["facets"]}, ensure_ascii=False))
        return 0
    finally:
        if process.poll() is None:
            try:
                call("stop")
            finally:
                process.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
