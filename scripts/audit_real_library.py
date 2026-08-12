"""Rescan and re-index the real test_search source through the packaged daemon."""
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
SOURCE = Path.home() / "Documents" / "test_search"


def main() -> int:
    env = os.environ.copy()
    env.pop("IMAGE_TAGGER_HOME", None)
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [str(PYTHON), "-m", "indexer.daemon", "--auto"],
        cwd=str(INDEXER), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    messages: queue.Queue[dict] = queue.Queue()
    stderr: list[str] = []

    def read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                messages.put({"raw": line.rstrip()})

    def read_stderr() -> None:
        assert process.stderr is not None
        stderr.extend(line.rstrip() for line in process.stderr)

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
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
        raise TimeoutError(f"daemon timed out; stderr={stderr[-20:]}")

    def call(command: str, **arguments) -> dict:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None
        process.stdin.write(json.dumps({"id": request_id, "cmd": command, **arguments}) + "\n")
        process.stdin.flush()
        response = until(lambda item: item.get("id") == request_id, 180)
        if not response.get("ok"):
            raise RuntimeError(response.get("error"))
        return response["result"]

    try:
        until(lambda item: item.get("event") == "ready", 60)
        roots = call("roots")["roots"]
        wanted = os.path.normcase(os.path.abspath(SOURCE))
        source = next((item for item in roots if item["mode"] == "include"
                       and os.path.normcase(os.path.abspath(item["path"])) == wanted), None)
        if source is None:
            raise RuntimeError(f"source is not configured: {SOURCE}")
        scan = call("rescan_root", root_id=source["id"])
        queued = call("reindex_all")
        print(json.dumps({"scan": scan, "reindex": queued}, ensure_ascii=False), flush=True)

        stable = 0
        last_report = 0.0
        deadline = time.time() + 1800
        final = None
        while time.time() < deadline:
            final = call("progress")
            jobs = final.get("jobs", {})
            active = int(jobs.get("queued", 0)) + int(jobs.get("running", 0))
            stable = stable + 1 if active == 0 else 0
            if time.time() - last_report >= 10:
                print(json.dumps(final, ensure_ascii=False), flush=True)
                last_report = time.time()
            if stable >= 3:
                break
            time.sleep(1)
        else:
            raise TimeoutError(f"indexing did not finish: {final}")
        if final and int(final.get("jobs", {}).get("error", 0)):
            raise RuntimeError(f"indexing errors remain: {final}")
        print(json.dumps({"final": final, "stderr_tail": stderr[-10:]}, ensure_ascii=False))
        return 0
    finally:
        if process.poll() is None:
            try:
                call("stop")
            finally:
                process.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
