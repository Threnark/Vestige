"""
module_a_pst.py
---------------
Module A (alternative source): an Outlook data file (.pst).

WHY THIS EXISTS
  Microsoft no longer lets apps sign in to Outlook.com / Hotmail / Live / MSN mail
  with a password (see models.MICROSOFT_PASSWORD_NOTE), so IMAP can't be used for
  those accounts. What Microsoft DOES document for getting your mail out is exporting
  the mailbox to a .pst file from Outlook (File > Open & Export > Import/Export >
  Export to a file > Outlook Data File). This module reads that file directly, so
  nobody has to convert it to MBOX first. No password, no internet connection; the
  file is only ever READ.

HOW IT READS A PST
  It uses libpff through its Python package `pypff` (pip name: libpff-python). That is
  the one optional dependency of the whole app; if it is missing the import stops
  with an explanation (ImportProblem) instead of a crash. It is imported lazily, so
  everything else in Vestige works without it.

WHAT IS TAKEN FROM EACH MESSAGE
  * The full internet headers Outlook stored with the message (From, To, Subject, Date,
    Message-ID, List-Unsubscribe). These are the same headers an IMAP fetch would give.
  * If a message has no stored headers, the sender's address, subject and date are taken
    from Outlook's own message properties instead. A message with no usable sender
    address is skipped (it can't be grouped by sender).
  * A short text snippet of the body (plain text, or the HTML version as text).

WHAT IS SKIPPED
  Folders that hold your own or non-mail items (Sent Items, Drafts, Outbox, Calendar,
  Contacts, Tasks, Notes, ... see SKIP_FOLDER_NAMES) and any message sent FROM the
  account's own address. Deleted Items and Junk ARE read: people often delete the
  welcome email they got when they signed up. The log lists every folder read and
  every folder skipped.

KNOWN LIMITS
  * A message whose body exists only as RTF has an empty snippet (its headers still work).
  * Folder names are matched in English, so in a PST from a differently-localised
    Outlook the skip list may not match. That only means a little extra mail is read.
"""

from __future__ import annotations

import email
import email.utils
from datetime import timezone

import jobcontrol
from models import Account, ImportProblem
from module_a_imap import (
    _decode_header_value, build_header_row, snippet_from_texts, store_message,
)

COMMIT_EVERY = 500
PROGRESS_UI_EVERY = 100

# Lower-case folder names that never hold mail worth scanning (and, for system folders,
# aren't mail at all). A skipped folder's sub-folders are skipped with it.
SKIP_FOLDER_NAMES = frozenset({
    "sent items", "sent", "sent mail", "drafts", "outbox",
    "calendar", "contacts", "tasks", "notes", "journal", "people", "suggested contacts",
    "im contact list", "quick step settings", "conversation action settings",
    "conversation history", "rss feeds", "rss subscriptions", "yammer root",
    "social activity notifications",
    "sync issues", "conflicts", "local failures", "server failures",
    "search root", "finder", "reminders", "views", "common views", "shortcuts",
    "spooler queue", "schedule", "freebusy data", "to-do search",
})

# Outlook's own property tags for a sender's address, tried in this order when a
# message carries no internet headers. (SMTP-form addresses first: the others can be
# internal Exchange addresses that aren't real email addresses.)
_MAPI_SENDER_TAGS = (0x5D01, 0x5D02, 0x0C1F, 0x0065)   # sender SMTP, sent-representing SMTP, sender, sent-representing


def load_pypff():
    """Import pypff, or stop with a clear message if it isn't installed."""
    try:
        import pypff
    except ImportError:
        raise ImportProblem(
            "Reading .pst files needs the 'libpff-python' package, and it isn't installed here.\n"
            "Install it with:   pip install libpff-python\n"
            "(In a terminal, using the same Python that runs Vestige.)\n"
            "Alternatively, convert the .pst to an .mbox file with another tool (Thunderbird or "
            "readpst) and import that instead.") from None
    return pypff


# ---------------------------------------------------------------------------
# Reading properties safely. libpff raises an error (not None) for a property a
# message doesn't have, and for damaged items, so every read is guarded.
# ---------------------------------------------------------------------------

def _safe(obj, attr: str):
    try:
        return getattr(obj, attr, None)
    except Exception:
        return None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _count(obj, attr: str) -> int:
    try:
        return int(getattr(obj, attr, 0) or 0)
    except Exception:
        return 0


def _mapi_string(msg, tag: int) -> str:
    """One string property from Outlook's raw property table; '' if absent or unreadable."""
    try:
        for index in range(_count(msg, "number_of_record_sets")):
            entry = msg.get_record_set(index).get_entry_by_type(tag)
            if entry is not None:
                value = _text(entry.get_data_as_string()).strip()
                if value:
                    return value
    except Exception:
        pass
    return ""


def _date_header(msg) -> str | None:
    """An RFC 2822 date string from Outlook's delivery (or sent) time."""
    stamp = _safe(msg, "delivery_time") or _safe(msg, "client_submit_time")
    try:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)           # libpff gives UTC without a zone
        return email.utils.format_datetime(stamp)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# One message -> one database row
# ---------------------------------------------------------------------------

