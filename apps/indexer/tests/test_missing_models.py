"""Missing-model resilience test — spec §5/§11, verification discipline §16.

Captioning (BLIP/JoyCaption) and Real faces (InsightFace) are both optional,
heavyweight facets. Per §11/§5: a missing dependency or an undownloaded model
must never require the rest of a file's indexing to fail, and must never be
silently unrecoverable — once the model is actually installed (or the daemon's
readiness check is satisfied), the same job kind the preview pane's "↻
re-Description" button enqueues must produce a real caption.

Three things are verified:
  1. With both facets enabled but neither model installed, indexing a file
     still completes successfully (index_status='done'); the other enabled
     facet (wd14) still runs; no caption/faces are recorded, just skipped.
  2. A *genuine* caption engine failure (dependency+model reported ready, but
     the engine still won't load — a real misconfiguration, not "not
     installed yet") still errors the job — the pre-existing troubleshooting
     behavior must not be silently swallowed by the new leniency.
  3. Once the model is "installed" (the .ready marker exists) and a working
     engine is available, re-running the same "caption" job kind used by
     "↻ re-Description" actually produces a caption.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_missing_models
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "1"
    os.environ["IMAGE_TAGGER_CLIP"] = "0"
    os.environ["IMAGE_TAGGER_FACES"] = "1"
    os.environ["IMAGE_TAGGER_CAPTION"] = "1"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_missing_"))
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
    from indexer import engine
    from indexer.scan import rescan
    from indexer.models import caption, faces, wd14

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    wd14.set_engine(wd14.FakeTaggerEngine([
        wd14.TagResult("general", "outdoors", 0.9),
    ]))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 60), (10, 120, 200)).save(lib / "photo1.jpg")

    con = db.connect()
    db.add_root(con, str(lib), mode="include")

    # --- 1. neither model installed: the expected, common state before the
    # user ever opens the Models screen. --------------------------------
    caption.set_engine(caption.NullCaptionEngine())
    faces.set_engine(faces.NullFaceEngine())

    ready_cap, why_cap = engine.facet_model_ready(con, "caption")
    ready_face, why_face = engine.facet_model_ready(con, "faces")
    check(not ready_cap and bool(why_cap),
          f"facet_model_ready reports caption not ready ({why_cap!r})")
    check(not ready_face and bool(why_face),
          f"facet_model_ready reports faces not ready ({why_face!r})")

    print("scan:", rescan(con))
    n = worker.drain(con)
    print(f"worker processed {n} jobs")

    row = con.execute(
        "SELECT id, index_status, caption FROM files WHERE filename='photo1.jpg'"
    ).fetchone()
    fid = row["id"]
    check(row["index_status"] == "done",
          f"file indexes successfully with both models missing (got {row['index_status']!r})")
    check(not row["caption"], "caption stays empty when the model isn't installed")
    wd14_tags = con.execute(
        "SELECT COUNT(*) c FROM file_tags WHERE file_id=? AND source='wd14'", (fid,)
    ).fetchone()["c"]
    check(wd14_tags > 0, "other enabled facets (wd14) still run when caption/faces are missing")
    faces_count = con.execute(
        "SELECT COUNT(*) c FROM faces WHERE file_id=?", (fid,)
    ).fetchone()["c"]
    check(faces_count == 0, "no faces recorded when InsightFace isn't installed (graceful no-op)")
    job = con.execute(
        "SELECT state FROM jobs WHERE file_id=? ORDER BY id DESC LIMIT 1", (fid,)
    ).fetchone()
    check(job["state"] == "done", "the infer job itself is not marked errored")

    # --- 2. genuine failure despite the model reportedly being ready must
    # still error the job — don't accidentally swallow real misconfigurations.
    orig_ready = engine.facet_model_ready
    engine.facet_model_ready = (
        lambda c, facet: (True, "") if facet == "caption" else orig_ready(c, facet))
    try:
        Image.new("RGB", (60, 60), (200, 10, 10)).save(lib / "photo2.jpg")
        print("scan:", rescan(con))
        worker.drain(con)
        row2 = con.execute(
            "SELECT id, index_status FROM files WHERE filename='photo2.jpg'"
        ).fetchone()
        check(row2["index_status"] == "error",
              "a genuine caption engine failure (model reported ready) still errors the file")
        job2 = con.execute(
            "SELECT state, error FROM jobs WHERE file_id=? ORDER BY id DESC LIMIT 1",
            (row2["id"],)
        ).fetchone()
        check(job2["state"] == "error"
              and "caption engine failed to load" in (job2["error"] or ""),
              f"the job error explains the failure (got {job2['error']!r})")
    finally:
        engine.facet_model_ready = orig_ready

    # --- 3. once the model is "installed" (marker present) and a working
    # engine is available, the "caption" job kind (what "↻ re-Description"
    # enqueues) really works. facet_model_ready is simulated as ready the
    # same way step 2 did, so this doesn't depend on transformers/accelerate
    # actually being importable in whichever venv runs this test (they're
    # optional runtime-installed deps, not part of the base dev venv). -----
    marker = engine.model_ready_marker(con, "caption")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ready\n", encoding="utf-8")
    engine.facet_model_ready = (
        lambda c, facet: (True, "") if facet == "caption" else orig_ready(c, facet))
    try:
        caption.set_engine(caption.FakeCaptionEngine({"photo1.jpg": "a blue photo"}))
        db.enqueue_job(con, fid, "caption", priority=100)
        worker.drain(con)
        row3 = con.execute("SELECT caption FROM files WHERE id=?", (fid,)).fetchone()
        check(row3["caption"] == "a blue photo",
              "re-Description produces a real caption once the model is installed")
    finally:
        engine.facet_model_ready = orig_ready

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — missing-model resilience (captioning + faces) verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
