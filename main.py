#!/usr/bin/env python3
# Vestige
# Copyright (C) 2026 THRENARK
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
main.py
-------
Command-line interface for Vestige. It runs the same pipeline
as the desktop app (see pipeline.py) one stage at a time.

IMPORTANT DIFFERENCE FROM THE DESKTOP APP
    Every command is a separate process, so progress has to live somewhere
    between commands: this tool keeps its database in a FILE (--db, default
    ./accounts.db). That file contains sensitive information (sender names,
    subject lines, short body excerpts, your addresses). The desktop app, by
    contrast, keeps everything in memory unless you turn on write mode. Delete
    the file when you are finished with it.

    Credentials are read from environment variables / a .env file that you
    manage - this tool never writes them anywhere. API keys are NEVER taken
    from command-line arguments (they would end up in your shell history).

Typical run:
    python main.py fetch
    python main.py prefilter
    python main.py prefilter-report          # sanity-check before using any AI
    python main.py fetch-bodies
    python main.py classify --ai anthropic
    python main.py find-changes --ai anthropic
    python main.py export --out-dir D:\\Safe\\Exports

Using a local AI instead of a cloud one:
    python main.py classify --local --model llama3.1:8b
"""

from __future__ import annotations

import argparse
import os
import sys

import ai_providers
import db
import module_d_export
import pipeline
from jobcontrol import Cancelled
from models import (
    DEFAULT_ANTHROPIC_MODEL, DEFAULT_BATCH_CLOUD, DEFAULT_BATCH_LOCAL, KIND_ANTHROPIC, KIND_OPENAI,
    MODE_CLOUD, MODE_LOCAL, ImportProblem, ProviderSettings,
)

DB_PATH_DEFAULT = "accounts.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_provider(args):
    """Build the AI provider from the command-line flags + environment."""
    if args.local:
        settings = ProviderSettings(
            mode=MODE_LOCAL, kind=KIND_OPENAI,
            base_url=args.base_url or "http://localhost:11434/v1", model=args.model or "")
    else:
        kind = KIND_ANTHROPIC if args.ai == "anthropic" else KIND_OPENAI
        env_name = args.api_key_env or ("ANTHROPIC_API_KEY" if kind == KIND_ANTHROPIC else "OPENAI_API_KEY")
        settings = ProviderSettings(
            mode=MODE_CLOUD, kind=kind, base_url=args.base_url or "",
            api_key=os.environ.get(env_name, ""),
            model=args.model or (DEFAULT_ANTHROPIC_MODEL if kind == KIND_ANTHROPIC else ""))
        if not settings.api_key:
            sys.exit(f"No API key found in the environment variable {env_name}. "
                     f"Set it (or pass --api-key-env NAME to use a different variable).")
    level, text = ai_providers.privacy_summary(settings)
    print(f"[{level}] {text}")
    try:
        return ai_providers.create_provider(settings)
    except ai_providers.ProviderError as exc:
        sys.exit(str(exc))


def default_batch(args) -> int:
    if args.batch_size:
        return args.batch_size
    return DEFAULT_BATCH_LOCAL if args.local else DEFAULT_BATCH_CLOUD


def load_accounts(args):
    import config                      # only the CLI needs the yaml/.env loader
    return config.load_accounts(args.config)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_fetch(args, conn):
    accounts = load_accounts(args)
    if args.account:
        accounts = [a for a in accounts if a.name == args.account]
        if not accounts:
            sys.exit(f"No account named '{args.account}' in {args.config}")
    pipeline.stage_fetch(conn, accounts, since=args.since)


def cmd_import_mail(args, conn):
    account = next((a for a in load_accounts(args) if a.name == args.account), None)
    if not account:
        sys.exit(f"No account named '{args.account}' in {args.config}")
    try:
        pipeline.stage_import_mail(conn, account, args.file)
    except ImportProblem as exc:
        sys.exit(str(exc))


def cmd_prefilter(args, conn):
    pipeline.stage_prefilter(conn)


def cmd_prefilter_report(args, conn):
    c = db.counts(conn)
    print(f"Total emails:            {c['total_emails']}")
    print(f"  sign-up candidates:    {c['candidates']}")
    print(f"  noise:                 {c['noise']}")
    print(f"  unscored:              {c['unscored']}")
    print(f"Candidate sender domains: {c['candidate_domains']}")
    print(f"Possible email-change notices: {c['change_notices']}")
    for label, where in (("candidate", "prefilter_label='candidate'"), ("noise", "prefilter_label='noise'")):
        print(f"\nSample of {args.n} {label} subjects"
              f"{' (check nothing real got dropped)' if label == 'noise' else ''}:")
        for r in conn.execute(f"SELECT sender_domain, subject, prefilter_score FROM emails "
                              f"WHERE {where} ORDER BY RANDOM() LIMIT ?", (args.n,)):
            print(f"  [{r['prefilter_score']:+.1f}] {str(r['sender_domain']):30s} {r['subject']}")


def cmd_fetch_bodies(args, conn):
    pipeline.stage_fetch_bodies(conn, load_accounts(args))


def cmd_classify(args, conn):
    pipeline.stage_classify(conn, make_provider(args), default_batch(args), args.max_domains)


def cmd_find_changes(args, conn):
    accounts = load_accounts(args) if os.path.exists(args.config) else []
    pipeline.stage_detect_changes(conn, make_provider(args), accounts, max(1, min(default_batch(args), 5)))


def cmd_export(args, conn):
    options = module_d_export.ExportOptions(
        folder=args.out_dir, base_name=args.name,
        formats=tuple(f.strip() for f in args.formats.split(",") if f.strip()),
        add_timestamp=not args.no_timestamp, use_subfolder=args.subfolder,
        min_confidence=args.min_confidence)
    try:
        result = module_d_export.export_files(conn, options)
    except module_d_export.ExportError as exc:
        sys.exit(str(exc))
    print(f"Wrote {len(result.files)} file(s) to {result.folder}:")
    for path in result.files:
        print(f"  {path}")


def cmd_status(args, conn):
    for key, value in db.counts(conn).items():
        print(f"{key:22s} {value}")


def cmd_all(args, conn):
    accounts = load_accounts(args)
    provider = make_provider(args)
    pipeline.run_full(conn, accounts, provider, fetch=not args.skip_fetch, since=args.since,
                      batch_size=default_batch(args), max_groups=args.max_domains)
    print(f"\nResults are in {args.db}. Run 'export --out-dir <folder>' to write CSV/Markdown files.")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def add_ai_options(p):
    g = p.add_argument_group("AI options")
    g.add_argument("--ai", choices=["anthropic", "openai-compatible"], default="anthropic",
                   help="Which kind of cloud service (default: anthropic)")
    g.add_argument("--local", action="store_true",
                   help="Use a local AI server (Ollama by default) - nothing leaves this computer")
    g.add_argument("--base-url", help="API base URL (needed for openai-compatible; default for --local is Ollama)")
    g.add_argument("--model", help="Model name (required for openai-compatible and --local)")
    g.add_argument("--api-key-env", help="Name of the environment variable holding the API key")
    g.add_argument("--batch-size", type=int, help="Groups per AI request (default 6 cloud / 2 local)")


def build_parser():
    p = argparse.ArgumentParser(description="Vestige - find the accounts your inbox remembers (command-line version)")
    p.add_argument("--db", default=DB_PATH_DEFAULT, help="Database FILE the CLI keeps between commands")
    p.add_argument("--config", default="accounts.yaml", help="Accounts config path")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Fetch email headers over IMAP")
    f.add_argument("--account", help="Only this account")
    f.add_argument("--since", help="IMAP date, e.g. 01-Jan-2020")
    f.set_defaults(func=cmd_fetch)

    # "import-mail" reads .pst (Outlook / Hotmail), .mbox and .eml files or a folder of .eml
    # files; "import-mbox" is the older name for the same command and still works.
    for command, text in (("import-mail", "Import an exported mail file: .pst, .mbox, .eml or a folder of "
                                          ".eml files (no password needed)"),
                          ("import-mbox", "Same as import-mail (older name)")):
        m = sub.add_parser(command, help=text)
        m.add_argument("--account", required=True)
        m.add_argument("--file", required=True, nargs="+",
                       help="One or more files, or a folder of .eml files")
        m.set_defaults(func=cmd_import_mail)

    sub.add_parser("prefilter", help="Rule-score new emails (free)").set_defaults(func=cmd_prefilter)

    pr = sub.add_parser("prefilter-report", help="Show what the pre-filter kept/dropped")
    pr.add_argument("-n", type=int, default=15)
    pr.set_defaults(func=cmd_prefilter_report)

    sub.add_parser("fetch-bodies", help="Fetch body snippets for candidates").set_defaults(func=cmd_fetch_bodies)

    cl = sub.add_parser("classify", help="AI: classify candidate senders")
    add_ai_options(cl)
    cl.add_argument("--max-domains", type=int, help="Cap for a cheap test run")
    cl.set_defaults(func=cmd_classify)

    fc = sub.add_parser("find-changes", help="AI: find possible email changes")
    add_ai_options(fc)
    fc.set_defaults(func=cmd_find_changes)

    ex = sub.add_parser("export", help="Write CSV/Markdown files to a folder you choose")
    ex.add_argument("--out-dir", required=True, help="Folder to save into (no default - you choose)")
    ex.add_argument("--name", default="signups", help="Base file name")
    ex.add_argument("--formats", default="csv,md", help="Comma-separated: csv, md")
    ex.add_argument("--no-timestamp", action="store_true", help="Don't add date/time to file names")
    ex.add_argument("--subfolder", action="store_true", help="Put the files in a new dated sub-folder")
    ex.add_argument("--min-confidence", type=float, default=0.5)
    ex.set_defaults(func=cmd_export)

    sub.add_parser("status", help="Row counts at each stage").set_defaults(func=cmd_status)

    al = sub.add_parser("all", help="Run fetch -> prefilter -> bodies -> classify -> find-changes (no export)")
    add_ai_options(al)
    al.add_argument("--since", help="IMAP date, e.g. 01-Jan-2020")
    al.add_argument("--skip-fetch", action="store_true", help="Skip the IMAP fetch (e.g. after an MBOX import)")
    al.add_argument("--max-domains", type=int)
    al.set_defaults(func=cmd_all)
    return p


def main():
    args = build_parser().parse_args()
    if args.command not in ("export",) or os.path.exists(args.db):
        print(f"(The command-line tool keeps its database as a file: {os.path.abspath(args.db)})")
    try:
        with db.connection(args.db) as conn:
            args.func(args, conn)
    except Cancelled:
        print("Stopped.")
    except KeyboardInterrupt:
        print("\nInterrupted. Safe to re-run the same command - progress is saved in the database file.")
        sys.exit(130)
    except (ai_providers.ProviderError, db.DatabaseVersionError, FileNotFoundError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
