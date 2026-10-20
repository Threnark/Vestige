#!/usr/bin/env python3
"""
gui_app.py
----------
Vestige - find the accounts your inbox remembers. Desktop application.

Built with CustomTkinter in a dark, purple look (see ui_theme.py). The window is
this file; each page lives in its own module:

    gui_setup.py    Setup        storage (write mode) / AI provider / email accounts
    gui_run.py      Run Pipeline options, stage buttons, progress bar, live log
    gui_results.py  Results      tables, possible email changes, export

PRIVACY MODEL (what the app enforces)
  * NOTHING is saved by default. Accounts, passwords, the AI API key, local-AI
    addresses and the scanned-email database all live in memory only
    (see session.py) and vanish when the app closes. No autofill, no "remember me".
  * Files are only written when YOU ask:
      - Export: you choose the folder; nothing is written before that.
      - Write mode (opt-in checkbox on the Setup page): saves scan RESULTS to
        Vestige_DATA/session.db next to the program, so a crash or an accidental
        close doesn't waste scanning time and AI cost. Never credentials.
  * "Run full pipeline" does NOT export.

THREADING MODEL
  Exactly one pipeline job runs at a time (App.busy). A job runs in a background
  thread so the window never freezes. Background threads NEVER touch widgets:
  they only push small callables onto App.gui_queue, which the main thread drains
  on a timer (App._poll) - the standard safe pattern for Tk + threads. Log lines
  (captured print() output) and progress snapshots travel the same way. While a
  job runs the main thread does not read the database either, so the single
  in-memory connection is only ever used by one thread at a time.
"""

from __future__ import annotations

import os
import queue
import sys
import threading

import customtkinter as ctk

import ai_providers as ai
import db
import paths
import ui_dialogs as dialogs
import ui_help as help_ui
import ui_theme as theme
from gui_results import ResultsPage
from gui_run import RunPage
from gui_setup import SetupPage
from jobcontrol import Cancelled, JobControl
from models import ImportProblem
from session import Session, SessionError, delete_session_files
from ui_common import QueueWriter

PAGES = ["Setup", "Run Pipeline", "Results"]

# Which Help page the Help button (and the F1 key) opens for each main page.
PAGE_HELP_TOPICS = {"Setup": "start", "Run Pipeline": "pipeline", "Results": "results"}


