"""Skip face re-detection for a file that already has faces — verification
discipline §16.

Face detection used to delete-and-redetect on every inference pass, even for
a file that was already scanned for faces — wasted GPU/CPU work for no
change in outcome. _run_faces_facet() now skips detection entirely when the
file already has at least one `faces` row, unless called with force=True
(the explicit single-file "↻ re-index" override).

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_faces_skip_existing
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "0"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "0"
    os.environ["IMAGE_TAGGER_FACES"] = "1"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_facesskip_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.worker as worker
    importlib.reload(worker)
    from indexer import engine
    from indexer.models import faces

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    img_path = lib / "person.png"
    Image.new("RGB", (32, 32), (50, 50, 50)).save(img_path)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)
    fid = con.execute("SELECT id FROM files WHERE filename='person.png'").fetchone()["id"]

    calls = {"n": 0}
    fake = faces.FakeFaceEngine({"person.png": [("alice", [0, 0, 10, 10])]})
    original_detect = fake.detect

    def counting_detect(path):
        calls["n"] += 1
        return original_detect(path)

    fake.detect = counting_detect
    faces.set_engine(fake)
    try:
        cfg = engine.get_engine_config(con)
        worker._run_faces_facet(con, fid, str(img_path), engine, cfg["onnx_providers"])
        check(calls["n"] == 1, f"no existing faces -> detection runs once (got {calls['n']})")
        row_count = con.execute(
            "SELECT count(*) c FROM faces WHERE file_id=?", (fid,)).fetchone()["c"]
        check(row_count == 1, f"a face row was written (got {row_count})")

        worker._run_faces_facet(con, fid, str(img_path), engine, cfg["onnx_providers"])
        check(calls["n"] == 1,
              f"faces already present -> detection is skipped, not re-run (got {calls['n']})")

        worker._run_faces_facet(con, fid, str(img_path), engine, cfg["onnx_providers"], force=True)
        check(calls["n"] == 2,
              f"force=True (re-index) always re-detects, even with existing faces (got {calls['n']})")
    finally:
        faces._ENGINE = None
        faces._ENGINE_KEY = None

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — faces skip-if-existing guard verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
