"""
ui_theme.py
-----------
The single source of truth for how Vestige looks: DARK mode only, purple-tinted,
taken from the concept design (the screenshot and the theme JSON).

Every page builds its widgets through the small factory functions below
(`button`, `entry`, `checkbox`, ...) instead of calling CustomTkinter directly,
so the whole application stays visually cohesive and a restyle is a change in ONE
place: this file.

WHAT WAS CLEANED UP FROM THE CONCEPT FILES
  The exported concept code contained a few things that can't work as written, so
  they were interpreted rather than copied:
    * invalid Python names (`500 = ...`, `self. = ...`)       -> proper variables
    * font "@Yu Gothic UI Semibold" (the "@" selects a vertical-text font on
      Windows)                                                  -> "Yu Gothic UI Semibold"
    * font names that only exist on some systems                -> a fallback list,
      resolved at start-up from the fonts actually installed
    * single-line fields drawn with a multi-line Textbox        -> real entry fields
    * hard-coded padding offsets used to centre things          -> real layout
  Colours, sizes and proportions are kept as designed.
"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import customtkinter as ctk

import ui_metrics as metrics

# ---------------------------------------------------------------------------
# Palette (dark / purple). Values come straight from the concept.
# ---------------------------------------------------------------------------
BG = "#242127"             # window background
PANEL = "#2c2833"          # tab bar, drop-down menus
BUTTON = "#3f384b"         # buttons, drop-down arrow buttons
BUTTON_HOVER = "#44424f"   # hover state; also the selected tab
SELECTED = "#44424f"       # the active tab
FIELD = "#3b3b3b"          # entry fields and the log box
TABLE_BG = "#2b2b2b"       # table body
TABLE_HEADER = "#4a4a4a"   # table header band
SCROLL = "#696969"         # scrollbar thumbs
SCROLL_HOVER = "#8a8a8a"
TEXT = "#dce4ee"           # normal text
TEXT_MUTED = "#bebebe"     # secondary text
TEXT_DISABLED = "#7d7885"
CHECK_BORDER = "#666c71"   # check box / radio outline
ACCENT = "#8f6c97"         # the purple from the app icon: progress fill, ticked boxes
ACCENT_HOVER = "#a07fa8"

# Privacy line colours, brightened for a dark background.
PRIVACY_COLORS = {"local": "#6fcf97", "lan": "#e0b04b", "cloud": "#e0b04b", "remote": "#ff7b72"}

CORNER = 6                 # corner radius used everywhere
WINDOW_SIZE = "940x800"
WINDOW_MIN = (860, 720)      # below this the Setup page would start to clip


# ---------------------------------------------------------------------------
# Fonts: first installed family wins
# ---------------------------------------------------------------------------
_HEADING_FONTS = ["Segoe UI Variable Display Semib", "Segoe UI Semibold", "SF Pro Display",
                  "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans"]
_BODY_FONTS = ["Yu Gothic UI Semibold", "Segoe UI Semibold", "Segoe UI", "SF Pro Text",
               "Helvetica Neue", "Ubuntu", "Noto Sans", "DejaVu Sans"]
_MONO_FONTS = ["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"]


def _pick_family(installed: set, preferred: list) -> tuple:
    """Return (family, already_semibold). `already_semibold` is True when the
    family name itself is a semibold face, so it must NOT also be set bold."""
    for name in preferred:
        if name in installed:
            return name, ("Semib" in name or "Semibold" in name)
    return "TkDefaultFont", False


def _detect_scale(root) -> float:
    """The factor CustomTkinter multiplies its own widget sizes and font sizes by
    (1.0 at 100% display scaling, 1.5 at 150%, ...). Table text is sized with the same
    factor so it matches every other piece of text in the window."""
    try:
        return metrics.clamp_scale(ctk.ScalingTracker.get_widget_scaling(root))
    except Exception:
        pass
    try:      # fallback: Tk's pixels-per-point, relative to a 96 dpi screen
        return metrics.clamp_scale(float(root.tk.call("tk", "scaling")) * 72.0 / 96.0)
    except Exception:
        return 1.0


class Fonts:
    """Resolved once the window exists (installed fonts can't be listed before)."""

    def __init__(self, root):
        installed = set(tkfont.families(root))
        head, head_semibold = _pick_family(installed, _HEADING_FONTS)
        body, body_semibold = _pick_family(installed, _BODY_FONTS)
        mono, _ = _pick_family(installed, _MONO_FONTS)
        hw = "normal" if head_semibold else "bold"
        bw = "normal" if body_semibold else "bold"
        self.family_body, self.family_mono = body, mono
        self.body_semibold = body_semibold
        self.scale = _detect_scale(root)       # CustomTkinter's display-scale factor (1.0 = 100%)
        # Plain-Tk fonts for wrapped paragraphs (sizes are in points, which Tk scales for DPI itself).
        self.tk_body = tkfont.Font(root, family=body, size=10)
        self.tk_small = tkfont.Font(root, family=body, size=9)
        self.tk_link = tkfont.Font(root, family=body, size=10, underline=True)     # web links in the Help window
        self.tab = ctk.CTkFont(family=head, size=15, weight=hw)
        self.heading = ctk.CTkFont(family=head, size=15, weight=hw)
        self.body = ctk.CTkFont(family=body, size=13, weight=bw)
        self.button = ctk.CTkFont(family=body, size=12, weight=bw)
        self.small = ctk.CTkFont(family=body, size=12, weight="normal" if body_semibold else "normal")
        self.mono = ctk.CTkFont(family=mono, size=12)
        self.big = ctk.CTkFont(family=head, size=22, weight=hw)


F: Fonts | None = None      # set by init(); every factory reads it


def init(root) -> None:
    """Call once, right after the main window is created."""
    global F
    F = Fonts(root)
    _style_treeview(root)


def setup() -> None:
    """Call BEFORE creating the main window: dark mode only, no light variant."""
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")


# ---------------------------------------------------------------------------
# Window icon
# ---------------------------------------------------------------------------
_icon_refs: list = []       # Tk drops images that nothing references


def asset_path(name: str) -> str:
    """Find a bundled asset both from source and inside a PyInstaller build."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", name)


def apply_icon(window) -> None:
    """Give a window the Vestige icon. Never raises: a missing icon must not stop the app."""
    try:
        if sys.platform.startswith("win"):
            window.iconbitmap(asset_path("vestige.ico"))
        else:
            image = tk.PhotoImage(file=asset_path("vestige_icon_256.png"))
            _icon_refs.append(image)
            window.iconphoto(True, image)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Widget factories
# ---------------------------------------------------------------------------
def frame(parent, **kw):
    """A transparent container (the concept's sections have no background)."""
    kw.setdefault("fg_color", "transparent")
    return ctk.CTkFrame(parent, **kw)


def heading(parent, text: str):
    return ctk.CTkLabel(parent, text=text, font=F.heading, text_color=TEXT, anchor="w", justify="left")


def label(parent, text: str = "", muted: bool = False, small: bool = False, anchor: str = "w",
          textvariable=None, text_color: str | None = None):
    """A SINGLE-LINE label. (For text that wraps over several lines use `paragraph`:
    a CustomTkinter label doesn't grow to fit wrapped text and scales its wrap width.)
    If `textvariable` is given the label follows it; this is done with a trace rather
    than a CustomTkinter option, so it behaves the same in every version."""
    lbl = ctk.CTkLabel(parent, text=textvariable.get() if textvariable is not None else text,
                       font=F.small if small else F.body, text_color=text_color or (TEXT_MUTED if muted else TEXT),
                       anchor=anchor, justify="left")
    if textvariable is not None:
        textvariable.trace_add("write", lambda *_: lbl.configure(text=textvariable.get()))
    return lbl


def paragraph(parent, text: str = "", muted: bool = False, small: bool = False, wraplength: int = 600,
              textvariable=None, fg: str | None = None, bg: str | None = None):
    """Text that WRAPS over several lines. A plain Tk label: it sizes itself to the wrapped
    text and takes its wrap width in real pixels, so re-wrapping when the window resizes
    (`widget.configure(wraplength=...)`) is accurate at any display scaling.
    `bg` is only needed when the text sits on a coloured panel instead of the window background."""
    kw = {"textvariable": textvariable} if textvariable is not None else {"text": text}
    return tk.Label(parent, bg=bg or BG, fg=fg or (TEXT_MUTED if muted else TEXT), font=F.tk_small if small else F.tk_body,
                    justify="left", anchor="w", wraplength=wraplength, borderwidth=0, highlightthickness=0, **kw)


def button(parent, text: str, command=None, width: int = 140, height: int = 28):
    return ctk.CTkButton(parent, text=text, command=command, width=width, height=height,
                         corner_radius=CORNER, border_width=0, fg_color=BUTTON, hover_color=BUTTON_HOVER,
                         text_color=TEXT, text_color_disabled=TEXT_DISABLED, font=F.button)


def entry(parent, width: int = 200, textvariable=None, show: str | None = None, placeholder: str | None = None):
    kw = {}
    if show:
        kw["show"] = show
    if placeholder:
        kw["placeholder_text"] = placeholder
    return ctk.CTkEntry(parent, width=width, height=28, corner_radius=CORNER, border_width=0,
                        fg_color=FIELD, text_color=TEXT, placeholder_text_color=TEXT_MUTED,
                        font=F.body, textvariable=textvariable, **kw)


def checkbox(parent, text: str, variable, command=None, width: int = 100):
    return ctk.CTkCheckBox(parent, text=text, variable=variable, command=command, onvalue=True, offvalue=False,
                           width=width, checkbox_width=22, checkbox_height=22, corner_radius=4, border_width=2,
                           border_color=CHECK_BORDER, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                           text_color=TEXT, font=F.body)


def radio(parent, text: str, variable, value, command=None):
    return ctk.CTkRadioButton(parent, text=text, variable=variable, value=value, command=command,
                              radiobutton_width=20, radiobutton_height=20, border_width_unchecked=2,
                              border_width_checked=6, border_color=CHECK_BORDER, fg_color=ACCENT,
                              hover_color=ACCENT_HOVER, text_color=TEXT, font=F.body)


def option_menu(parent, values: list, variable=None, command=None, width: int = 200):
    return ctk.CTkOptionMenu(parent, values=values, variable=variable, command=command, width=width,
                             height=28, corner_radius=CORNER, fg_color=PANEL, button_color=BUTTON,
                             button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL,
                             dropdown_hover_color=BUTTON, dropdown_text_color=TEXT, text_color=TEXT,
                             font=F.body, dropdown_font=F.body, dynamic_resizing=False, anchor="w")


def combo(parent, values: list, variable=None, width: int = 200):
    """An editable drop-down (used for the model name: type it or pick it)."""
    return ctk.CTkComboBox(parent, values=values, variable=variable, width=width, height=28,
                           corner_radius=CORNER, border_width=0, fg_color=PANEL, button_color=BUTTON,
                           button_hover_color=BUTTON_HOVER, dropdown_fg_color=PANEL,
                           dropdown_hover_color=BUTTON, dropdown_text_color=TEXT, text_color=TEXT,
                           font=F.body, dropdown_font=F.body)


def segmented(parent, values: list, command=None, height: int = 33):
    return ctk.CTkSegmentedButton(parent, values=values, command=command, height=height,
                                  corner_radius=CORNER, border_width=0, fg_color=PANEL,
                                  selected_color=SELECTED, selected_hover_color=SELECTED,
                                  unselected_color=PANEL, unselected_hover_color=BUTTON,
                                  text_color=TEXT, font=F.tab)


def textbox(parent, height: int = 150):
    return ctk.CTkTextbox(parent, height=height, corner_radius=CORNER, border_width=0, fg_color=FIELD,
                          text_color=TEXT, font=F.mono, wrap="word", scrollbar_button_color=SCROLL,
                          scrollbar_button_hover_color=SCROLL_HOVER)


def progressbar(parent):
    return ctk.CTkProgressBar(parent, height=14, corner_radius=7, border_width=0, fg_color=FIELD,
                              progress_color=ACCENT, mode="determinate")


def scrollbar(parent, command):
    return ctk.CTkScrollbar(parent, command=command, button_color=SCROLL, button_hover_color=SCROLL_HOVER)


def scroll_area(parent, height: int = 80):
    """A small scrolling list area (used for the per-account tick boxes)."""
    return ctk.CTkScrollableFrame(parent, height=height, corner_radius=CORNER, fg_color=TABLE_BG,
                                  scrollbar_button_color=SCROLL, scrollbar_button_hover_color=SCROLL_HOVER)


# ---------------------------------------------------------------------------
# Help window and [?] buttons (see ui_help.py)
# ---------------------------------------------------------------------------
# The coloured bar at the left of a Help note: neutral, helpful, careful.
CALLOUT_BAR = {"info": ACCENT, "tip": PRIVACY_COLORS["local"], "warning": PRIVACY_COLORS["lan"]}
CALLOUT_BG = PANEL
LINK = ACCENT_HOVER        # web links (they are also underlined, so colour is not the only cue)


def title(parent, text: str):
    """A big page title (used at the top of each Help page)."""
    return ctk.CTkLabel(parent, text=text, font=F.big, text_color=TEXT, anchor="w", justify="left")


def help_button(parent, command=None):
    """The small [?] box that sits next to a part of the interface and opens its Help page."""
    return ctk.CTkButton(parent, text="?", command=command, width=24, height=24, corner_radius=CORNER,
                         border_width=0, fg_color=BUTTON, hover_color=ACCENT, text_color=TEXT, font=F.button)


def nav_button(parent, text: str, command=None):
    """One entry in the Help window's left-hand list. Highlight it with set_nav_selected()."""
    return ctk.CTkButton(parent, text=text, command=command, height=32, corner_radius=CORNER, border_width=0,
                         fg_color="transparent", hover_color=BUTTON, text_color=TEXT, font=F.body, anchor="w")


def set_nav_selected(button, selected: bool) -> None:
    button.configure(fg_color=SELECTED if selected else "transparent")


def page_scroll(parent):
    """A scrolling area with the window's own background (so text blends in), used for Help pages."""
    return ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0,
                                  scrollbar_button_color=SCROLL, scrollbar_button_hover_color=SCROLL_HOVER)


# ---------------------------------------------------------------------------
# Tables. CustomTkinter has no table widget, and one built from labels would be
# far too slow for thousands of rows, so tables are a ttk Treeview restyled to
# match (see _style_treeview), wrapped in a rounded frame with a themed scrollbar.
# ---------------------------------------------------------------------------
TREE_STYLE = "Vestige.Treeview"


def _style_treeview(root) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")            # the only built-in ttk theme that accepts these colours everywhere
    except tk.TclError:
        pass
    scale = F.scale
    body_font, head_font = metrics.table_font_specs(F.family_body, F.body_semibold, scale)
    probe = tkfont.Font(root, font=body_font)                     # only used to measure the text height
    row_height = probe.metrics("linespace") + metrics.scaled(metrics.TABLE_ROW_PAD, scale)
    pad_x, pad_y = (metrics.scaled(p, scale) for p in metrics.TABLE_HEAD_PAD)
    style.layout(TREE_STYLE, [(TREE_STYLE + ".treearea", {"sticky": "nswe"})])    # no border
    style.configure(TREE_STYLE, background=TABLE_BG, fieldbackground=TABLE_BG, foreground=TEXT,
                    borderwidth=0, relief="flat", rowheight=row_height, font=body_font)
    style.map(TREE_STYLE, background=[("selected", BUTTON)], foreground=[("selected", TEXT)])
    style.configure(TREE_STYLE + ".Heading", background=TABLE_HEADER, foreground=TEXT, relief="flat",
                    borderwidth=0, padding=(pad_x, pad_y), font=head_font)
    style.map(TREE_STYLE + ".Heading", background=[("active", BUTTON_HOVER)])


def table(parent, columns: tuple, headings: dict, widths: dict, height: int = 8):
    """Return (container, tree). Pack/grid the container; use the tree for data."""
    container = ctk.CTkFrame(parent, corner_radius=8, fg_color=TABLE_BG)
    container.grid_columnconfigure(0, weight=1)
    container.grid_rowconfigure(0, weight=1)
    tree = ttk.Treeview(container, columns=columns, show="headings", height=height, style=TREE_STYLE,
                        selectmode="browse")
    for col in columns:
        tree.heading(col, text=headings[col], anchor="w")
        tree.column(col, width=metrics.scaled(widths.get(col, 110), F.scale), anchor="w")
    bar = scrollbar(container, tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
    bar.grid(row=0, column=1, sticky="ns", padx=(2, 6), pady=8)
    return container, tree
