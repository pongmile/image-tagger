"""Central config. Values come from the settings table + env overrides."""
from pathlib import Path
import os
import site
import sys

APP_DIR = Path(os.environ.get("IMAGE_TAGGER_HOME", Path.home() / ".image-tagger"))
DB_PATH = APP_DIR / "library.db"
THUMBS_DIR = APP_DIR / "thumbs"
MODELS_DIR = APP_DIR / "models"
RUNTIME_PACKAGES_DIR = APP_DIR / "runtime-packages"

# Optional AI dependencies are installed into user data, not the packaged app.
# This survives upgrades, works without administrator rights, and keeps model
# setup from disappearing whenever a new desktop build replaces resources.
runtime_packages = str(RUNTIME_PACKAGES_DIR)
if runtime_packages not in sys.path:
    sys.path.insert(0, runtime_packages)
if RUNTIME_PACKAGES_DIR.exists():
    site.addsitedir(runtime_packages)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
# Browse/search only (per §12 scope decision): a thumbnail frame + filename/
# folder/tag search, same as any other file — no AI facets run on these (the
# ingest()/worker.py "not a readable image" skip already keeps wd14/clip/faces/
# ocr/caption off video rows without any extra branching).
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".wmv"}
SUPPORTED_EXT = IMAGE_EXT | VIDEO_EXT

# --- Inference facets (per-library settings; env overrides for dev/test) ----
# OCR (§10). Engine names: "rapidocr" (PP-OCR via onnxruntime) | "null" (off).
OCR_ENABLED = os.environ.get("IMAGE_TAGGER_OCR", "1") not in ("0", "false", "")
OCR_ENGINE = os.environ.get("IMAGE_TAGGER_OCR_ENGINE", "rapidocr")

# WD14 anime tagger (§5, M4). Off by default until a model is downloaded (§12
# model manager); enable with IMAGE_TAGGER_WD14=1. The model *folder* is resolved
# per-library via db.model_dir(con,'wd14') — see db.get_models_dir() precedence
# (env > settings 'models_dir' > default) so the user can pick any drive.
#
# These are the *storage* floor, not a display floor: anything below this never
# reaches the database at all (wd14.py Wd14Engine.tag() drops it outright), so a
# UI confidence slider has nothing to reveal below whatever this is set to. 0.25
# is deliberately low — it exists to catch genuine near-misses (e.g. a 5th
# character WD14 was fairly-but-not-fully sure about) as *visible-but-filterable*
# tags rather than silently discarding them. The separate, user-adjustable
# *display* floor (search.js/writes.js `minConfidence`, default "show all") is
# what actually controls clutter day to day. Existing libraries need a re-index
# (daemon "reindex_all") to backfill tags that were below the old 0.35/0.75 cut.
WD14_ENABLED = os.environ.get("IMAGE_TAGGER_WD14", "0") not in ("0", "false", "")
WD14_GENERAL_THRESHOLD = float(os.environ.get("IMAGE_TAGGER_WD14_GEN", "0.25"))
WD14_CHARACTER_THRESHOLD = float(os.environ.get("IMAGE_TAGGER_WD14_CHAR", "0.25"))

# CLIP zero-shot scene/clothing + embedding store (§5/§8, M5). Off by default
# until the model is downloaded (§12); enable with IMAGE_TAGGER_CLIP=1. Model
# folder resolves via db.model_dir(con,'clip') (open_clip cache_dir).
# Same storage-floor reasoning as WD14 above — lowered so borderline scene/
# clothing labels are stored and left to the display-side confidence filter.
CLIP_ENABLED = os.environ.get("IMAGE_TAGGER_CLIP", "0") not in ("0", "false", "")
CLIP_MODEL = os.environ.get("IMAGE_TAGGER_CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED = os.environ.get("IMAGE_TAGGER_CLIP_PRETRAINED", "openai")
CLIP_THRESHOLD = float(os.environ.get("IMAGE_TAGGER_CLIP_THRESH", "0.25"))

# InsightFace real-face detection + clustering (§5, M6). Off by default until the
# buffalo_l pack is present (§12); enable with IMAGE_TAGGER_FACES=1. Faces are
# skipped for images the kind router labeled 'anime'. FACE_THRESHOLD is the cosine
# cutoff for attaching a face to an existing person cluster.
FACES_ENABLED = os.environ.get("IMAGE_TAGGER_FACES", "0") not in ("0", "false", "")
FACE_THRESHOLD = float(os.environ.get("IMAGE_TAGGER_FACE_THRESH", "0.5"))

# Captioning (§11, M8). Local VLM -> files.caption -> FTS. Off by default until
# a model is downloaded (§12); enable with IMAGE_TAGGER_CAPTION=1. The model id is
# swappable per library (settings 'caption_model'); env overrides for dev/test.
CAPTION_ENABLED = os.environ.get("IMAGE_TAGGER_CAPTION", "0") not in ("0", "false", "")
CAPTION_MODEL = os.environ.get("IMAGE_TAGGER_CAPTION_MODEL",
                               "Salesforce/blip-image-captioning-base")


_FACET_ENV = {
    "ocr": "IMAGE_TAGGER_OCR",
    "wd14": "IMAGE_TAGGER_WD14",
    "clip": "IMAGE_TAGGER_CLIP",
    "faces": "IMAGE_TAGGER_FACES",
    "caption": "IMAGE_TAGGER_CAPTION",
}
_FACET_DEFAULT = {
    "ocr": True,
    "wd14": False,
    "clip": False,
    "faces": False,
    "caption": False,
}


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "off", "no", "")


def facet_enabled(con, facet: str) -> bool:
    """Return the live per-library state for an inference facet.

    An explicitly-set environment variable remains the highest-priority
    dev/test override. Otherwise the Models screen's ``<facet>_enabled`` DB
    setting is read for every job, so toggles take effect without restarting
    the daemon. OCR defaults on; heavyweight facets default off until the user
    enables them.
    """
    if facet not in _FACET_ENV:
        raise ValueError(f"unknown inference facet: {facet}")
    env_name = _FACET_ENV[facet]
    if env_name in os.environ:
        return _as_bool(os.environ[env_name])
    if con is not None:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?", (f"{facet}_enabled",)
        ).fetchone()
        if row is not None:
            try:
                value = row["value"]
            except (TypeError, IndexError):
                value = row[0]
            return _as_bool(value)
    return _FACET_DEFAULT[facet]
