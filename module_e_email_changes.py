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

import ai_providers as ai
import db
import jobcontrol
import migration_matcher as matcher
from prompts.email_change_prompt import CHANGE_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS, SYSTEM_PROMPT

SNIPPET_CHARS_SENT = 800       # masked addresses can sit a little way into the body
MAX_TOKENS = 2500
ABORT_AFTER_FAILED_BATCHES = 3


def _one_line(text, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def build_user_message(batch: list) -> str:
    lines = []
    for row in batch:
        lines.append(f"### EMAIL id={row['id']}")
        lines.append(f"from: {_one_line(row['sender_raw'], 120)}")
        lines.append(f"date: {row['date_iso'] or row['date_raw'] or 'unknown'}")
        lines.append(f'subject: "{_one_line(row["subject"], 200)}"')
        lines.append(f"body_snippet: {_one_line(row['body_snippet'], SNIPPET_CHARS_SENT)}")
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
                   batch_size: int = 5, control=None) -> dict:
    """Examine every not-yet-examined change notice. `extra_known_addresses` are
    the configured mailbox addresses (they count as 'known' even if no scanned
    email happened to be addressed to them)."""
    pending = db.change_notices_pending(conn)
    if not pending:
        print("No email-change notices to examine.")
        jobcontrol.report(control, 1, 1)
        return {"notices": 0, "changes_found": 0, "requests": 0}

    known = db.known_addresses(conn) | {a.strip().lower() for a in extra_known_addresses if a}
    batch_size = max(1, int(batch_size))
    total_batches = -(-len(pending) // batch_size)
    where = "on this computer" if provider.is_local else "over the internet"
    print(f"{len(pending)} possible email-change notices to examine "
          f"(~{total_batches} requests, sent {where}).")
    jobcontrol.report(control, 0, total_batches, f"0 of {total_batches} requests")

    found = requests = consecutive_failures = 0
    for start in range(0, len(pending), batch_size):
        jobcontrol.check(control)
        batch = pending[start:start + batch_size]
        requests += 1
        try:
            results = provider.structured_call(
                SYSTEM_PROMPT, build_user_message(batch), CHANGE_TOOL,
                essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, max_tokens=MAX_TOKENS)
        except ai.ProviderFormatError as exc:
            consecutive_failures += 1
            print(f"  batch {requests}/{total_batches} skipped: {exc}")
            jobcontrol.report(control, requests, total_batches, f"{requests} of {total_batches} requests")
            if consecutive_failures >= ABORT_AFTER_FAILED_BATCHES:
                raise ai.ProviderError(
                    f"The AI failed {ABORT_AFTER_FAILED_BATCHES} batches in a row, so I stopped. "
                    f"Try a smaller batch size or a more capable model. "
                    f"Everything finished so far is kept.") from None
            continue
        consecutive_failures = 0

        by_id = {}
        for r in results:
            try:
                by_id[int(r.get("email_id"))] = r
            except (TypeError, ValueError):
                continue
        for row in batch:
            r = by_id.get(row["id"])
            if r is None:
                continue                  # unanswered: stays pending, asked again next run
            record = _record_for(row, r, known)
            db.save_email_change(conn, record)
            found += bool(record["is_email_change"])
        print(f"  request {requests}/{total_batches} done")
        jobcontrol.report(control, requests, total_batches, f"{requests} of {total_batches} requests")

    print(f"{found} possible email change(s) found.")
    return {"notices": len(pending), "changes_found": found, "requests": requests}
