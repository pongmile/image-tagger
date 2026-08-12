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

from PIL import Image, ImageOps


def open_oriented(path: str) -> Image.Image:
    """Open an image and bake in its EXIF orientation, so callers always get
    pixels the way a viewer would show them."""
    img = Image.open(path)
    return ImageOps.exif_transpose(img)
