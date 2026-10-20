"""
migration_matcher.py
--------------------
Pure, deterministic helpers for "possible email changes". No AI, no network, no
database - just text matching, so the behaviour is predictable and easy to test.

THE PROBLEM
  An email-change notice often shows the other address only in masked form:
      "Your email was changed to j***@gmail.com"
  The AI (module_e_email_changes.py) copies that text out exactly as printed.
  THIS file then decides which of the user's REAL, already-seen addresses could
  fit the mask. It never invents an address: if nothing known fits, the result
  is just the printed hint, clearly labelled as such.

HOW A NOTICE IS READ
  Every email has a recipient: the address it was delivered to (we know this for
  certain from the headers). The notice either went to the OLD address ("we
  changed your email to ...") or to the NEW one ("confirm your new email").
    * recipient is the OLD address -> the printed address is the NEW one
    * recipient is the NEW address -> the printed address is the OLD one
  The side that is the recipient is certain; the other side is matched.

MATCH QUALITY (about the address we are trying to find)
  strong    exactly one known address fits the mask (or it is printed in full
            and is one of your scanned addresses)
  possible  more than one known address fits the mask
  hint_only a mask/address is printed but nothing you scanned fits
  none      the notice doesn't print the address at all
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Characters services use to hide part of an address. A run of three or more
# dots ("j...@gmail.com") also counts as a mask; single dots are real dots.
_MASK_RUN = re.compile(r"(?:[*\u2022\u25cf\u2217\u00b7\u2026]|\.{3,})+")

# Things the AI sometimes returns instead of an empty string.
_NONE_WORDS = {"", "none", "n/a", "na", "null", "unknown", "not shown", "not provided", "-", "\u2014"}

_ADDRESS_TOKEN = re.compile(r"[^\s<>\"',;()\[\]]+@[^\s<>\"',;()\[\]]+")


def normalize_printed(text) -> str:
    """Clean an address string copied from an email: lower-case, no brackets or
    trailing punctuation. Returns "" when there is no address-like text."""
    t = (text or "").strip() if isinstance(text, str) else ""
    if t.lower() in _NONE_WORDS:
        return ""
    match = _ADDRESS_TOKEN.search(t)
    if not match:
        return ""
    return match.group(0).rstrip(".,;:!?").lower()


def is_masked(address: str) -> bool:
    return bool(address) and bool(_MASK_RUN.search(address))


def mask_to_regex(masked: str):
    """Compile a masked address into a regex, or None if it is too vague to be
    useful (for example "***@***.***", which would match anything)."""
    s = (masked or "").strip().strip("<>\"'").lower()
    if "@" not in s or not _MASK_RUN.search(s):
        return None
    pieces = _MASK_RUN.split(s)
    visible = "".join(pieces).replace("@", "").replace(".", "")
    if not visible:
        return None
    # A mask never spans the "@", so the wildcard excludes it.
    return re.compile("[^@\\s]+".join(re.escape(p) for p in pieces))


def mask_matches(masked: str, candidate: str) -> bool:
    pattern = mask_to_regex(masked)
    return bool(pattern and pattern.fullmatch((candidate or "").strip().lower()))


# ---------------------------------------------------------------------------
# Analysis of one notice
# ---------------------------------------------------------------------------

@dataclass
class ChangeAnalysis:
    recipient: str
    recipient_role: str                  # "old_address" | "new_address" | "unclear" (after sanity checks)
    old_status: str
    new_status: str
    likely_old: str = ""                 # a single resolved address, or "" if not resolved
    likely_new: str = ""
    old_candidates: list = field(default_factory=list)
    new_candidates: list = field(default_factory=list)
    match_quality: str = "none"          # strong | possible | hint_only | none
    summary: str = ""


_ACTION = {
    "changed": "the account's email address was changed",
    "change_requested": "a change of the account's email address was requested",
    "added": "an email address was added to the account",
    "removed": "an email address was removed from the account",
    "other": "the account's email settings were updated",
}


def _resolve_side(printed: str, known: set, recipient: str, exclude_recipient: bool):
    """Work out what a printed address refers to. Returns (status, address, candidates)."""
    if not printed:
        return "not_shown", "", []
    if is_masked(printed):
        pool = [k for k in known if not (exclude_recipient and k == recipient)]
        found = sorted(k for k in pool if mask_matches(printed, k))
        if len(found) == 1:
            return "mask_match", found[0], found
        if len(found) > 1:
            return "mask_multiple", "", found
        return "mask_no_match", "", []
    if printed in known:
        return "exact_known", printed, [printed]
    return "exact_unknown", printed, []


def _describe(label: str, printed: str, status: str, address: str, candidates: list) -> str:
    """One plain-English sentence about the address we are trying to find."""
    if status == "mask_match":
        return f"It prints the {label} address as {printed}, which matches your address {address}."
    if status == "mask_multiple":
        return (f"It prints the {label} address as {printed}, which fits more than one of your "
                f"addresses: {', '.join(candidates)}.")
    if status == "mask_no_match":
        return (f"It prints the {label} address as {printed}, which doesn't match any address "
                f"you have scanned.")
    if status == "exact_known":
        return f"The {label} address, {address}, is one of the addresses you scanned."
    if status == "exact_unknown":
        return f"The {label} address is {address}, which isn't one of the addresses you scanned."
    return f"It doesn't say what the {label} address is."


_RANK = {"strong": 3, "possible": 2, "hint_only": 1, "none": 0}
_QUALITY = {"mask_match": "strong", "exact_known": "strong", "mask_multiple": "possible",
            "mask_no_match": "hint_only", "exact_unknown": "hint_only", "not_shown": "none",
            "recipient": "none"}


def analyze_change(recipient: str, recipient_role: str, old_printed, new_printed,
                   known_addresses, change_type: str = "changed") -> ChangeAnalysis:
    """Combine what the AI copied from the notice with what we know for certain
    (the recipient and the user's own addresses) into a ChangeAnalysis."""
    recipient = (recipient or "").strip().lower()
    known = {a.strip().lower() for a in known_addresses if a}
    if recipient:
        known.add(recipient)
    old_p, new_p = normalize_printed(old_printed), normalize_printed(new_printed)

    # 1. Settle which side the recipient is. Hard facts beat the AI's opinion:
    #    if the notice prints the recipient's own address, that decides it.
    role = recipient_role if recipient_role in ("old_address", "new_address") else "unclear"
    if recipient:
        if old_p and not is_masked(old_p) and old_p == recipient:
            role = "old_address"
        elif new_p and not is_masked(new_p) and new_p == recipient:
            role = "new_address"
        elif role == "unclear":
            if old_p and is_masked(old_p) and mask_matches(old_p, recipient):
                role = "old_address"
            elif new_p and is_masked(new_p) and mask_matches(new_p, recipient):
                role = "new_address"

    # 2. Resolve each side.
    if role == "old_address":
        old_s, likely_old, old_c = "recipient", recipient, [recipient]
        new_s, likely_new, new_c = _resolve_side(new_p, known, recipient, exclude_recipient=True)
        other = new_s
    elif role == "new_address":
        new_s, likely_new, new_c = "recipient", recipient, [recipient]
        old_s, likely_old, old_c = _resolve_side(old_p, known, recipient, exclude_recipient=True)
        other = old_s
    else:
        old_s, likely_old, old_c = _resolve_side(old_p, known, recipient, exclude_recipient=False)
        new_s, likely_new, new_c = _resolve_side(new_p, known, recipient, exclude_recipient=False)
        other = old_s if _RANK[_QUALITY[old_s]] >= _RANK[_QUALITY[new_s]] else new_s

    # 3. A readable summary.
    action = _ACTION.get(change_type, _ACTION["other"])
    parts = [f"Notice sent to {recipient or 'your mailbox'}: {action}."]
    if role == "old_address":
        parts.append(_describe("new", new_p, new_s, likely_new, new_c))
    elif role == "new_address":
        parts.append(_describe("previous", old_p, old_s, likely_old, old_c))
    else:
        if old_p:
            parts.append(_describe("old", old_p, old_s, likely_old, old_c))
        if new_p:
            parts.append(_describe("new", new_p, new_s, likely_new, new_c))
        if not old_p and not new_p:
            parts.append("It doesn't print either address.")
        parts.append("It isn't clear whether the notice was sent to the old or the new address.")

    return ChangeAnalysis(
        recipient=recipient, recipient_role=role, old_status=old_s, new_status=new_s,
        likely_old=likely_old, likely_new=likely_new, old_candidates=old_c, new_candidates=new_c,
        match_quality=_QUALITY[other], summary=" ".join(parts),
    )
