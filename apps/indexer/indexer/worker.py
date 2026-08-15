"""Job-queue worker — spec §7.

Pulls queued jobs (FIFO), runs ingest, writes tags in a single transaction per
file (upsert_file already batches the transaction), marks the job done/error.
Idempotent & resumable: re-running never duplicates; errored jobs can be retried.

The `infer` stage runs OCR (M3). Later milestones extend it with WD14/CLIP/
InsightFace/caption; each is an independent facet writing into the same tables.
"""
from __future__ import annotations

import os
import sys
import threading

from . import db
from . import config
from . import heartbeat
from . import status
from .config import OCR_ENGINE
from .imgio import open_oriented
from .ingest import ingest, is_video


# CUDA/Torch/ONNX sessions must not be driven concurrently by the background
# worker and on-demand learned-tag preparation.
INFERENCE_LOCK = threading.RLock()


def _image_is_readable(path: str) -> bool:
    """Validate once before loading a heavyweight model.

    A zero-byte cloud placeholder or corrupt source is not repaired by retrying,
    so finish the facet job normally and leave one actionable line in the log.

    Videos are answered from the extension alone, without touching the file.
    They are browse/search-only by design (§12), so the answer is known in
    advance -- and *asking* is ruinously expensive: open_oriented() hands
    anything under its size guard to OpenCV, which reads the whole file into
    memory before it can confirm that a container it cannot demux is still a
    container it cannot demux. Per file that is a few hundred MB of pointless
    I/O; across a library's worth of queued jobs it is tens of GB, and it
    stalls the single worker thread the entire time. It also kept one
    "skipping facets" line per video in the Sources log, drowning the real
    errors it exists to show.
    """
    if is_video(path):
        return False
    try:
        with open_oriented(path):
            return True
    except Exception as exc:
        print(f"skipping facets for {path}: {exc}", file=sys.stderr)
        return False


def run_job(con, job) -> None:
    kind = job["kind"]
    fid = job["file_id"]
    try:
        db.set_job_state(con, job["id"], "running")
        if kind in ("ingest", "reindex"):
            path = con.execute(
                "SELECT path FROM files WHERE id=?", (fid,)
            ).fetchone()
            if path is None:
                db.set_job_state(con, job["id"], "error", "file row missing")
                return
            p = path["path"]
            if not os.path.exists(p):
                db.delete_file(con, p)
                db.set_job_state(con, job["id"], "done")
                return
            status.set(f"ingest · {os.path.basename(p)}")
            ing = ingest(p)
            db.upsert_file(con, ing)
            # Chain inference (kind router + WD14 + OCR; CLIP/faces later) as its
            # own job so ingest stays cheap and inference can batch/downgrade.
            # Never for a video: no facet runs on one, so the chained job's only
            # job would be to prove that again, per file, at the cost described
            # in _image_is_readable().
            if not is_video(p) and any(config.facet_enabled(con, facet) for facet in
                                       ("ocr", "wd14", "clip", "faces", "caption")):
                db.enqueue_job(con, fid, "infer", priority=int(job["priority"] or 0))
        elif kind == "infer":
            _run_infer(con, fid)
        elif kind == "clip":
            _run_clip(con, fid)
        elif kind == "caption":
            _run_caption(con, fid)
        elif kind == "caption_force":
            # recaption_all()/recaption_root() -- a deliberate "regenerate
            # every caption" click. Must bypass the per-model cache, or an
            # unchanged active model would turn the click into a no-op.
            _run_caption(con, fid, force=True)
        elif kind == "tag":
            _run_tag(con, fid)
        db.set_job_state(con, job["id"], "done")
    except Exception as e:  # never let one file kill the queue (§5.2 downgrade ethos)
        con.execute(
            "UPDATE files SET index_status='error' WHERE id=?", (fid,)
        )
        con.commit()
        db.set_job_state(con, job["id"], "error", repr(e))


def _run_infer(con, fid: int, *, force: bool = False) -> None:
    with INFERENCE_LOCK:
        _run_infer_locked(con, fid, force=force)


