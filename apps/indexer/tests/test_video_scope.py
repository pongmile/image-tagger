"""Videos are scan-only, in the queue and in the progress numbers — §12/§16.

Videos are indexed rows: they get a thumbnail frame and filename/folder
search, and no AI facet ever runs on one. Two consequences were being missed,
and each one was individually expensive:

  * Bulk actions ("Reindex", "Regen all captions", the caption backfill run
    when captioning is first enabled) queued every video alongside every
    image. The resulting job could only ever fail to decode — but not
    cheaply: imgio.open_oriented() hands anything under its size guard to
    OpenCV, which reads the *entire* file into memory before concluding that
    a container it cannot demux is still a container it cannot demux. On a
    library with a few hundred GB of video that is hours of disk I/O per
    reindex, plus one "skipping facets" line per video drowning the Sources
    error log.

  * db.progress() counted videos in the Tags and Caption denominators. Since
    a video can never acquire either, those bars were pinned at the
    images/(images+videos) ratio permanently: a fully-tagged library that is
    a sixth video displayed 82% and read as stuck, forever, with no way for
    the user to tell that from a genuine stall.

Verified here end to end against a real library: a video file and an image
file, through scan -> bulk enqueue -> drain, checking the *jobs table* and
the *progress payload*, not just that nothing raised.

Run standalone:
    apps/indexer/.venv/Scripts/python -m tests.test_video_scope
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

    tmp = Path(tempfile.mkdtemp(prefix="imgtag_videoscope_"))
    home, lib = tmp / "home", tmp / "lib"
    os.environ["IMAGE_TAGGER_HOME"] = str(home)

    import importlib
    import indexer.config as config
    importlib.reload(config)
    import indexer.db as db
    import indexer.worker as worker
    importlib.reload(worker)

    fails: list[str] = []

    def check(cond, msg):
        print(("  ok  " if cond else " FAIL ") + msg)
        if not cond:
            fails.append(msg)

    from PIL import Image
    lib.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), (120, 80, 200)).save(lib / "photo.png")
    # Not a real MP4 — deliberately. Nothing here should ever try to decode
    # it, so its contents are irrelevant, and if some future change *does*
    # start decoding videos this test will notice by failing rather than by
    # quietly reading a valid file.
    (lib / "clip.mp4").write_bytes(b"\x00" * 2048)

    con = db.connect()
    db.add_root(con, str(lib), mode="include")
    from indexer.scan import rescan
    rescan(con)

    def fid_of(name):
        row = con.execute("SELECT id FROM files WHERE filename=?", (name,)).fetchone()
        return row["id"] if row else None

    image_id, video_id = fid_of("photo.png"), fid_of("clip.mp4")
    check(image_id is not None, "the image is indexed")
    check(video_id is not None, "the video is indexed too (browse/search scope)")

    # Drain the scan's own jobs first so what follows is only what the bulk
    # actions add.
    worker.drain(con)

    def jobs_for(fid):
        return {r["kind"] for r in con.execute(
            "SELECT DISTINCT kind FROM jobs WHERE file_id=? AND state IN ('queued','running')",
            (fid,))}

    check("infer" not in jobs_for(video_id),
          "ingesting a video does not chain an inference job")

    db.reindex_all(con)
    check("reindex" in jobs_for(image_id), "Reindex queues the image")
    check(not jobs_for(video_id), "Reindex does not queue the video")

    worker.drain(con)
    db.recaption_all(con)
    # kind='caption_force', not plain 'caption': a deliberate "regenerate
    # everything" click must bypass the per-model cache (see
    # test_caption_cache.py) rather than share a job kind with the passive
    # backfill path that is supposed to skip files the active model already
    # captioned.
    check("caption_force" in jobs_for(image_id), "Regen-all-captions queues the image")
    check(not jobs_for(video_id), "Regen-all-captions does not queue the video")

    worker.drain(con)
    queued = db.enqueue_missing_captions(con)
    check(not jobs_for(video_id),
          f"the caption backfill skips videos (queued {queued})")
    worker.drain(con)

    # The video must never have been opened for decoding. _image_is_readable
    # answers from the extension alone, so a facet run on it is a no-op that
    # costs no I/O -- assert on the observable consequence: no error row, and
    # no caption/tag output.
    errors = con.execute(
        "SELECT count(*) c FROM jobs WHERE file_id=? AND state='error'", (video_id,)
    ).fetchone()["c"]
    check(errors == 0, f"a video produces no job errors (got {errors})")
    check(worker._image_is_readable(str(lib / "clip.mp4")) is False,
          "a video reports 'not readable as an image' without touching the file")

    # --- the progress payload ------------------------------------------------
    p = db.progress(con)
    check(p["files_total"] == 2, f"files_total counts every row (got {p['files_total']})")
    check(p["videos_total"] == 1, f"videos are counted separately (got {p['videos_total']})")
    check(p["scan_total"] == 2,
          f"Scan is measured against the whole library (got {p['scan_total']})")
    check(p["tag_total"] == 1,
          f"Tags is measured against taggable files only (got {p['tag_total']})")
    check(p["caption_total"] == 1,
          f"Caption is measured against captionable files only (got {p['caption_total']})")
    check(p["tag_done"] <= p["tag_total"] and p["caption_done"] <= p["caption_total"],
          "no stage can report more done than its own total")

    # The bug this whole file exists for: with the video captioned-out of the
    # denominator, a library whose images are all done reads 100%, not 50%.
    db.set_caption(con, image_id, "a purple square")
    p = db.progress(con)
    check(p["caption_done"] == 1 and p["caption_total"] == 1,
          f"captioning every image reaches 100% "
          f"({p['caption_done']}/{p['caption_total']}, not out of {p['files_total']})")

    check("jobs_pending" in p, "progress reports queue depth in jobs")
    check(p["jobs_pending"] == 0,
          f"a fully drained queue reports zero jobs pending (got {p['jobs_pending']})")

    # --- per-media breakdown -------------------------------------------------
    # One ratio per stage cannot describe what the program is doing: Scan
    # covers both media types, Tags/Caption cover only images, and Index
    # measures queue freshness rather than coverage at all. The UI renders the
    # structure rather than making the user infer it, which needs the halves
    # reported separately -- and needs "never applies" to be distinguishable
    # from "none done yet", or a video's Tags row renders as a bar at 0%.
    media = p["media"]
    check(media["images"]["total"] == 1, "the image half counts taggable files")
    check(media["videos"]["total"] == 1, "the video half counts videos")
    check(media["images"]["scanned"] == 1 and media["videos"]["scanned"] == 1,
          "Scan reports both halves — it is the one stage that covers videos")
    check(media["videos"]["tagged"] is None and media["videos"]["captioned"] is None,
          "tagged/captioned are None for video — 'never applies', not 'zero done'")
    check(media["images"]["captioned"] == 1,
          f"the captioned image is counted in its own half "
          f"(got {media['images']['captioned']})")
    check(media["images"]["scanned"] + media["videos"]["scanned"] == p["scan_done"],
          "the two halves of a stage add up to its total")

    # Facets that cost time per image but store nothing countable still have to
    # be reported, or the user watching "ocr · file.png" scroll past has no way
    # to know OCR is even switched on, let alone that it is part of why each
    # file takes as long as it does.
    for facet in ("wd14", "caption", "ocr", "clip", "faces"):
        check(facet in p["facets"], f"'{facet}' is reported as on/off")

    # --- per-root coverage ---------------------------------------------------
    # The Sources table sits directly under the library-wide bars, so it has to
    # measure the same thing they do. It used to divide index_status='done' by
    # every row, videos included, and so read "301/3,294" beneath a Tags bar
    # saying 99% -- two numbers on one screen contradicting each other.
    root = next(r for r in db.list_roots(con) if r["mode"] == "include")
    check(root["files"] == 2, f"the root counts every file under it (got {root['files']})")
    check(root["videos"] == 1, f"and reports its videos separately (got {root['videos']})")
    check(root["analyzable"] == 1,
          f"coverage is measured against taggable files only (got {root['analyzable']})")
    check(root["captioned"] == 1,
          f"the captioned image is counted (got {root['captioned']})")
    check(root["queued"] == 0,
          f"queue depth per root counts live jobs, not stale rows (got {root['queued']})")

    # A reindex is exactly the moment the two measures diverge: it resets
    # index_status on every row while touching no stored tag or caption. The
    # old ratio collapsed here; coverage must not move at all.
    before = (root["scanned"], root["tagged"], root["captioned"])
    db.reindex_all(con)
    after_root = next(r for r in db.list_roots(con) if r["mode"] == "include")
    check((after_root["scanned"], after_root["tagged"], after_root["captioned"]) == before,
          "queueing a reindex does not change coverage (it changes freshness)")
    check(after_root["queued"] > 0,
          f"but it does show up as queued work (got {after_root['queued']})")
    p2 = db.progress(con)
    check((p2["tag_done"], p2["caption_done"]) == (p["tag_done"], p["caption_done"]),
          "and the library-wide bars agree with the per-root numbers")
    worker.drain(con)

    print()
    if fails:
        print(f"RESULT: FAIL — {len(fails)} check(s)")
        return 1
    print("RESULT: PASS — video scope + split progress denominators verified")
    return 0


if __name__ == "__main__":
    sys.exit(run())
