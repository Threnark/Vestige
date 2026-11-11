"""
pipeline.py
-----------
The pipeline's stages as plain functions, used by BOTH the desktop app
(gui_app.py) and the command line (main.py) so they can never drift apart.

The SHARED free steps (every pipeline starts from these):
    1. stage_fetch / stage_import_mail   Module A  get email headers (IMAP, or a .pst/.mbox/.eml file)
    2. stage_prefilter                   Module B  free rule-based scoring (all four rule tracks)
    3. stage_fetch_bodies                Module A  short snippets, only for the chosen pipelines' shortlists

The four AI pipelines. Each has its OWN prompt and sees only its OWN shortlist, so choosing
fewer of them costs less (see run_options.py):
    4. stage_classify                    Module C  AI: which senders are sign-ups
    5. stage_detect_changes              Module E  AI: possible email changes
    6. stage_check_closed                Module F  AI: closed or deactivated accounts
    7. stage_find_subscriptions          Module G  AI: subscriptions and their cost

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

import sqlite3

import db
import jobcontrol
import module_a_files
import module_a_imap
import module_b_prefilter
import module_c_classify
import module_e_email_changes
import module_f_closures
import module_g_subscriptions
import run_options
from models import Account

# What the progress bar calls each stage, and how much of a full run it is
# expected to take (a rough estimate of relative time, used only to make the bar
# move at a sensible pace - tune freely).
STAGES = {
    "fetch": ("Scanning mailbox", 0.25),
    "import": ("Importing mail from file", 1.00),          # run on its own, so weight is irrelevant
    "prefilter": ("Shortlisting emails (free, no AI)", 0.05),
    "bodies": ("Downloading email text", 0.15),
    "classify": ("AI: finding sign-ups", 0.40),
    "changes": ("AI: finding possible email changes", 0.15),
    "closed": ("AI: checking closed accounts", 0.10),
    "subscriptions": ("AI: finding subscriptions", 0.15),
}

# Which progress-bar stage each pipeline runs as.
PIPELINE_STAGE = {
    run_options.PIPELINE_SIGNUPS: "classify",
    run_options.PIPELINE_CHANGES: "changes",
    run_options.PIPELINE_CLOSED: "closed",
    run_options.PIPELINE_SUBSCRIPTIONS: "subscriptions",
}


def _begin(control, key: str) -> None:
    label, weight = STAGES[key]
    jobcontrol.begin(control, key, label, weight)


def _timed(conn, stage: str, fn):
    """Record the stage in run_log, run it, and record how it ended: its result, "stopped" (the person
    pressed Stop) or "failed: ...". Before, only a stage that finished normally was closed, so a stage
    that was stopped or hit an error stayed "started, never finished" for ever."""
    log_id = db.log_stage(conn, stage)
    outcome = "interrupted"
    try:
        result = fn()
        outcome = str(result)
        return result
    except jobcontrol.Cancelled:
        outcome = "stopped"
        raise
    except Exception as exc:
        outcome = f"failed: {type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        try:
            db.finish_stage(conn, log_id, outcome)
        except sqlite3.Error:
            pass            # e.g. the window was closed mid-job: never let the log hide the real error


def stage_fetch(conn, accounts: list, since: str | None = None, control=None) -> int:
    """Fetch headers over IMAP for every account that has an app password.
    An account that fails (wrong password, offline) is reported and skipped
    so the others still run. `since` may be written 01-Jan-2024 or 2024-01-31; anything else raises
    ValueError before any account is contacted."""
    since = module_a_imap.parse_since(since)

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
                          f"(Setup > Import mail file...).")
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


def stage_prefilter(conn, control=None) -> dict:
    def run():
        _begin(control, "prefilter")
        result = module_b_prefilter.run_prefilter(conn, control=control)
        print(f"Scored {result['scored']} new emails: {result['candidates']} sign-up emails spotted, "
              f"{result['noise']} noise, {result['change_notices']} possible email-change notices.")
        if result["rechecked"]:
            print(f"Also re-checked {result['rechecked']} emails from an earlier session for closed-account "
                  f"and billing clues.")
        print(f"Found {result['closure_notices']} closure-style notices and "
              f"{result['billing_emails']} billing emails (free rules only; nothing has gone to the AI).")
        return result
    return _timed(conn, "prefilter", run)


def stage_fetch_bodies(conn, accounts: list, control=None, pipelines=None, depth=None) -> int:
    """Download short body snippets (IMAP-sourced rows only; file-imported mail already has
    its text). Only the emails the chosen `pipelines` will actually READ are fetched (None = all four):

      * Sign-ups and Closed accounts show the AI a few emails per sender (how many depends on `depth`),
        not every shortlisted email, so only those are downloaded (body_ids_wanted in module_b_prefilter
        and module_f_closures: the same grouping code the AI steps use);
      * Subscriptions likewise picks a few per sender (module_g_subscriptions.body_ids_wanted);
      * Email changes reads every one of its candidates, so all of them are downloaded.

    An unticked pipeline's emails are not downloaded at all. If the depth is raised AFTER this step, the
    extra emails the AI would then read have no text yet; the AI steps say so (ai_batches.note_missing_text)."""
    pipelines = run_options.normalize_pipelines(pipelines)
    depth = run_options.get_depth(depth)

    def run():
        _begin(control, "bodies")
        wanted: list = []
        if run_options.PIPELINE_SIGNUPS in pipelines:
            wanted += module_b_prefilter.body_ids_wanted(conn, depth.signup_samples)
        if run_options.PIPELINE_CLOSED in pipelines:
            wanted += module_f_closures.body_ids_wanted(conn, depth)
        if run_options.PIPELINE_SUBSCRIPTIONS in pipelines:
            wanted += module_g_subscriptions.body_ids_wanted(conn, depth)
        changes = (run_options.PIPELINE_CHANGES,) if run_options.PIPELINE_CHANGES in pipelines else ()
        pending = db.emails_missing_body(conn, changes, sorted(set(wanted)))
        left_out = len(db.emails_missing_body(conn, pipelines, wanted)) - len(pending)
        if left_out > 0:
            print(f"Not downloading {left_out:,} other shortlisted emails: the AI reads only a few per "
                  f"sender, so the rest aren't needed.")
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


def stage_classify(conn, provider, batch_size: int, max_groups: int | None = None, control=None,
                   depth=None) -> dict:
    def run():
        _begin(control, "classify")
        return module_c_classify.classify_all(
            conn, provider, batch_size=batch_size, max_groups=max_groups, control=control, depth=depth)
    return _timed(conn, "classify", run)


def stage_detect_changes(conn, provider, accounts: list, batch_size: int, control=None, depth=None) -> dict:
    def run():
        _begin(control, "changes")
        return module_e_email_changes.detect_changes(
            conn, provider, extra_known_addresses=[a.email for a in accounts],
            batch_size=batch_size, control=control, depth=depth)
    return _timed(conn, "find-changes", run)


def stage_check_closed(conn, provider, batch_size: int, max_groups: int | None = None, control=None,
                       depth=None) -> dict:
    def run():
        _begin(control, "closed")
        return module_f_closures.check_closed(
            conn, provider, batch_size=batch_size, max_groups=max_groups, control=control, depth=depth)
    return _timed(conn, "check-closed", run)


def stage_find_subscriptions(conn, provider, batch_size: int, max_groups: int | None = None, control=None,
                             depth=None) -> dict:
    def run():
        _begin(control, "subscriptions")
        return module_g_subscriptions.find_subscriptions(
            conn, provider, batch_size=batch_size, max_groups=max_groups, control=control, depth=depth)
    return _timed(conn, "find-subscriptions", run)


def run_full(conn, accounts: list, provider, *, fetch: bool = True, since: str | None = None,
             batch_size: int = 6, max_groups: int | None = None, control=None,
             pipelines=None, depth=None) -> None:
    """The shared free steps, then the chosen AI pipelines, in order. `pipelines` is any subset of
    run_options.PIPELINE_ORDER (None = all four) and `depth` is a run_options.Depth or its name.

    Deliberately does NOT export: results stay in memory (or in the write-mode file, if the user
    turned that on) until the user chooses to export them. The caller reports where results
    live, because only the caller knows whether write mode is on."""
    pipelines = run_options.normalize_pipelines(pipelines)
    depth = run_options.get_depth(depth)
    order = (["fetch"] if fetch else []) + ["prefilter", "bodies"] + [PIPELINE_STAGE[p] for p in pipelines]
    if control is not None:
        control.progress.plan([(key, STAGES[key][0], STAGES[key][1]) for key in order])
    steps = len(order)
    n = 0
    print("Pipelines: " + ", ".join(run_options.PIPELINE_LABELS[p] for p in pipelines)
          + f"   Depth: {depth.name}")
    if fetch:
        n += 1
        print(f"=== Step {n}/{steps}: scan mailbox ===")
        stage_fetch(conn, accounts, since, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: shortlist emails (free, no AI) ===")
    stage_prefilter(conn, control)
    n += 1
    print(f"\n=== Step {n}/{steps}: download email text ===")
    stage_fetch_bodies(conn, accounts, control, pipelines, depth)
    for pipeline in pipelines:
        n += 1
        if pipeline == run_options.PIPELINE_SIGNUPS:
            print(f"\n=== Step {n}/{steps}: AI - find sign-ups ===")
            stage_classify(conn, provider, batch_size, max_groups, control, depth)
        elif pipeline == run_options.PIPELINE_CHANGES:
            print(f"\n=== Step {n}/{steps}: AI - find possible email changes ===")
            # Change notices carry longer snippets, so cap their batch size at 5.
            stage_detect_changes(conn, provider, accounts, max(1, min(batch_size, 5)), control, depth)
        elif pipeline == run_options.PIPELINE_CLOSED:
            print(f"\n=== Step {n}/{steps}: AI - check closed accounts ===")
            stage_check_closed(conn, provider, batch_size, max_groups, control, depth)
        else:
            print(f"\n=== Step {n}/{steps}: AI - find subscriptions ===")
            stage_find_subscriptions(conn, provider, batch_size, max_groups, control, depth)
