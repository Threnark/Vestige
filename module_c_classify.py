"""
module_c_classify.py
--------------------
Module C: asks the configured AI (cloud OR local - this file doesn't care which)
whether each group of candidate emails is evidence of a real account sign-up.

What is sent to the AI, and nothing more, for every group:
    the sender domain, and for up to 3 sample emails: date, subject and the first
    SNIPPET_CHARS_SENT characters of the body text.
Your email addresses, the full emails and your other mail are never sent.

Cost / time control, in order of effect:
  1. Module B already discarded most mail before this runs.
  2. One request judges a whole sender domain, not every email.
  3. Several groups share one request (batch_size).
  4. Groups already classified are skipped, so re-running only does new work.
  5. A failed batch never loses finished work: results are stored after every
     batch (and, in write mode, saved to disk), so you can simply run it again.
"""

from __future__ import annotations

import ai_providers as ai
import db
import jobcontrol
from module_b_prefilter import build_groups
from prompts.classification_prompt import CLASSIFY_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS, SYSTEM_PROMPT

MAX_SAMPLES_PER_GROUP = 3
SNIPPET_CHARS_SENT = 400
MAX_TOKENS = 3000
ABORT_AFTER_FAILED_BATCHES = 3       # stop wasting time/money if the model keeps failing


def _one_line(text, limit: int) -> str:
    """Flatten whitespace so untrusted text can't break the prompt's layout."""
    return " ".join((text or "").split())[:limit]


def build_user_message(batch: list) -> str:
    """Render a batch of groups as the plain-text prompt body."""
    lines = []
    for group in batch:
        tag = (" (identity-provider notification: the real app name is in the text)"
               if group.is_identity_provider else "")
        lines.append(f"### GROUP: {group.key}{tag}")
        for idx, row in enumerate(group.samples[:MAX_SAMPLES_PER_GROUP]):
            date = row["date_iso"] or row["date_raw"] or "unknown date"
            lines.append(f'  [{idx}] date={date} subject="{_one_line(row["subject"], 200)}"')
            snippet = _one_line(row["body_snippet"], SNIPPET_CHARS_SENT)
            if snippet:
                lines.append(f"      body_snippet: {snippet}")
        lines.append("")
    return "\n".join(lines)


def _save_batch(conn, batch: list, results: list) -> int:
    """Store the answers; return how many groups received an answer."""
    by_key = {str(r.get("group_id", "")).strip(): r for r in results}
    answered = 0
    for group in batch:
        r = by_key.get(group.key)
        if r is None:
            # No answer for this group. Deliberately NOT stored: it will simply
            # be asked again next run, rather than hiding behind a placeholder.
            continue
        idx = r.get("representative_sample_index", 0)
        rep = group.samples[idx] if isinstance(idx, int) and 0 <= idx < len(group.samples) else group.samples[0]
        db.save_classification(conn, {
            "group_key": group.key,
            "sender_domain": group.sender_domain,
            "is_signup": bool(r.get("is_signup")),
            "service_name": (r.get("service_name") or "").strip() or None,
            "category": r.get("category"),
            "signup_method": r.get("signup_method"),
            "confidence": float(r.get("confidence") or 0.0),
            "reasoning": r.get("reasoning"),
            "representative_email_id": rep["id"],
            "signup_date_iso": rep["date_iso"],
            "raw_model_response": r,
        })
        answered += 1
    return answered


def classify_all(conn, provider: "ai.AIProvider", batch_size: int = 6,
                 max_groups: int | None = None, control=None) -> dict:
    groups = build_groups(conn, MAX_SAMPLES_PER_GROUP)
    if max_groups:
        groups = groups[:max_groups]
    if not groups:
        print("Nothing to classify - no new candidate senders.")
        jobcontrol.report(control, 1, 1)
        return {"groups": 0, "answered": 0, "requests": 0, "failed_batches": 0}

    batch_size = max(1, int(batch_size))
    total_batches = -(-len(groups) // batch_size)
    where = "on this computer" if provider.is_local else "over the internet"
    print(f"{len(groups)} candidate groups to classify, {batch_size} per request "
          f"(~{total_batches} requests, sent {where}).")
    jobcontrol.report(control, 0, total_batches, f"0 of {total_batches} requests")

    answered = requests = failed = consecutive_failures = 0
    for start in range(0, len(groups), batch_size):
        jobcontrol.check(control)
        batch = groups[start:start + batch_size]
        requests += 1
        try:
            results = provider.structured_call(
                SYSTEM_PROMPT, build_user_message(batch), CLASSIFY_TOOL,
                essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, max_tokens=MAX_TOKENS)
        except ai.ProviderFormatError as exc:
            # The model answered but couldn't produce usable output. Skip this
            # batch (it will be retried next run) and carry on.
            failed += 1
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
        got = _save_batch(conn, batch, results)
        answered += got
        usage = ""
        if provider.last_usage and provider.last_usage[0] is not None:
            usage = f" ({provider.last_usage[0]} tokens in / {provider.last_usage[1]} out)"
        note = f", {len(batch) - got} unanswered" if got < len(batch) else ""
        print(f"  request {requests}/{total_batches}: {got}/{len(batch)} groups classified{note}{usage}")
        jobcontrol.report(control, requests, total_batches, f"{requests} of {total_batches} requests")

    if answered < len(groups):
        print(f"{len(groups) - answered} group(s) got no answer; run this stage again to retry them.")
    return {"groups": len(groups), "answered": answered, "requests": requests, "failed_batches": failed}
