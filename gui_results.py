"""
gui_results.py
--------------
The Results page: what Vestige found, and the only place files get exported.

Four tables share the page (switch with the control at the top):
  * Sign-ups                - one row per service AND address you used. Two extra columns carry
                              notes: "Account check" (a CONFIRMED closure notice) and "Activity
                              hint" (a weak "no mail since ..." clue that never says "closed").
  * Possible email changes  - clues that an account's address was changed
  * Closed accounts         - confirmed closure notices, each with a later-mail check
  * Subscriptions           - recurring charges, whether they still look active, and totals

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

# What each table shows, as (column in the export, heading, width). Every other export column is shown in the
# panel under the table when a row is clicked, so nothing is lost by keeping the tables narrow.
SIGNUP_TABLE = (("Service Name", "Service Name", 150), ("Email Address Used", "Email Address Used", 200),
                ("Sign-up Confirmation Date", "Sign-up Date", 100), ("Account Check", "Account check", 200),
                ("Activity Hint", "Activity hint", 170), ("Category", "Category", 130),
                ("Confidence", "Confidence", 90))
CHANGE_TABLE = (("Service Name", "Service", 130), ("Notice Date", "Notice date", 100),
                ("Notice Sent To", "Notice sent to", 170), ("Previous Address", "Previous address", 170),
                ("Account May Now Use", "Account may now use", 200), ("Match", "Match", 140),
                ("Confidence", "Confidence", 90))
CLOSED_TABLE = (("Service Name", "Service", 150), ("Email Address Used", "Email Address Used", 200),
                ("Notice Date", "Notice date", 100), ("Type", "Type", 130), ("Check Result", "Check result", 150),
                ("Later Mail", "Later mail", 200), ("Confidence", "Confidence", 90))
SUB_TABLE = (("Service Name", "Service", 150), ("Email Address Used", "Email Address Used", 270),
             ("Status", "Status", 130), ("Last Charge Amount", "Amount", 100), ("Billing Cycle", "Cycle", 130),
             ("Last Charge Date", "Last charge", 100), ("Cost Per Month", "Per month", 100),
             ("Confidence", "Confidence", 90))
# Export columns holding several values joined with "; ": the panel puts each on its own line.
LIST_COLUMNS = ("Originally Signed Up With", "Charges By Address")
DETAIL_HINT = "Click a row to see its details here."
DETAIL_HEIGHT = 150
VIEWS = ["Sign-ups", "Possible email changes", "Closed accounts", "Subscriptions"]

# Row colours: words always carry the meaning, the colour only helps the eye.
_STATE_TAGS = {"Active": "good", "Possibly stopped": "warn", "Cancelled": "dim"}


def _table_args(spec: tuple) -> tuple:
    """(column ids, headings, widths) in the form theme.table wants. The ids are just positions."""
    ids = tuple(f"c{i}" for i in range(len(spec)))
    return ids, {i: heading for i, (_, heading, _) in zip(ids, spec)}, {i: width for i, (_, _, width) in zip(ids, spec)}


class ResultsPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._signups: list = []
        self._changes: list = []
        self._closed: list = []
        self._subs: list = []
        self.controls: list = []
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=2)         # the tables and the detail panel share any spare height, 2 : 1
        self.grid_rowconfigure(3, weight=1)         # (and give it up in the same ratio when the window is small)

        self._build_top()
        self._build_tables()
        self._build_detail()
        self._build_export()

    # ---- building ---------------------------------------------------------------------------
    def _build_top(self) -> None:
        top = theme.frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        theme.label(top, "Minimum AI confidence:").pack(side="left")
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
        self.host = host
        self.notes: list = []                     # the explanatory paragraphs, re-wrapped when the window resizes
        self.totals_var = tk.StringVar()          # the subscription totals line

        # Sign-ups: just the table.
        ids, headings, widths = _table_args(SIGNUP_TABLE)
        self.signup_view, self.signup_tree = theme.table(host, ids, headings, widths, height=9)
        self._wire(self.signup_tree, SIGNUP_TABLE, self._signups_rows, export.SIGNUP_COLUMNS)
        self.signup_view.grid(row=0, column=0, sticky="nsew")

        # The other three: a short note above the table.
        self.change_view, self.change_tree = self._noted_table(
            "POSSIBLE email changes, based on notices that an account's email address was changed. They are clues, "
            "not proof: a masked address (like j***@gmail.com) is only matched against addresses found in the mail "
            "you scanned. Click a row to read the full explanation.",
            CHANGE_TABLE, self._changes_rows, export.CHANGE_COLUMNS)
        self.closed_view, self.closed_tree = self._noted_table(
            "POSSIBLY closed accounts, each based on an email that says the account was closed, deactivated or "
            "deleted. \u201cLikely closed\u201d: nothing has arrived from that sender since. \u201cPossibly closed\u201d: "
            "it has emailed again, which may be marketing or a reopened account. The email address is the one the "
            "notice was sent to. Click a row for details.",
            CLOSED_TABLE, self._closed_rows, export.CLOSED_COLUMNS)
        self.sub_view, self.sub_tree = self._noted_table(
            "Subscriptions and whether they still seem to be running. The status is a best guess from the emails "
            "Vestige could see: a deleted receipt looks like a missed payment. A sender is one row, so if several of "
            "your addresses got its billing emails they are all listed. Click a row for the reason and the charges "
            "for each address.",
            SUB_TABLE, self._subs_rows, export.SUBSCRIPTION_COLUMNS, totals=True)
        for tag in ("good", "warn", "dim"):
            self.sub_tree.tag_configure(tag, foreground=theme.ROW_COLORS[tag])
        self.signup_tree.tag_configure("flag", foreground=theme.ROW_COLORS["warn"])

        self.views = {VIEWS[0]: self.signup_view, VIEWS[1]: self.change_view,
                      VIEWS[2]: self.closed_view, VIEWS[3]: self.sub_view}
        for name, view in self.views.items():
            view.grid(row=0, column=0, sticky="nsew")
            if name != VIEWS[0]:
                view.grid_remove()                # hidden until selected
        host.bind("<Configure>", lambda e: [n.configure(wraplength=max(300, e.width - 20)) for n in self.notes])

    def _noted_table(self, note: str, spec: tuple, rows_of, columns: list, totals: bool = False):
        """A view made of an explanatory note, a table, and (optionally) a totals line."""
        view = theme.frame(self.host)
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)
        paragraph = theme.paragraph(view, note, small=True, muted=True, wraplength=820)
        paragraph.grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.notes.append(paragraph)
        ids, headings, widths = _table_args(spec)
        container, tree = theme.table(view, ids, headings, widths, height=8)
        container.grid(row=1, column=0, sticky="nsew")
        self._wire(tree, spec, rows_of, columns)
        if totals:
            line = theme.paragraph(view, textvariable=self.totals_var, wraplength=820)
            line.grid(row=2, column=0, sticky="w", pady=(8, 0))
            self.notes.append(line)
        return view, tree

    def _wire(self, tree, spec: tuple, rows_of, columns: list) -> None:
        """Click a heading to sort; click a row to see the rest of its details under the table."""
        for i in range(len(spec)):
            tree.heading(f"c{i}", command=lambda c=f"c{i}", t=tree: sort_tree(t, c, False))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail(tree, rows_of(), spec, columns))

    def _signups_rows(self) -> list:
        return self._signups

    def _changes_rows(self) -> list:
        return self._changes

    def _closed_rows(self) -> list:
        return self._closed

    def _subs_rows(self) -> list:
        return self._subs

    def _build_detail(self) -> None:
        self.detail_box = theme.textbox(self, height=DETAIL_HEIGHT)
        self.detail_box.grid(row=3, column=0, sticky="nsew", padx=20, pady=(8, 0))
        self._set_detail(DETAIL_HINT)

    def _build_export(self) -> None:
        box = theme.frame(self)
        box.grid(row=4, column=0, sticky="ew", padx=20, pady=(12, 14))
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
        for name, view in self.views.items():
            if name == value:
                view.grid()
            else:
                view.grid_remove()
        self._set_detail(DETAIL_HINT)

    def min_confidence(self) -> float:
        try:
            return float(self.minconf_var.get())
        except ValueError:
            return 0.5

    def refresh(self) -> None:
        if self.app.busy:                       # the job thread owns the database right now
            return
        conn = self.app.session.conn
        confidence = self.min_confidence()
        self._signups = export.build_signup_rows(conn, confidence)
        self._changes = export.build_change_rows(conn, confidence)
        self._closed = export.build_closed_rows(conn, confidence)
        self._subs, _records, totals_line = export.subscription_table(conn, confidence)
        self._fill(self.signup_tree, SIGNUP_TABLE, self._signups, lambda r: "flag" if r["Account Check"] else None)
        self._fill(self.change_tree, CHANGE_TABLE, self._changes)
        self._fill(self.closed_tree, CLOSED_TABLE, self._closed)
        self._fill(self.sub_tree, SUB_TABLE, self._subs, lambda r: _STATE_TAGS.get(r["Status"]))
        self._set_detail(DETAIL_HINT)
        self.totals_var.set(totals_line or "No subscriptions found yet.")
        self.count_var.set(f"{len(self._signups)} sign-ups   |   {len(self._changes)} email changes   |   "
                           f"{len(self._closed)} closed   |   {len(self._subs)} subscriptions")

    @staticmethod
    def _fill(tree, spec: tuple, rows: list, tag_of=None) -> None:
        """Replace a table's rows. A row's id is its position in `rows`, which stays true after sorting."""
        for item in tree.get_children():
            tree.delete(item)
        for i, row in enumerate(rows):
            tag = tag_of(row) if tag_of else None
            tree.insert("", "end", iid=str(i), tags=(tag,) if tag else (),
                        values=tuple(row[column] for column, _, _ in spec))

    def _show_detail(self, tree, rows: list, spec: tuple, columns: list) -> None:
        """Show under the table everything the export holds for the selected row that the table doesn't."""
        selected = tree.selection()
        if not selected:
            return
        row = rows[int(selected[0])]
        shown = {column for column, _, _ in spec}
        lines = [row["Service Name"]]
        for column in columns:
            if column in shown:
                continue
            value = row[column]
            if column in LIST_COLUMNS and "; " in value:
                value = "\n    " + value.replace("; ", "\n    ")
            lines.append(f"{column}: {value}")
        self._set_detail("\n".join(lines))

    def _set_detail(self, text: str) -> None:
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("1.0", text)
        self.detail_box.configure(state="disabled")

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
        self.app.set_busy(True, stoppable=False)          # an export can't be interrupted: keep Stop greyed out

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
