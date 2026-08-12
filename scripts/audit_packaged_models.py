"""Warm every selected model through a fresh packaged daemon process."""
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
MODELS = ("wd14", "clip", "insightface", "caption")


def audit(model: str) -> dict:
    env = os.environ.copy()
    env.pop("IMAGE_TAGGER_HOME", None)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [str(PYTHON), "-m", "indexer.daemon"], cwd=str(INDEXER), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    messages: queue.Queue[dict] = queue.Queue()
    errors: list[str] = []

    def stdout_reader() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                messages.put({"raw": line.rstrip()})

    def stderr_reader() -> None:
        assert process.stderr is not None
        errors.extend(line.rstrip() for line in process.stderr)

    threading.Thread(target=stdout_reader, daemon=True).start()
    threading.Thread(target=stderr_reader, daemon=True).start()
    pending: list[dict] = []
    request_id = 0

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
        raise TimeoutError(f"{model} timed out; stderr={errors[-10:]}")

    def call(command: str, **arguments) -> dict:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None
        process.stdin.write(json.dumps({"id": request_id, "cmd": command, **arguments}) + "\n")
        process.stdin.flush()
        return until(lambda item: item.get("id") == request_id, 120)

    try:
        until(lambda item: item.get("event") == "ready", 30)
        started = call("download", model=model)
        if not started.get("ok") or not started.get("result", {}).get("started"):
            raise RuntimeError(f"could not start {model}: {started}")
        finished = until(
            lambda item: item.get("event") == "download_done" and item.get("model") == model,
            3600,
        )
        if not finished.get("ok"):
            finished["stderr"] = errors[-20:]
            raise RuntimeError(json.dumps(finished, ensure_ascii=False))
        return {"result": finished, "stderr_tail": errors[-5:]}
    finally:
        if process.poll() is None:
            try:
                call("stop")
            finally:
                process.wait(timeout=10)


def main() -> int:
    for model in MODELS:
        print(f"START {model}", flush=True)
        print(json.dumps(audit(model), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
