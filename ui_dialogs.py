"""
ui_dialogs.py
-------------
Themed pop-up dialogs, so the whole app keeps the Vestige look.

The operating system's own message boxes ignore dark mode and would look like a
different program, so these are built from the same widgets as everything else.
The function names and behaviour deliberately mirror tkinter.messagebox
(showinfo / showwarning / showerror / askyesno / askokcancel) plus `ask_choice`
for dialogs with custom buttons, so calling code reads the same.

What stays native: the file/folder pickers (tkinter.filedialog). Those are
provided by the operating system and can't be themed.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

import ui_theme as theme

_root = None
LONG_MESSAGE = 900          # longer messages are shown in a scrolling box instead of a label


def set_root(root) -> None:
    """Register the main window; dialogs are centred over it and are modal to it."""
    global _root
    _root = root


class _Modal(ctk.CTkToplevel):
    def __init__(self, title: str, message: str, buttons: list, default: int = 0, width: int = 520):
        parent = _root
        super().__init__(parent, fg_color=theme.BG)
        self.title(title)
        self.resizable(False, False)
        self.result = None

        if len(message) > LONG_MESSAGE:
            body = theme.textbox(self, height=340)
            body.configure(width=width - 48)
            body.insert("end", message)
            body.configure(state="disabled")
        else:
            body = theme.paragraph(self, message, wraplength=width - 48)
        body.pack(padx=24, pady=(22, 14), anchor="w")

        row = theme.frame(self)
        row.pack(padx=24, pady=(0, 20), anchor="e")
        for label in buttons:
            theme.button(row, label, command=lambda l=label: self._pick(l),
                         width=max(96, 9 * len(label) + 34)).pack(side="left", padx=5)

        self.bind("<Escape>", lambda _e: self._pick(None))
        self.bind("<Return>", lambda _e: self._pick(buttons[default]))
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))
        # CustomTkinter windows apply their own default icon a moment after being
        # created, so ours is set slightly later to win.
        self.after(250, lambda: theme.apply_icon(self))
        self._centre(parent)
        if parent is not None:
            self.transient(parent.winfo_toplevel())
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass                                  # not fatal: the dialog just isn't strictly modal
        self.wait_window()

    def _centre(self, parent) -> None:
        self.update_idletasks()
        if parent is None:
            return
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")

    def _pick(self, label) -> None:
        self.result = label
        self.destroy()


def ask_choice(title: str, message: str, buttons: list):
    """Show custom buttons; return the label clicked, or None if the window was closed."""
    return _Modal(title, message, buttons).result


def showinfo(title: str, message: str = "") -> None:
    _Modal(title, message, ["OK"])


def showwarning(title: str, message: str = "") -> None:
    _Modal(title, message, ["OK"])


def showerror(title: str, message: str = "") -> None:
    _Modal(title, message, ["OK"])


def askyesno(title: str, message: str = "") -> bool:
    return _Modal(title, message, ["Yes", "No"]).result == "Yes"


def askokcancel(title: str, message: str = "") -> bool:
    return _Modal(title, message, ["OK", "Cancel"]).result == "OK"
