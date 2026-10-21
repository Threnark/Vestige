"""
ui_icons.py
-----------
The small illustrations on the About page's support buttons: a star, a heart and a coffee
cup. They are DRAWN with Pillow (already a requirement of Vestige) instead of being loaded
from image files, so there is nothing extra to bundle into a build and nothing that can go
missing.

HOW THEY ARE DRAWN
  Each icon is painted at 8x the wanted size and then shrunk, which smooths the edges
  (anti-aliasing). The result is a transparent square image in a single colour.
  ui_theme.icon() wraps these in a CustomTkinter image and caches them.

No GUI library is imported here, so the drawing code can be tested anywhere Pillow exists.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

_SUPERSAMPLE = 8


def _canvas(size: int):
    """A transparent square at 8x size, and a pen for it."""
    big = size * _SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    return image, ImageDraw.Draw(image), big


def _finish(image, size: int):
    return image.resize((size, size), Image.LANCZOS)


def star(size: int, color: str):
    """A softly rounded five-pointed star."""
    image, pen, n = _canvas(size)
    cx, cy = n * 0.5, n * 0.54
    outer, inner = n * 0.47, n * 0.215
    points = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = -math.pi / 2 + i * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    pen.polygon(points, fill=color)
    # Trace the outline once round and a little past the start, so the joint at the first tip is
    # drawn like all the others (stopping exactly at the start leaves a small nick in that tip).
    pen.line(points + [points[0], points[1]], fill=color, width=int(n * 0.05), joint="curve")
    return _finish(image, size)


def heart(size: int, color: str):
    """A heart, from the classic parametric heart curve."""
    image, pen, n = _canvas(size)
    raw = []
    for step in range(0, 361, 2):
        t = math.radians(step)
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        raw.append((x, -y))                                   # minus: the picture's y axis points down
    xs, ys = [p[0] for p in raw], [p[1] for p in raw]
    scale = n * 0.84 / max(max(xs) - min(xs), max(ys) - min(ys))
    ox = n / 2 - (max(xs) + min(xs)) / 2 * scale              # centre the shape in the square
    oy = n / 2 - (max(ys) + min(ys)) / 2 * scale
    pen.polygon([(x * scale + ox, y * scale + oy) for x, y in raw], fill=color)
    return _finish(image, size)


def coffee(size: int, color: str):
    """A coffee cup on a saucer with two wisps of steam."""
    image, pen, n = _canvas(size)
    line = int(n * 0.07)
    # the cup: a rounded block
    pen.rounded_rectangle((n * 0.16, n * 0.40, n * 0.64, n * 0.76), radius=int(n * 0.12), fill=color)
    pen.rectangle((n * 0.16, n * 0.40, n * 0.64, n * 0.52), fill=color)               # square off the rim
    # the handle: a ring on the right
    pen.arc((n * 0.52, n * 0.43, n * 0.86, n * 0.69), start=-90, end=90, fill=color, width=line)
    # the saucer
    pen.rounded_rectangle((n * 0.08, n * 0.82, n * 0.72, n * 0.89), radius=int(n * 0.035), fill=color)
    # two wisps of steam
    for x0 in (0.29, 0.46):
        wisp = [(n * (x0 + 0.035 * math.sin(i / 3.0)), n * (0.31 - i * 0.022)) for i in range(0, 8)]
        pen.line(wisp, fill=color, width=int(n * 0.05), joint="curve")
    return _finish(image, size)


DRAWERS = {"star": star, "heart": heart, "coffee": coffee}


def make(name: str, size: int, color: str):
    """The named icon as a transparent square PIL image (an unknown name raises KeyError)."""
    return DRAWERS[name](size, color)
