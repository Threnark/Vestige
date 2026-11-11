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

TWO THINGS THIS MODULE DOES ON PURPOSE (both added after a real 37,000-message
export stopped at message ~10,000 with "UnicodeDecodeError: 'ascii' codec can't
decode byte 0xf0"):

  1. Messages are read as RAW BYTES (`get_file`), then parsed with the `email`
     package. Only the first MBOX_READ_BYTES of each message are read: the headers and the
     start of the text are all that is used, and a message with a large attachment would
     otherwise be read and parsed in full for nothing (about 3x slower on a file of such mail). The obvious alternative, iterating the mailbox object, makes the
     standard library decode each message's "From " SEPARATOR line as ASCII. Some
     exports put a non-ASCII character in that line (for example an emoji or an
     accented name in the sender part), and a single such message used to abort
     the whole import. The separator line is never needed, so it is never decoded.

  2. ONE UNREADABLE MESSAGE NEVER STOPS THE IMPORT. If a message can't be read for
     any reason it is skipped and counted, and the log says which message numbers
     were skipped so a problem can be tracked down. A stop request (Cancelled) and
     database errors are NOT swallowed: those must still end the job.
"""

from __future__ import annotations

import email
import mailbox
import os
import sqlite3

import jobcontrol
from models import Account
from module_a_imap import (_decode_header_value, _extract_body, body_start, build_header_row,
                           stand_in_message_id, store_message, tidy_message_id, wants_amounts)

MBOX_READ_BYTES = 262144     # 256 KB per message, as for .eml files (module_a_eml.EML_READ_BYTES)
_LINESEP = os.linesep.encode("ascii")
COMMIT_EVERY = 500
PROGRESS_EVERY = 5000        # how often a line is printed to the log
PROGRESS_UI_EVERY = 100      # how often the progress bar is told (it throttles itself too)
SKIP_DETAILS_SHOWN = 10      # how many skipped messages get their own log line (the rest are only counted)
SKIP_NUMBERS_SHOWN = 20      # how many skipped message numbers are listed in the summary


def _read_message_start(box, key) -> bytes:
    """The first MBOX_READ_BYTES of one message, without its "From " separator line (never decoded: see the
    module docstring). Line endings are treated exactly as `box.get_bytes` treats them, so the same message
    gives the same bytes whichever way it is read, and so the same stand-in Message-ID."""
    return box.get_file(key).read(MBOX_READ_BYTES).replace(_LINESEP, b"\n")


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
    skipped: list = []                     # message numbers (1-based) that could not be read
    try:
        for index, key in enumerate(box.iterkeys()):
            jobcontrol.check(control)      # a Stop request ends the job here, outside the try below
            seen += 1
            if seen % PROGRESS_EVERY == 0:
                print(f"  ... {seen:,} of {total:,} messages read")
            if seen % PROGRESS_UI_EVERY == 0:
                jobcontrol.report(control, seen, total, f"{seen:,} of {total:,} messages")

            try:
                # Raw bytes, NOT box[key]: see the module docstring (the separator line is never decoded).
                raw = _read_message_start(box, key)
                msg = email.message_from_bytes(raw)
                from_raw = _decode_header_value(msg.get("From"))
                to_raw = _decode_header_value(msg.get("To"))
                subject = _decode_header_value(msg.get("Subject"))
                date_raw = msg.get("Date")
                row = build_header_row(
                    account, "mbox", None, from_raw=from_raw, to_raw=to_raw, subject=subject, date_raw=date_raw,
                    # No Message-ID? Build one from what the message says, NOT from its position in this
                    # file: a position would match the first no-ID message of a different file.
                    message_id=tidy_message_id(msg.get("Message-ID")) or stand_in_message_id(
                        from_raw, to_raw, subject, date_raw, body_start(raw), where=f"{mbox_path}#{index}"),
                    list_unsub=msg.get("List-Unsubscribe"),
                )
                if store_message(conn, row, lambda msg=msg, row=row: _extract_body(msg, wants_amounts(row))):
                    inserted += 1
            except (jobcontrol.Cancelled, sqlite3.Error):
                raise                       # a stop request or a database problem must still end the job
            except Exception as exc:        # anything else wrong with THIS message: skip it, keep going
                skipped.append(seen)
                if len(skipped) <= SKIP_DETAILS_SHOWN:
                    print(f"  skipped message #{seen:,}: {type(exc).__name__}: {exc}")
                elif len(skipped) == SKIP_DETAILS_SHOWN + 1:
                    print("  (more messages were skipped; they are counted but not listed one by one)")
            if seen % COMMIT_EVERY == 0:
                conn.commit()
    finally:
        conn.commit()
        box.close()

    jobcontrol.report(control, total, total, f"{total:,} of {total:,} messages")
    note = f", {len(skipped)} skipped (could not be read)" if skipped else ""
    print(f"  done: {seen} messages read, {inserted} new{note}")
    if skipped:
        shown = ", ".join(f"#{n:,}" for n in skipped[:SKIP_NUMBERS_SHOWN])
        more = f" and {len(skipped) - SKIP_NUMBERS_SHOWN} more" if len(skipped) > SKIP_NUMBERS_SHOWN else ""
        print(f"  skipped message numbers: {shown}{more}")
    return inserted