class App(ctk.CTk):
    def __init__(self):
        theme.setup()                                    # dark mode only; must happen before any widget exists
        super().__init__(fg_color=theme.BG)
        self.title("Vestige")
        self.geometry(theme.WINDOW_SIZE)
        self.minsize(*theme.WINDOW_MIN)
        theme.init(self)                                 # fonts + table style need the window to exist
        theme.apply_icon(self)
        dialogs.set_root(self)

        self.session = Session()                         # memory only; see session.py
        self.busy = False
        self.control: JobControl | None = None
        self.gui_queue: "queue.Queue" = queue.Queue()
        self._exported_counts = (0, 0)                   # what had been exported at the last export

        header = ctk.CTkFrame(self, fg_color="transparent")          # the tab bar, with the Help button at its right
        header.pack(fill="x", padx=10, pady=(10, 4))
        self.tabs = theme.segmented(header, PAGES, self.show_page)
        self.tabs.pack(side="left", fill="x", expand=True)
        theme.button(header, "?  Help", self.open_help, width=90, height=33).pack(side="left", padx=(8, 0))
        self.bind("<F1>", lambda _event: self.open_help())
        self.host = ctk.CTkFrame(self, fg_color="transparent")
        self.host.pack(fill="both", expand=True)

        self.setup_page = SetupPage(self.host, self)
        self.run_page = RunPage(self.host, self)
        self.results_page = ResultsPage(self.host, self)
        self._pages = {PAGES[0]: self.setup_page, PAGES[1]: self.run_page, PAGES[2]: self.results_page}
        self.show_page(PAGES[0])
        self.tabs.set(PAGES[0])

        self.run_page.refresh_status()                   # safe now: all three pages exist
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()
        self.after(300, self._offer_resume)

    # ---- pages ---------------------------------------------------------------------------
    def open_help(self) -> None:
        """Open the Help window on the page that matches the tab being looked at."""
        help_ui.open_help(self, PAGE_HELP_TOPICS.get(self.tabs.get(), "start"))

    def show_page(self, name: str) -> None:
        for page_name, page in self._pages.items():
            if page_name == name:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()

    # ---- plumbing --------------------------------------------------------------------------
    def log(self, message: str) -> None:
        """Thread-safe: may be called from a job thread."""
        self.gui_queue.put(lambda: self.run_page.append_log(message))

    def _on_progress(self, snapshot: dict) -> None:
        """Thread-safe: called by the job thread whenever progress changes."""
        self.gui_queue.put(lambda: self.run_page.show_progress(snapshot))

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.run_page.set_busy_state(busy)
        self.results_page.set_busy_state(busy)

    def _poll(self) -> None:
        try:
            while True:
                self.gui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def run_job(self, fn) -> None:
        """Run fn(control) in a background thread, capturing print() into the log and
        progress into the progress bar. Only one job at a time."""
        if self.busy:
            dialogs.showwarning("Busy", "A job is already running. Wait for it to finish, or press Stop.")
            return
        self.set_busy(True)
        self.control = JobControl(listener=self._on_progress)
        self.run_page.reset_progress()
        self.run_page.append_log("")

        def target():
            previous = sys.stdout
            sys.stdout = QueueWriter(self.log)
            outcome = "finished"
            try:
                fn(self.control)
            except Cancelled:
                outcome = "stopped"
                print("Stopped. Everything finished so far is kept.")
            except (ai.ProviderError, db.DatabaseVersionError, ImportProblem) as exc:
                outcome = "problem"
                print(f"PROBLEM: {exc}")
            except Exception as exc:
                outcome = "problem"
                print(f"ERROR: {type(exc).__name__}: {exc}")
            else:
                self.control.progress.finish()
                print("--- finished ---")
            finally:
                sys.stdout.flush()
                sys.stdout = previous
                self.gui_queue.put(lambda: self._job_finished(outcome))
        threading.Thread(target=target, daemon=True).start()

    def _job_finished(self, outcome: str) -> None:
        self.set_busy(False)                             # first: the UI may read the database again
        self.run_page.end_progress(outcome)
        self.run_page.refresh_status()

    def request_stop(self) -> None:
        if self.control is not None:
            self.control.cancel()
            self.run_page.append_log("Stop requested - finishing the current step...")

    def after_data_replaced(self) -> None:
        """The database changed under us (e.g. a saved session was resumed)."""
        self.setup_page.refresh_storage_status()
        self.run_page.refresh_status()

    # ---- unexported-results tracking ---------------------------------------------------------
    def _result_counts(self) -> tuple:
        c = db.counts(self.session.conn)
        return c["confirmed_signups"], c["email_changes_found"]

    def mark_exported(self) -> None:
        self._exported_counts = self._result_counts()

    # ---- start-up / shutdown ---------------------------------------------------------------------
    def _offer_resume(self) -> None:
        """If a previous write-mode session is sitting next to the program, say so."""
        path = paths.session_db_path(paths.default_data_dir())
        if self.session.write_mode or not os.path.exists(path):
            return
        choice = dialogs.ask_choice(
            "Saved session found",
            f"A saved session from an earlier run was found:\n\n{path}\n\n"
            f"It contains sensitive scan results. Resume it, delete it, or leave it alone?",
            ["Resume it", "Delete it", "Leave it"])
        if choice == "Resume it":
            try:
                self.session.resume_from(path)
            except db.DatabaseVersionError as exc:
                dialogs.showerror("Can't resume", str(exc))
            else:
                self.after_data_replaced()
        elif choice == "Delete it":
            self.setup_page.delete_files(path)

    def _on_close(self) -> None:
        if self.busy and not dialogs.askyesno(
                "Job running", "A job is still running. Quit anyway? (Anything already saved stays saved.)"):
            return
        if self.session.write_mode:
            path = self.session.db_path
            choice = dialogs.ask_choice(
                "Saved data on this computer",
                f"Write mode saved scan results here:\n\n{path}\n\nIt contains sensitive information. "
                f"Delete it now, or keep it so you can resume later?", ["Keep it", "Delete it", "Cancel"])
            if choice in (None, "Cancel"):
                return
            self.session.close()
            if choice == "Delete it":
                try:
                    delete_session_files(path)
                except SessionError as exc:
                    dialogs.showerror("Couldn't delete", str(exc))
        else:
            try:
                current = self._result_counts()
            except Exception:                            # e.g. a job thread still holds the database
                current = (0, 0)
            if any(current) and current != self._exported_counts:
                if not dialogs.askyesno(
                        "Close without exporting?",
                        "You have results that haven't been exported. Nothing is saved automatically, so they "
                        "will be lost when you close.\n\nClose anyway?"):
                    return
            self.session.close()
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
