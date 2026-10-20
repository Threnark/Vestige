"""
module_d_export.py
------------------
Module D: turns stored results into tidy tables and (only when asked) files.

TABLES
  Sign-ups              one row per SERVICE + ADDRESS. If you used two different
                        addresses for Spotify you get two rows, each with its own
                        earliest sign-up date. (Collapsing them into one row would
                        throw away exactly the "which address did I use?" answer.)
  Possible email changes one row per email-change notice found by Module E, with
                        the sign-up address(es) it relates to.

FILES - the rules
  * Nothing is written unless the user chooses a folder and clicks Export (or
    passes --out-dir on the command line). There is NO default location.
  * Existing files are NEVER overwritten: a name clash gets " (2)", " (3)" ...
    (files are created in exclusive mode, so even a race can't overwrite).
  * Optional date/time stamp in file names and an optional new sub-folder per
    export keep repeated exports organised.
  * CSV cells that start with = + - @ are prefixed with ' so a spreadsheet can
    never run service names (which come from email content) as formulas.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

import db
import paths

_WS_RE = re.compile(r"\s+")

SIGNUP_COLUMNS = ["Service Name", "Email Address Used", "Sign-up Confirmation Date",
                  "Category", "Sign-up Method", "Confidence", "Source Domain(s)"]
CHANGE_COLUMNS = ["Service Name", "Notice Date", "Notice Sent To", "Previous Address",
                  "Account May Now Use", "Match", "Confidence", "Originally Signed Up With",
                  "What The Notice Says", "Notice Subject"]

_MATCH_LABELS = {"strong": "Strong", "possible": "Possible (several fit)",
                 "hint_only": "Hint only", "none": "No address shown"}


class ExportError(Exception):
    """A problem exporting; the message is written for the end user."""


# ---------------------------------------------------------------------------
# Building the tables
# ---------------------------------------------------------------------------

def _normalize(name) -> str:
    return _WS_RE.sub(" ", (name or "").strip()).lower()


def _day(iso) -> str:
    return iso[:10] if iso else "(unknown)"


def build_signup_rows(conn, min_confidence: float = 0.5) -> list:
    """One row per (service, address). Different sender domains that the AI
    named the same service are merged; addresses stay separate."""
    services: dict = {}
    for cls in db.all_signups(conn, min_confidence):
        key = _normalize(cls["service_name"]) or f"domain:{cls['sender_domain']}"
        svc = services.setdefault(key, {
            "name": cls["service_name"] or cls["sender_domain"], "category": None,
            "methods": set(), "confidence": 0.0, "domains": set(), "addresses": {}})
        svc["category"] = svc["category"] or cls["category"]
        if cls["signup_method"] and cls["signup_method"] != "unknown":
            svc["methods"].add(cls["signup_method"])
        svc["confidence"] = max(svc["confidence"], cls["confidence"] or 0.0)
        svc["domains"].add(cls["sender_domain"])

        addresses = db.signup_addresses(conn, cls) or [("", cls["signup_date_iso"])]
        for address, first_date in addresses:
            current = svc["addresses"].get(address, "\uffff")      # sorts after any real date
            if first_date and first_date < current:
                svc["addresses"][address] = first_date
            elif address not in svc["addresses"]:
                svc["addresses"][address] = first_date or ""

    rows = []
    for svc in services.values():
        for address, date in sorted(svc["addresses"].items(), key=lambda kv: (kv[1] or "\uffff", kv[0])):
            rows.append({
                "Service Name": svc["name"],
                "Email Address Used": address or "(unknown)",
                "Sign-up Confirmation Date": _day(date),
                "Category": svc["category"] or "other",
                "Sign-up Method": "; ".join(sorted(svc["methods"])) or "unknown",
                "Confidence": round(svc["confidence"], 2),
                "Source Domain(s)": "; ".join(sorted(svc["domains"])),
            })
    rows.sort(key=lambda r: (r["Service Name"].lower(), r["Email Address Used"]))
    return rows


def _describe_address(likely: str, printed: str, candidates: list) -> str:
    if likely:
        return likely
    if candidates:
        return " or ".join(candidates) + " (possible)"
    if printed:
        return f"{printed} (as printed in the notice)"
    return "(not shown)"


def build_change_rows(conn, min_confidence: float = 0.5) -> list:
    import json
    rows, seen = [], set()
    for r in db.change_rows(conn, min_confidence):
        # The same notice can be stored twice if it sat in two folders/mailboxes.
        dedupe = (_normalize(r["service_name"]), r["notice_date_iso"], r["recipient_address"],
                  r["old_address_printed"], r["new_address_printed"])
        if dedupe in seen:
            continue
        seen.add(dedupe)

        originally = sorted({addr for s in db.signups_for_service(conn, r["service_name"], r["sender_domain"])
                             for addr, _ in db.signup_addresses(conn, s) if addr})
        rows.append({
            "Service Name": r["service_name"] or r["sender_domain"],
            "Notice Date": _day(r["notice_date_iso"]),
            "Notice Sent To": r["recipient_address"] or "",
            "Previous Address": _describe_address(r["likely_old_address"], r["old_address_printed"],
                                                  json.loads(r["old_candidates"] or "[]")),
            "Account May Now Use": _describe_address(r["likely_new_address"], r["new_address_printed"],
                                                     json.loads(r["new_candidates"] or "[]")),
            "Match": _MATCH_LABELS.get(r["match_quality"], r["match_quality"] or ""),
            "Confidence": round(r["confidence"] or 0.0, 2),
            "Originally Signed Up With": "; ".join(originally) or "(no sign-up record found)",
            "What The Notice Says": r["summary"] or "",
            "Notice Subject": r["subject"] or "",
        })
    rows.sort(key=lambda r: (r["Service Name"].lower(), r["Notice Date"]))
    return rows


# ---------------------------------------------------------------------------
# Writing files
# ---------------------------------------------------------------------------

@dataclass
class ExportOptions:
    folder: str                          # chosen by the user; there is no default
    base_name: str = "signups"
    formats: tuple = ("csv", "md")
    add_timestamp: bool = True
    use_subfolder: bool = False
    min_confidence: float = 0.5


@dataclass
class ExportResult:
    folder: str
    files: list = field(default_factory=list)


def _csv_safe(value):
    """Defuse spreadsheet formula injection (see module docstring)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _create_new(path: str, **open_kwargs):
    """Create a file that does not exist yet. On a name clash use 'name (2).ext'
    etc. Returns (file_object, final_path). Exclusive mode ('x') means an
    existing file can never be overwritten."""
    stem, ext = os.path.splitext(path)
    for n in range(1, 1000):
        candidate = path if n == 1 else f"{stem} ({n}){ext}"
        try:
            return open(candidate, "x", **open_kwargs), candidate
        except FileExistsError:
            continue
    raise ExportError(f"Couldn't find a free file name for {path}")


