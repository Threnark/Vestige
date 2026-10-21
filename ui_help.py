"""
ui_help.py
----------
The Help window and the small [?] buttons that open it.

WHAT THIS FILE DOES
  * HelpWindow: a separate window with the help topics listed down the left (like tabs)
    and the chosen page on the right. It does NOT hold any help text. The text lives in
    help_content.py as plain data, and this file turns each block of it into widgets.
  * open_help(widget, topic): opens the window (or reuses it) on a given topic.
  * button(parent, topic) / heading(parent, text, topic): the [?] box, on its own or
    next to a heading. The pages of the program use these.

HOW IT BEHAVES
  * Only ONE Help window exists at a time. Clicking another [?] switches the open window
    to that page instead of opening a second window.
  * The window belongs to the window that opened it. That matters for pop-up forms such
    as "Add account": while such a form is open, the rest of the program is locked, but a
    window that belongs to the form is not. So a [?] inside the form creates the Help
    window under the form; it closes with the form.
  * Text re-wraps when the window is resized. Pages are rebuilt each time you switch
    topic, which is fast because they are only text.
  * Web links open in the user's normal browser (only https:// addresses), and each
    link shows its full address and a "Copy link" button. The support buttons on the About
    page open their address the same way and also show it in full.

LOOK AND FEEL
  Every colour, font and widget comes from ui_theme.py, the same as the rest of Vestige.
"""

from __future__ import annotations

import tkinter as tk
import webbrowser

import customtkinter as ctk

import help_content as content
import ui_metrics as metrics
import ui_theme as theme

# Space taken up by things that are not page text, used to work out how wide a line can be.
# All of these are "design pixels" at 100% display scaling; _px() scales them to the real
# screen the same way CustomTkinter scales its own widgets (see ui_metrics.py), because a
# sidebar that is 330 real pixels wide at 150% must be subtracted as 330, not 220.
_SIDEBAR_WIDTH = 220
_CHROME_WIDTH = 130            # page margins + scroll bar
_FALLBACK_WINDOW_WIDTH = 1000  # used before the window has a real size

# How far each kind of block is indented from the page's left edge, so wrapped text stops
# before it runs under the number, bullet or box edge. Slightly generous on purpose: a
# line that is a little too short looks fine, a line that runs off the edge is cut off.
_INDENT_STEP = 52
_INDENT_BULLET = 42
_INDENT_NOTE = 56
_INDENT_SETTINGS = 270
_INDENT_LINK = 140

_NOTE_WORD = {"info": "Note", "tip": "Tip", "warning": "Careful"}


