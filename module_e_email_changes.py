"""
module_e_email_changes.py
-------------------------
Module E: finds POSSIBLE account email changes ("migrations").

WHY: if you're hunting for an account you've forgotten, finding "Service X
exists" is only half the answer - if you changed the email on it years ago,
logging in with the address you signed up with won't work. A notice such as
"the email on your Service X account was changed to j***@gmail.com" is the clue
that tells you where to look.

TRIGGER: only emails that look like email-change NOTICES (Module B's
change_candidate flag). Having two accounts on one service never triggers this.

TWO STEPS, deliberately separated:
  1. AI (this file calls it): for each notice, copy out what it literally says -
     the service, whether it went to the old or new address, and any address
     printed (often masked). The AI is told never to guess or complete anything.
  2. Plain code (migration_matcher.py): works out which of YOUR real, already-
     seen addresses could fit a masked address. No invented addresses, ever.

What the AI receives: the sender, date, subject and the first part of the body
of each notice. It does NOT receive your list of addresses or the address the
notice was delivered to.

Results are labelled "possible" because a notice can only ever be evidence:
it may be missing, deleted, or silent about the new address.
"""

from __future__ import annotations

import ai_batches
import ai_providers as ai
import db
import jobcontrol
import migration_matcher as matcher
import run_options
from prompts.email_change_prompt import CHANGE_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS, SYSTEM_PROMPT

SNIPPET_CHARS_SENT = run_options.BALANCED.change_chars   # masked addresses can sit a little way into the body
MAX_TOKENS = 2500
_one_line = ai_batches.one_line        # flattens untrusted email text before it goes into the prompt


def build_user_message(batch: list, depth=None) -> str:
    chars = run_options.get_depth(depth).change_chars
    lines = []
    for row in batch:
        lines.append(f"### EMAIL id={row['id']}")
        lines.append(f"from: {_one_line(row['sender_raw'], 120)}")
        lines.append(f"date: {row['date_iso'] or row['date_raw'] or 'unknown'}")
        lines.append(f'subject: "{_one_line(row["subject"], 200)}"')
        lines.append(f"body_snippet: {_one_line(row['body_snippet'], chars)}")
        lines.append("")
    return "\n".join(lines)


def _record_for(row, ai_result: dict, known: set) -> dict:
    """Combine one AI extraction with the matcher's analysis into a stored record."""
    is_change = bool(ai_result.get("is_email_change"))
    base = {
        "email_id": row["id"], "is_email_change": is_change,
        "change_type": ai_result.get("change_type"),
        "service_name": (ai_result.get("service_name") or "").strip() or row["sender_domain"],
        "sender_domain": row["sender_domain"],
        "recipient_address": row["email_address_used"],
        "recipient_role": ai_result.get("recipient_role"),
        "old_address_printed": ai_result.get("old_address_printed") or "",
        "new_address_printed": ai_result.get("new_address_printed") or "",
        "confidence": float(ai_result.get("confidence") or 0.0),
        "notice_date_iso": row["date_iso"], "subject": row["subject"],
        "reasoning": ai_result.get("reasoning"), "raw_model_response": ai_result,
    }
    if not is_change:
        return base                       # stored only so it isn't asked about again
    analysis = matcher.analyze_change(
        row["email_address_used"], ai_result.get("recipient_role"),
        ai_result.get("old_address_printed"), ai_result.get("new_address_printed"),
        known, ai_result.get("change_type") or "changed")
    base.update({
        "recipient_role": analysis.recipient_role,
        "old_candidates": analysis.old_candidates, "new_candidates": analysis.new_candidates,
        "likely_old_address": analysis.likely_old, "likely_new_address": analysis.likely_new,
        "match_quality": analysis.match_quality, "summary": analysis.summary,
    })
    return base


def detect_changes(conn, provider: "ai.AIProvider", extra_known_addresses=(),
                   batch_size: int = 5, control=None, depth=None) -> dict:
    """Examine every not-yet-examined change notice. `extra_known_addresses` are
    the configured mailbox addresses (they count as 'known' even if no scanned
    email happened to be addressed to them)."""
    depth = run_options.get_depth(depth)
    pending = db.change_notices_pending(conn)
    if not pending:
        print("No email-change notices to examine.")
        jobcontrol.report(control, 1, 1)
        return {"notices": 0, "changes_found": 0, "requests": 0}

    known = db.known_addresses(conn) | {a.strip().lower() for a in extra_known_addresses if a}
    found = 0

    def save(batch: list, results: list) -> int:
        """Store the answers; return how many notices received one."""
        nonlocal found
        by_id = {}
        for r in results:
            try:
                by_id[int(r.get("email_id"))] = r
            except (TypeError, ValueError):
                continue
        answered = 0
        for row in batch:
            r = by_id.get(row["id"])
            if r is None:
                continue                  # unanswered: stays pending, asked again next run
            record = _record_for(row, r, known)
            db.save_email_change(conn, record)
            found += bool(record["is_email_change"])
            answered += 1
        return answered

    stats = ai_batches.run_batches(
        provider, pending, batch_size=batch_size, build_message=lambda b: build_user_message(b, depth),
        tool=CHANGE_TOOL, essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, system_prompt=SYSTEM_PROMPT,
        max_tokens=MAX_TOKENS, save=save, noun="email-change notices", verb="examine", done="examined",
        control=control)
    print(f"{found} possible email change(s) found.")
    return {"notices": len(pending), "changes_found": found, "requests": stats["requests"]}
