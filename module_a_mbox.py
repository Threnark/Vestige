"""
module_a_mbox.py
----------------
Module A (alternative source): reads an exported MBOX file - for example a
Google Takeout "Mail" export, or an Outlook mailbox converted to MBOX.

Why this matters: it needs NO password and NO internet connection. It is the
easiest, most private way to use the tool, and the only option for providers
(e.g. personal Outlook/Hotmail) that no longer allow app-password IMAP access.

Unlike IMAP, an MBOX file already contains complete messages, so headers and the
body snippet are taken in a single pass. Messages are processed ONE AT A TIME:
memory use stays small even for a multi-gigabyte export.

The file is only ever read. (The standard library's mailbox module is told not
to create a file if the path is wrong, and falls back to read-only when the file
is on read-only media.)
"""

from __future__ import annotations

import mailbox
import os

import db
import jobcontrol
from models import Account
from module_a_imap import _decode_header_value, _extract_snippet, build_header_row, store_message

COMMIT_EVERY = 500
PROGRESS_EVERY = 5000        # how often a line is printed to the log
PROGRESS_UI_EVERY = 100      # how often the progress bar is told (it throttles itself too)


def import_mbox(account: Account, mbox_path: str, conn, control=None) -> int:
    """Import one MBOX file. `account` is used only to work out which of the
    user's addresses each email was sent to. Returns rows inserted."""
    if not os.path.isfile(mbox_path):
        raise FileNotFoundError(f"MBOX file not found: {mbox_path}")

    print(f"[{account.name}] reading MBOX file: {mbox_path}")
    box = mailbox.mbox(mbox_path, create=False)
    # The first access scans the whole file once to find where each message starts.
    # We can't know how many messages there are until that finishes, so the
    # progress bar shows its "working..." marquee; afterwards it shows a real %.
    jobcontrol.indeterminate(control, "Indexing the MBOX file...")
    print("  indexing the file (this can take a while for a large export) ...")
    total = len(box)
    print(f"  {total:,} messages found")
    jobcontrol.report(control, 0, total, f"0 of {total:,} messages")
    inserted = seen = 0
    try:
        for index, msg in enumerate(box):
            jobcontrol.check(control)
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                print(f"  ... {seen:,} of {total:,} messages read")
            if seen % PROGRESS_UI_EVERY == 0:
                jobcontrol.report(control, seen, total, f"{seen:,} of {total:,} messages")

            row = build_header_row(
                account, "mbox", None,
                from_raw=_decode_header_value(msg.get("From")),
                to_raw=_decode_header_value(msg.get("To")),
                subject=_decode_header_value(msg.get("Subject")),
                date_raw=msg.get("Date"),
                message_id=msg.get("Message-ID") or f"<no-id-{account.name}-mbox-{index}>",
                list_unsub=msg.get("List-Unsubscribe"),
            )
            if store_message(conn, row, lambda msg=msg: _extract_snippet(msg)):
                inserted += 1
            if seen % COMMIT_EVERY == 0:
                conn.commit()
    finally:
        conn.commit()
        box.close()

    jobcontrol.report(control, total, total, f"{total:,} of {total:,} messages")
    print(f"  done: {seen} messages read, {inserted} new")
    return inserted
