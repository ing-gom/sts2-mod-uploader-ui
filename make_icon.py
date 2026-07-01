#!/usr/bin/env python3
"""Generate icon.ico for the STS2 Mod Uploader UI launcher.

Draws a rounded-square badge with an "upload to the Workshop" motif
(an upward arrow rising out of a tray) and writes a multi-size .ico
next to this script. Pillow is already a dependency of the dashboard.

    python make_icon.py            # -> icon.ico
    python make_icon.py my.ico     # custom output path
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

# Render big, then downscale each size for crisp anti-aliasing.
MASTER = 1024
SIZES = [256, 128, 64, 48, 32, 16]

BG_TOP = (58, 90, 214)     # blue-violet
BG_BOTTOM = (30, 41, 92)   # deep navy
ARROW = (245, 249, 255)    # near-white
TRAY = (150, 178, 255)     # light blue


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    return mask


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    return grad.resize((size, size))


def build_master() -> Image.Image:
    s = MASTER
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Gradient background, clipped to a rounded square.
    bg = _vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA")
    img.paste(bg, (0, 0), _rounded_mask(s, radius=int(s * 0.22)))

    d = ImageDraw.Draw(img)
    cx = s / 2

    # Upward arrow (shaft + head) — the "upload" cue.
    shaft_w = s * 0.13
    shaft_top = s * 0.24
    shaft_bottom = s * 0.62
    d.rounded_rectangle(
        [cx - shaft_w / 2, shaft_top, cx + shaft_w / 2, shaft_bottom],
        radius=shaft_w / 2,
        fill=ARROW,
    )
    head_half = s * 0.19
    head_top = s * 0.14
    head_base = s * 0.36
    d.polygon(
        [(cx, head_top), (cx - head_half, head_base), (cx + head_half, head_base)],
        fill=ARROW,
    )

    # Tray / baseline the arrow rises out of.
    tray_w = s * 0.46
    tray_y = s * 0.72
    tray_h = s * 0.075
    d.rounded_rectangle(
        [cx - tray_w / 2, tray_y, cx + tray_w / 2, tray_y + tray_h],
        radius=tray_h / 2,
        fill=TRAY,
    )

    return img


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "icon.ico")

    master = build_master()
    frames = [master.resize((n, n), Image.LANCZOS) for n in SIZES]
    frames[0].save(out, format="ICO", sizes=[(n, n) for n in SIZES])

    # Also drop a PNG preview next to it for READMEs / GitHub.
    png = os.path.splitext(out)[0] + ".png"
    master.resize((256, 256), Image.LANCZOS).save(png)

    print(f"wrote {out}")
    print(f"wrote {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
