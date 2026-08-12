"""Embedded-metadata extraction — spec §5.1 / §6 (file_metadata).

Pulls EXIF, PNG text chunks (including Stable Diffusion `parameters`), and any
XMP packet out of an image into flat (key, value) rows. Keys are namespaced:
  exif:Make, exif:Model, png:parameters, png:Comment, xmp:Rating, ...

Selected values are also joined into `meta_text` for FTS (§6), so a user can
search "DPM++ 2M", a LoRA name, or a camera model on the fast path.
"""
from __future__ import annotations

import re
from typing import Any

from PIL import Image, ExifTags

# EXIF tag id -> human name (e.g. 271 -> "Make").
_EXIF_NAMES = ExifTags.TAGS

# Keys whose values are worth folding into FTS meta_text. SD generation params
# and camera identity are the high-value ones (§5.1).
_META_TEXT_KEYS = (
    "png:parameters", "png:Comment", "png:Description", "png:prompt",
    "exif:Make", "exif:Model", "exif:LensModel", "exif:Software",
    "exif:ImageDescription", "xmp:Rating", "xmp:subject",
)


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            # Binary metadata (notably ICC profiles) is not display text. Only
            # accept byte payloads that really are UTF-8.
            v = v.decode("utf-8", "strict")
        except (UnicodeDecodeError, ValueError):
            return None
    s = str(v).strip().replace("\x00", "")
    return s or None


def extract(path: str, img: Image.Image | None = None) -> dict[str, str]:
    """Return a {namespaced_key: value} dict of embedded metadata for one image.

    `img` may be passed in to avoid re-opening a file already open in ingest.
    Never raises: a corrupt/foreign metadata block yields fewer keys, not a crash.
    """
    out: dict[str, str] = {}
    owns = img is None
    try:
        if img is None:
            img = Image.open(path)
    except Exception:
        return out
    try:
        _exif(img, out)
        _png_text(img, out)
        _xmp(img, out)
    finally:
        if owns:
            try:
                img.close()
            except Exception:
                pass
    return out


def _exif(img: Image.Image, out: dict[str, str]) -> None:
    try:
        exif = img.getexif()
    except Exception:
        return
    if not exif:
        return
    for tag_id, value in exif.items():
        name = _EXIF_NAMES.get(tag_id, f"0x{tag_id:04x}")
        cv = _clean(value)
        if cv is not None and len(cv) <= 4096:
            out[f"exif:{name}"] = cv


def _png_text(img: Image.Image, out: dict[str, str]) -> None:
    # PNG tEXt/iTXt/zTXt chunks surface in img.text and img.info.
    blobs: dict[str, Any] = {}
    blobs.update(getattr(img, "text", {}) or {})
    for k, v in (getattr(img, "info", {}) or {}).items():
        if isinstance(v, (str, bytes)):
            blobs.setdefault(k, v)
    for k, v in blobs.items():
        if str(k).lower() in {"icc_profile", "exif", "transparency"}:
            continue
        cv = _clean(v)
        if cv is not None:
            out[f"png:{k}"] = cv


def _xmp(img: Image.Image, out: dict[str, str]) -> None:
    xmp = (getattr(img, "info", {}) or {}).get("XML:com.adobe.xmp") \
        or (getattr(img, "info", {}) or {}).get("xmp")
    cv = _clean(xmp)
    if not cv:
        return
    # Lightweight field pull — no full RDF parse in v1.
    m = re.search(r"xmp:Rating>(\d+)", cv)
    if m:
        out["xmp:Rating"] = m.group(1)
    subjects = re.findall(r"<rdf:li[^>]*>([^<]+)</rdf:li>", cv)
    if subjects:
        out["xmp:subject"] = ", ".join(s.strip() for s in subjects if s.strip())


# --- Stable Diffusion parameters -> structured tag candidates (§5.1) --------

_SD_FIELD = re.compile(r"([A-Za-z][\w ]*?):\s*([^,]+)")


def parse_sd_parameters(params: str) -> dict[str, str]:
    """Parse an A1111-style PNG `parameters` string into a small dict of
    high-value fields (Model, Sampler, Seed, CFG scale, Steps, Size, plus LoRA
    names). Best-effort; unknown formats yield {}."""
    out: dict[str, str] = {}
    if not params:
        return out
    # The trailing settings line holds "Steps: 20, Sampler: ..., Model: ...".
    tail = params.rsplit("\n", 1)[-1]
    for key, val in _SD_FIELD.findall(tail):
        k = key.strip().lower()
        if k in ("steps", "sampler", "cfg scale", "seed", "model",
                 "model hash", "size", "vae", "clip skip", "schedule type"):
            out[k] = val.strip()
    for lora in re.findall(r"<lora:([^:>]+)", params):
        out.setdefault("lora", "")
        out["lora"] = (out["lora"] + " " + lora).strip()
    return out


def meta_text(md: dict[str, str]) -> str:
    """Join the FTS-worthy metadata values into one searchable blob (§6)."""
    parts: list[str] = []
    for k in _META_TEXT_KEYS:
        v = md.get(k)
        if v:
            parts.append(v)
    # Fold parsed SD fields in too, if present.
    sd = parse_sd_parameters(md.get("png:parameters", ""))
    parts.extend(f"{k} {v}" for k, v in sd.items())
    return " ".join(parts)