def _run_infer_locked(con, fid: int, *, force: bool = False) -> None:
    """Inference stage. M3: OCR text-in-image -> ocr_regions + files.ocr_text + FTS.
    A file with no image on disk (or undecodable) simply yields no regions.

    `force=True` (single-file "↻ re-index") always re-runs every facet from
    scratch. `force=False` (fresh files, bulk reindex/reindex-all) lets WD14
    and faces skip work already known to be current -- see _run_wd14_facet
    and _run_faces_facet."""
    row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
    if row is None or not os.path.exists(row["path"]):
        return
    path = row["path"]

    # ingest() already treats an unreadable file (corrupt, truncated, or a
    # cloud-sync placeholder that hasn't finished downloading — e.g. Google
    # Drive/OneDrive "online-only" files, common on network-mounted drives)
    # as a normal, gracefully-degraded case: index the row, skip dims/thumb.
    # Every facet below independently re-opens the same file, though, with no
    # such guard — so the *first* one to run would raise and abort the whole
    # stage with a raw, alarming exception, even though this isn't actually
    # fixable by retrying. Check once, up front, and skip the same way
    # ingest() does: log it plainly (visible in the Sources page's indexer
    # log) and complete the job normally rather than erroring it.
    if not _image_is_readable(path):
        return

    # Resolve the execution provider + device from the active tier (§5.2) once.
    from . import engine as _engine
    cfg = _engine.get_engine_config(con)
    providers, device = cfg["onnx_providers"], cfg["torch_device"]

    # Pet the liveness clock before each facet below: up to 5 run sequentially
    # per file here, each with its own model (cold-load cost included), so a
    # single per-file beat at the top of the outer loop isn't tight enough —
    # see heartbeat.py.
    # Kind router + anime tagging (§5). WD14 also decides image_kind, so it runs
    # before facets that dispatch on kind (CLIP/InsightFace land in later stages).
    if config.facet_enabled(con, "wd14"):
        heartbeat.beat()
        _run_wd14_facet(con, fid, path, _engine, providers, force=force)

    # CLIP zero-shot scene/clothing/type + embedding store (§5/§8). Open-vocab
    # via the editable clip_labels table; the embedding also feeds semantic search
    # and learned tags (§5.3), so it's stored whenever CLIP runs.
    if config.facet_enabled(con, "clip"):
        heartbeat.beat()
        _run_clip_facet(con, fid, path, _engine, device)

    # Real faces (§5): detect -> embed -> incremental cluster -> auto-attach.
    # Skipped for illustrations (the kind router's 'anime' label); WD14 handles
    # character identity there. If the kind is unknown (WD14 off), run it.
    if config.facet_enabled(con, "faces"):
        kind = con.execute(
            "SELECT image_kind FROM files WHERE id=?", (fid,)
        ).fetchone()["image_kind"]
        if kind != "anime":
            heartbeat.beat()
            status.set(f"faces · {os.path.basename(path)}")
            _run_faces_facet(con, fid, path, _engine, providers, force=force)

    # OCR text-in-image (kind-agnostic — memes/screenshots aren't kind-specific).
    if config.facet_enabled(con, "ocr"):
        heartbeat.beat()
        status.set(f"ocr · {os.path.basename(path)}")
        from .models import ocr
        engine = ocr.get_engine(OCR_ENGINE, providers=providers)
        regions = engine.recognize(path)
        db.write_ocr(con, fid, regions)

    # Natural-language caption (§11) -> files.caption -> FTS. Model id is
    # swappable per library (settings 'caption_model'). `force` threads
    # through exactly like the wd14/faces facets above it: False for the
    # ordinary per-file 'infer' chain (so a bulk backlog can skip files
    # already captioned by the active model), True only for an explicit
    # single-file re-index.
    if config.facet_enabled(con, "caption"):
        heartbeat.beat()
        _run_caption_facet(con, fid, path, _engine, device, force=force)


def _run_wd14_facet(con, fid: int, path: str, engine_config, providers,
                     *, force: bool = False) -> None:
    """WD14 general/character/rating tagging for one file.

    Skips actual inference (no model load, no GPU/CPU work) when this file was
    already tagged by the *currently active* WD14 variant before — restoring
    that prior result from facet_model_cache instead. Switching the variant in
    Settings and switching back therefore restores each model's own tags
    instantly rather than re-tagging from scratch, and file_tags always
    mirrors only the active model (search never sees an inactive model's
    cached tags). `force=True` (single-file "↻ re-Tag"/"↻ re-index") always
    bypasses the cache and re-runs inference, then refreshes the cache entry.
    """
    from .models import wd14
    # Thresholds are part of the identity of a result, not just the model:
    # the same variant at a lower general/character threshold emits strictly
    # more tags, so a cache keyed on the variant alone would keep serving the
    # old, sparser set after a threshold change.
    variant_id = (engine_config.selected_variant(con, "wd14") or {}).get("id", "default")
    model_key = (f"{variant_id}@g{config.WD14_GENERAL_THRESHOLD:g}"
                 f"c{config.WD14_CHARACTER_THRESHOLD:g}")
    if not force:
        cached = db.get_facet_cache(con, fid, "wd14", model_key)
        if cached is not None:
            tags = [wd14.TagResult(t["category"], t["name"], t["confidence"])
                    for t in cached["tags"]]
            db.write_auto_tags(con, fid, "wd14", tags)
            if cached.get("image_kind"):
                db.set_image_kind(con, fid, cached["image_kind"])
            return
    status.set(f"tagging (wd14) · {os.path.basename(path)}")
    tagger = wd14.get_engine(
        engine_config.active_model_dir(con, "wd14"),
        general_threshold=config.WD14_GENERAL_THRESHOLD,
        character_threshold=config.WD14_CHARACTER_THRESHOLD,
        providers=providers,
    )
    tags = tagger.tag(path)
    kind = tagger.image_kind(tags)
    db.write_auto_tags(con, fid, "wd14", tags)
    db.set_image_kind(con, fid, kind)
    db.set_facet_cache(con, fid, "wd14", model_key, kind, tags)


