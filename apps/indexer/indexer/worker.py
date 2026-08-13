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
from .ingest import ingest


# CUDA/Torch/ONNX sessions must not be driven concurrently by the background
# worker and on-demand learned-tag preparation.
INFERENCE_LOCK = threading.RLock()


def _image_is_readable(path: str) -> bool:
    """Validate once before loading a heavyweight model.

    A zero-byte cloud placeholder or corrupt source is not repaired by retrying,
    so finish the facet job normally and leave one actionable line in the log.
    """
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
            if any(config.facet_enabled(con, facet) for facet in
                   ("ocr", "wd14", "clip", "faces", "caption")):
                db.enqueue_job(con, fid, "infer", priority=int(job["priority"] or 0))
        elif kind == "infer":
            _run_infer(con, fid)
        elif kind == "clip":
            _run_clip(con, fid)
        elif kind == "caption":
            _run_caption(con, fid)
        db.set_job_state(con, job["id"], "done")
    except Exception as e:  # never let one file kill the queue (§5.2 downgrade ethos)
        con.execute(
            "UPDATE files SET index_status='error' WHERE id=?", (fid,)
        )
        con.commit()
        db.set_job_state(con, job["id"], "error", repr(e))


def _run_infer(con, fid: int) -> None:
    with INFERENCE_LOCK:
        _run_infer_locked(con, fid)


def _run_infer_locked(con, fid: int) -> None:
    """Inference stage. M3: OCR text-in-image -> ocr_regions + files.ocr_text + FTS.
    A file with no image on disk (or undecodable) simply yields no regions."""
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
        status.set(f"tagging (wd14) · {os.path.basename(path)}")
        from .models import wd14
        tagger = wd14.get_engine(
            _engine.active_model_dir(con, "wd14"),
            general_threshold=config.WD14_GENERAL_THRESHOLD,
            character_threshold=config.WD14_CHARACTER_THRESHOLD,
            providers=providers,
        )
        tags = tagger.tag(path)
        db.write_auto_tags(con, fid, "wd14", tags)
        db.set_image_kind(con, fid, tagger.image_kind(tags))

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
            from .models import faces
            from . import learned
            fv = _engine.selected_variant(con, "insightface") or {}
            fe = faces.get_engine(str(_engine.active_model_dir(con, "insightface")),
                                  providers=providers, pack=fv.get("pack", "buffalo_l"))
            detected = fe.detect(path)
            db.write_faces(con, fid, detected, threshold=config.FACE_THRESHOLD)
            # Score this file against 'face'-space learned tags immediately
            # (§5.3), the same online-learning treatment CLIP gets above —
            # otherwise a real-person learned tag would only catch up on the
            # next manual "Refresh" instead of as the library indexes.
            learned.apply_to_file(con, fid, "face")

    # OCR text-in-image (kind-agnostic — memes/screenshots aren't kind-specific).
    if config.facet_enabled(con, "ocr"):
        heartbeat.beat()
        status.set(f"ocr · {os.path.basename(path)}")
        from .models import ocr
        engine = ocr.get_engine(OCR_ENGINE, providers=providers)
        regions = engine.recognize(path)
        db.write_ocr(con, fid, regions)

    # Natural-language caption (§11) -> files.caption -> FTS. Model id is
    # swappable per library (settings 'caption_model').
    if config.facet_enabled(con, "caption"):
        heartbeat.beat()
        _run_caption_facet(con, fid, path, _engine, device)


def _run_caption(con, fid: int) -> None:
    """Caption-only regenerate, e.g. "↻ re-Description" in the preview pane or
    after switching the caption model variant. Mirrors _run_clip: does not
    rerun WD14/OCR/faces/clip, just the one facet that changed."""
    with INFERENCE_LOCK:
        row = con.execute("SELECT path FROM files WHERE id=?", (fid,)).fetchone()
        if row is None or not os.path.exists(row["path"]):
            return
        if not _image_is_readable(row["path"]):
            return
        from . import engine as engine_config
        cfg = engine_config.get_engine_config(con)
        _run_caption_facet(con, fid, row["path"], engine_config, cfg["torch_device"])


def _run_caption_facet(con, fid: int, path: str, engine_config, device: str) -> None:
    status.set(f"captioning · {os.path.basename(path)}")
    from .models import caption
    capv = engine_config.selected_variant(con, "caption") or {}
    model_id = (capv.get("model_id") or db.get_setting(con, "caption_model")
                or config.CAPTION_MODEL)
    cap_engine = caption.get_engine(
        str(engine_config.active_model_dir(con, "caption")), model_id=model_id,
        device=device, name=capv.get("engine", "blip"),
        load_in_4bit=capv.get("load_in_4bit", False))
    if isinstance(cap_engine, caption.NullCaptionEngine):
        # A silent empty caption on load failure (bad model id, missing
        # bitsandbytes, insufficient VRAM for JoyCaption, ...) used to look
        # identical to "nothing to caption" — surface it as a real per-file
        # job error instead so it shows up in the Sources page error list.
        raise RuntimeError(caption.last_error() or "caption engine failed to load")
    text = cap_engine.caption(path)
    if text:
        db.set_caption(con, fid, text)


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