class HelpWindow(ctk.CTkToplevel):
    MAX_SIZE = (980, 720)            # preferred size at 100% scaling (smaller if the screen is)
    MIN_SIZE = (780, 540)

    def __init__(self, owner, topic_id: str = content.DEFAULT_TOPIC):
        super().__init__(owner, fg_color=theme.BG)
        self.owner = owner                       # the window this one belongs to (see module notes)
        self.title("Vestige Help")
        self.geometry(self._starting_size())
        self.minsize(*self.MIN_SIZE)
        self.topic_id = None
        self._nav_buttons: dict = {}
        self._wrapped: list = []                 # (label, indent) pairs re-wrapped when the window resizes
        self._page = None                        # the frame holding the current page's widgets
        self._resize_job = None
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_content()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Configure>", self._on_configure)
        self.after(250, lambda: theme.apply_icon(self))      # CustomTkinter sets its own icon late
        self.show_topic(topic_id)
        self.lift()
        self.after(150, self.focus_force)

    def _starting_size(self) -> str:
        """The preferred size, made smaller if the screen can't fit it (CustomTkinter multiplies the
        size by the display scale, so the screen is divided by it first)."""
        scale = theme.F.scale
        width = min(self.MAX_SIZE[0], max(self.MIN_SIZE[0], int(self.winfo_screenwidth() / scale) - 120))
        height = min(self.MAX_SIZE[1], max(self.MIN_SIZE[1], int(self.winfo_screenheight() / scale) - 140))
        return f"{width}x{height}"

    # ---- layout --------------------------------------------------------------------------
    def _build_sidebar(self) -> None:
        side = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=0, width=_SIDEBAR_WIDTH)
        side.grid(row=0, column=0, sticky="ns")
        side.pack_propagate(False)               # keep the fixed width; the grid stretches the height
        theme.heading(side, "Help").pack(anchor="w", padx=18, pady=(18, 6))
        theme.paragraph(side, "Tip: click a [?] box anywhere in Vestige to jump to the help for that part. "
                              "Press Esc to close this window.",
                        muted=True, small=True, wraplength=_SIDEBAR_WIDTH - 36, bg=theme.PANEL
                        ).pack(side="bottom", anchor="w", padx=18, pady=(8, 16))
        for group, topic_ids in content.NAV:
            theme.label(side, group.upper(), muted=True, small=True).pack(anchor="w", padx=18, pady=(14, 2))
            for topic_id in topic_ids:
                button = theme.nav_button(side, content.TOPICS[topic_id].nav,
                                          lambda tid=topic_id: self.show_topic(tid))
                button.pack(fill="x", padx=10, pady=1)
                self._nav_buttons[topic_id] = button

    def _build_content(self) -> None:
        main = theme.frame(self)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        head = theme.frame(main)
        head.grid(row=0, column=0, sticky="ew", padx=(28, 24), pady=(18, 0))
        self.title_label = theme.title(head, "")
        self.title_label.pack(anchor="w")
        self.summary_label = theme.paragraph(head, "", muted=True, wraplength=640)
        self.summary_label.pack(anchor="w", fill="x", pady=(2, 0))
        self.pane = theme.page_scroll(main)
        self.pane.grid(row=1, column=0, sticky="nsew", padx=(18, 8), pady=(8, 14))

    # ---- showing a topic -----------------------------------------------------------------------
    def show_topic(self, topic_id: str) -> None:
        """Switch to a topic (an unknown id shows Getting started)."""
        topic = content.get_topic(topic_id)
        self.topic_id = topic.id
        for tid, button in self._nav_buttons.items():
            theme.set_nav_selected(button, tid == topic.id)
        self.title_label.configure(text=topic.title)
        self.summary_label.configure(text=topic.summary)
        if self._page is not None:
            self._page.destroy()
        self._wrapped = [(self.summary_label, -20)]
        self._page = theme.frame(self.pane)
        self._page.pack(fill="x", padx=(10, 12))
        for block in topic.blocks:
            self._render(self._page, block)
        if topic.checked:
            self._note(self._page, content.Note("info", "Provider websites change", content.CHECKED_NOTE))
        self._apply_wrap()
        self.after(80, self._apply_wrap)         # again once the window has its real size
        self._scroll_to_top()

    def _scroll_to_top(self) -> None:
        try:
            self.pane._parent_canvas.yview_moveto(0)     # CustomTkinter's inner canvas; no public way to do this
        except Exception:
            pass

    # ---- keeping text wrapped to the window ------------------------------------------------------
    @staticmethod
    def _px(design_pixels: int) -> int:
        """Design pixels -> real pixels for this display (negative values allowed)."""
        return int(design_pixels * metrics.clamp_scale(theme.F.scale))

    def _avail_width(self) -> int:
        """How wide a line of page text can be, in real pixels."""
        width = self.winfo_width()
        if width <= 100:                                   # not on screen yet
            width = self._px(_FALLBACK_WINDOW_WIDTH)
        return width - self._px(_SIDEBAR_WIDTH) - self._px(_CHROME_WIDTH)

    def _wrap_for(self, indent: int) -> int:
        return max(220, self._avail_width() - self._px(indent))

    def _apply_wrap(self) -> None:
        for label, indent in self._wrapped:
            try:
                label.configure(wraplength=self._wrap_for(indent))
            except tk.TclError:
                pass                                     # that label was destroyed with an old page

    def _on_configure(self, event) -> None:
        if event.widget is not self:                     # a toplevel also hears every child's events
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(60, self._apply_wrap)

    def _text(self, parent, text: str, indent: int = 0, **options):
        """A wrapped text label that follows the window width."""
        label = theme.paragraph(parent, text, wraplength=self._wrap_for(indent), **options)
        self._wrapped.append((label, indent))
        return label

    # ---- block renderers -------------------------------------------------------------------------
    def _render(self, page, block) -> None:
        kinds = {content.Section: self._section, content.Para: self._para, content.Steps: self._steps,
                 content.Bullets: self._bullets, content.Note: self._note, content.Link: self._link,
                 content.Settings: self._settings, content.Identity: self._identity,
                 content.SupportButtons: self._support_buttons}
        kinds[type(block)](page, block)

    def _section(self, page, block) -> None:
        theme.heading(page, block.text).pack(anchor="w", pady=(22, 6))

    def _para(self, page, block) -> None:
        self._text(page, block.text).pack(anchor="w", fill="x", pady=(0, 8))

    def _list(self, page, items, marker, indent: int) -> None:
        """Shared by steps (numbers) and bullets (dots): a marker column and a wrapped text column."""
        for position, text in enumerate(items, start=1):
            row = theme.frame(page)
            row.pack(anchor="w", fill="x", pady=(0, 6))
            theme.paragraph(row, marker(position), fg=theme.ACCENT_HOVER, wraplength=40).pack(
                side="left", anchor="n", padx=(2, 8))
            self._text(row, text, indent).pack(side="left", anchor="n", fill="x", expand=True)

    def _steps(self, page, block) -> None:
        self._list(page, block.items, lambda n: f"{n}.", _INDENT_STEP)

    def _bullets(self, page, block) -> None:
        self._list(page, block.items, lambda _n: "\u2022", _INDENT_BULLET)

    def _note(self, page, block) -> None:
        """A highlighted box: coloured bar at the left, the kind in words (so colour is not the only
        cue), a title and the text."""
        box = ctk.CTkFrame(page, fg_color=theme.CALLOUT_BG, corner_radius=theme.CORNER)
        box.pack(fill="x", pady=(6, 14))
        # height=1: an empty CustomTkinter frame asks for 200 px unless told otherwise; it stretches to the box.
        ctk.CTkFrame(box, width=5, height=1, corner_radius=3,
                     fg_color=theme.CALLOUT_BAR.get(block.kind, theme.ACCENT)).pack(side="left", fill="y", padx=(8, 0), pady=8)
        inner = theme.frame(box)
        inner.pack(side="left", fill="x", expand=True, padx=(10, 12), pady=8)
        theme.heading(inner, f"{_NOTE_WORD.get(block.kind, 'Note')}: {block.title}").pack(anchor="w", pady=(0, 2))
        self._text(inner, block.text, _INDENT_NOTE, bg=theme.CALLOUT_BG).pack(anchor="w", fill="x")

    def _settings(self, page, block) -> None:
        """A small two-column table of what to type where."""
        box = ctk.CTkFrame(page, fg_color=theme.CALLOUT_BG, corner_radius=theme.CORNER)
        box.pack(fill="x", pady=(0, 10))
        theme.heading(box, block.title).pack(anchor="w", padx=14, pady=(10, 4))
        grid = theme.frame(box)
        grid.pack(fill="x", padx=14, pady=(0, 10))
        grid.grid_columnconfigure(1, weight=1)
        for row, (name, value) in enumerate(block.rows):
            theme.paragraph(grid, name, muted=True, wraplength=150, bg=theme.CALLOUT_BG).grid(
                row=row, column=0, sticky="nw", padx=(0, 16), pady=3)
            self._text(grid, value, _INDENT_SETTINGS, bg=theme.CALLOUT_BG).grid(row=row, column=1, sticky="nw", pady=3)

    def _identity(self, page, block) -> None:
        """The card at the top of the About page: the program icon, its name and the lines under it."""
        card = ctk.CTkFrame(page, fg_color=theme.CALLOUT_BG, corner_radius=theme.CORNER)
        card.pack(fill="x", pady=(4, 12))
        picture = theme.app_icon(72)
        if picture is not None:                              # no picture is fine; the text is what matters
            # width/height: a CustomTkinter label is 140 px wide unless told otherwise
            ctk.CTkLabel(card, text="", image=picture, width=76, height=76).pack(side="left", padx=(20, 4), pady=18)
        text = theme.frame(card)
        text.pack(side="left", fill="x", expand=True, padx=(16, 18), pady=16)
        theme.title(text, block.name).pack(anchor="w")
        for line in block.lines:
            self._text(text, line, 150, muted=True, bg=theme.CALLOUT_BG).pack(anchor="w", pady=(2, 0))

    def _support_buttons(self, page, block) -> None:
        """The ways to say thanks. Each is a button with a small picture, a gentle line saying what it
        is, and the address it opens in small grey text (so nothing is hidden). They are ordinary
        buttons: nothing happens until one is clicked."""
        for item in block.items:
            holder = theme.frame(page)
            holder.pack(anchor="w", fill="x", pady=(2, 14))
            theme.icon_button(holder, item.label, lambda u=item.url: self._open(u),
                              image=theme.icon(item.icon, 22)).pack(anchor="w")
            self._text(holder, item.note, _INDENT_STEP, muted=True, small=True).pack(anchor="w", fill="x", pady=(5, 0))
            self._text(holder, item.url, _INDENT_STEP, muted=True, small=True).pack(anchor="w", fill="x")

    def _link(self, page, block) -> None:
        """A clickable link with its address shown underneath and a Copy button."""
        holder = theme.frame(page)
        holder.pack(anchor="w", fill="x", pady=(2, 8))
        top = theme.frame(holder)
        top.pack(anchor="w", fill="x")
        label = tk.Label(top, text=block.label, bg=theme.BG, fg=theme.LINK, font=theme.F.tk_link, cursor="hand2",
                         anchor="w", justify="left", wraplength=self._wrap_for(_INDENT_LINK), borderwidth=0,
                         highlightthickness=0)
        self._wrapped.append((label, _INDENT_LINK))
        label.pack(side="left")
        label.bind("<Button-1>", lambda _e, u=block.url: self._open(u))
        label.bind("<Enter>", lambda _e: label.configure(fg=theme.TEXT))
        label.bind("<Leave>", lambda _e: label.configure(fg=theme.LINK))
        copy = theme.button(top, "Copy link", None, width=88, height=24)
        copy.configure(command=lambda u=block.url, b=copy: self._copy(u, b))
        copy.pack(side="left", padx=(12, 0))
        self._text(holder, block.url, _INDENT_STEP, muted=True, small=True).pack(anchor="w", fill="x")

    # ---- link actions ----------------------------------------------------------------------------------
    def _open(self, url: str) -> None:
        """Open a web link in the normal browser. Only https:// addresses from help_content are ever used."""
        if not url.startswith("https://"):
            return
        try:
            webbrowser.open(url)
        except Exception:
            self._copy(url, None)                        # no browser available: at least put it on the clipboard

    def _copy(self, url: str, button) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            if button is not None:
                button.configure(text="Copied")
                self.after(1500, lambda: self._restore(button))
        except tk.TclError:
            pass

    @staticmethod
    def _restore(button) -> None:
        try:
            button.configure(text="Copy link")
        except tk.TclError:
            pass                                         # the page was rebuilt in the meantime

    # ---- closing ---------------------------------------------------------------------------------------
    def close(self) -> None:
        global _window
        if _window is self:
            _window = None
        self.destroy()


