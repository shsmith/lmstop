#!/usr/bin/env python3
"""Generate lmstop.ico — a small 256x256 memory-bar glyph.

A text placeholder is invalid and breaks PyInstaller's icon embedding, so the
icon is drawn programmatically. Run via `make lmstop.ico` or directly.
"""
import colorsys
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "lmstop.ico")


def _build(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inset = max(1, size // 40)
    d.rounded_rectangle([inset, inset, size - 1 - inset, size - 1 - inset],
                        radius=size // 4, fill=(18, 18, 28, 255))
    bar_l, bar_r = int(size * 0.22), int(size * 0.78)
    top, bot = int(size * 0.20), int(size * 0.88)
    bot_h = bot - top
    n = 6
    step = (bar_r - bar_l) / n
    for i in range(n):
        t = i / (n - 1)
        c = colorsys.hls_to_rgb(0.47 - 0.14 * t, 0.55, 0.65)
        col = tuple(int(x * 255) for x in c) + (255,)
        x0 = bar_l + i * step
        x1 = bar_l + (i + 1) * step + 0.5
        d.rectangle([x0, bot - bot_h * (i + 1) // n, x1, bot], fill=col)
    return img


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [_build(s) for s in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()