def _run_faces_facet(con, fid: int, path: str, engine_config, providers,
                      *, force: bool = False) -> None:
    """Real-face detection for one file. Mirrors _run_caption_facet's
    "missing model must never abort the file" handling (§5): a facet the user
    hasn't downloaded InsightFace for yet is the expected, common state, not
    an error — only a genuine load failure *despite* the dependency/model
    both being present is worth a visible (but still non-fatal) log line.

    A file that already has detected faces is skipped (no re-detect, no
    GPU/CPU work) unless `force=True` (single-file "↻ re-index") — faces
    already found for a file don't need finding again."""
    from .models import faces
    from . import learned
    if not force and con.execute(
        "SELECT 1 FROM faces WHERE file_id=? LIMIT 1", (fid,)
    ).fetchone() is not None:
        # Detection is the expensive part and its result cannot change for an
        # unchanged file -- but learned 'face'-space tags *can* have been
        # trained since, so still score this file against them (pure vector
        # math on the embeddings already stored). Skipping this too would
        # leave a reindex silently failing to pick up a newly taught person.
        learned.apply_to_file(con, fid, "face")
        return
    fv = engine_config.selected_variant(con, "insightface") or {}
    fe = faces.get_engine(str(engine_config.active_model_dir(con, "insightface")),
                          providers=providers, pack=fv.get("pack", "buffalo_l"))
    if isinstance(fe, faces.NullFaceEngine):
        ready, reason = engine_config.facet_model_ready(con, "faces")
        if ready:
            print(f"faces engine failed to load for {os.path.basename(path)} "
                  f"though the model is installed: {faces.last_error()}", file=sys.stderr)
        else:
            print(f"skipping faces for {os.path.basename(path)}: {reason}", file=sys.stderr)
        return
    detected = fe.detect(path)
    db.write_faces(con, fid, detected, threshold=config.FACE_THRESHOLD)
    # Score this file against 'face'-space learned tags immediately (§5.3),
    # the same online-learning treatment CLIP gets — otherwise a real-person
    # learned tag would only catch up on the next manual "Refresh" instead of
    # as the library indexes.
    learned.apply_to_file(con, fid, "face")


def _run_tag(con, fid: int) -> None:
    """WD14-only regenerate, e.g. "↻ re-Tag" in the preview pane. Mirrors
    _run_caption/_run_clip: does not rerun OCR/faces/clip/caption, just WD14 —
    and always forces fresh inference (that's the point of a manual re-Tag
    click), bypassing the per-model cache that the normal indexing path uses."""
    with INFERENCE_LOCK:
        row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        if row is None or not os.path.exists(row["path"]):
            return
        if not _image_is_readable(row["path"]):
            return
        from . import engine as engine_config
        cfg = engine_config.get_engine_config(con)
        _run_wd14_facet(con, fid, row["path"], engine_config, cfg["onnx_providers"],
                         force=True)


def _run_caption(con, fid: int, *, force: bool = False) -> None:
    """Caption-only regenerate. Mirrors _run_clip: does not rerun
    WD14/OCR/faces/clip, just the one facet that changed.

    Two different callers need two different answers to "redo it anyway?":
    the queued kind='caption' job (bulk sweeps -- recaption_all/_root,
    enqueue_missing_captions, a caption-variant switch backfilling existing
    files) wants force=False, the default, so a file the active model has
    already captioned is served from cache instead of paying real GPU
    seconds for a result that would come out the same. The preview pane's
    "↻ re-Description" button (daemon.py's `recaption` single-file action)
    passes force=True explicitly -- that click means "redo this one, right
    now," and a cache hit silently no-op'ing it would look like the button
    did nothing.
    """
    with INFERENCE_LOCK:
        row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        if row is None or not os.path.exists(row["path"]):
            return
        if not _image_is_readable(row["path"]):
            return
        from . import engine as engine_config
        cfg = engine_config.get_engine_config(con)
        _run_caption_facet(con, fid, row["path"], engine_config, cfg["torch_device"],
                            force=force)


