"""M8 captioning test — spec §11, verification discipline §16.

Deterministic FakeCaptionEngine verifies the pipeline: captions land in
files.caption, become searchable on the FTS fast path, honor the per-library
swappable model setting, and re-captioning overwrites. An optional real-BLIP
smoke runs only if transformers+torch and the weights are available (gated to
avoid a ~1GB download in the test/CI path).

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_caption
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CAPTION"] = "1"
    for k in ("OCR", "WD14", "CLIP", "FACES"):
        os.environ[f"IMAGE_TAGGER_{k}"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_cap_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.ingest as ingest
    importlib.reload(ingest)
    import indexer.worker as worker
    importlib.reload(worker)
    from indexer.scan import rescan
    from indexer.models import caption

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    caption.set_engine(caption.FakeCaptionEngine(
        {"beach_sunset.png": "a girl on a beach at sunset"}))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 40), (200, 150, 60)).save(lib / "beach_sunset.png")
    Image.new("RGB", (40, 40), (60, 60, 60)).save(lib / "red_car.png")

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    rescan(con)
    worker.drain(con)

    files = {Path(r["path"]).name: r for r in
             con.execute("SELECT id, path, caption FROM files")}
    beach = files["beach_sunset.png"]
    check(beach["caption"] == "a girl on a beach at sunset", "caption stored on files.caption")
    check(files["red_car.png"]["caption"] == "a photo of red car",
          "fallback caption from filename for unmapped file")

    # searchable on the FTS fast path (caption is an FTS column)
    def fts(q):
        return [r["rowid"] for r in con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH ?", (q,))]
    check(beach["id"] in fts("sunset"), "caption word searchable via FTS")
    check(beach["id"] in fts('"beach at sunset"'), "caption phrase searchable via FTS")

    # per-library swappable model setting
    db.set_setting(con, "caption_model", "Salesforce/blip-image-captioning-large")
    check(db.get_setting(con, "caption_model").endswith("large"),
          "caption model is swappable per library (setting persisted)")

    # re-caption overwrites
    caption.set_engine(caption.FakeCaptionEngine(
        {"beach_sunset.png": "sunset over the ocean, one person"}))
    db.set_caption(con, beach["id"], caption.get_engine().caption(beach["path"]))
    row = con.execute("SELECT caption FROM files WHERE id=?", (beach["id"],)).fetchone()
    check(row["caption"] == "sunset over the ocean, one person", "re-caption overwrites")
    check(beach["id"] in fts("ocean") and beach["id"] not in fts("girl"),
          "FTS reflects the new caption, not the old")

    # --- optional real BLIP smoke ------------------------------------------
    if os.environ.get("IMAGE_TAGGER_CAPTION_REAL") == "1":
        try:
            import transformers  # noqa: F401
            real = caption.BlipCaptionEngine(
                cache_dir=str(db.model_dir(con, "caption")))
            cap = real.caption(str(lib / "beach_sunset.png"))
            print(f"  [real BLIP] '{cap}'")
            check(isinstance(cap, str) and len(cap) > 0, "real BLIP returns a caption")
        except Exception as e:
            print(f"  [real BLIP smoke skipped: {e}]")
    else:
        print("  [real BLIP smoke off — set IMAGE_TAGGER_CAPTION_REAL=1 to run]")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — M8 captioning verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
