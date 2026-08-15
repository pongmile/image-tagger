"""Caption model switching no longer force-recaptions the whole library —
verification discipline §16.

Switching the caption model in Settings used to immediately re-queue a
"caption" job for every file that already had one (daemon._set_variant),
because the old caption was written by the model just switched away from.
That flips every existing caption's wording on every model change even when
the user never asked for that — the opposite of "don't waste compute", and
surprising besides. Now a variant switch leaves existing captions alone;
only files with *no* caption at all get queued automatically
(enqueue_missing_captions, unchanged). A full redo is an explicit opt-in via
db.recaption_all() (the Sources page's "↻ Regen all captions" button).

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_caption_variant_switch
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

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_capswitch_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.daemon as daemon
    importlib.reload(daemon)

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    captioned = lib / "captioned.png"
    uncaptioned = lib / "uncaptioned.png"
    for p in (captioned, uncaptioned):
        Image.new("RGB", (16, 16), (10, 20, 30)).save(p)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)
    fid_captioned = con.execute(
        "SELECT id FROM files WHERE filename='captioned.png'").fetchone()["id"]
    fid_uncaptioned = con.execute(
        "SELECT id FROM files WHERE filename='uncaptioned.png'").fetchone()["id"]
    db.set_caption(con, fid_captioned, "a photo of something blue")

    def queued_caption_jobs(kind="caption"):
        # 'caption' (passive backfill, cache-aware) and 'caption_force' (an
        # explicit "regenerate everything" click, always bypasses the cache —
        # see test_caption_cache.py) are deliberately different job kinds;
        # keep them distinct here so a dispatch mistake shows up as a kind
        # mismatch, not just as "a job got queued somehow".
        return {r["file_id"] for r in con.execute(
            "SELECT file_id FROM jobs WHERE kind=? AND state='queued'", (kind,))}

    db.set_setting(con, "caption_variant", "blip-base")
    result = daemon._set_variant(con, "caption", "blip-large")
    check(result["ok"], f"variant switch succeeds (got {result})")
    check(result.get("recaptioning", 0) == 0,
          f"switching models queues nothing by itself (got {result})")
    check(fid_captioned not in queued_caption_jobs(),
          "the already-captioned file is NOT re-queued just because the model changed")
    check(con.execute("SELECT caption FROM files WHERE id=?", (fid_captioned,)).fetchone()["caption"]
          == "a photo of something blue",
          "the existing caption text is untouched")

    missing = db.enqueue_missing_captions(con)
    check(missing == 1, f"enqueue_missing_captions still queues only the uncaptioned file (got {missing})")
    check(fid_uncaptioned in queued_caption_jobs(),
          "the file with no caption at all is queued")
    check(fid_captioned not in queued_caption_jobs(),
          "enqueue_missing_captions does not touch the already-captioned file either")

    # Clear the queue so recaption_all's count is unambiguous.
    with con:
        con.execute("DELETE FROM jobs WHERE kind IN ('caption', 'caption_force')")

    forced = db.recaption_all(con)
    check(forced == 2, f"the explicit 'Regen all captions' action queues every file (got {forced})")
    check({fid_captioned, fid_uncaptioned} <= queued_caption_jobs("caption_force"),
          "recaption_all queues both the captioned and uncaptioned file, as kind='caption_force' "
          "so the forced redo cannot be quietly skipped by the per-model cache "
          "(see test_caption_cache.py)")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — caption variant switch no longer force-recaptions verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
