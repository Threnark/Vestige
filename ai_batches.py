"""
ai_batches.py
-------------
The request loop shared by all four AI steps (sign-ups, email changes, closed accounts and
subscriptions): send the items to the AI a few at a time, save what comes back after EVERY
request, skip a request the model could not answer (it is simply asked again next run), and stop
if the model keeps failing. Each step supplies only what differs: what to say, how to save an answer.

Also here: `one_line`, the helper that flattens untrusted email text before it goes into a prompt.
"""

from __future__ import annotations

import ai_providers as ai
import jobcontrol


def note_missing_text(rows) -> None:
    """Say so when emails about to be shown to the AI still have their text on the mail server, not yet
    downloaded. The download step only fetches the emails the AI will read at the depth chosen THEN, so
    if the depth was raised afterwards (or the download step was skipped) some of them have no text, and
    their sender would be judged from the subject line alone without anyone being told. `rows` are
    `emails` rows; mail that came from a file always has its text already."""
    rows = list(rows)
    missing = sum(1 for r in rows if r["uid"] and not r["body_fetched"])
    if missing:
        print(f"  NOTE: the text of {missing:,} of the {len(rows):,} emails to be read hasn't been downloaded, "
              f"so they will be judged from their subject lines alone. Run \"Download email text\" at this depth "
              f"first for better results.")

ABORT_AFTER_FAILED_BATCHES = 3       # stop wasting time/money if the model keeps failing


def one_line(text, limit: int) -> str:
    """Flatten whitespace so untrusted text can't break the prompt's layout, and cut it to `limit` characters."""
    return " ".join((text or "").split())[:limit]


def run_batches(provider: "ai.AIProvider", items: list, *, batch_size: int, build_message, tool: dict,
                essential: tuple, fallbacks: dict, system_prompt: str, max_tokens: int, save,
                noun: str, verb: str = "check", done: str = "checked", control=None) -> dict:
    """Ask the AI about `items` in batches.

    build_message(batch) -> the user message text
    save(batch, results) -> how many of the batch were answered (and stored)
    noun                  what the items are called in the log ("senders")
    verb, done            what is done to them, as the log says it: "to check" / "checked"
    """
    batch_size = max(1, int(batch_size))
    total_batches = -(-len(items) // batch_size)
    where = "on this computer" if provider.is_local else "over the internet"
    print(f"{len(items)} {noun} to {verb}, {batch_size} per request (~{total_batches} requests, sent {where}).")
    jobcontrol.report(control, 0, total_batches, f"0 of {total_batches} requests")

    answered = requests = failed = consecutive_failures = 0
    for start in range(0, len(items), batch_size):
        jobcontrol.check(control)
        batch = items[start:start + batch_size]
        requests += 1
        try:
            results = provider.structured_call(system_prompt, build_message(batch), tool,
                                               essential=essential, fallbacks=fallbacks, max_tokens=max_tokens)
        except ai.ProviderFormatError as exc:
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
        got = save(batch, results)
        answered += got
        usage = ""
        if provider.last_usage and provider.last_usage[0] is not None:
            usage = f" ({provider.last_usage[0]} tokens in / {provider.last_usage[1]} out)"
        note = f", {len(batch) - got} unanswered" if got < len(batch) else ""
        print(f"  request {requests}/{total_batches}: {got}/{len(batch)} {noun} {done}{note}{usage}")
        jobcontrol.report(control, requests, total_batches, f"{requests} of {total_batches} requests")

    if answered < len(items):
        print(f"{len(items) - answered} of the {noun} got no answer; run this stage again to retry them.")
    return {"groups": len(items), "answered": answered, "requests": requests, "failed_batches": failed}