# ---------------------------------------------------------------------------
# The one open window, and the functions the rest of the program uses
# ---------------------------------------------------------------------------
_window: HelpWindow | None = None


def _alive(window) -> bool:
    try:
        return window is not None and bool(window.winfo_exists())
    except tk.TclError:
        return False


def open_help(widget, topic: str = content.DEFAULT_TOPIC):
    """Show the Help window on `topic`. `widget` is whatever the user clicked: its window becomes the
    owner of the Help window (see the module notes about pop-up forms). Returns the window."""
    global _window
    owner = widget.winfo_toplevel()
    if _alive(_window):
        if _window.owner is owner:
            _window.show_topic(topic)
            _window.deiconify()
            _window.lift()
            _window.focus_force()
            return _window
        _window.close()                                  # belongs to a different window: start fresh under this one
    _window = HelpWindow(owner, topic)
    return _window


def button(parent, topic):
    """A [?] box. `topic` is a help topic id, or a function that returns one (it is called when the
    button is clicked, so the page can depend on what the user has chosen by then)."""
    box = theme.help_button(parent)
    box.configure(command=lambda: open_help(box, topic() if callable(topic) else topic))
    return box


def heading(parent, text: str, topic):
    """A section heading with a [?] box right after it. Returns the frame holding both."""
    row = theme.frame(parent)
    theme.heading(row, text).pack(side="left")
    button(row, topic).pack(side="left", padx=(10, 0))
    return row