def _create_new_dir(path: str) -> str:
    for n in range(1, 1000):
        candidate = path if n == 1 else f"{path} ({n})"
        try:
            os.mkdir(candidate)
            return candidate
        except FileExistsError:
            continue
    raise ExportError(f"Couldn't create a new folder at {path}")


def _write_csv(path: str, columns: list, rows: list) -> str:
    # utf-8-sig adds a byte-order mark so Excel opens accented names correctly.
    handle, final = _create_new(path, newline="", encoding="utf-8-sig")
    with handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_safe(v) for k, v in row.items()})
    return final


def _md_table(columns: list, rows: list) -> list:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines += ["| " + " | ".join(esc(r[c]) for c in columns) + " |" for r in rows]
    return lines


def _write_markdown(path: str, signups: list, changes: list, stamp: str) -> str:
    lines = ["# Discovered account sign-ups", "", f"_Generated {stamp}._", ""]
    if signups:
        lines += _md_table(SIGNUP_COLUMNS, signups)
        lines += ["", f"_{len(signups)} service/address combinations._"]
    else:
        lines.append("_No confirmed sign-ups above the confidence threshold._")
    if changes:
        lines += ["", "## Possible email changes", "",
                  "These are **possible**, not certain. Each is based on a notice that an account's "
                  "email address was changed. Masked addresses (like `j***@gmail.com`) are only matched "
                  "against addresses found in the mail you scanned.", ""]
        lines += _md_table(CHANGE_COLUMNS, changes)
    handle, final = _create_new(path, encoding="utf-8")
    with handle:
        handle.write("\n".join(lines) + "\n")
    return final


def export_files(conn, options: ExportOptions, now: datetime | None = None) -> ExportResult:
    """Write the chosen files. Raises ExportError with a friendly message on any problem."""
    if not options.folder or not options.folder.strip():
        raise ExportError("Choose a folder to save into first.")
    formats = tuple(f for f in options.formats if f in ("csv", "md"))
    if not formats:
        raise ExportError("Tick at least one format (CSV or Markdown).")
    ok, message = paths.check_writable(options.folder)
    if not ok:
        raise ExportError(message)

    signups = build_signup_rows(conn, options.min_confidence)
    changes = build_change_rows(conn, options.min_confidence)
    if not signups and not changes:
        raise ExportError("There is nothing to export yet. Run the pipeline first, "
                          "or lower the minimum confidence.")

    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    base = paths.safe_filename(options.base_name)
    suffix = f"_{stamp}" if options.add_timestamp else ""

    try:
        target = options.folder
        if options.use_subfolder:
            target = _create_new_dir(os.path.join(options.folder, f"Vestige_{stamp}"))
        result = ExportResult(folder=target)
        if "csv" in formats:
            result.files.append(_write_csv(os.path.join(target, f"{base}{suffix}.csv"), SIGNUP_COLUMNS, signups))
            if changes:
                result.files.append(_write_csv(
                    os.path.join(target, f"{base}_possible_email_changes{suffix}.csv"), CHANGE_COLUMNS, changes))
        if "md" in formats:
            result.files.append(_write_markdown(os.path.join(target, f"{base}{suffix}.md"), signups, changes,
                                                now.strftime("%Y-%m-%d %H:%M")))
    except OSError as exc:
        raise ExportError(f"Couldn't write the files: {exc}") from None
    return result
