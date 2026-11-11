"""
module_c_classify.py
--------------------
Module C: asks the configured AI (cloud OR local - this file doesn't care which)
whether each group of candidate emails is evidence of a real account sign-up.

What is sent to the AI, and nothing more, for every group:
    the sender domain, and for a few sample emails: date, subject and the first
    few hundred characters of the body text. How many samples and how many characters is
    the DEPTH setting (run_options.py); the default, Balanced, is 3 emails of 400 characters.
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
import run_options
import ai_batches
from module_b_prefilter import build_groups
from prompts.classification_prompt import CLASSIFY_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS, SYSTEM_PROMPT

# The default (Balanced) depth. Other depths are in run_options.py.
MAX_SAMPLES_PER_GROUP = run_options.BALANCED.signup_samples
SNIPPET_CHARS_SENT = run_options.BALANCED.signup_chars
MAX_TOKENS = 3000
_one_line = ai_batches.one_line        # flattens untrusted email text before it goes into the prompt


def build_user_message(batch: list, depth=None) -> str:
    """Render a batch of groups as the plain-text prompt body. `depth` (run_options.py) sets
    how many sample emails and how many characters of each are included."""
    depth = run_options.get_depth(depth)
    lines = []
    for group in batch:
        tag = (" (identity-provider notification: the real app name is in the text)"
               if group.is_identity_provider else "")
        lines.append(f"### GROUP: {group.key}{tag}")
        for idx, row in enumerate(group.samples[:depth.signup_samples]):
            date = row["date_iso"] or row["date_raw"] or "unknown date"
            lines.append(f'  [{idx}] date={date} subject="{_one_line(row["subject"], 200)}"')
            snippet = _one_line(row["body_snippet"], depth.signup_chars)
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
                 max_groups: int | None = None, control=None, depth=None) -> dict:
    depth = run_options.get_depth(depth)
    groups = build_groups(conn, depth.signup_samples)
    if max_groups:
        groups = groups[:max_groups]
    ai_batches.note_missing_text(row for group in groups for row in group.samples)
    if not groups:
        print("Nothing to classify - no new candidate senders.")
        jobcontrol.report(control, 1, 1)
        return {"groups": 0, "answered": 0, "requests": 0, "failed_batches": 0}

    return ai_batches.run_batches(
        provider, groups, batch_size=batch_size, build_message=lambda b: build_user_message(b, depth),
        tool=CLASSIFY_TOOL, essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, system_prompt=SYSTEM_PROMPT,
        max_tokens=MAX_TOKENS, save=lambda b, r: _save_batch(conn, b, r),
        noun="groups", verb="classify", done="classified", control=control)
