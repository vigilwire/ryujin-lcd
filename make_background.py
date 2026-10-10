#!/usr/bin/env python3
"""Build the dashboard background (240x320 portrait). Standard Pillow only; no device access.

  python3 make_background.py                  # default: generated green circuit grid
  python3 make_background.py picture.jpg      # your own picture in the wireframe look
  -> assets/background.png   (install.sh copies it to /usr/local/share/ryujin-lcd/ if none is there yet;
                              to replace an installed one: sudo install -m 0644 assets/background.png
                              /usr/local/share/ryujin-lcd/background.png && sudo systemctl restart ryujin-lcd-monitor)

Wireframe look: the picture is cropped to 3:4, turned into green terminal tones, its
outlines glow, the rest stays faint, and CRT scanlines go over everything. Keep the
background dark: the numbers are drawn on top of it.
"""
import os
import random
import sys

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

W, H = 240, 320
HERE = os.path.dirname(os.path.abspath(__file__))


def green(lum):
    return ImageOps.colorize(lum, black=(0, 0, 0), mid=(0, 90, 40), white=(120, 255, 160))


def scanlines(im):
    d = ImageDraw.Draw(im)
    for y in range(0, H, 2):                                  # CRT scanlines
        d.line((0, y, W, y), fill=(0, 0, 0))
    return im


def wireframe(src_path):
    src = Image.open(src_path).convert("RGB")
    g = ImageOps.autocontrast(ImageOps.fit(src, (W, H), Image.LANCZOS).convert("L"), cutoff=1)
    edges = g.filter(ImageFilter.FIND_EDGES).point(lambda v: 255 if v > 40 else 0)
    glow = edges.filter(ImageFilter.GaussianBlur(2.5))
    im = ImageEnhance.Brightness(green(g.point(lambda v: v if v < 200 else 0))).enhance(0.35)
    im = ImageChops.add(im, green(glow).point(lambda v: int(v * 0.8)))
    im = ImageChops.add(im, ImageOps.colorize(edges, (0, 0, 0), (60, 255, 140)).point(lambda v: int(v * 0.55)))
    return scanlines(im)


def circuit(seed=1988):
    """Faint grid + circuit traces with solder pads, drawn from scratch (no picture needed)."""
    rnd = random.Random(seed)
    lines = Image.new("L", (W, H))
    d = ImageDraw.Draw(lines)
    step = 20
    for x in range(0, W, step):                               # faint grid
        d.line((x, 0, x, H), fill=28)
    for y in range(0, H, step):
        d.line((0, y, W, y), fill=28)
    for _ in range(26):                                       # traces: walk along the grid, one 45° bend
        x, y = rnd.randrange(0, W, step), rnd.randrange(0, H, step)
        pts = [(x, y)]
        for _ in range(rnd.randint(2, 4)):
            dx, dy = rnd.choice([(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, 1)])
            n = rnd.randint(1, 4) * step
            x, y = x + dx * n, y + dy * n
            pts.append((x, y))
        d.line(pts, fill=150, width=2)
        for px, py in (pts[0], pts[-1]):                      # pads at both ends
            d.ellipse((px - 4, py - 4, px + 4, py + 4), outline=200, width=2)
    glow = lines.filter(ImageFilter.GaussianBlur(3))
    im = ImageChops.add(green(lines).point(lambda v: int(v * 0.55)), green(glow).point(lambda v: int(v * 0.5)))
    return scanlines(im)


if __name__ == "__main__":
    out = os.path.join(HERE, "assets", "background.png")
    im = wireframe(sys.argv[1]) if len(sys.argv) > 1 else circuit()
    im.save(out)
    print("wrote", out)
