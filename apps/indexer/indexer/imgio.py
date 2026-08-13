"""Shared image-open helper.

Every facet (thumbnail generation, WD14, CLIP, captioning, faces) used to call
`Image.open(path)` directly, which returns pixels in the camera sensor's raw
orientation — ignoring the EXIF `Orientation` tag phones/cameras set. Browsers
auto-rotate on display by default, so the lightbox (raw file bytes) looked
correct while the pre-baked thumbnail (no viewer to auto-rotate a static
WEBP) came out sideways/upside-down, and every model was fed the same
un-rotated pixels a human wouldn't recognize as "right side up" — plausibly
degrading tagging/captioning/face-detection quality on real photos, which are
exactly the files phones stamp with a non-default orientation.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageDecodeError(OSError):
    """An input exists but cannot currently be decoded as an image."""


# No real still image in this app's own library exceeds a few hundred MB even
# at extreme resolution (the largest confirmed real photos are ~13500x19000,
# ~30MB on disk once compressed) -- generous enough to never reject a
# legitimate image, while decisively excluding video, which is the entire
# population this limit exists to catch.
_CV2_FALLBACK_MAX_BYTES = 512 * 1024 * 1024


def open_oriented(path: str) -> Image.Image:
    """Open an image and bake in its EXIF orientation, so callers always get
    pixels the way a viewer would show them.

    Pillow defers most decoding until pixels are accessed. Force that work here
    so every caller gets the same result, then try OpenCV as a fallback for a
    valid encoding Pillow does not recognise. Empty cloud placeholders and
    genuinely corrupt files receive actionable diagnostics.
    """
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ImageDecodeError(f"Cannot read image file: {exc}") from exc
    if size == 0:
        raise ImageDecodeError(
            "Empty image file (0 bytes). If it is stored in Google Drive, "
            "OneDrive, or another cloud drive, make it available offline and rescan."
        )

    pillow_error: Exception | None = None
    img: Image.Image | None = None
    try:
        img = Image.open(source)
        img.load()
        oriented = ImageOps.exif_transpose(img)
        if oriented is not img:
            img.close()
        return oriented
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if img is not None:
            img.close()
        pillow_error = exc

    # rapidocr normally supplies cv2; keep it optional for lightweight tests.
    # Skipped above this size: np.fromfile() below reads the *entire* file into
    # memory before cv2 even looks at it, and Pillow already rejected it as an
    # unrecognised format. For an ordinary photo that is a wasted copy; for a
    # library that also indexes video (§12 scope: video rows get a thumbnail
    # frame + filename search, no facets -- they are expected to land here and
    # be turned away) it means quietly reading multi-gigabyte, sometimes
    # 20GB+, .mp4/.mov files start to finish on every reindex, stalling the
    # single worker thread for minutes per file for a decode that was always
    # going to fail. cv2.imdecode() cannot demux video regardless of how much
    # of the file is handed to it, so skipping the read changes no outcome --
    # every file this threshold turns away would have hit the
    # ImageDecodeError below anyway, just after an expensive detour.
    if size <= _CV2_FALLBACK_MAX_BYTES:
        try:
            import cv2
            import numpy as np

            encoded = np.fromfile(source, dtype=np.uint8)
            decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
            if decoded is not None:
                if decoded.ndim == 2:
                    decoded = cv2.cvtColor(decoded, cv2.COLOR_GRAY2RGB)
                elif decoded.shape[2] == 4:
                    decoded = cv2.cvtColor(decoded, cv2.COLOR_BGRA2RGBA)
                    return Image.fromarray(decoded, mode="RGBA")
                else:
                    decoded = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
                return Image.fromarray(decoded, mode="RGB")
        except Exception:
            pass

    raise ImageDecodeError(
        f"Unsupported or corrupt image data ({size:,} bytes). Restore or "
        "re-export the original file, then rescan."
    ) from pillow_error