def _run_caption_facet(con, fid: int, path: str, engine_config, device: str,
                        *, force: bool = False) -> None:
    """Natural-language caption for one file.

    Skips generation (no model load, no GPU/CPU work) when this file was
    already captioned by the *currently active* caption variant before --
    restoring that prior result from facet_model_cache, exactly mirroring
    _run_wd14_facet's per-model cache and for the same reason: switching the
    caption variant in Settings and switching back restores each model's own
    caption instantly, and a bulk sweep across a large library only pays for
    inference on files that genuinely need it. Before this cache existed, a
    "↻ Reindex" or a caption-variant switch regenerated every caption in the
    library from scratch regardless of whether the active model had already
    produced one -- on a library with several thousand images that is real
    GPU minutes to hours spent reproducing output byte-for-identical to what
    was already stored. `force=True` (the preview pane's single-file
    "↻ re-Description") always bypasses the cache, then refreshes it.
    """
    from .models import caption
    capv = engine_config.selected_variant(con, "caption") or {}
    variant_id = capv.get("id", "default")
    if not force:
        cached = db.get_caption_cache(con, fid, variant_id)
        if cached is not None:
            if cached:
                db.set_caption(con, fid, cached)
            return
    status.set(f"captioning · {os.path.basename(path)}")
    model_id = (capv.get("model_id") or db.get_setting(con, "caption_model")
                or config.CAPTION_MODEL)
    cap_engine = caption.get_engine(
        str(engine_config.active_model_dir(con, "caption")), model_id=model_id,
        device=device, name=capv.get("engine", "blip"),
        load_in_4bit=capv.get("load_in_4bit", False))
    if isinstance(cap_engine, caption.NullCaptionEngine):
        ready, reason = engine_config.facet_model_ready(con, "caption")
        if not ready:
            # Dependency/model genuinely not installed yet — the expected,
            # common state before the user downloads it (§11): skip this
            # file's caption quietly rather than erroring the whole per-file
            # job. "↻ re-Description" (or the next reindex) picks it up once
            # the model is installed.
            print(f"skipping caption for {os.path.basename(path)}: {reason}", file=sys.stderr)
            return
        # A silent empty caption on load failure (bad model id, missing
        # bitsandbytes, insufficient VRAM for JoyCaption, ...) used to look
        # identical to "nothing to caption" — surface it as a real per-file
        # job error instead so it shows up in the Sources page error list.
        raise RuntimeError(caption.last_error() or "caption engine failed to load")
    text = cap_engine.caption(path)
    if text:
        db.set_caption(con, fid, text)
    # Cache the outcome even when empty, so a file the model genuinely has
    # nothing to say about isn't retried at full model cost on every future
    # reindex -- only a load failure above (which raises) skips this.
    db.set_caption_cache(con, fid, variant_id, text or "")


def _run_clip(con, fid: int) -> None:
    """CLIP-only backfill used after teaching the first learned tag.

    It deliberately does not rerun WD14, OCR, faces, or captioning: those facets
    are already current and repeating them wastes CPU/GPU time.
    """
    with INFERENCE_LOCK:
        row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        if row is None or not os.path.exists(row["path"]):
            return
        if not _image_is_readable(row["path"]):
            return
        from . import engine as engine_config
        cfg = engine_config.get_engine_config(con)
        _run_clip_facet(con, fid, row["path"], engine_config, cfg["torch_device"])


def _run_clip_facet(con, fid: int, path: str, engine_config, device: str) -> None:
    status.set(f"clip · {os.path.basename(path)}")
    from .models import clip
    from . import learned, vec
    cv = engine_config.selected_variant(con, "clip") or {}
    engine = clip.get_engine(
        str(engine_config.active_model_dir(con, "clip")),
        model_name=cv.get("model", config.CLIP_MODEL),
        pretrained=cv.get("pretrained", config.CLIP_PRETRAINED),
        device=device,
    )
    emb = engine.encode_image(path)
    if emb is None:
        return
    vec.upsert(con, fid, emb, dim=len(emb))
    vocab = db.get_clip_vocab(con)
    zs = engine.classify(emb, vocab, threshold=config.CLIP_THRESHOLD)
    db.write_auto_tags(con, fid, "clip", zs)
    learned.apply_to_file(con, fid, "clip")


def drain(con, max_jobs: int | None = None) -> int:
    """Process queued jobs until empty (or max_jobs). Returns count processed."""
    n = 0
    while True:
        if max_jobs is not None and n >= max_jobs:
            break
        job = db.next_job(con)
        if job is None:
            break
        run_job(con, job)
        n += 1
    return n
