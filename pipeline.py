"""
pipeline.py
-----------
The pipeline's stages as plain functions, used by BOTH the desktop app
(gui_app.py) and the command line (main.py) so they can never drift apart.

    1. stage_fetch / stage_import_mail   Module A  get email headers (IMAP, or a .pst/.mbox/.eml file)
    2. stage_prefilter                   Module B  free rule-based scoring
    3. stage_fetch_bodies                Module A  short snippets, candidates only
    4. stage_classify                    Module C  AI: which senders are sign-ups
    5. stage_detect_changes              Module E  AI: possible email changes
       (export is separate - see module_d_export.py - and only ever happens
        when the user asks for it)

Every stage:
  * takes an open database connection and does its own reporting with print()
    (the GUI shows that output in its log box);
  * accepts an optional JobControl so it can be stopped politely AND so it can
    report progress to the progress bar (see jobcontrol.py);
  * is safe to re-run - it only does work that hasn't been done yet.
"""

from __future__ import annotations

import db
import jobcontrol
import module_a_files
import module_a_imap
import module_b_prefilter
import module_c_classify
import module_e_email_changes
from models import Account

# What the progress bar calls each stage, and how much of a full run it is
# expected to take (a rough estimate of relative time, used only to make the bar
# move at a sensible pace - tune freely).
STAGES = {
    "fetch": ("Fetching email headers", 0.25),
    "import": ("Importing mail from file", 1.00),          # run on its own, so weight is irrelevant
    "prefilter": ("Pre-filtering (free, no AI)", 0.05),
    "bodies": ("Fetching email bodies", 0.15),
    "classify": ("AI: classifying sign-ups", 0.40),
    "changes": ("AI: finding possible email changes", 0.15),
}


def _begin(control, key: str) -> None:
    label, weight = STAGES[key]
    jobcontrol.begin(control, key, label, weight)


def _timed(conn, stage: str, fn):
    """Record the stage in run_log, run it, and record how it ended."""
    log_id = db.log_stage(conn, stage)
    result = fn()
    db.finish_stage(conn, log_id, str(result))
    return result


def stage_fetch(conn, accounts: list, since: str | None = None, control=None) -> int:
    """Fetch headers over IMAP for every account that has an app password.
    An account that fails (wrong password, offline) is reported and skipped
    so the others still run."""
    def run():
        _begin(control, "fetch")
        total = 0
        for index, account in enumerate(accounts):
            jobcontrol.check(control)
            jobcontrol.slice_(control, index, len(accounts))     # each account is an equal slice of the bar
            if not account.has_password:
                if account.is_microsoft:
                    print(f"[{account.name}] skipped: Microsoft doesn't allow password sign-in for "
                          f"Outlook/Hotmail IMAP. Import your exported .pst file instead "
                          f"(Run Pipeline > 1. Import mail file).")
                else:
                    print(f"[{account.name}] no app password entered - skipping IMAP "
                          f"(you can still import a mail file for it).")
                jobcontrol.report(control, 1, 1)
                continue
            try:
                total += module_a_imap.fetch_headers_for_account(account, conn, since=since, control=control)
            except jobcontrol.Cancelled:
                raise
            except Exception as exc:
                print(f"[{account.name}] FAILED: {exc}")
            jobcontrol.report(control, 1, 1)
        print(f"Fetched {total} new message headers.")
        return total
    return _timed(conn, "fetch", run)


def stage_import_mail(conn, account: Account, paths, control=None) -> int:
    """Import one or more exported mail files (.pst, .mbox, .eml, or a folder of .eml
    files). `paths` is a path or a list of paths. See module_a_files.py."""
    def run():
        _begin(control, "import")
        return module_a_files.import_mail(account, paths, conn, control=control)
    return _timed(conn, "import-mail", run)


def stage_import_mbox(conn, account: Account, path: str, control=None) -> int:
    """Kept so older callers keep working; it is the same as stage_import_mail."""
    return stage_import_mail(conn, account, path, control)


def stage_prefilter(conn, control=None) -> dict:
    def run():
        _begin(control, "prefilter")
        result = module_b_prefilter.run_prefilter(conn, control=control)
        print(f"Scored {result['scored']} new emails: {result['candidates']} sign-up candidates, "
              f"{result['noise']} noise, {result['change_notices']} possible email-change notices.")
        return result
    return _timed(conn, "prefilter", run)


def stage_fetch_bodies(conn, accounts: list, control=None) -> int:
    """Download short body snippets for candidates (IMAP-sourced rows only;
    MBOX rows already have theirs)."""
    def run():
        _begin(control, "bodies")
        pending = db.emails_missing_body(conn)
        if not pending:
            print("No email bodies need fetching.")
            jobcontrol.report(control, 1, 1)
            return 0
        by_name = {a.name: a for a in accounts}
        grouped: dict = {}
        for row in pending:
            grouped.setdefault(row["account_name"], []).append(row)
        total = 0
        for index, (name, rows) in enumerate(grouped.items()):
            jobcontrol.check(control)
            jobcontrol.slice_(control, index, len(grouped))
            account = by_name.get(name)
            if account is None or not account.has_password:
                print(f"  skipping {len(rows)} emails from '{name}' - that account isn't set up "
                      f"with an app password in this session.")
                jobcontrol.report(control, 1, 1)
                continue
            print(f"[{name}] fetching {len(rows)} email bodies ...")
            try:
                total += module_a_imap.fetch_body_snippets(account, conn, rows, control=control)
            except jobcontrol.Cancelled:
                raise
            except Exception as exc:
                print(f"[{name}] FAILED: {exc}")
            jobcontrol.report(control, 1, 1)
        print(f"Fetched {total} email bodies.")
        return total
    return _timed(conn, "fetch-bodies", run)


def stage_classify(conn, provider, batch_size: int, max_groups: int | None = None, control=None) -> dict:
    def run():
        _begin(control, "classify")
        return module_c_classify.classify_all(
            conn, provider, batch_size=batch_size, max_groups=max_groups, control=control)
    return _timed(conn, "classify", run)


def stage_detect_changes(conn, provider, accounts: list, batch_size: int, control=None) -> dict:
    def run():
        _begin(control, "changes")
        return module_e_email_changes.detect_changes(
            conn, provider, extra_known_addresses=[a.email for a in accounts],
            batch_size=batch_size, control=control)
    return _timed(conn, "find-changes", run)


def run_full(conn, accounts: list, provider, *, fetch: bool = True, since: str | None = None,
             batch_size: int = 6, max_groups: int | None = None, control=None) -> None:
    """Stages 1-5 in order. Deliberately does NOT export: results stay in memory
    (or in the write-mode file, if the user turned that on) until the user
    chooses to export them. The caller reports where results live, because only
    the caller knows whether write mode is on."""
    order = (["fetch"] if fetch else []) + ["prefilter", "bodies", "classify", "changes"]
    if control is not None:
        control.progress.plan([(key, STAGES[key][0], STAGES[key][1]) for key in order])
    steps = len(order)
    n = 0
    if fetch:
        n += 1
        print(f"=== Step {n}/{steps}: fetch email headers ===")
        stage_fetch(conn, accounts, since, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: pre-filter (free, no AI) ===")
    stage_prefilter(conn, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: fetch email bodies ===")
    stage_fetch_bodies(conn, accounts, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: AI - classify sign-ups ===")
    stage_classify(conn, provider, batch_size, max_groups, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: AI - find possible email changes ===")
    # Change notices carry longer snippets, so cap their batch size at 5.
    stage_detect_changes(conn, provider, accounts, max(1, min(batch_size, 5)), control)
