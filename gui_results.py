"""
gui_results.py
--------------
The Results page: what Vestige found, and the only place files get exported.

Two tables share the page (switch with the control at the top):
  * Sign-ups                - one row per service AND address you used
  * Possible email changes  - clues that an account's address was changed

EXPORT. Nothing is written until you press "Export now". There is no default
folder: you choose where the files go (for example a drive you trust). Existing
files are never overwritten. See module_d_export.py for the rules.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

import module_d_export as export
import paths
import ui_dialogs as dialogs
import ui_help as help_ui
import ui_theme as theme
from ui_common import open_folder, sort_tree

SIGNUP_HEADINGS = {"service": "Service Name", "email": "Email Address Used", "date": "Sign-up Date",
                   "category": "Category", "method": "Sign-up Method", "confidence": "Confidence",
                   "domain": "Source Domain(s)"}
SIGNUP_WIDTHS = {"service": 150, "email": 200, "date": 100, "category": 140, "method": 130,
                 "confidence": 90, "domain": 160}
CHANGE_HEADINGS = {"service": "Service", "date": "Notice date", "sent_to": "Notice sent to",
                   "previous": "Previous address", "now": "Account may now use", "match": "Match",
                   "confidence": "Confidence"}
CHANGE_WIDTHS = {"service": 130, "date": 100, "sent_to": 170, "previous": 170, "now": 200, "match": 140,
                 "confidence": 90}
VIEWS = ["Sign-ups", "Possible email changes"]


class ResultsPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._signups: list = []
        self._changes: list = []
        self.controls: list = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)         # the tables take any spare height

        self._build_top()
        self._build_tables()
        self._build_export()

    # ---- building ---------------------------------------------------------------------------
    def _build_top(self) -> None:
        top = theme.frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        theme.label(top, "Minimum confidence:").pack(side="left")
        self.minconf_var = tk.StringVar(value="0.5")
        theme.entry(top, 64, self.minconf_var).pack(side="left", padx=(8, 12))
        theme.button(top, "Refresh", self.refresh, width=100).pack(side="left")
        self.count_var = tk.StringVar()
        theme.label(top, textvariable=self.count_var, small=True, muted=True).pack(side="left", padx=16)
        help_ui.button(top, "results").pack(side="left")

        switch = theme.frame(self)
        switch.grid(row=1, column=0, sticky="w", padx=20, pady=(2, 8))
        self.view_switch = theme.segmented(switch, VIEWS, self._show_view, height=32)
        self.view_switch.pack()
        self.view_switch.set(VIEWS[0])

    def _build_tables(self) -> None:
        host = theme.frame(self)
        host.grid(row=2, column=0, sticky="nsew", padx=20)
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)

        self.signup_view, self.signup_tree = theme.table(host, tuple(SIGNUP_HEADINGS), SIGNUP_HEADINGS,
                                                         SIGNUP_WIDTHS, height=9)
        for col in SIGNUP_HEADINGS:
            self.signup_tree.heading(col, command=lambda c=col: sort_tree(self.signup_tree, c, False))
        self.signup_tree.bind("<Double-1>", lambda _e: self._detail(self.signup_tree, self._signups,
                                                                    export.SIGNUP_COLUMNS))
        self.signup_view.grid(row=0, column=0, sticky="nsew")

        self.change_view = theme.frame(host)
        self.change_view.grid_columnconfigure(0, weight=1)
        self.change_view.grid_rowconfigure(1, weight=1)
        self.change_note = theme.paragraph(
            self.change_view,
            "POSSIBLE email changes, based on notices that an account's email address was changed. They are clues, "
            "not proof: a masked address (like j***@gmail.com) is only matched against addresses found in the mail "
            "you scanned. Double-click a row for the full explanation.", small=True, muted=True, wraplength=820)
        self.change_note.grid(row=0, column=0, sticky="w", pady=(0, 6))
        container, self.change_tree = theme.table(self.change_view, tuple(CHANGE_HEADINGS), CHANGE_HEADINGS,
                                                  CHANGE_WIDTHS, height=8)
        container.grid(row=1, column=0, sticky="nsew")
        for col in CHANGE_HEADINGS:
            self.change_tree.heading(col, command=lambda c=col: sort_tree(self.change_tree, c, False))
        self.change_tree.bind("<Double-1>", lambda _e: self._detail(self.change_tree, self._changes,
                                                                    export.CHANGE_COLUMNS))
        self.change_view.grid(row=0, column=0, sticky="nsew")
        self.change_view.grid_remove()                # hidden until selected
        host.bind("<Configure>", lambda e: self.change_note.configure(wraplength=max(300, e.width - 20)))

    def _build_export(self) -> None:
        box = theme.frame(self)
        box.grid(row=3, column=0, sticky="ew", padx=20, pady=(12, 14))
        box.grid_columnconfigure(1, weight=1)
        help_ui.heading(box, "Export (nothing is written until you click Export now)", "results").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        theme.label(box, "Save into folder:", anchor="e").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        self.folder_var = tk.StringVar()
        theme.entry(box, 400, self.folder_var).grid(row=1, column=1, sticky="ew")
        browse = theme.button(box, "Browse...", self._browse, width=110)
        browse.grid(row=1, column=2, padx=(10, 0))

        theme.label(box, "File name:", anchor="e").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
        self.name_var = tk.StringVar(value="signups")
        theme.entry(box, 200, self.name_var).grid(row=2, column=1, sticky="w")

        options = theme.frame(box)
        options.grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        self.csv_var, self.md_var = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        self.stamp_var, self.sub_var = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        theme.checkbox(options, "CSV", self.csv_var, width=70).pack(side="left", padx=(0, 14))
        theme.checkbox(options, "Markdown", self.md_var, width=100).pack(side="left", padx=(0, 14))
        theme.checkbox(options, "Add date & time to file names", self.stamp_var, width=290).pack(side="left", padx=(0, 14))
        theme.checkbox(options, "Put files in a new dated sub-folder", self.sub_var, width=330).pack(side="left")

        self.export_button = theme.button(box, "Export now", self._export, width=140)
        self.export_button.grid(row=4, column=1, sticky="w", pady=(10, 4))
        self.export_note = theme.paragraph(
            box, "Existing files are never overwritten. The files list every service and the addresses you used, in "
                 "plain text: save them somewhere you trust (an encrypted drive is best).",
            small=True, muted=True, wraplength=820)
        self.export_note.grid(row=5, column=0, columnspan=3, sticky="w")
        box.bind("<Configure>", lambda e: self.export_note.configure(wraplength=max(300, e.width - 20)))
        self.controls = [browse, self.export_button]

    # ---- showing results -----------------------------------------------------------------------
    def _show_view(self, value: str) -> None:
        if value == VIEWS[0]:
            self.change_view.grid_remove()
            self.signup_view.grid()
        else:
            self.signup_view.grid_remove()
            self.change_view.grid()

    def min_confidence(self) -> float:
        try:
            return float(self.minconf_var.get())
        except ValueError:
            return 0.5

    def refresh(self) -> None:
        if self.app.busy:                       # the job thread owns the database right now
            return
        conn = self.app.session.conn
        self._signups = export.build_signup_rows(conn, self.min_confidence())
        self._changes = export.build_change_rows(conn, self.min_confidence())
        for tree in (self.signup_tree, self.change_tree):
            for item in tree.get_children():
                tree.delete(item)
        for i, r in enumerate(self._signups):
            self.signup_tree.insert("", "end", iid=str(i), values=(
                r["Service Name"], r["Email Address Used"], r["Sign-up Confirmation Date"], r["Category"],
                r["Sign-up Method"], r["Confidence"], r["Source Domain(s)"]))
        for i, r in enumerate(self._changes):
            self.change_tree.insert("", "end", iid=str(i), values=(
                r["Service Name"], r["Notice Date"], r["Notice Sent To"], r["Previous Address"],
                r["Account May Now Use"], r["Match"], r["Confidence"]))
        self.count_var.set(f"{len(self._signups)} service/address rows   |   "
                           f"{len(self._changes)} possible email changes")

    def _detail(self, tree, rows: list, columns: list) -> None:
        sel = tree.selection()
        if not sel:
            return
        row = rows[int(sel[0])]
        dialogs.showinfo(row["Service Name"], "\n\n".join(f"{c}:\n{row[c]}" for c in columns))

    # ---- export ----------------------------------------------------------------------------------
    def set_busy_state(self, busy: bool) -> None:
        for c in self.controls:
            c.configure(state="disabled" if busy else "normal")

    def _browse(self) -> str:
        folder = filedialog.askdirectory(title="Choose a folder for the exported files (e.g. your secure drive)")
        if folder:
            self.folder_var.set(folder)
        return folder

    def _export(self) -> None:
        if self.app.busy:
            dialogs.showwarning("Busy", "Wait for the running job to finish first.")
            return
        folder = self.folder_var.get().strip() or self._browse()
        if not folder:
            return
        service = paths.cloud_sync_warning(folder)
        if service and not dialogs.askyesno(
                "Cloud-synced folder?", f"This folder looks like it is inside {service}, so the exported files "
                                        f"may be uploaded to the cloud automatically.\n\nExport here anyway?"):
            return
        formats = tuple(f for f, v in (("csv", self.csv_var), ("md", self.md_var)) if v.get())
        options = export.ExportOptions(folder=folder, base_name=self.name_var.get(), formats=formats,
                                       add_timestamp=self.stamp_var.get(), use_subfolder=self.sub_var.get(),
                                       min_confidence=self.min_confidence())
        conn = self.app.session.conn
        self.app.set_busy(True)

        def work():
            try:
                outcome = (export.export_files(conn, options), None)
            except export.ExportError as exc:
                outcome = (None, str(exc))
            except Exception as exc:
                outcome = (None, f"Unexpected problem while exporting: {exc}")
            self.app.gui_queue.put(lambda: self._export_done(*outcome))
        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, result, error) -> None:
        self.app.set_busy(False)
        if error:
            dialogs.showerror("Export failed", error)
            return
        self.app.mark_exported()
        names = "\n".join(os.path.basename(f) for f in result.files)
        if dialogs.askyesno("Exported", f"Saved {len(result.files)} file(s) in:\n{result.folder}\n\n{names}\n\n"
                                        f"Open the folder?"):
            open_folder(result.folder)
