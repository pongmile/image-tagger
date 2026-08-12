#!/usr/bin/env python3
"""Generate the app icon (a photo + magnifier motif = 'image search/tagger').

Writes apps/desktop/build/icon.ico (multi-size, for the Windows exe + shortcut)
and icon.png (512, for Linux/dev window). Pure Pillow — no external assets.

    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
OUT = Path(__file__).resolve().parents[1] / "apps" / "desktop" / "build"


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return m


def build() -> Image.Image:
    blue, violet = (59, 130, 246), (139, 92, 246)
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    px = img.load()
    for y in range(SIZE):                       # diagonal gradient background
        for x in range(SIZE):
            t = (x + y) / (2 * SIZE)
            r, g, b = lerp(blue, violet, t)
            px[x, y] = (r, g, b, 255)
    img.putalpha(rounded_mask(SIZE, 96))        # rounded app-icon shape

    d = ImageDraw.Draw(img)
    # Photo frame (white rounded rect) with a mountain + sun inside.
    fx0, fy0, fx1, fy1 = 96, 120, 356, 320
    d.rounded_rectangle((fx0, fy0, fx1, fy1), radius=26, fill=(255, 255, 255, 255))
    d.ellipse((fx0 + 30, fy0 + 28, fx0 + 78, fy0 + 76), fill=(250, 204, 21, 255))  # sun
    d.polygon([(fx0 + 20, fy1 - 24), (fx0 + 110, fy0 + 96),
               (fx0 + 170, fy1 - 24)], fill=(52, 168, 110, 255))                    # mountain
    d.polygon([(fx0 + 140, fy1 - 24), (fx0 + 205, fy0 + 120),
               (fx1 - 20, fy1 - 24)], fill=(37, 140, 90, 255))                      # mountain 2

    # Magnifier (search) overlapping the lower-right.
    cx, cy, rr = 360, 360, 78
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(255, 255, 255, 255), width=26)
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=(30, 41, 59, 90), width=4)
    d.line((cx + rr - 6, cy + rr - 6, cx + rr + 54, cy + rr + 54),
           fill=(255, 255, 255, 255), width=30)
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    img = build()
    png = OUT / "icon.png"
    ico = OUT / "icon.ico"
    img.save(png)
    img.save(ico, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("wrote", png)
    print("wrote", ico)


if __name__ == "__main__":
    main()
