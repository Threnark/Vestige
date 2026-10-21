"""
ui_footer.py
------------
The quiet line along the bottom of the main window:

    Support the dev    Vestige v0.1.0  ·  Copyright © 2026 THRENARK  ·  GPLv3

"Support the dev" is a small link that opens the About page of the Help window (where the
ways to say thanks are, none of them required). The rest is plain, greyed-out text.

DESIGN RULES (it must never get in the user's way)
  * Small text (9 pt) in muted colours, one line, about 22 pixels tall.
  * It never changes, flashes, pops anything up or asks for anything. It only reacts when
    clicked, and the only reaction is opening the About page.
  * It sits under the pages, so it takes nothing from the space the pages use except its
    own height.
The wording and facts come from app_info.py.
"""

from __future__ import annotations

import tkinter as tk

import app_info
import ui_theme as theme


class Footer(tk.Frame):
    """The footer bar. `on_support` is called (with no arguments) when "Support the dev" is clicked."""

    def __init__(self, parent, on_support):
        super().__init__(parent, bg=theme.BG, borderwidth=0, highlightthickness=0)
        self.link = tk.Label(self, text=app_info.FOOTER_LINK_TEXT, bg=theme.BG, fg=theme.LINK,
                             font=theme.F.tk_small_link, cursor="hand2", borderwidth=0, highlightthickness=0)
        self.link.pack(side="left")
        self.link.bind("<Button-1>", lambda _event: on_support())
        self.link.bind("<Enter>", lambda _event: self.link.configure(fg=theme.TEXT))      # a gentle hover cue
        self.link.bind("<Leave>", lambda _event: self.link.configure(fg=theme.LINK))
        self.info = tk.Label(self, text=app_info.FOOTER_TEXT, bg=theme.BG, fg=theme.FAINT, font=theme.F.tk_small,
                             borderwidth=0, highlightthickness=0)
        self.info.pack(side="left", padx=(16, 0))
