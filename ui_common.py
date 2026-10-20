"""
ui_common.py
------------
Small helpers shared by the pages (no widgets of their own).
"""

from __future__ import annotations

import subprocess
import sys

import ui_dialogs as dialogs

DELETE_NOTE = ("Note: deleting a file removes it from view but does not securely erase it from the drive. "
               "For real protection, keep sensitive data on an encrypted drive.")


def open_folder(path: str) -> None:
    """Show a folder in the system file manager."""
    try:
        if sys.platform.startswith("win"):
            import os
            os.startfile(path)                                     # type: ignore[attr-defined]
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path], check=False)
    except Exception as exc:
        dialogs.showerror("Couldn't open folder", f"{path}\n\n{exc}")


class QueueWriter:
    """File-like object that forwards each complete line written to it to a
    callback. Used to capture print() output from the pipeline into the log box
    while a job runs (print() is how the pipeline reports what it is doing)."""

    def __init__(self, line_callback):
        self.line_callback, self._buf = line_callback, ""

    def write(self, text: str) -> None:
        self._buf += text or ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.line_callback(line)

    def flush(self) -> None:
        if self._buf:                                     # emit a last line that had no newline
            self.line_callback(self._buf)
            self._buf = ""


def sort_tree(tree, col: str, reverse: bool) -> None:
    """Sort a table by a column; clicking the header again reverses the order."""
    items = [(tree.set(k, col), k) for k in tree.get_children("")]
    try:
        items.sort(key=lambda t: float(t[0]), reverse=reverse)
    except ValueError:
        items.sort(key=lambda t: t[0].lower(), reverse=reverse)
    for index, (_, k) in enumerate(items):
        tree.move(k, "", index)
    tree.heading(col, command=lambda: sort_tree(tree, col, not reverse))
