"""
module_a_eml.py
---------------
Module A (alternative source): individual .eml email files, or a folder full of them.

An .eml file is one complete email saved as plain text. Many tools produce them:
Thunderbird add-ons, Apple Mail (drag a message to a folder), Outlook-backup tools,
"save as" in webmail, and so on. Like an MBOX or PST import it needs NO password and
no internet connection, and the files are only ever READ.

A FOLDER you pick is searched (including its sub-folders) for *.eml files. A FILE
you pick yourself is read as an email whatever its name.

Only the first EML_READ_BYTES of each file are read. That is plenty for the headers
and the text of the message; it just means a huge attachment further down is never
loaded into memory.
"""

from __future__ import annotations

import email
import os

import jobcontrol
from models import Account, ImportProblem
from module_a_imap import _decode_header_value, _extract_snippet, build_header_row, store_message

EML_READ_BYTES = 262144      # 256 KB per file
COMMIT_EVERY = 500
PROGRESS_UI_EVERY = 25


def find_eml_files(paths: list) -> list:
    """Expand the chosen files and folders into one sorted list of file paths
    (no duplicates). Folders are searched recursively for *.eml."""
    found = []
    for path in paths:
        if os.path.isdir(path):
            for folder, _dirs, names in os.walk(path):
                found.extend(os.path.join(folder, n) for n in names if n.lower().endswith(".eml"))
        elif os.path.isfile(path):
            found.append(path)
        else:
            raise FileNotFoundError(f"Not found: {path}")
    return sorted(dict.fromkeys(found))


def import_eml(account: Account, paths: list, conn, control=None) -> int:
    """Import every .eml file under `paths`. Returns how many new emails were stored."""
    files = find_eml_files(paths)
    if not files:
        raise ImportProblem("No .eml files were found in what you chose. (A folder is searched, "
                            "including its sub-folders, for files ending in .eml.)")
    total = len(files)
    print(f"[{account.name}] reading {total:,} .eml file(s)")
    jobcontrol.report(control, 0, total, f"0 of {total:,} files")
    inserted = not_mail = unreadable = 0
    try:
        for index, path in enumerate(files, start=1):
            jobcontrol.check(control)
            if index % PROGRESS_UI_EVERY == 0 or index == total:
                jobcontrol.report(control, index, total, f"{index:,} of {total:,} files")
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(EML_READ_BYTES)
            except OSError as exc:
                unreadable += 1
                print(f"  (couldn't read {path}: {exc})")
                continue
            msg = email.message_from_bytes(raw)
            if not msg.get("From"):
                not_mail += 1                          # not an email (or one with no sender at all)
                continue
            row = build_header_row(
                account, "eml", None,
                from_raw=_decode_header_value(msg.get("From")),
                to_raw=_decode_header_value(msg.get("To")),
                subject=_decode_header_value(msg.get("Subject")),
                date_raw=msg.get("Date"),
                message_id=msg.get("Message-ID") or f"<no-id-{account.name}-eml-{path}>",
                list_unsub=msg.get("List-Unsubscribe"),
            )
            if store_message(conn, row, lambda msg=msg: _extract_snippet(msg)):
                inserted += 1
            if index % COMMIT_EVERY == 0:
                conn.commit()
    finally:
        conn.commit()
    notes = []
    if not_mail:
        notes.append(f"{not_mail} didn't look like an email")
    if unreadable:
        notes.append(f"{unreadable} couldn't be read")
    print(f"  done: {total:,} files, {inserted} new emails" + (f" ({'; '.join(notes)})" if notes else ""))
    return inserted
