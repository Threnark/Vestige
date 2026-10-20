"""
gui_run.py
----------
The Run Pipeline page. Everything that was on this page before is still here
(options, which accounts to fetch, the five stage buttons, Run full pipeline,
Stop, the status line and the live log), now in the Vestige look, with one new
thing: a PROGRESS BAR.

THE PROGRESS BAR
  Fed by the Progress tracker in jobcontrol.py. It shows:
    * the step ("Step 3 of 5: Fetching email bodies") and an overall percentage;
    * a moving "working..." marquee while a stage doesn't know its total yet
      (for example while an MBOX file is being indexed), switching to a real
      percentage as soon as it does;
    * a detail line such as "412 of 1,380 emails".
  It is a general guide, not an exact clock: stage weights are estimates and a
  mail server only reveals how many messages a folder holds when it is searched.

NO WIDGET IS EVER TOUCHED FROM A BACKGROUND THREAD. Each button handler reads
every value it needs from the widgets first, then hands a plain function to
App.run_job, which runs it in a thread and feeds log lines and progress snapshots
back through a queue (see gui_app.py).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

import ai_providers as ai
import db
import ui_dialogs as dialogs
import ui_help as help_ui
import ui_theme as theme
from models import DEFAULT_BATCH_CLOUD, DEFAULT_BATCH_LOCAL, ProviderSettings
import pipeline


class ImportDialog(ctk.CTkToplevel):
    """Small modal for importing exported mail: pick which account the mail belongs to,
    then choose what to import. Sets self.result to (account_name, [paths]) or leaves it
    None if cancelled. The choice of file/folder happens here so one dialog does it all."""

    FILE_TYPES = [("Mail exports (.pst .mbox .eml)", ("*.pst", "*.mbox", "*.mbx", "*.eml")),
                  ("Outlook data file (.pst)", "*.pst"), ("MBOX file", ("*.mbox", "*.mbx")),
                  ("Email messages (.eml)", "*.eml"), ("All files", "*.*")]

    def __init__(self, parent, accounts: list):
        super().__init__(parent, fg_color=theme.BG)
        self.accounts = accounts                  # Account objects (the [?] button needs to know if one is Microsoft)
        names = [a.name for a in accounts]
        self.title("Import mail from a file")
        self.resizable(False, False)
        self.result = None
        theme.paragraph(self, "Which account is this mail from?\n(Used only to work out which of your "
                              "addresses each email was sent to.)", wraplength=420).pack(padx=24, pady=(20, 8), anchor="w")
        self.var = tk.StringVar(value=names[0])
        theme.option_menu(self, names, self.var, width=260).pack(padx=24, anchor="w")
        theme.paragraph(self, "What do you have?\n"
                              "\u2022 Outlook / Hotmail / Live: the .pst file exported from Outlook\n"
                              "\u2022 Gmail Takeout or Thunderbird: a .mbox file\n"
                              "\u2022 Individual emails: .eml files, or a folder of them",
                        small=True, muted=True, wraplength=420).pack(padx=24, pady=(14, 4), anchor="w")
        row = theme.frame(self)
        row.pack(padx=24, pady=(10, 4), anchor="w")
        theme.button(row, "Choose file(s)...", self._pick_files, width=200).pack(side="left", padx=(0, 10))
        theme.button(row, "Choose a folder of .eml...", self._pick_folder, width=200).pack(side="left")
        bottom = theme.frame(self)
        bottom.pack(padx=24, pady=(10, 20), anchor="e")
        help_ui.button(bottom, self._help_topic).pack(side="left", padx=(0, 12))
        theme.button(bottom, "Cancel", self.destroy, width=90).pack(side="left")
        self.after(250, lambda: theme.apply_icon(self))
        self.update_idletasks()
        self.transient(parent.winfo_toplevel())
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window()

    def _help_topic(self) -> str:
        """The [?] here explains importing: the Outlook/Hotmail page if the chosen account is a Microsoft
        one (that is where the .pst export steps are), otherwise the Run Pipeline page."""
        chosen = next((a for a in self.accounts if a.name == self.var.get()), None)
        return "microsoft" if chosen is not None and chosen.is_microsoft else "pipeline"

    def _finish(self, paths) -> None:
        if paths:
            self.result = (self.var.get(), [str(p) for p in paths])
            self.destroy()

    def _pick_files(self) -> None:
        self._finish(filedialog.askopenfilenames(parent=self, title="Select the mail file(s)",
                                                 filetypes=self.FILE_TYPES))

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, title="Select the folder that contains the .eml files")
        self._finish([folder] if folder else [])


class RunPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.stage_buttons: list = []
        self._acct_vars: dict = {}
        self._marquee = False                      # True while the bar shows the "working..." animation
        self._last_snapshot: dict = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)        # the log takes any spare height

        self._build_options()
        self._build_accounts()
        self._build_stages()
        self._build_progress()
        self._build_status()
        self._build_log()

    # ---- building the page ------------------------------------------------------------------
    def _build_options(self) -> None:
        box = theme.frame(self)
        box.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        help_ui.heading(box, "Options", "pipeline").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        def label(r: int, c: int, text: str) -> None:
            theme.label(box, text, anchor="e").grid(row=r, column=c, sticky="e", padx=(0, 8), pady=4)

        label(1, 0, "Only mail since (optional, e.g. 01-Jan-2024):")
        self.since_var = tk.StringVar()
        theme.entry(box, 130, self.since_var).grid(row=1, column=1, sticky="w", padx=(0, 24))
        label(1, 2, "Groups per AI request:")
        self.batch_var = tk.StringVar(value=str(DEFAULT_BATCH_CLOUD))
        theme.entry(box, 70, self.batch_var).grid(row=1, column=3, sticky="w")
        label(2, 0, "Limit AI to N groups (cheap test run, blank = all):")
        self.max_var = tk.StringVar()
        theme.entry(box, 130, self.max_var).grid(row=2, column=1, sticky="w", padx=(0, 24))
        self.fetch_var = tk.BooleanVar(value=True)
        theme.checkbox(box, "Full pipeline starts with an IMAP fetch", self.fetch_var, width=360).grid(
            row=2, column=2, columnspan=2, sticky="w")

    def _build_accounts(self) -> None:
        box = theme.frame(self)
        box.grid(row=1, column=0, sticky="ew", padx=20, pady=6)
        box.grid_columnconfigure(0, weight=1)
        help_ui.heading(box, "Accounts to fetch over IMAP (none ticked = all with an app password)", "pipeline").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.acct_area = theme.scroll_area(box, height=70)
        self.acct_area.grid(row=1, column=0, sticky="ew")
        theme.button(box, "Select all", self._select_all, width=110).grid(row=1, column=1, sticky="n", padx=(12, 0))
        self.refresh_accounts()

    def _build_stages(self) -> None:
        box = theme.frame(self)
        box.grid(row=2, column=0, sticky="ew", padx=20, pady=6)
        help_ui.heading(box, "Pipeline stages (nothing is exported or saved unless you choose to)", "pipeline").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        spec = [
            ("1. Fetch headers (IMAP)", self._do_fetch), ("1. Import mail file...", self._do_import_mail),
            ("2. Pre-filter (free)", self._do_prefilter), ("3. Fetch email bodies", self._do_bodies),
            ("4. AI: classify sign-ups", self._do_classify), ("5. AI: find email changes", self._do_changes),
            ("\u25b6  Run full pipeline (1-5)", self._do_all),
        ]
        for i, (text, command) in enumerate(spec):
            b = theme.button(box, text, command, width=200)
            b.grid(row=1 + i // 4, column=i % 4, padx=(0, 10), pady=(0, 8), sticky="w")
            self.stage_buttons.append(b)
        self.stop_button = theme.button(box, "\u25a0  Stop", self.app.request_stop, width=200)
        self.stop_button.grid(row=2, column=3, padx=(0, 10), pady=(0, 8), sticky="w")
        self.stop_button.configure(state="disabled")

    def _build_progress(self) -> None:
        box = theme.frame(self)
        box.grid(row=3, column=0, sticky="ew", padx=20, pady=(4, 6))
        box.grid_columnconfigure(0, weight=1)
        help_ui.heading(box, "Progress", "pipeline").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.progress_title_var = tk.StringVar(value="Ready. Nothing is running.")
        self.progress_percent_var = tk.StringVar(value="")
        self.progress_detail_var = tk.StringVar(value="")
        theme.label(box, textvariable=self.progress_title_var).grid(row=1, column=0, sticky="w")
        theme.label(box, textvariable=self.progress_percent_var, anchor="e").grid(row=1, column=1, sticky="e")
        self.bar = theme.progressbar(box)
        self.bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        self.bar.set(0)
        theme.label(box, textvariable=self.progress_detail_var, small=True, muted=True).grid(
            row=3, column=0, columnspan=2, sticky="w")

    def _build_status(self) -> None:
        row = theme.frame(self)
        row.grid(row=4, column=0, sticky="ew", padx=20, pady=(4, 0))
        row.grid_columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="")
        self.status_label = theme.paragraph(row, textvariable=self.status_var, small=True, wraplength=700)
        self.status_label.grid(row=0, column=0, sticky="w")
        theme.button(row, "Refresh status", self.refresh_status, width=130).grid(row=0, column=1, sticky="e")
        row.bind("<Configure>", lambda e: self.status_label.configure(wraplength=max(300, e.width - 170)))

    def _build_log(self) -> None:
        box = theme.frame(self)
        box.grid(row=5, column=0, sticky="nsew", padx=20, pady=(8, 14))
        box.grid_columnconfigure(0, weight=1)
        box.grid_rowconfigure(1, weight=1)
        help_ui.heading(box, "Log (kept in memory only)", "pipeline").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.log_text = theme.textbox(box, height=150)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

    # ---- small helpers ----------------------------------------------------------------------------
    def set_default_batch(self, local: bool) -> None:
        self.batch_var.set(str(DEFAULT_BATCH_LOCAL if local else DEFAULT_BATCH_CLOUD))

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_busy_state(self, busy: bool) -> None:
        for b in self.stage_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def refresh_accounts(self) -> None:
        """Rebuild the tick-box list, keeping any ticks the user already made.
        (We destroy only the widgets WE created, never winfo_children(): a scrolling
        frame also holds internal widgets that must not be removed.)"""
        for widget in getattr(self, "_acct_widgets", []):
            widget.destroy()
        self._acct_widgets = []
        names = [a.name for a in self.app.session.accounts]
        self._acct_vars = {n: self._acct_vars.get(n) or tk.BooleanVar(value=False) for n in names}
        if not names:
            empty = theme.label(self.acct_area, "No accounts yet: add one in Setup.", muted=True, small=True)
            empty.pack(anchor="w")
            self._acct_widgets.append(empty)
        for n in names:
            box = theme.checkbox(self.acct_area, n, self._acct_vars[n], width=300)
            box.pack(anchor="w", pady=2)
            self._acct_widgets.append(box)

    def _select_all(self) -> None:
        for var in self._acct_vars.values():
            var.set(True)

    def _selected_accounts(self) -> list:
        accounts = list(self.app.session.accounts)            # a snapshot: safe to use from the job thread
        picked = {n for n, v in self._acct_vars.items() if v.get()}
        return [a for a in accounts if a.name in picked] if picked else accounts

    @staticmethod
    def _int(var, default=None):
        text = var.get().strip()
        return int(text) if text.isdigit() and int(text) > 0 else default

    def refresh_status(self) -> None:
        if self.app.busy:
            self.status_var.set("A job is running - the status will refresh when it finishes.")
            return
        c = db.counts(self.app.session.conn)
        self.status_var.set(
            f"Emails scanned: {c['total_emails']}   |   sign-up candidates: {c['candidates']}   |   "
            f"sender groups classified: {c['classified_groups']}   |   confirmed sign-ups: {c['confirmed_signups']}   |   "
            f"email-change notices: {c['change_notices']}   |   possible email changes: {c['email_changes_found']}")
        self.app.results_page.refresh()

    def _provider(self):
        """Build the AI provider from the Setup page (main thread only), or tell the
        user what's missing. Returns (provider, settings) or (None, None)."""
        settings = self.app.setup_page.get_provider_settings()
        try:
            return ai.create_provider(settings), settings
        except ai.ProviderError as exc:
            dialogs.showwarning("AI provider needs attention", str(exc))
            return None, None

    @staticmethod
    def _announce(settings: ProviderSettings) -> None:
        level, text = ai.privacy_summary(settings)
        print(f"[AI: {level}] {text}")

    # ---- progress bar ----------------------------------------------------------------------------------
    def reset_progress(self) -> None:
        self._stop_marquee()
        self.bar.set(0)
        self.progress_title_var.set("Starting...")
        self.progress_percent_var.set("")
        self.progress_detail_var.set("")
        self._last_snapshot = {}

    def _stop_marquee(self) -> None:
        if self._marquee:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self._marquee = False

    def show_progress(self, snap: dict) -> None:
        """Main thread only. `snap` comes from jobcontrol.Progress.snapshot()."""
        self._last_snapshot = snap
        if snap.get("steps", 0) > 1 and snap.get("step"):
            title = f"Step {snap['step']} of {snap['steps']}: {snap['label']}"
        else:
            title = snap.get("label") or "Working..."
        self.progress_title_var.set(title)
        self.progress_detail_var.set(snap.get("detail", ""))
        if snap.get("indeterminate"):
            if not self._marquee:
                self.bar.configure(mode="indeterminate")
                self.bar.start()
                self._marquee = True
            self.progress_percent_var.set("working...")
        else:
            self._stop_marquee()
            value = max(0.0, min(1.0, snap.get("overall", 0.0)))
            self.bar.set(value)
            self.progress_percent_var.set(f"{round(value * 100)}%")

    def end_progress(self, outcome: str) -> None:
        """Called when a job ends: outcome is 'finished', 'stopped' or 'problem'."""
        self._stop_marquee()
        value = 1.0 if outcome == "finished" else max(0.0, min(1.0, self._last_snapshot.get("overall", 0.0)))
        self.bar.set(value)
        percent = f"{round(value * 100)}%"
        self.progress_percent_var.set(percent)
        if outcome == "finished":
            self.progress_title_var.set("Finished")
        elif outcome == "stopped":
            self.progress_title_var.set(f"Stopped at {percent}. Everything finished so far is kept.")
        else:
            self.progress_title_var.set(f"Stopped at {percent} because of a problem. See the log below.")

    # ---- stage actions -------------------------------------------------------------------------------------
    # Each action reads every widget value it needs FIRST (main thread), then hands
    # a closure to App.run_job. The closure never touches a widget.
    def _do_fetch(self) -> None:
        accounts, since, conn = self._selected_accounts(), self.since_var.get().strip() or None, self.app.session.conn
        if not any(a.has_password for a in accounts):
            dialogs.showinfo("No app password", "Add an account with an app password first, or use "
                                                "'Import mail file' (no password needed).")
            return
        self.app.run_job(lambda control: pipeline.stage_fetch(conn, accounts, since, control))

    def _do_import_mail(self) -> None:
        accounts, conn = list(self.app.session.accounts), self.app.session.conn
        if not accounts:
            dialogs.showinfo("Add an account first", "Add an account in the Setup tab first. Its email "
                             "address is used to work out which address each email was sent to. "
                             "No password is needed to import a file.")
            return
        picked = ImportDialog(self, accounts).result
        if not picked:
            return
        name, paths = picked
        account = next(a for a in accounts if a.name == name)
        self.app.run_job(lambda control: pipeline.stage_import_mail(conn, account, paths, control))

    def _do_prefilter(self) -> None:
        conn = self.app.session.conn
        self.app.run_job(lambda control: pipeline.stage_prefilter(conn, control))

    def _do_bodies(self) -> None:
        accounts, conn = self._selected_accounts(), self.app.session.conn
        self.app.run_job(lambda control: pipeline.stage_fetch_bodies(conn, accounts, control))

    def _do_classify(self) -> None:
        provider, settings = self._provider()
        if not provider:
            return
        batch, limit, conn = self._int(self.batch_var, DEFAULT_BATCH_CLOUD), self._int(self.max_var), self.app.session.conn

        def job(control):
            self._announce(settings)
            pipeline.stage_classify(conn, provider, batch, limit, control)
        self.app.run_job(job)

    def _do_changes(self) -> None:
        provider, settings = self._provider()
        if not provider:
            return
        accounts, batch, conn = list(self.app.session.accounts), self._int(self.batch_var, 5), self.app.session.conn

        def job(control):
            self._announce(settings)
            pipeline.stage_detect_changes(conn, provider, accounts, max(1, min(batch, 5)), control)
        self.app.run_job(job)

    def _do_all(self) -> None:
        provider, settings = self._provider()
        if not provider:
            return
        accounts, conn = self._selected_accounts(), self.app.session.conn
        fetch = self.fetch_var.get()
        if fetch and not any(a.has_password for a in accounts):
            if not dialogs.askyesno("No IMAP accounts", "None of the accounts has an app password, so the IMAP "
                                    "fetch will be skipped. Continue with what's already loaded "
                                    "(for example an imported .pst or .mbox file)?"):
                return
        since = self.since_var.get().strip() or None
        batch, limit = self._int(self.batch_var, DEFAULT_BATCH_CLOUD), self._int(self.max_var)
        session = self.app.session

        def job(control):
            self._announce(settings)
            pipeline.run_full(conn, accounts, provider, fetch=fetch, since=since, batch_size=batch,
                              max_groups=limit, control=control)
            if session.write_mode:
                print(f"\nResults are saved in {session.db_path}. Export them from the Results tab if you "
                      f"want CSV/Markdown files.")
            else:
                print("\nResults are held in memory only (nothing has been saved). Go to the Results tab to "
                      "review them and export if you want to keep them.")
        self.app.run_job(job)
