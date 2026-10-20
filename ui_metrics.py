"""
ui_metrics.py
-------------
The size arithmetic behind Vestige's TABLES (the account list on the Setup tab and
the two result tables). It has no GUI imports on purpose, so it can be unit-tested
on any machine; ui_theme.py is the only caller.

WHY TABLES NEED THEIR OWN SIZE RULES
  CustomTkinter widgets size their text in PIXELS multiplied by CustomTkinter's own
  scale factor. The table is a plain ttk Treeview, which knows nothing about that
  factor, and the old code sized it in POINTS (10 pt). Points are scaled by Tk's own
  idea of the screen's DPI, so the table text could end up a different size from
  every other piece of text in the window, depending on the display's scaling or the
  Windows "make text bigger" setting. The fix is to size table text the same way
  CustomTkinter does: pixels x the CustomTkinter scale factor.

TO MAKE TABLE TEXT BIGGER OR SMALLER, change TABLE_FONT_PX below (it is the size at
100% display scaling; everything else follows it).
"""

from __future__ import annotations

TABLE_FONT_PX = 12         # text size in table rows AND column headings, at 100% scaling
TABLE_ROW_PAD = 10         # extra pixels added to a row's height on top of the text height
TABLE_HEAD_PAD = (8, 6)    # column-heading padding: (left/right, top/bottom) pixels

MIN_SCALE, MAX_SCALE = 0.75, 4.0


def clamp_scale(value) -> float:
    """A scale factor we can trust: a number between 0.75 and 4 (1.0 if it isn't a number)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if number != number:                       # NaN
        return 1.0
    return max(MIN_SCALE, min(MAX_SCALE, number))


def scaled(pixels: float, scale: float) -> int:
    """`pixels` at the given display scale, rounded, never less than 1."""
    return max(1, int(round(pixels * clamp_scale(scale))))


def table_font_specs(family: str, semibold: bool, scale: float, base_px: int = TABLE_FONT_PX) -> tuple:
    """Return (body_font, heading_font) as Tk font tuples.

    A NEGATIVE size means "pixels" to Tk, which is what makes the size independent of
    Tk's own DPI guess. The heading is bold unless the family is already a semibold
    face (a semibold face must not also be made bold). They are tuples rather than
    tkinter Font objects on purpose: a Font object that nothing references is deleted
    by Python, and a ttk style that still names it silently falls back to a system
    font - one plausible cause of the oversized table text.
    """
    size = -scaled(base_px, scale)
    body = (family, size)
    head = (family, size) if semibold else (family, size, "bold")
    return body, head
