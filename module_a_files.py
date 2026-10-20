"""
module_a_files.py
-----------------
One front door for every "import mail from a file" source, so the desktop app and the
command line don't each have to know which reader to use.

    .pst  (.ost)      Outlook data file        -> module_a_pst   (Outlook.com / Hotmail / Live)
    .mbox (.mbx)      MBOX file                -> module_a_mbox  (Gmail Takeout, Thunderbird, ...)
    .eml, or a FOLDER individual email files   -> module_a_eml

The file TYPE is decided from what is inside the file first (its first few bytes), and
only then from its name, so a mis-named file is still read correctly. Everything is
checked BEFORE anything is imported, so a bad choice in a list of files stops cleanly
instead of half-way through.

Formats that can't be read give a message that says what to do instead (ImportProblem).
"""

from __future__ import annotations

import os
import re

import jobcontrol
import module_a_eml
import module_a_mbox
import module_a_pst
from models import Account, ImportProblem

# First bytes of files we can recognise by content.
_PST_MAGIC = b"!BDN"                                       # Outlook data file (PST/OST)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"           # old Microsoft "compound" files (.msg)
_ZIP_MAGIC = b"PK\x03\x04"                                 # .zip, and Outlook for Mac's .olm
_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9-]*:[ \t]")

UNSUPPORTED = {
    "olm": ("'{name}' is an Outlook for Mac archive (.olm), which Vestige can't read directly.\n"
            "Open it in Outlook for Mac and export again, or add the account to Outlook for Windows "
            "and export a .pst (File > Open & Export > Import/Export), or import the account into "
            "Thunderbird and use its .mbox files."),
    "msg": ("'{name}' is a single Outlook message (.msg), which Vestige can't read directly.\n"
            "Export the whole mailbox as a .pst instead (File > Open & Export > Import/Export > "
            "Export to a file > Outlook Data File)."),
    "csv": ("'{name}' is a CSV file. An Outlook CSV export does not normally include the sending date or "
            "message ID that Vestige needs, so it can't be used.\n"
            "Export a .pst instead (File > Open & Export > Import/Export > Export to a file > "
            "Outlook Data File)."),
    "zip": ("'{name}' is a compressed .zip file. Unzip it first, then choose the .pst, .mbox or .eml "
            "files inside. (Google Takeout downloads are zip files containing the .mbox.)"),
    "unknown": ("Vestige doesn't recognise '{name}' as a mail export.\n"
                "It can read: .pst (Outlook / Hotmail / Live), .mbox (Gmail Takeout, Thunderbird) and "
                ".eml (individual emails, or a folder of them)."),
}


def sniff(path: str) -> str:
    """What kind of mail file is this? Returns 'pst', 'mbox', 'eml', 'olm', 'msg', 'zip',
    'csv' or 'unknown'. File contents win over the file name."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as handle:
            head = handle.read(512)
    except OSError:
        head = b""
    if head.startswith(_PST_MAGIC):
        return "pst"
    if head.startswith(_OLE_MAGIC):
        return "msg"
    if head.startswith(_ZIP_MAGIC):
        return "olm" if ext == ".olm" else "zip"
    # Nothing recognisable inside: fall back on the name (an empty or damaged .pst
    # still goes to the PST reader, which explains what is wrong with it).
    by_name = {".pst": "pst", ".ost": "pst", ".olm": "olm", ".mbox": "mbox", ".mbx": "mbox",
               ".eml": "eml", ".csv": "csv", ".msg": "msg"}
    if ext in by_name:
        return by_name[ext]
    if head.startswith(b"From "):
        return "mbox"                                      # e.g. Thunderbird folders, which have no extension
    if _HEADER_LINE.match(head):
        return "eml"
    return "unknown"


def plan_import(paths: list) -> tuple:
    """Check what was chosen BEFORE importing anything. Returns (sources, eml_inputs):
    `sources` is a list of ('pst'|'mbox', path); `eml_inputs` is the files/folders that
    go to the .eml reader together. Raises ImportProblem for anything unreadable."""
    if not paths:
        raise ImportProblem("No file was chosen.")
    sources, eml_inputs = [], []
    for path in paths:
        if os.path.isdir(path):
            eml_inputs.append(path)
            continue
        if not os.path.exists(path):
            raise ImportProblem(f"File not found: {path}")
        kind = sniff(path)
        if kind in ("pst", "mbox"):
            sources.append((kind, path))
        elif kind == "eml":
            eml_inputs.append(path)
        else:
            raise ImportProblem(UNSUPPORTED[kind].format(name=os.path.basename(path)))
    return sources, eml_inputs


def import_mail(account: Account, paths, conn, control=None) -> int:
    """Import one or more mail files (or a folder of .eml files) for `account`.
    `paths` may be a single path or a list. Returns how many new emails were stored."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    sources, eml_inputs = plan_import([os.fspath(p) for p in paths])
    jobs = len(sources) + (1 if eml_inputs else 0)
    total, position = 0, 0
    for kind, path in sources:
        if jobs > 1:
            jobcontrol.slice_(control, position, jobs)
        reader = module_a_pst.import_pst if kind == "pst" else module_a_mbox.import_mbox
        total += reader(account, path, conn, control=control)
        position += 1
    if eml_inputs:
        if jobs > 1:
            jobcontrol.slice_(control, position, jobs)
        total += module_a_eml.import_eml(account, eml_inputs, conn, control=control)
    if jobs > 1:
        print(f"All files done: {total} new emails in total.")
    return total
