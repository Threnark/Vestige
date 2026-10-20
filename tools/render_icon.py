"""
tools/render_icon.py
--------------------
Regenerates the PNG / ICO / ICNS app icons from assets/vestige_icon.svg.

Why this exists: there is no SVG renderer in the standard library, so this script reads the SVG's own paths
and colours (it understands the simple absolute M / L / C / Z path commands this icon uses) and draws them with
matplotlib, then Pillow writes the icon files. If you replace the SVG with a more complex one (curves written
with other commands, gradients, masks), render it with a proper tool such as Inkscape instead and save the PNG
as assets/vestige_icon_1024.png before running the "resize" step below.

Needs:  pip install matplotlib pillow
Run from the project folder:  python tools/render_icon.py
"""

from __future__ import annotations

import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt                      # noqa: E402
from matplotlib.patches import PathPatch             # noqa: E402
from matplotlib.path import Path                     # noqa: E402
from PIL import Image                                # noqa: E402

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
SVG = os.path.join(ASSETS, "vestige_icon.svg")
CANVAS = 1080                                         # the SVG's viewBox size


def parse_path(d: str) -> Path:
    """Parse the absolute M, L, C, Z commands of an SVG path."""
    tokens = re.findall(r"[MLCZ]|-?\d+(?:\.\d+)?", d)
    verts, codes, i, cmd = [], [], 0, None
    while i < len(tokens):
        t = tokens[i]
        if t in "MLCZ":
            cmd = t
            i += 1
            if cmd == "Z":
                verts.append(verts[-1] if verts else (0, 0))
                codes.append(Path.CLOSEPOLY)
            continue
        if cmd == "M":
            verts.append((float(t), float(tokens[i + 1])))
            codes.append(Path.MOVETO)
            i, cmd = i + 2, "L"
        elif cmd == "L":
            verts.append((float(t), float(tokens[i + 1])))
            codes.append(Path.LINETO)
            i += 2
        elif cmd == "C":
            for k in range(3):
                verts.append((float(tokens[i + 2 * k]), float(tokens[i + 2 * k + 1])))
                codes.append(Path.CURVE4)
            i += 6
        else:
            raise ValueError(f"unsupported SVG path command near {t!r}")
    return Path(verts, codes)


def render(px: int):
    svg = open(SVG, encoding="utf-8").read()
    css = dict(re.findall(r"\.(cls-\d)\{([^}]*)\}", svg))
    elements = re.findall(r'<path d="([^"]+)" class="(cls-\d)"', svg)
    fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, CANVAS)
    ax.set_ylim(CANVAS, 0)
    ax.axis("off")
    fig.patch.set_alpha(0)
    ax.patch.set_alpha(0)
    for d, cls in elements:
        fill = re.search(r"fill:(#[0-9a-fA-F]{6})", css[cls])
        stroke = re.search(r"stroke:(#[0-9a-fA-F]{6})", css[cls])
        width = re.search(r"stroke-width:(\d+)", css[cls])
        if cls == "cls-2":            # a hairline outline hidden by a mask in the SVG; not needed
            continue
        linewidth = float(width.group(1)) * (px / CANVAS) * 72 / 100 if width else 0
        ax.add_patch(PathPatch(parse_path(d), facecolor=fill.group(1) if fill else "none",
                               edgecolor=stroke.group(1) if stroke else "none", linewidth=linewidth,
                               joinstyle="miter", capstyle="butt", fill=bool(fill)))
    return fig


def main() -> None:
    master = os.path.join(ASSETS, "vestige_icon_1024.png")
    render(1024).savefig(master, dpi=100, transparent=True)
    src = Image.open(master).convert("RGBA")
    for size in (512, 256, 64, 32):
        src.resize((size, size), Image.LANCZOS).save(os.path.join(ASSETS, f"vestige_icon_{size}.png"))
    src.save(os.path.join(ASSETS, "vestige.ico"), format="ICO",
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    src.save(os.path.join(ASSETS, "vestige.icns"), format="ICNS")
    print("icons written to", ASSETS)


if __name__ == "__main__":
    main()
