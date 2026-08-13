"""Resetting a learned tag — spec §5.3, verification discipline §16.

A few-shot tag that has learned the wrong thing cannot be repaired by
retraining: train() only ever adds to the same examples that produced the bad
behaviour, and apply() rewrites the same rows. The Learned tags view's Delete
therefore calls learned.forget(), which has to undo precisely the learned layer
and nothing else.

Verified here:
  1. Every auto-applied source='learned' row is withdrawn (the "applied to N
     file(s)" count goes to zero) and stops matching in FTS.
  2. The hand tagging that seeded the tag survives untouched — this is the
     whole point of a reset rather than deleting the tag.
  3. The trained model and every accumulated example are gone, so a later
     apply() is a no-op and newly indexed files are not scored against it.
  4. Rejections recorded against *other* sources survive; only the ones the
     learned loop created are cleared.
  5. The daemon command refuses an unknown tag instead of creating one.
  6. Teaching the same tag again still works, re-seeded from the manual tags.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_learned_forget
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "1"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "0"
    os.environ["IMAGE_TAGGER_FACES"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_forget_"))
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
    from indexer import vec, learned
    from indexer.scan import rescan
    from indexer.models import clip

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    if not vec.available():
        print("sqlite-vec required for M7 — pip install sqlite-vec")
        return 1

    # The visual axis ("aurora") is deliberately *not* the tag name ("nova"):
    # files_fts indexes filename and path too, so a tag whose name appears in
    # the filename would keep matching after the reset for reasons that have
    # nothing to do with tagging. Keeping them distinct makes the FTS check
    # below actually about the tag. Category is a plain concept rather than
    # 'character' for the same reason — character suggestions additionally
    # require filename evidence (_eligible_for_auto_apply).
    clip.set_engine(clip.FakeClipEngine(["aurora", "beach", "forest", "city", "park", "misc"]))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    pos_files = ["aurora_beach.png", "aurora_forest.png", "aurora_city.png",
                 "aurora_park.png", "aurora_beach2.png"]
    heldout = "aurora_lone.png"
    rejected_learned, rejected_wd14 = "misc_city.png", "misc_park.png"
    for fn in pos_files + [heldout, rejected_learned, rejected_wd14]:
        Image.new("RGB", (32, 32), (100, 100, 100)).save(lib / fn)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    rescan(con)
    worker.drain(con)

    fid = {Path(r["path"]).name: r["id"] for r in
           con.execute("SELECT id, path FROM files")}
    for fn in pos_files:
        db.add_manual_tag(con, fid[fn], "nova", "concept")

    summary = learned.build(con, "concept", "nova", space="clip")
    check(summary is not None, f"learned tag trains (got {summary})")
    tag_id = db.get_or_create_tag(con, "nova", "concept")

    def source_of(file_id):
        row = con.execute(
            "SELECT source FROM file_tags WHERE file_id=? AND tag_id=?",
            (file_id, tag_id)).fetchone()
        return row["source"] if row else None

    def count(sql, *args):
        return con.execute(sql, args).fetchone()[0]

    check(source_of(fid[heldout]) == "learned",
          "held-out image auto-tagged before the reset")
    applied_before = count(
        "SELECT count(*) FROM file_tags WHERE tag_id=? AND source='learned'", tag_id)
    check(applied_before > 0, f"tag is applied to {applied_before} file(s) before the reset")

    # Two rejections of the same tag on different files: one the learned loop
    # produced, one from wd14. Written directly rather than through reject(),
    # which would retrain and re-apply and so change the applied set this test
    # is about. write_auto_tags() relies on the wd14 row to stop that tag
    # reappearing on the next reindex, so the reset must not take it too.
    with con:
        for file_name, source in ((rejected_learned, "learned"), (rejected_wd14, "wd14")):
            con.execute(
                "INSERT INTO rejected_tags (file_id, tag_id, source, rejected_at) "
                "VALUES (?,?,?,1) ON CONFLICT(file_id, tag_id) DO UPDATE SET "
                "source=excluded.source",
                (fid[file_name], tag_id, source))
    check(count("SELECT count(*) FROM rejected_tags WHERE tag_id=? AND source='learned'",
                tag_id) == 1, "learned rejection recorded before the reset")

    # --- the reset ---------------------------------------------------------
    result = learned.forget(con, tag_id)

    check(result["ok"] and result["was_trained"] and result["unapplied"] == applied_before,
          f"forget() reports the {applied_before} withdrawn row(s) (got {result})")
    check(count("SELECT count(*) FROM file_tags WHERE tag_id=? AND source='learned'",
                tag_id) == 0,
          "every auto-applied 'learned' row is withdrawn")
    check(all(source_of(fid[fn]) == "manual" for fn in pos_files),
          "hand tagging survives the reset untouched")
    check(count("SELECT count(*) FROM learned_tags WHERE tag_id=?", tag_id) == 0,
          "the trained model is gone")
    check(count("SELECT count(*) FROM tag_examples WHERE tag_id=?", tag_id) == 0,
          "every accumulated example is cleared")
    check(count("SELECT count(*) FROM rejected_tags WHERE tag_id=? AND source='learned'",
                tag_id) == 0,
          "rejections the learned loop created are cleared")
    check(count("SELECT count(*) FROM rejected_tags WHERE tag_id=? AND source='wd14'",
                tag_id) == 1,
          "a rejection from another source is left alone")

    # FTS must forget it too, or search still returns the un-tagged file.
    # Checked on a *separate* connection on purpose: refresh_fts() issues its
    # delete+insert without committing, so reading back through `con` shows
    # writes that may never have been persisted — which is precisely how a
    # stale-search bug survived this test once already.
    # Read-only so it reports the stale row as a plain failed check: a normal
    # connection would instead block on the writer's open transaction and die
    # with "database is locked", which says the same thing far less clearly.
    import sqlite3
    verify_con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    verify_con.row_factory = sqlite3.Row
    try:
        hits = {r["rowid"] for r in verify_con.execute(
            "SELECT rowid FROM files_fts WHERE files_fts MATCH 'nova'")}
    finally:
        verify_con.close()
    check(fid[heldout] not in hits,
          "reset file no longer matches the tag in FTS (committed)")
    check(fid[pos_files[0]] in hits, "hand-tagged files still match in FTS")

    # Nothing may creep back: no model means apply() has nothing to do, and a
    # newly indexed file is not scored against the tag any more.
    check(learned.apply(con, tag_id) == 0, "re-applying after a reset is a no-op")
    Image.new("RGB", (32, 32), (100, 100, 100)).save(lib / "aurora_after_reset.png")
    rescan(con)
    worker.drain(con)
    fresh = con.execute(
        "SELECT id FROM files WHERE filename='aurora_after_reset.png'").fetchone()["id"]
    check(source_of(fresh) is None,
          "a file indexed after the reset is not auto-tagged any more")

    # --- daemon guard ------------------------------------------------------
    from indexer import daemon
    missing = daemon._learn_forget(con, {"category": "concept", "name": "does-not-exist"})
    check(not missing["ok"], f"unknown tag is an error, not a new tag (got {missing})")
    check(count("SELECT count(*) FROM tags WHERE name='does-not-exist'") == 0,
          "the unknown tag was not created as a side effect")

    # --- teachable again ---------------------------------------------------
    retrained = learned.build(con, "concept", "nova", space="clip")
    check(retrained is not None and retrained["n_pos"] >= len(pos_files),
          f"the tag can be taught again from its manual tags (got {retrained})")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — learned-tag reset verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
