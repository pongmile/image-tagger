"""Ingest one file — spec §5 step 1 + §8.2 thumbnails.

Compute sha256 (exact dedup) + perceptual hash (near-dup), read dimensions and
mime, split path into filename/folder, extract embedded metadata, generate a
thumbnail. Pure I/O + Pillow; no AI models. Returns a plain dict the DB layer
persists in a single transaction (§7).
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import metadata as meta
from .config import THUMBS_DIR, VIDEO_EXT
from .imgio import open_oriented

_THUMB_MAX = 320          # longest edge, px (§8.2 grid thumbs)
_HASH_CHUNK = 1 << 20     # 1 MiB


@dataclass
class Ingested:
    path: str
    filename: str
    folder: str
    sha256: str
    phash: str | None
    mime: str | None
    width: int | None
    height: int | None
    size_bytes: int
    mtime: int
    metadata: dict[str, str] = field(default_factory=dict)
    meta_text: str = ""
    path_tags: list[tuple[str, str]] = field(default_factory=list)  # (category, name)
    thumb_path: str | None = None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash(img: Image.Image, size: int = 8) -> str:
    """Difference perceptual hash — 64-bit, robust to scaling/light edits.
    Returns 16 hex chars. Used for near-duplicate grouping (§5)."""
    small = img.convert("L").resize((size + 1, size), Image.BILINEAR)
    px = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | int(px[base + col] < px[base + col + 1])
    return f"{bits:016x}"


def _thumb_path(sha: str) -> Path:
    return THUMBS_DIR / sha[:2] / f"{sha}.webp"


def make_thumb(img: Image.Image, sha: str) -> str | None:
    """Write a webp thumbnail keyed by sha256 (§8.2). Idempotent."""
    dst = _thumb_path(sha)
    if dst.exists():
        return str(dst)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        t = img.convert("RGB")
        t.thumbnail((_THUMB_MAX, _THUMB_MAX), Image.LANCZOS)
        t.save(dst, "WEBP", quality=80, method=4)
        return str(dst)
    except Exception:
        return None


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXT


def _grab_video_frame(path: str) -> Image.Image | None:
    """A representative frame for the thumbnail grid (§12: browse/search only
    — no frame-level AI analysis). Uses OpenCV, already pulled in by the OCR
    facet's dependency (on by default), so this adds no new install step; if
    it's genuinely unavailable this just leaves the video without a thumbnail
    rather than failing ingest, same as an undecodable image today.

    Reads a frame ~10% into the video rather than frame 0, which is often a
    black or fade-in frame and a worse thumbnail than something mid-content.
    """
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        if not cap.isOpened():
            return None
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if total and total > 10:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(total - 1, int(total * 0.1)))
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            return None
        return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    finally:
        cap.release()


def _path_tags(folder: str, filename: str) -> list[tuple[str, str]]:
    """Low-confidence tag candidates derived from the path (§5.1). Kept under
    the `path` category so they never silently outrank AI/manual tags. The last
    folder segment becomes a candidate (e.g. .../Hatsune Miku/ -> path:hatsune miku)."""
    tags: list[tuple[str, str]] = []
    seg = os.path.basename(folder.rstrip("/\\"))
    if seg and seg not in (".", ".."):
        tags.append(("path", seg.replace("_", " ").strip().lower()))
    return tags


def ingest(path: str) -> Ingested:
    st = os.stat(path)
    folder = os.path.dirname(path)
    filename = os.path.basename(path)
    sha = sha256_file(path)
    mime = mimetypes.guess_type(path)[0]

    width = height = None
    phash = None
    md: dict[str, str] = {}
    mtext = ""
    thumb = None
    try:
        if is_video(path):
            # No EXIF/SD metadata to extract from a video container here —
            # just dims + a thumbnail, per the browse/search-only scope.
            frame = _grab_video_frame(path)
            if frame is not None:
                width, height = frame.size
                try:
                    phash = dhash(frame)
                except Exception:
                    phash = None
                thumb = make_thumb(frame, sha)
        else:
            with open_oriented(path) as img:
                width, height = img.size
                md = meta.extract(path, img)
                mtext = meta.meta_text(md)
                try:
                    phash = dhash(img)
                except Exception:
                    phash = None
                thumb = make_thumb(img, sha)
    except Exception:
        # Non-decodable file: still indexed as a row (path/hash searchable),
        # just without dims/metadata/thumb. Never fail the whole ingest.
        pass

    return Ingested(
        path=path,
        filename=filename,
        folder=folder,
        sha256=sha,
        phash=phash,
        mime=mime,
        width=width,
        height=height,
        size_bytes=st.st_size,
        mtime=int(st.st_mtime),
        metadata=md,
        meta_text=mtext,
        path_tags=_path_tags(folder, filename),
        thumb_path=thumb,
    )
