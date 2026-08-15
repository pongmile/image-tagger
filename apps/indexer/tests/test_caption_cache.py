"""Per-model caption cache — verification discipline §16.

Mirrors test_facet_model_cache.py's WD14 story, for captioning: a bulk
"↻ Reindex" (or a caption-model switch backfilling existing files) used to
call the caption model fresh for *every* file, every time, even when that
file was already captioned by the model that is still active. Captioning is
the heaviest per-file facet in this app (real GPU seconds per image, not
onnxruntime's sub-second WD14 pass), so on a library of any size this was
minutes-to-hours of GPU time spent reproducing output byte-identical to what
was already stored -- diagnosed against the maintainer's own library, where
~5,300 already-captioned images sat queued for a redo with no way to tell
that from genuine new work.

_run_caption_facet() now checks facet_model_cache (facet='caption') before
calling the model, exactly mirroring WD14's pattern:
  * same variant as last time -> restore from cache, no model load/inference.
  * a variant switch -> run inference (cache miss), and stash the result so a
    switch *back* restores instantly instead of re-generating.
  * the queued kind='caption' job (bulk backfill paths --
    enqueue_missing_captions, enqueue_job_for_captioned_files) defaults to
    force=False and gets the cache benefit.
  * a genuinely deliberate "redo everything" click -- the single-file
    "↻ re-Description" button, and the library-wide "↻ Regen all captions" /
    per-root "↻ Desc" actions -- must NOT be silently defeated by the cache:
    the single-file action always passes force=True, and the bulk "regen"
    actions enqueue a distinct kind='caption_force' that always forces.

Verified here via a call-counting wrapper around caption.get_engine (same
technique test_facet_model_cache.py uses for wd14.get_engine): a cache hit
never reaches the model at all, not just "returns fast" -- and the
force-vs-cached job kinds are each driven through worker.run_job() end to
end, not called directly, so a dispatch mistake would show up here too.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_caption_cache
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
    os.environ["IMAGE_TAGGER_FACES"] = "0"
    os.environ["IMAGE_TAGGER_CAPTION"] = "1"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_captioncache_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.worker as worker
    importlib.reload(worker)
    from indexer import engine
    from indexer.models import caption

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    img_path = lib / "girl.png"
    Image.new("RGB", (32, 32), (120, 80, 200)).save(img_path)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)
    fid = con.execute("SELECT id FROM files WHERE filename='girl.png'").fetchone()["id"]

    calls = {"n": 0}
    original_get_engine = caption.get_engine

    def counting_get_engine(*a, **kw):
        calls["n"] += 1
        variant = db.get_setting(con, "caption_variant")
        text = "a photo of a girl" if variant == "blip-base" else "a photo of a woman outdoors"
        return caption.FakeCaptionEngine({"girl.png": text})

    def current_caption():
        return con.execute("SELECT caption FROM files WHERE id=?", (fid,)).fetchone()["caption"]

    caption.get_engine = counting_get_engine
    try:
        db.set_setting(con, "caption_variant", "blip-base")
        cfg = engine.get_engine_config(con)

        worker._run_caption_facet(con, fid, str(img_path), engine, cfg["torch_device"])
        check(calls["n"] == 1, f"cache miss on first run calls the model once (got {calls['n']})")
        check(current_caption() == "a photo of a girl", "blip-base's caption is written")

        worker._run_caption_facet(con, fid, str(img_path), engine, cfg["torch_device"])
        check(calls["n"] == 1,
              f"re-running with the same active variant never calls the model again (got {calls['n']})")
        check(current_caption() == "a photo of a girl", "the caption is unchanged after the cache hit")

        db.set_setting(con, "caption_variant", "blip-large")
        worker._run_caption_facet(con, fid, str(img_path), engine, cfg["torch_device"])
        check(calls["n"] == 2, f"switching variant is a cache miss, calls the model (got {calls['n']})")
        check(current_caption() == "a photo of a woman outdoors", "blip-large's caption is now stored")

        db.set_setting(con, "caption_variant", "blip-base")
        worker._run_caption_facet(con, fid, str(img_path), engine, cfg["torch_device"])
        check(calls["n"] == 2,
              f"switching back to blip-base restores from cache, no model call (got {calls['n']})")
        check(current_caption() == "a photo of a girl",
              "blip-base's own caption comes back exactly as it was")

        # Explicit single-file force bypasses the cache even on a hit -- the
        # preview pane's "↻ re-Description" click must never silently no-op.
        worker._run_caption_facet(con, fid, str(img_path), engine, cfg["torch_device"], force=True)
        check(calls["n"] == 3,
              f"force=True (re-Description) always calls the model, even on a cache hit (got {calls['n']})")

        # _run_caption() threads force through the same way -- this is what
        # the daemon's single-file 'recaption' action and the queued
        # kind='caption'/'caption_force' jobs actually call.
        worker._run_caption(con, fid)
        check(calls["n"] == 3, f"_run_caption() default (force=False) still hits the cache (got {calls['n']})")
        worker._run_caption(con, fid, force=True)
        check(calls["n"] == 4, f"_run_caption(force=True) still bypasses the cache (got {calls['n']})")

        # --- end to end through the real job queue, not called directly ------
        # A bulk "regenerate everything" action (recaption_all/_root) must not
        # be defeated by the very cache that makes a passive backfill cheap:
        # it enqueues kind='caption_force', which worker.run_job() must
        # dispatch with force=True.
        queued = db.recaption_all(con)
        check(queued == 1, f"recaption_all queues the one analyzable file (got {queued})")
        row = con.execute(
            "SELECT kind FROM jobs WHERE file_id=? AND state='queued' ORDER BY id DESC LIMIT 1", (fid,)
        ).fetchone()
        check(row is not None and row["kind"] == "caption_force",
              f"recaption_all's job is kind='caption_force', not plain 'caption' (got {row['kind'] if row else None})")
        worker.drain(con)
        check(calls["n"] == 5,
              f"recaption_all's job forces a fresh call even though blip-base was already cached (got {calls['n']})")
        check(current_caption() == "a photo of a girl",
              "the regenerated caption still matches blip-base's own output")

        # A passive backfill (variant-switch re-queue), by contrast, must get
        # the cache benefit: re-queueing captioning for an already-captioned
        # file under the *same* active variant should not call the model again.
        queued = db.enqueue_job_for_captioned_files(con)
        check(queued == 1, f"enqueue_job_for_captioned_files queues the file (got {queued})")
        row = con.execute(
            "SELECT kind FROM jobs WHERE file_id=? AND state='queued' ORDER BY id DESC LIMIT 1", (fid,)
        ).fetchone()
        check(row is not None and row["kind"] == "caption",
              f"the passive backfill's job is plain 'caption' (got {row['kind'] if row else None})")
        worker.drain(con)
        check(calls["n"] == 5,
              f"a passive re-queue under an unchanged variant hits the cache, no model call (got {calls['n']})")
    finally:
        caption.get_engine = original_get_engine

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — per-model caption cache verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
