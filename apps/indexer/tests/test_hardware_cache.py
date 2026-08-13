"""Hardware-detection caching — spec §5.2, verification discipline §16.

worker.py calls engine.get_engine_config() -> detect_hardware() once per job:
the combined infer() job, and each narrow _run_caption/_run_clip job again for
the same file. detect_hardware() used to re-probe from scratch on every one of
those calls, spawning two throwaway subprocesses each time to keep torch/
onnxruntime out of the daemon process (see _probe_python's docstring). The
torch/CUDA probe alone measured ~3.2s on this machine, almost entirely torch's
own cold-import time in a fresh interpreter — repeated for hardware that
cannot change mid-session. On a library where most files queue both a reindex
and a standalone caption job, that is ~7s of pure subprocess overhead per
file, dwarfing anything Task Manager would show as sustained CPU/GPU/disk use
and making active indexing look idle.

Verified here: the probe subprocess runs at most once across many
get_engine_config() calls, regardless of whether a `con` (settings overrides)
is passed, and the returned dict is unchanged.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_hardware_cache
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imgtag_hwcache_"))
    os.environ["IMAGE_TAGGER_HOME"] = str(tmp / "home")

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    from indexer import engine

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # A fresh module state: nothing has probed hardware yet in this process.
    engine._HARDWARE_CACHE = None

    calls = {"n": 0}
    original_probe = engine._probe_python

    def counting_probe(code, timeout=8.0):
        calls["n"] += 1
        # A real (cheap, deterministic) response shaped like the two probes
        # detect_hardware() actually issues, so the surrounding parsing in
        # detect_hardware() exercises its normal path rather than being
        # short-circuited by a probe failure.
        if "get_device_properties" in code:
            return '{"cuda": false, "vram_gb": 0.0, "name": null}'
        return "[]"

    engine._probe_python = counting_probe
    try:
        first = engine.detect_hardware()
        check(calls["n"] == 2,
              f"first call probes torch+onnx once each (got {calls['n']} probe(s))")

        second = engine.detect_hardware()
        check(calls["n"] == 2,
              f"a second call reuses the cache instead of re-probing (got {calls['n']})")
        check(second == first, "cached result is returned unchanged")

        con = db.connect()
        for _ in range(5):
            engine.get_engine_config(con)
        check(calls["n"] == 2,
              f"get_engine_config(con), called repeatedly (as worker.py does once "
              f"per job), still probes only once total (got {calls['n']})")

        engine.get_engine_config()  # con=None path (worker.py's other call shape)
        check(calls["n"] == 2, "the con=None call shape also reuses the cache")
    finally:
        engine._probe_python = original_probe
        engine._HARDWARE_CACHE = None  # leave module state clean for later tests

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — hardware-detection caching verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
