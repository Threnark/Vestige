"""
gui_run.py
----------
The Run Pipeline page.

LAYOUT
  The page SCROLLS, because it holds more than fits on a small window. One part does not scroll: the
  progress card at the bottom holds the progress bar, "Run selected pipelines" and Stop, so you can
  always start, stop and watch a run, wherever you have scrolled to. Inside the scrolling part each
  section is a card, in the order you work through them:

    Fetch mail from   which accounts to fetch over IMAP, an optional date limit, and whether the full
    your accounts     run fetches new mail first (exported mail files are imported on the Setup page)
    What to run       tick the pipelines to include (Sign-ups, Email changes, Closed accounts,
                      Subscriptions) and choose the DEPTH (Quick / Balanced / Thorough)
    AI requests       how many senders go into one request, and the cheap test-run limit
    Individual steps  every step as its own button, for running one thing at a time
    Status            the counters, one row per pipeline, and a Refresh button
    Log               what is happening, resizable: drag the bar underneath it

WHAT THE TICK BOXES AND DEPTH DO
  Tick boxes choose which AI pipelines "Run selected pipelines" runs. An unticked pipeline costs
  nothing: its emails are not downloaded and nothing about them is sent to the AI. The free steps
  (scan mailbox, shortlist emails, download email text) are shared. Depth sets how much of each sender's mail
  the AI reads (see run_options.py). The individual AI buttons use the same depth.

THE PROGRESS BAR
  Fed by the Progress tracker in jobcontrol.py. It shows:
    * the step ("Step 3 of 5: Downloading email text") and an overall percentage;
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

import customtkinter as ctk

import ai_providers as ai
import db
import ui_dialogs as dialogs
import ui_help as help_ui
import ui_theme as theme
from models import DEFAULT_BATCH_CLOUD, DEFAULT_BATCH_LOCAL, ProviderSettings
from module_a_imap import parse_since
import pipeline
import run_options

LOG_MIN_HEIGHT = 90          # the log can be dragged between these heights (pixels)
LOG_MAX_HEIGHT = 700
LOG_START_HEIGHT = 150

# The Status counters: one row per pipeline, as (row title, ((key in db.counts(), label), ...)).
STATUS_ROWS = (
    ("Mail", (("total_emails", "Emails scanned"),)),
    ("Sign-ups", (("candidates", "Sign-up emails spotted"), ("classified_groups", "Senders checked by AI"),
                  ("confirmed_signups", "Confirmed sign-ups"))),
    ("Email changes", (("change_notices", "Change notices spotted"), ("email_changes_found", "Email changes found"))),
    ("Closed accounts", (("closure_notices", "Closure notices spotted"),
                         ("closed_accounts_found", "Closed accounts found"))),
    ("Subscriptions", (("billing_emails", "Billing emails spotted"), ("subscriptions_found", "Subscriptions found"))),
)


class RunPage(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.stage_buttons: list = []
        self.run_controls: list = []               # everything that is locked while a job runs
        self._acct_vars: dict = {}
        self._marquee = False                      # True while the bar shows the "working..." animation
        self._last_snapshot: dict = {}
        self._log_height = LOG_START_HEIGHT
        self._drag = None                          # (mouse y, log height) while the log bar is being dragged
        self._steps_shown = False                  # the individual steps start folded away
        self._remote_ai_approved: set = set()      # destinations (see ai.destination_of) the person has agreed to send text to
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)        # the scrolling body takes the spare height; progress stays put

        self.body = theme.page_scroll(self)
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.grid_columnconfigure(0, weight=1)

        self._build_imap()
        self._build_what_to_run()
        self._build_ai_requests()
        self._build_stages()
        self._build_status()
        self._build_log()
        self._build_progress()                     # pinned below the scrolling body

    # ---- building the page ------------------------------------------------------------------
    def _build_imap(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 0))
        box.grid_columnconfigure(0, weight=1)
        top = theme.frame(box)                     # the title, with the switch for the full run's fetch at its right
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        help_ui.heading(top, "Fetch mail from your accounts", "pipeline").grid(row=0, column=0, sticky="w")
        self.fetch_var = tk.BooleanVar(value=True)
        theme.checkbox(top, "Fetch new mail first", self.fetch_var, width=180).grid(
            row=0, column=1, sticky="e")
        theme.paragraph(box, "Tick the accounts to fetch; none ticked = every account with an app password. To use "
                             "exported mail files instead, import them on the Setup tab.", wraplength=760,
                        **theme.CARD_NOTE).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))
        # A plain frame, not a nested scrolling list: the whole page scrolls, and two scrolling
        # areas inside one another fight over the mouse wheel.
        self.acct_area = theme.frame(box)
        self.acct_area.grid(row=2, column=0, sticky="ew")
        theme.button(box, "Select all", self._select_all, width=110).grid(row=2, column=1, sticky="n", padx=(12, 0))
        self.refresh_accounts()

        options = theme.frame(box)
        options.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        theme.label(options, "Only mail since (optional, e.g. 01-Jan-2024 or 2024-01-31):").grid(
            row=0, column=0, sticky="w", padx=(0, 8))
        self.since_var = tk.StringVar()
        theme.entry(options, 130, self.since_var).grid(row=0, column=1, sticky="w")

    def _build_what_to_run(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=(10, 0))
        box.grid_columnconfigure(2, weight=1)
        help_ui.heading(box, "What to run", "pipeline").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ticks = theme.frame(box)
        ticks.grid(row=1, column=0, columnspan=3, sticky="w")
        self.pipeline_vars: dict = {}
        for i, key in enumerate(run_options.PIPELINE_ORDER):
            var = tk.BooleanVar(value=True)
            self.pipeline_vars[key] = var
            check = theme.checkbox(ticks, run_options.PIPELINE_LABELS[key], var, self._plan_changed, width=170)
            check.grid(row=0, column=i, padx=(0, 14), pady=2, sticky="w")
            self.run_controls.append(check)
        self.plan_var = tk.StringVar()
        theme.paragraph(box, textvariable=self.plan_var, wraplength=760, **theme.CARD_NOTE).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 10))

        theme.label(box, "Depth:").grid(row=3, column=0, sticky="w", padx=(0, 10))
        self.depth_control = theme.segmented(box, run_options.DEPTH_NAMES, self._depth_changed, height=30)
        self.depth_control.set(run_options.DEFAULT_DEPTH.name)
        self.depth_control.grid(row=3, column=1, sticky="w")
        self.run_controls.append(self.depth_control)
        self.depth_var = tk.StringVar()
        theme.paragraph(box, textvariable=self.depth_var, wraplength=400, **theme.CARD_NOTE).grid(
            row=3, column=2, sticky="w", padx=(14, 0))
        theme.paragraph(box, "\u201cRun selected pipelines\u201d runs the free steps first, then the ticked pipelines. "
                             "Nothing is exported or saved unless you choose to.", wraplength=760, **theme.CARD_NOTE).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._plan_changed()
        self._depth_changed(run_options.DEFAULT_DEPTH.name)

    def _build_ai_requests(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 0))
        help_ui.heading(box, "AI requests", "pipeline").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        theme.label(box, "Senders per AI request:").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.batch_var = tk.StringVar(value=str(DEFAULT_BATCH_CLOUD))
        theme.entry(box, 70, self.batch_var).grid(row=1, column=1, sticky="w", padx=(0, 24))
        theme.label(box, "Test run: check only N senders (blank = all):").grid(
            row=1, column=2, sticky="w", padx=(0, 8))
        self.max_var = tk.StringVar()
        theme.entry(box, 130, self.max_var).grid(row=1, column=3, sticky="w")

    def _build_stages(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=3, column=0, sticky="ew", padx=20, pady=(10, 0))
        box.grid_columnconfigure(1, weight=1)
        help_ui.heading(box, "Individual steps", "pipeline").grid(row=0, column=0, sticky="w")
        self.steps_toggle = theme.button(box, "Show steps", self._toggle_steps, width=120)
        self.steps_toggle.grid(row=0, column=1, sticky="e")
        theme.label(box, "For running one step at a time. Nothing is exported or saved unless you choose to.",
                    small=True, muted=True).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        # Most people only need "Run selected pipelines", so the buttons stay folded away until asked for.
        self.steps_area = theme.frame(box)
        self.steps_area.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.steps_area.grid_remove()
        steps = (      # the free steps, then the four AI pipelines
            (("1. Scan mailbox (IMAP)", self._do_fetch), ("2. Shortlist emails (free)", self._do_prefilter),
             ("3. Download email text", self._do_bodies)),
            (("4. AI: find sign-ups", self._do_classify), ("5. AI: find email changes", self._do_changes),
             ("6. AI: closed accounts", self._do_closed), ("7. AI: subscriptions", self._do_subscriptions)),
        )
        for row, group in enumerate(steps, start=2):
            for column, (text, command) in enumerate(group):
                button = theme.button(self.steps_area, text, command, width=184)
                button.grid(row=row, column=column, padx=(0, 8), pady=(0, 8 if row == 2 else 0), sticky="w")
                self.stage_buttons.append(button)

    def _build_status(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=4, column=0, sticky="ew", padx=20, pady=(10, 0))
        box.grid_columnconfigure(1, weight=1)
        help_ui.heading(box, "Status", "pipeline").grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="")                 # only ever says that a job is running
        theme.label(box, textvariable=self.status_var, small=True, muted=True).grid(
            row=0, column=1, sticky="w", padx=14)
        theme.button(box, "Refresh status", self.refresh_status, width=130).grid(row=0, column=2, sticky="e")
        counters = theme.frame(box)
        counters.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.status_counters: dict = {}                          # db.counts() key -> (its label widget, its text)
        for row, (title, fields) in enumerate(STATUS_ROWS):
            theme.label(counters, title, small=True, muted=True).grid(row=row, column=0, sticky="w", padx=(0, 16))
            for column, (key, text) in enumerate(fields, start=1):
                label = theme.label(counters, f"{text}: 0", small=True)
                label.grid(row=row, column=column, sticky="w", padx=(0, 16))
                self.status_counters[key] = (label, text)

    def _build_log(self) -> None:
        card, box = theme.card(self.body)
        card.grid(row=5, column=0, sticky="ew", padx=20, pady=(10, 14))
        box.grid_columnconfigure(1, weight=1)
        help_ui.heading(box, "Log", "pipeline").grid(row=0, column=0, sticky="w", pady=(0, 6))
        theme.label(box, "Kept in memory only.", small=True, muted=True).grid(row=0, column=1, sticky="w", padx=14)
        self.log_text = theme.textbox(box, height=self._log_height)
        self.log_text.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.log_text.configure(state="disabled")
        theme.contain_wheel(self.log_text)
        # The bar under the log: drag it up or down to make the log shorter or taller; double-click resets it.
        self.log_grip = theme.grip(box)
        self.log_grip.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.log_grip.bind("<ButtonPress-1>", self._grip_press)
        self.log_grip.bind("<B1-Motion>", self._grip_drag)
        self.log_grip.bind("<ButtonRelease-1>", self._grip_release)
        self.log_grip.bind("<Double-Button-1>", lambda _e: self.set_log_height(LOG_START_HEIGHT))

    def _build_progress(self) -> None:
        card, box = theme.card(self)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=(8, 12))
        box.grid_columnconfigure(0, weight=1)
        help_ui.heading(box, "Progress", "pipeline").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.run_button = theme.button(box, "\u25b6  Run selected pipelines", self._do_all, width=230, height=34)
        self.run_button.grid(row=0, column=1, rowspan=2, sticky="ne", padx=(12, 0))
        self.run_controls.append(self.run_button)
        self.stop_button = theme.button(box, "\u25a0  Stop", self.app.request_stop, width=110, height=34)
        self.stop_button.grid(row=0, column=2, rowspan=2, sticky="ne", padx=(8, 0))
        self.stop_button.configure(state="disabled")
        self.progress_title_var = tk.StringVar(value="Ready. Nothing is running.")
        self.progress_percent_var = tk.StringVar(value="")
        self.progress_detail_var = tk.StringVar(value="")
        theme.label(box, textvariable=self.progress_title_var).grid(row=1, column=0, sticky="w")
        self.bar = theme.progressbar(box)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 4))
        self.bar.set(0)
        theme.label(box, textvariable=self.progress_percent_var, small=True, muted=True).grid(
            row=3, column=2, sticky="e")
        theme.label(box, textvariable=self.progress_detail_var, small=True, muted=True).grid(
            row=3, column=0, sticky="w")

    # ---- what to run: tick boxes and depth -------------------------------------------------------
    def chosen_pipelines(self) -> tuple:
        """The ticked pipelines, in running order (empty if none are ticked)."""
        return tuple(k for k in run_options.PIPELINE_ORDER if self.pipeline_vars[k].get())

    def chosen_depth(self) -> str:
        return self.depth_control.get() or run_options.DEFAULT_DEPTH.name

    def _plan_changed(self) -> None:
        chosen = self.chosen_pipelines()
        left_out = [k for k in run_options.PIPELINE_ORDER if k not in chosen]
        if not chosen:
            self.plan_var.set("Nothing is ticked. Tick at least one pipeline to use \u201cRun selected pipelines\u201d.")
            return
        text = "The AI will be used for: " + ", ".join(run_options.PIPELINE_LABELS[k] for k in chosen) + "."
        if left_out:
            text += (" Skipped: " + ", ".join(run_options.PIPELINE_LABELS[k] for k in left_out)
                     + " (their emails are not downloaded or sent to the AI).")
        self.plan_var.set(text)

    def _depth_changed(self, value: str) -> None:
        self.depth_var.set(run_options.get_depth(value).blurb)

    # ---- the resizable log -----------------------------------------------------------------------
    def set_log_height(self, height) -> None:
        self._log_height = int(max(LOG_MIN_HEIGHT, min(LOG_MAX_HEIGHT, height)))
        self.log_text.configure(height=self._log_height)

    def _grip_press(self, event) -> None:
        self._drag = (event.y_root, self._log_height)

    def _grip_drag(self, event) -> None:
        if self._drag is None:
            return
        start_y, start_height = self._drag
        scale = max(0.5, theme.F.scale)                       # mouse movement is in screen pixels
        self.set_log_height(start_height + (event.y_root - start_y) / scale)

    def _grip_release(self, _event) -> None:
        self._drag = None

    # ---- small helpers ----------------------------------------------------------------------------
    def set_default_batch(self, local: bool) -> None:
        self.batch_var.set(str(DEFAULT_BATCH_LOCAL if local else DEFAULT_BATCH_CLOUD))

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_busy_state(self, busy: bool, stoppable: bool = True) -> None:
        state = "normal"
        if busy:
            state = "disabled"
        for b in self.stage_buttons + self.run_controls:
            b.configure(state=state)
        self.stop_button.configure(state="normal" if (busy and stoppable) else "disabled")

    def refresh_accounts(self) -> None:
        """Rebuild the tick-box list, keeping any ticks the user already made.
        (We destroy only the widgets WE created, never winfo_children().)"""
        for widget in getattr(self, "_acct_widgets", []):
            widget.destroy()
        self._acct_widgets = []
        names = [a.name for a in self.app.session.accounts]
        self._acct_vars = {n: self._acct_vars.get(n) or tk.BooleanVar(value=False) for n in names}
        if not names:
            empty = theme.label(self.acct_area, "No accounts yet: add one in Setup.", muted=True, small=True)
            empty.grid(row=0, column=0, sticky="w")
            self._acct_widgets.append(empty)
        for i, n in enumerate(names):                          # three to a row, so many accounts stay short
            box = theme.checkbox(self.acct_area, n, self._acct_vars[n], width=250)
            box.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 10), pady=2)
            self._acct_widgets.append(box)

    def _select_all(self) -> None:
        for var in self._acct_vars.values():
            var.set(True)

    def _selected_accounts(self) -> list:
        accounts = list(self.app.session.accounts)            # a snapshot: safe to use from the job thread
        picked = {n for n, v in self._acct_vars.items() if v.get()}
        return [a for a in accounts if a.name in picked] if picked else accounts

    def _read_since(self) -> tuple:
        """The "Only mail since" date, checked and tidied: (True, '01-Jan-2024'), or (True, None) when the
        box is empty. A date the mail server could not read is refused HERE, with what to write, instead of
        failing after a connection and a wait. Returns (False, None) after telling the person."""
        try:
            since = parse_since(self.since_var.get())
        except ValueError as exc:
            dialogs.showwarning("Check the date", str(exc))
            return False, None
        if since:
            self.since_var.set(since)             # show it the way the mail server will read it
        return True, since

    @staticmethod
    def _int(var, default=None):
        text = var.get().strip()
        return int(text) if text.isdigit() and int(text) > 0 else default

    def refresh_status(self) -> None:
        if self.app.busy:
            self.status_var.set("A job is running - the status will refresh when it finishes.")
            return
        self.status_var.set("")
        counts = db.counts(self.app.session.conn)
        for key, (label, text) in self.status_counters.items():
            label.configure(text=f"{text}: {counts[key]}")
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

    def _toggle_steps(self) -> None:
        self._steps_shown = not self._steps_shown
        if self._steps_shown:
            self.steps_area.grid()
        else:
            self.steps_area.grid_remove()
        self.steps_toggle.configure(text="Hide steps" if self._steps_shown else "Show steps")

    def _confirm_remote_ai(self, settings: ProviderSettings, batch: int, limit) -> bool:
        """Before the first AI run of a session, say where email text is about to go and what the settings are,
        unless it stays on this computer. Agreeing covers the rest of the session FOR THAT DESTINATION: pointing
        Vestige at a different server asks again, because the answer was about the old one. A paid cloud AI also
        gets a hint about the test-run limit, because the request count is only known after the free steps."""
        level, text = ai.privacy_summary(settings)
        destination = ai.destination_of(settings)
        if level == "local" or destination in self._remote_ai_approved:
            return True
        lines = [text, f"Depth: {self.chosen_depth()}.  Senders per request: {batch}.  "
                       f"Test-run limit: {f'{limit} senders' if limit else 'none'}."]
        if level == "cloud":
            lines.append("A cloud AI usually charges for every request. How many there will be is worked out after "
                         "the free steps and shown in the log. For a first look, type a small number into "
                         "\u201cTest run: check only N senders\u201d. You can stop at any time and finished work is kept.")
        if dialogs.ask_choice("Send email text to the AI?", "\n\n".join(lines), ["Run", "Cancel"]) != "Run":
            return False
        self._remote_ai_approved.add(destination)
        return True

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
        accounts, conn = self._selected_accounts(), self.app.session.conn
        if not any(a.has_password for a in accounts):
            dialogs.showinfo("No app password", "Add an account with an app password first, or import a mail "
                                                "file on the Setup tab (no password needed).")
            return
        ok, since = self._read_since()
        if not ok:
            return
        self.app.run_job(lambda control: pipeline.stage_fetch(conn, accounts, since, control))

    def _do_prefilter(self) -> None:
        conn = self.app.session.conn
        self.app.run_job(lambda control: pipeline.stage_prefilter(conn, control))

    def _do_bodies(self) -> None:
        accounts, conn = self._selected_accounts(), self.app.session.conn
        pipelines, depth = self.chosen_pipelines() or run_options.PIPELINE_ORDER, self.chosen_depth()
        self.app.run_job(lambda control: pipeline.stage_fetch_bodies(conn, accounts, control, pipelines, depth))

    def _run_ai_stage(self, stage) -> None:
        """Shared by the four AI buttons. `stage(conn, provider, batch, limit, control, depth)`."""
        provider, settings = self._provider()
        if not provider:
            return
        batch, limit, conn = self._int(self.batch_var, DEFAULT_BATCH_CLOUD), self._int(self.max_var), self.app.session.conn
        if not self._confirm_remote_ai(settings, batch, limit):
            return
        depth = self.chosen_depth()

        def job(control):
            self._announce(settings)
            stage(conn, provider, batch, limit, control, depth)
        self.app.run_job(job)

    def _do_classify(self) -> None:
        self._run_ai_stage(lambda conn, provider, batch, limit, control, depth: pipeline.stage_classify(
            conn, provider, batch, limit, control, depth))

    def _do_changes(self) -> None:
        accounts = list(self.app.session.accounts)
        self._run_ai_stage(lambda conn, provider, batch, limit, control, depth: pipeline.stage_detect_changes(
            conn, provider, accounts, max(1, min(batch, 5)), control, depth))

    def _do_closed(self) -> None:
        self._run_ai_stage(lambda conn, provider, batch, limit, control, depth: pipeline.stage_check_closed(
            conn, provider, batch, limit, control, depth))

    def _do_subscriptions(self) -> None:
        self._run_ai_stage(lambda conn, provider, batch, limit, control, depth: pipeline.stage_find_subscriptions(
            conn, provider, batch, limit, control, depth))

    def _do_all(self) -> None:
        pipelines = self.chosen_pipelines()
        if not pipelines:
            dialogs.showinfo("Nothing to run", "Tick at least one pipeline (Sign-ups, Email changes, Closed "
                                               "accounts or Subscriptions) under \u201cWhat to run\u201d first.")
            return
        ok, since = self._read_since()
        if not ok:
            return
        provider, settings = self._provider()
        if not provider:
            return
        accounts, conn = self._selected_accounts(), self.app.session.conn
        fetch = self.fetch_var.get() and any(a.has_password for a in accounts)       # an IMAP fetch needs an app password
        if not fetch and not db.counts(conn)["total_emails"]:
            dialogs.showinfo("No mail to work on", "No mail has been loaded yet. Fetch some over IMAP (an account "
                                                   "with an app password, and \u201cFetch new mail first\u201d "
                                                   "ticked), or import a mail file on the Setup tab.")
            return
        batch, limit = self._int(self.batch_var, DEFAULT_BATCH_CLOUD), self._int(self.max_var)
        if not self._confirm_remote_ai(settings, batch, limit):
            return
        depth = self.chosen_depth()
        session = self.app.session

        def job(control):
            self._announce(settings)
            pipeline.run_full(conn, accounts, provider, fetch=fetch, since=since, batch_size=batch,
                              max_groups=limit, control=control, pipelines=pipelines, depth=depth)
            if session.write_mode:
                print(f"\nResults are saved in {session.db_path}. Export them from the Results tab if you "
                      f"want CSV/Markdown files.")
            else:
                print("\nResults are held in memory only (nothing has been saved). Go to the Results tab to "
                      "review them and export if you want to keep them.")
        self.app.run_job(job)