def message_to_row(account: Account, folder_label: str, index: int, msg):
    """Return (row, snippet_function) for one PST message, or None when it has no usable
    sender address. `index` only makes the stand-in Message-ID unique."""
    headers_text = _text(_safe(msg, "transport_headers")).strip()
    parsed = email.message_from_string(headers_text) if headers_text else None

    from_raw = _decode_header_value(parsed.get("From")) if parsed is not None else ""
    if "@" not in from_raw:
        # No internet headers (or no From in them): use Outlook's own sender properties.
        address = next((a for a in (_mapi_string(msg, t) for t in _MAPI_SENDER_TAGS) if "@" in a), "")
        if not address:
            return None
        name = _text(_safe(msg, "sender_name")).strip()
        from_raw = f"{name} <{address}>" if name else address

    def header(field: str) -> str:
        return _decode_header_value(parsed.get(field)) if parsed is not None else ""

    subject = header("Subject") or _text(_safe(msg, "subject")).strip()
    date_raw = (parsed.get("Date") if parsed is not None else None) or _date_header(msg)
    message_id = (parsed.get("Message-ID") if parsed is not None else None) \
        or f"<no-id-{account.name}-pst-{folder_label}-{index}>"
    row = build_header_row(account, f"pst:{folder_label}", None, from_raw=from_raw, to_raw=header("To"),
                           subject=subject, date_raw=date_raw, message_id=message_id,
                           list_unsub=header("List-Unsubscribe") or None)

    def snippet() -> str:
        return snippet_from_texts(_text(_safe(msg, "plain_text_body")), _text(_safe(msg, "html_body")))
    return row, snippet


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

def _children(folder) -> list:
    out = []
    for i in range(_count(folder, "number_of_sub_folders")):
        try:
            out.append(folder.get_sub_folder(i))
        except Exception:
            continue                                   # one damaged folder shouldn't stop the rest
    return out


def mail_folders(root) -> tuple:
    """Walk the folder tree. Returns (folders_to_read, skipped_names) where folders_to_read
    is a list of (folder, 'Path/Like/This') in on-screen order."""
    read, skipped = [], []
    stack = [(root, [], True)]
    while stack:
        folder, trail, is_root = stack.pop()
        name = _text(_safe(folder, "name")).strip()
        # The root's own name ("Root - Mailbox") and the "Top of Outlook data file" wrapper
        # folder only add noise to the log, so they are left out of the displayed path.
        hidden = is_root or name.lower().startswith("top of ")
        here = trail if (hidden or not name) else trail + [name]
        if not is_root and name.lower() in SKIP_FOLDER_NAMES:      # never skip the root itself
            skipped.append("/".join(here))
            continue
        read.append((folder, "/".join(here)))
        stack.extend((child, here, False) for child in reversed(_children(folder)))
    return read, skipped


# ---------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------

def import_pst(account: Account, pst_path: str, conn, control=None) -> int:
    """Import one .pst file. Returns how many new emails were stored."""
    pypff = load_pypff()
    print(f"[{account.name}] reading Outlook data file: {pst_path}")
    pst = pypff.file()
    try:
        pst.open(pst_path)
    except Exception as exc:
        raise ImportProblem(
            f"Couldn't open that file as an Outlook data file ({exc}).\n"
            f"Check that it is a complete .pst (the export finished and the file wasn't cut short), "
            f"and that it isn't an .ost cache file that is still in use by Outlook. If Outlook is "
            f"open, closing it first can help.") from None

    own = account.email.strip().lower()
    inserted = seen = no_sender = from_self = errors = 0
    try:
        jobcontrol.indeterminate(control, "Looking through the folders...")
        folders, skipped = mail_folders(pst.get_root_folder())
        total = sum(_count(folder, "number_of_sub_messages") for folder, _ in folders)
        print(f"  {total:,} messages in {len(folders)} folder(s)")
        jobcontrol.report(control, 0, total, f"0 of {total:,} messages")

        for folder, label in folders:
            count = _count(folder, "number_of_sub_messages")
            if count:
                print(f"  [{label or '(top level)'}] {count:,} messages")
            for index in range(count):
                jobcontrol.check(control)
                seen += 1
                if seen % PROGRESS_UI_EVERY == 0:
                    jobcontrol.report(control, seen, total, f"{seen:,} of {total:,} messages")
                try:
                    built = message_to_row(account, label, index, folder.get_sub_message(index))
                except Exception:
                    errors += 1                            # a damaged item: skip it, keep going
                    continue
                if built is None:
                    no_sender += 1
                    continue
                row, snippet = built
                if own and row["sender_email"] == own:
                    from_self += 1                         # mail you sent yourself isn't a sign-up
                    continue
                if store_message(conn, row, snippet):
                    inserted += 1
                if seen % COMMIT_EVERY == 0:
                    conn.commit()
    finally:
        conn.commit()
        try:
            pst.close()
        except Exception:
            pass

    jobcontrol.report(control, seen, max(seen, 1), f"{seen:,} messages")
    if skipped:
        print(f"  skipped folders (not mail you received): {', '.join(skipped)}")
    notes = []
    if no_sender:
        notes.append(f"{no_sender} had no sender address (calendar items, drafts, ...)")
    if from_self:
        notes.append(f"{from_self} were sent from your own address")
    if errors:
        notes.append(f"{errors} damaged items couldn't be read")
    print(f"  done: {seen:,} messages read, {inserted} new" + (f" ({'; '.join(notes)})" if notes else ""))
    return inserted
