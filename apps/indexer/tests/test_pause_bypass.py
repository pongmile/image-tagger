"""Pause Tagger: single-file force actions and Learned tags keep working —
verification discipline §16.

"↻ re-index" / "↻ re-Description" / "↻ re-Tag" used to just enqueue a
priority=100 job into the same table the (pausable) worker loop drains —
so a deliberate single-file click silently did nothing until the user
pressed Resume, even though the button gave no such warning. They now run
synchronously on the daemon's RPC thread (mirroring how learn_* commands
already bypass pause), so they act immediately regardless of _paused, and
touch only the one file they were asked about — nothing else in the queue
moves. Learned tags similarly must keep scoring already-embedded files
while paused (learned.apply_all(), cheap vector math, no GPU inference).

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_pause_bypass
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run() -> int:
    os.environ["IMAGE_TAGGER_CLIP"] = "1"
    os.environ["IMAGE_TAGGER_OCR"] = "0"
    os.environ["IMAGE_TAGGER_WD14"] = "1"
    os.environ["IMAGE_TAGGER_FACES"] = "0"

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_pausebypass_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.worker as worker
    importlib.reload(worker)
    import indexer.daemon as daemon
    importlib.reload(daemon)
    from indexer import vec, learned
    from indexer import engine as engine_config
    from indexer.models import clip, wd14

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    clip.set_engine(clip.FakeClipEngine(["beach", "forest", "city"]))
    wd14.set_engine(wd14.FakeTaggerEngine([wd14.TagResult("general", "1girl", 0.9)]))

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    target = lib / "target.png"
    other = lib / "other.png"
    for p in (target, other):
        Image.new("RGB", (16, 16), (5, 5, 5)).save(p)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)
    fid_target = con.execute("SELECT id FROM files WHERE filename='target.png'").fetchone()["id"]
    fid_other = con.execute("SELECT id FROM files WHERE filename='other.png'").fetchone()["id"]
    # A normal background job sitting in the queue, representing the heavy
    # indexing work that Pause Tagger is supposed to actually hold back.
    db.enqueue_job(con, fid_other, "infer", priority=0)

    def other_job_untouched():
        row = con.execute(
            "SELECT state FROM jobs WHERE file_id=? AND kind='infer'", (fid_other,)
        ).fetchone()
        return row is not None and row["state"] == "queued"

    # Collect the daemon's outgoing events so the async completion can be
    # awaited the same way the renderer does (file_done), instead of sleeping.
    import threading as _threading
    events: list[dict] = []
    done_evt = _threading.Event()
    original_emit = daemon._emit

    def capture_emit(obj):
        events.append(obj)
        if obj.get("event") == "file_done":
            done_evt.set()

    daemon._emit = capture_emit

    def wait_file_done(timeout=60.0):
        ok = done_evt.wait(timeout)
        done_evt.clear()
        return ok, next((e for e in reversed(events) if e.get("event") == "file_done"), None)

    daemon.handle(con, {"id": 1, "cmd": "pause"})
    check(daemon._paused.is_set(), "daemon reports paused")

    # The real regression guard: a single-file action must not run inline on
    # the RPC thread. The daemon reads commands with one `for line in sys.stdin`
    # loop, so a slow inline inference blocks *every* other RPC -- including the
    # heartbeat Electron uses to decide the daemon is wedged, which kills and
    # restarts it after ~3 missed polls. Make the tagger deliberately slow and
    # assert handle() still returns promptly.
    slow_tagger = wd14.FakeTaggerEngine([wd14.TagResult("general", "1girl", 0.9)])
    _plain_tag = slow_tagger.tag
    slow_tagger.tag = lambda path: (time.sleep(2.0), _plain_tag(path))[1]
    wd14.set_engine(slow_tagger)

    t0 = time.monotonic()
    resp = daemon.handle(con, {"id": 2, "cmd": "retag_file", "path": str(target)})
    elapsed = time.monotonic() - t0
    check(resp.get("ok") and resp["result"].get("started"),
          f"retag_file reports it started (got {resp})")
    check(elapsed < 1.0,
          f"retag_file returns without waiting for the 2s inference "
          f"(took {elapsed:.2f}s -- inline would be >=2s and would block the RPC loop)")
    finished, evt = wait_file_done()
    check(finished and evt and evt.get("ok"), f"a file_done event reports the result (got {evt})")
    check(any(r["name"] == "1girl" for r in con.execute(
        "SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id "
        "WHERE ft.file_id=? AND ft.source='wd14'", (fid_target,))),
        "the tag was actually written while paused")
    check(con.execute(
        "SELECT count(*) c FROM jobs WHERE file_id=? AND kind='tag'", (fid_target,)
    ).fetchone()["c"] == 0, "retag_file never touches the job queue at all")
    check(other_job_untouched(), "the unrelated queued job is untouched by retag_file")

    wd14.set_engine(wd14.FakeTaggerEngine([wd14.TagResult("general", "1girl", 0.9)]))
    resp = daemon.handle(con, {"id": 3, "cmd": "reindex_file", "path": str(target)})
    check(resp.get("ok") and resp["result"].get("started"),
          f"reindex_file reports it started while paused (got {resp})")
    finished, evt = wait_file_done()
    check(finished and evt and evt.get("ok"), f"reindex_file completes on its own thread (got {evt})")
    check(other_job_untouched(), "the unrelated queued job is still untouched by reindex_file")

    # Captioning is off in this test (IMAGE_TAGGER_CAPTION unset) -- exercises
    # recaption_file's error path instead, and confirms even a failed call
    # touches nothing else in the queue.
    resp = daemon.handle(con, {"id": 4, "cmd": "recaption_file", "path": str(target)})
    check(resp.get("ok") and resp["result"].get("ok") is False
          and "disabled" in resp["result"]["error"],
          f"recaption_file still reports a clean error when captioning is off (got {resp})")
    daemon._emit = original_emit
    check(other_job_untouched(),
          "the unrelated queued job is untouched even by a recaption_file that can't run")

    # Learned tags: cheap vector-math re-scoring must not be gated by _paused.
    # learned.py has no reference to daemon._paused at all -- structurally it
    # cannot be blocked by it -- but verify apply_all() actually reaches and
    # re-scores a trained tag while paused, not just that it returns cleanly.
    # A hand-built learned_tags row (prototype = the file's own embedding)
    # sidesteps train()'s MIN_POSITIVES=5 real-example requirement, which
    # would otherwise need five separate embedded files just for this check.
    clip.set_engine(clip.FakeClipEngine(["beach", "forest", "city"]))
    if vec.available():
        worker._run_clip_facet(con, fid_target, str(target), engine_config, "cpu")
        tag_id = db.get_or_create_tag(con, "nova", "concept")
        emb = learned.embedding_for(con, fid_target, "clip")
        check(emb is not None, "target file has a CLIP embedding to build a fake prototype from")
        with con:
            con.execute(
                """INSERT INTO learned_tags
                   (tag_id, space, method, threshold, n_pos, n_neg, prototype, updated_at)
                   VALUES (?,'clip','centroid',0.5,1,0,?,?)
                   ON CONFLICT(tag_id) DO UPDATE SET prototype=excluded.prototype""",
                (tag_id, db._emb_to_blob(emb), 1),
            )
        check(daemon._paused.is_set(), "still paused going into the learned-tags sweep")
        applied = learned.apply_all(con)
        check(applied.get(tag_id, 0) >= 1,
              f"apply_all() re-scores and applies the trained tag while paused (got {applied})")
        row = con.execute(
            "SELECT source FROM file_tags WHERE file_id=? AND tag_id=?",
            (fid_target, tag_id)).fetchone()
        check(row is not None and row["source"] == "learned",
              "the learned tag was actually written to file_tags while paused")
    else:
        print("  skip  sqlite-vec not installed -- learned.apply_all() check skipped")

    # The paused sweep is guarded by a change detector: learned.apply()
    # rewrites every matching file's tag row and FTS entry on each pass, so
    # re-running it on a bare timer would keep the disk busy forever while the
    # user believes the app is paused.
    fp1 = daemon._learned_input_fingerprint(con)
    fp2 = daemon._learned_input_fingerprint(con)
    check(fp1 == fp2, "the learned-sweep fingerprint is stable when nothing changed")
    with con:
        con.execute(
            "INSERT INTO faces (file_id, person_id, bbox, embedding) VALUES (?,NULL,'0,0,1,1',X'00')",
            (fid_other,))
    check(daemon._learned_input_fingerprint(con) != fp1,
          "the fingerprint changes when new input to score appears")

    daemon.handle(con, {"id": 5, "cmd": "resume"})
    check(not daemon._paused.is_set(), "resume clears the pause flag")

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — pause-bypassing single-file actions + always-on learned tags verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
