"""
module_f_closures.py
--------------------
Module F: closed or deactivated accounts.

HOW IT WORKS - three steps, each doing only what it is good at
  1. FREE RULES (module_b_prefilter.py) flag emails whose subject looks like a closure notice
     ("Your account has been closed", "Sorry to see you go", ...). Nothing is sent anywhere.
  2. THE AI CONFIRMS ONLY THAT SHORTLIST. It is shown a few of the flagged emails per sender and
     says whether one is really a notice that the person's own account was closed, deactivated
     or deleted (and not marketing, a how-to, a warning, or a cancelled subscription).
  3. PLAIN CODE CHECKS FOR LATER MAIL. Is there any email from that sender dated after the
     notice? This is a question about dates, so the AI is not asked it. The answer is worked
     out whenever it is shown (never stored), so importing more mail updates it.

  The result keeps the service visible in the results; it only adds a note. Two words are used,
  and only for a notice the AI confirmed:
      "Likely closed"    a closure notice was found and nothing has arrived from the service since
      "Possibly closed"  a closure notice was found, but the service has emailed since (that may be
                         goodbye or marketing mail, or the account may have been reopened)

THE QUIET HINT - deliberately separate and weaker
  A service that has simply gone quiet is NOT evidence of a closed account (people stop getting
  mail for all sorts of reasons), so it is never called "closed". quiet_hint() gives a plain fact
  - "No mail since 2022-03-14" - only when that is a year or more before the newest mail that
  reached the same address, and it appears in its own column.

What is sent to the AI: for each flagged sender, the domain and up to a few flagged emails'
date, subject and a short excerpt. Never your addresses, other mail or full emails.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import ai_batches
import ai_providers as ai
import billing_math
import db
import jobcontrol
import run_options
from module_b_prefilter import IDENTITY_PROVIDER_DOMAINS, sender_groups
from prompts.closure_prompt import CLOSURE_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS, SYSTEM_PROMPT

MAX_TOKENS = 2000
LATER_MAIL_GRACE_DAYS = 7      # goodbye and confirmation mail right after a closure doesn't count as "later"
QUIET_DAYS = 365               # how long a service must have been silent before the quiet hint appears

VERDICT_LIKELY = "Likely closed"
VERDICT_POSSIBLE = "Possibly closed"
VERDICT_NOTICE = "Closure notice found"      # the later-mail check is not possible for this sender

KIND_LABELS = {"closed": "Closed", "deactivated": "Deactivated", "deleted": "Deleted",
               "scheduled": "Scheduled to close", "other": "Other"}
WHO_LABELS = {"user": "You", "service": "The service", "unclear": "Unclear"}


# ---------------------------------------------------------------------------
# Step 2: asking the AI
# ---------------------------------------------------------------------------

def body_ids_wanted(conn, depth=None) -> list:
    """The ids of the emails whose TEXT the closed-account question will show the AI: exactly the samples of
    the groups build_groups() makes, so what is downloaded and what is read can never disagree."""
    return [row["id"] for group in build_groups(conn, depth) for row in group.samples]


def build_groups(conn, depth=None) -> list:
    """The senders not yet checked, with the flagged emails to show the AI."""
    d = run_options.get_depth(depth)
    return sender_groups(db.closure_candidate_domains(conn), db.closure_done_keys(conn),
                         lambda domain, limit=None: db.closure_candidate_emails(conn, domain, limit),
                         d.closure_samples)


_one_line = ai_batches.one_line        # flattens untrusted email text before it goes into the prompt


def build_user_message(batch: list, depth=None) -> str:
    d = run_options.get_depth(depth)
    lines = []
    for group in batch:
        lines.append(f"### GROUP id={group.key}")
        lines.append(f"sender: {group.sender_domain}")
        for number, row in enumerate(group.samples[:d.closure_samples], start=1):
            when = (row["date_iso"] or "")[:10] or "unknown date"
            lines.append(f'[{number}] {when} | "{_one_line(row["subject"], 120)}" | '
                         f'{_one_line(row["body_snippet"], d.closure_chars)}')
        lines.append("")
    return "\n".join(lines)


def _save_batch(conn, batch: list, results: list) -> int:
    by_id = {str(r.get("group_id")): r for r in results}
    saved = 0
    for group in batch:
        r = by_id.get(group.key)
        if r is None:
            continue                       # unanswered: stays unchecked, asked again next run
        is_closure = bool(r.get("is_closure"))
        shown = group.samples                    # build_groups() already limited these to what the AI was shown
        notice = None
        if is_closure:
            number = int(r.get("sample") or 0)
            notice = shown[number - 1] if 1 <= number <= len(shown) else shown[0]
        db.save_closure_check(conn, {
            "group_key": group.key, "sender_domain": group.sender_domain, "is_closure": is_closure,
            "service_name": (r.get("service_name") or "").strip() or group.sender_domain,
            "kind": r.get("kind"), "initiated_by": r.get("who"),
            "notice_email_id": notice["id"] if notice else None,
            "notice_date_iso": notice["date_iso"] if notice else None,
            "confidence": float(r.get("confidence") or 0.0), "raw_model_response": r})
        saved += 1
    return saved


def check_closed(conn, provider: "ai.AIProvider", batch_size: int = 8, max_groups: int | None = None,
                 control=None, depth=None) -> dict:
    """Ask the AI about every flagged sender not yet checked. Results are saved after every request."""
    d = run_options.get_depth(depth)
    groups = build_groups(conn, d)
    if max_groups:
        groups = groups[:max_groups]
    ai_batches.note_missing_text(row for group in groups for row in group.samples)
    if not groups:
        print("Nothing to check - no new closure-style notices.")
        jobcontrol.report(control, 1, 1)
        return {"groups": 0, "answered": 0, "requests": 0, "failed_batches": 0, "closures_found": 0}
    before = db.counts(conn)["closed_accounts_found"]
    stats = ai_batches.run_batches(
        provider, groups, batch_size=batch_size, build_message=lambda b: build_user_message(b, d),
        tool=CLOSURE_TOOL, essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, system_prompt=SYSTEM_PROMPT,
        max_tokens=MAX_TOKENS, save=lambda b, r: _save_batch(conn, b, r), noun="senders", control=control)
    stats["closures_found"] = db.counts(conn)["closed_accounts_found"] - before
    print(f"{stats['closures_found']} closure notice(s) confirmed.")
    return stats


# ---------------------------------------------------------------------------
# Step 3: plain code looks for later mail (worked out when shown, never stored)
# ---------------------------------------------------------------------------

@dataclass
class ClosureRecord:
    key: str
    service: str
    domain: str
    kind: str                    # closed | deactivated | deleted | scheduled | other
    who: str                     # user | service | unclear
    notice_date: str             # 'YYYY-MM-DD'
    subject: str
    confidence: float
    verdict: str                 # VERDICT_*
    later_count: int
    later_latest: str            # 'YYYY-MM-DD' of the newest later email, or ''
    note: str                    # plain sentences saying what was found
    address: str = ""            # the address the notice was sent to ('' if unknown)


def _verdict(conn, row) -> tuple:
    """(verdict, later_count, later_latest_day, note) for one confirmed closure notice."""
    notice_day = (row["notice_date_iso"] or "")[:10]
    scheduled = row["kind"] == "scheduled"
    if "#" in row["group_key"] or not notice_day:
        why = ("this notice came from a sign-in service that emails about many apps"
               if "#" in row["group_key"] else "the notice has no date")
        return VERDICT_NOTICE, 0, "", f"A closure notice was found, but later mail cannot be checked because {why}."
    cutoff = (billing_math.to_date(notice_day) + timedelta(days=LATER_MAIL_GRACE_DAYS)).isoformat()
    count, newest = db.later_mail(conn, row["sender_domain"], cutoff)
    pending = " The notice says closure was only scheduled, so it may not have happened." if scheduled else ""
    if count == 0:
        return (VERDICT_POSSIBLE if scheduled else VERDICT_LIKELY, 0, "",
                f"A closure notice is dated {notice_day}, and nothing has arrived from this service "
                f"since (in the mail Vestige could see).{pending}")
    newest_day = (newest or "")[:10]
    return (VERDICT_POSSIBLE, count, newest_day,
            f"A closure notice is dated {notice_day}, but {count} later email{'s' if count != 1 else ''} "
            f"arrived from this service, the newest on {newest_day}. They may be goodbye or marketing "
            f"mail, or the account may have been reopened.{pending}")


def closure_records(conn, min_confidence: float = 0.0) -> list:
    """Every confirmed closure notice with its later-mail verdict, newest notice first."""
    records = []
    for row in db.closure_rows(conn, min_confidence):
        verdict, count, newest, note = _verdict(conn, row)
        subject = address = ""
        if row["notice_email_id"]:
            found = conn.execute("SELECT subject, email_address_used FROM emails WHERE id=?",
                                 (row["notice_email_id"],)).fetchone()
            subject, address = (found["subject"], found["email_address_used"] or "") if found else ("", "")
        records.append(ClosureRecord(
            key=row["group_key"], service=row["service_name"] or row["sender_domain"], domain=row["sender_domain"],
            kind=row["kind"] or "other", who=row["initiated_by"] or "unclear",
            notice_date=(row["notice_date_iso"] or "")[:10], subject=subject or "",
            confidence=row["confidence"] or 0.0, verdict=verdict, later_count=count, later_latest=newest, note=note,
            address=address))
    return records


# ---------------------------------------------------------------------------
# The quiet hint: weaker, separate, and never called "closed"
# ---------------------------------------------------------------------------

class ActivityIndex:
    """Answers "when did this service last email this address?" for every row of the Sign-ups
    table from two queries made once, not one query per row."""

    def __init__(self, conn):
        self._by_pair = db.newest_mail_by_sender_and_address(conn)
        self._by_address = db.newest_mail_by_address(conn)

    def last_mail(self, domains, address: str) -> str:
        """'YYYY-MM-DD' of the newest email from any of `domains` to `address`, or ''. Identity
        providers are skipped: they send mail about many other apps, so their silence says
        nothing about this one."""
        address = (address or "").lower()
        newest = ""
        for domain in domains:
            if domain in IDENTITY_PROVIDER_DOMAINS:
                continue
            newest = max(newest, self._by_pair.get((domain, address)) or "")
        return newest[:10]

    def hint(self, last_mail_day: str, address: str) -> str:
        """'No mail since YYYY-MM-DD' when the service's last mail to this address is at least
        QUIET_DAYS older than the newest mail that reached the same address, else ''."""
        last = billing_math.to_date(last_mail_day)
        end = billing_math.to_date(self._by_address.get((address or "").lower()))
        if last and end and (end - last).days >= QUIET_DAYS:
            return f"No mail since {last.isoformat()}"
        return ""
