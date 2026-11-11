"""
module_g_subscriptions.py
-------------------------
Module G: subscriptions, what they cost, and whether they still seem to be running.

THE SPLIT OF WORK (the same philosophy as the rest of Vestige: the AI reads, code counts)
  1. FREE RULES (module_b_prefilter.py) flag billing emails and give each a kind:
     charge / cancel / failed / trial / other. No AI, nothing sent anywhere.
  2. CODE GROUPS them by sender and works out, from the dates alone: how many charges, how often
     they come (the cycle), the last charge, and whether a cancellation notice exists
     (billing_math.py). Senders that look like a one-off shop (irregular receipts, no mention of
     a subscription or plan) are dropped here, so they cost no AI tokens at all.
  3. THE AI CONFIRMS THE SHORTLIST. Per sender it is given those code-made facts plus a few chosen
     emails (usually the latest receipt and the latest notice), and says: is this really a
     recurring subscription, what is the plan called, what do the emails say about its status, and
     WHICH OF THE NUMBERED AMOUNTS is the recurring charge. It cannot write a price; it can only
     point at a number that code found in the email.
  4. CODE DECIDES THE STATUS AND ADDS UP THE COST. Active / Possibly stopped / Cancelled / Trial /
     Unclear is judged from the dates (billing_math.judge_state), measured against the newest mail
     in the same mailbox rather than today. Monthly and yearly totals are plain addition. All of
     this is worked out when shown, never stored, so importing more mail updates it.

PAYMENT PROCESSORS (Apple, Google Play, PayPal, Stripe ...)
  One sender covers many different services, so grouping by sender would mix them up. Their
  receipts are grouped by sender AND amount instead (all the 9.99 charges together), which keeps a
  repeating charge together. Over IMAP only the newest few receipts are downloaded (see
  run_options.Depth.processor_receipts), because each one has to be opened to read its amount.

What is sent to the AI: for each shortlisted sender, the sender domain, the code-made facts and a
few emails' date, kind, subject, a short excerpt and (for one of them) the numbered amounts.
Never your addresses, other mail or full emails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

import ai_batches
import ai_providers as ai
import billing_math as bm
import db
import jobcontrol
import run_options
from prompts.subscription_prompt import ENUM_FALLBACKS, ESSENTIAL_KEYS, SUBSCRIPTION_TOOL, SYSTEM_PROMPT

MAX_TOKENS = 2500

# Senders whose receipts are for MANY services. Add more here if your mailbox has them.
PAYMENT_PROCESSOR_DOMAINS = {
    "email.apple.com", "apple.com", "itunes.com", "paypal.com", "paypal.co.uk", "stripe.com",
    "paddle.com", "google.com", "microsoft.com", "email.microsoft.com", "gumroad.com",
    "fastspring.com", "2checkout.com", "squareup.com", "braintreepayments.com",
}


def _amounts(row) -> list:
    try:
        return json.loads(row["amounts_json"]) if row["amounts_json"] else []
    except (TypeError, ValueError):
        return []


def _group_key(row):
    """The sender domain, or for a payment processor "<domain>~<amount>". None when a processor's
    email has no readable amount (it can't be told apart from other services)."""
    domain = row["sender_domain"]
    if domain not in PAYMENT_PROCESSOR_DOMAINS:
        return domain
    amounts = _amounts(row)
    index = bm.main_amount_index(amounts)
    if index < 0:
        return None
    return f"{domain}~{amounts[index]['currency']}{amounts[index]['value']:.2f}"


# ---------------------------------------------------------------------------
# Steps 1-2: grouping and the facts code works out
# ---------------------------------------------------------------------------

@dataclass
class BillGroup:
    key: str
    sender_domain: str
    rows: list = field(default_factory=list)        # billing emails, oldest first

    def finish(self) -> None:
        self.charges = [r for r in self.rows if r["billing_kind"] == "charge"]
        self.cancels = [r for r in self.rows if r["billing_kind"] == "cancel"]
        self.failed = [r for r in self.rows if r["billing_kind"] == "failed"]
        self.trials = [r for r in self.rows if r["billing_kind"] == "trial"]
        self.cadence = bm.compute_cadence([bm.to_date(r["date_iso"]) for r in self.charges])
        self.has_marker = any("sub" in (r["billing_signal"] or "").split(";") for r in self.rows)

    @property
    def last_charge(self):
        days = [d for d in (bm.to_date(r["date_iso"]) for r in self.charges) if d]
        return max(days) if days else None

    @property
    def cancel_date(self):
        days = [d for d in (bm.to_date(r["date_iso"]) for r in self.cancels) if d]
        return max(days) if days else None

    def worth_asking(self) -> bool:
        """Is this sender worth the AI's time? Yes if it has sent a cancellation, a repeating
        pattern of charges, or any email that talks about a subscription/plan as such. A shop
        that sends the occasional irregular receipt is not."""
        return bool(self.cancels or (self.cadence.known and self.cadence.charges >= 2) or self.has_marker)

    def sample_rows(self, depth) -> list:
        """The few emails the AI is shown. Sample [1] is the latest charge (its amounts are listed);
        then the latest notice (cancellation first, then failed payment, then trial); then the
        earliest charge, to show a change of plan. Never more than the depth allows."""
        picks: list = []

        def add(row):
            if row is not None and all(row["id"] != p["id"] for p in picks):
                picks.append(row)

        add(self.charges[-1] if self.charges else None)
        for notices in (self.cancels, self.failed, self.trials):
            if notices:
                add(notices[-1])
                break
        add(self.charges[0] if len(self.charges) > 1 else None)
        add(self.rows[-1])
        return picks[:max(1, run_options.get_depth(depth).billing_samples)]


def collect_groups(conn) -> dict:
    """{group key: BillGroup} for every billing email in the database."""
    groups: dict = {}
    for row in db.billing_emails(conn):
        key = _group_key(row)
        if key is None:
            continue
        groups.setdefault(key, BillGroup(key, row["sender_domain"])).rows.append(row)
    for group in groups.values():
        group.finish()
    return groups


def body_ids_wanted(conn, depth=None) -> list:
    """Ids of the emails whose BODY the Subscriptions pipeline needs downloaded (IMAP only; file
    imports already have their text). Far fewer than all the billing emails: only the sample
    emails of senders worth asking about, plus the newest receipts of payment processors."""
    d = run_options.get_depth(depth)
    done = db.subscription_done_keys(conn)
    ids = []
    for group in collect_groups(conn).values():
        if group.key not in done and group.worth_asking():
            ids += [r["id"] for r in group.sample_rows(d)]
    by_domain: dict = {}
    for row in db.billing_emails(conn):
        if row["sender_domain"] in PAYMENT_PROCESSOR_DOMAINS and row["billing_kind"] == "charge":
            by_domain.setdefault(row["sender_domain"], []).append(row["id"])
    for rows in by_domain.values():
        ids += rows[-d.processor_receipts:]
    return ids


# ---------------------------------------------------------------------------
# Step 3: asking the AI
# ---------------------------------------------------------------------------

def _facts_line(group: BillGroup) -> str:
    parts = [f"charges: {len(group.charges)}", f"cycle: {group.cadence.label}",
             f"last charge: {bm.day(group.last_charge) or 'none seen'}"]
    notices = []
    for label, rows in (("cancel", group.cancels), ("failed payment", group.failed), ("trial", group.trials)):
        if rows:
            notices.append(f"{label} {(rows[-1]['date_iso'] or '')[:10]}")
    return ", ".join(parts) + ("; notices: " + ", ".join(notices) if notices else "")


_one_line = ai_batches.one_line        # flattens untrusted email text before it goes into the prompt


def build_user_message(batch: list, depth=None) -> str:
    d = run_options.get_depth(depth)
    lines = []
    for group in batch:
        lines.append(f"### GROUP id={group.key}")
        lines.append(f"sender: {group.sender_domain}")
        lines.append(_facts_line(group))
        for number, row in enumerate(group.sample_rows(d), start=1):
            lines.append(f'[{number}] {(row["date_iso"] or "")[:10] or "unknown date"} {row["billing_kind"]} | '
                         f'"{_one_line(row["subject"], 100)}" | {_one_line(row["body_snippet"], d.billing_chars)}')
            if number == 1 and _amounts(row):
                lines.append(f"    amounts: {bm.amount_line(_amounts(row))}")
        lines.append("")
    return "\n".join(lines)


def _save_batch(conn, batch: list, results: list, depth) -> int:
    by_id = {str(r.get("group_id")): r for r in results}
    saved = 0
    for group in batch:
        r = by_id.get(group.key)
        if r is None:
            continue                       # unanswered: stays unchecked, asked again next run
        first = group.sample_rows(depth)[0]
        amounts = _amounts(first)
        number = int(r.get("amount") or 0)
        chosen = amounts[number - 1] if 1 <= number <= len(amounts) else None
        db.save_subscription(conn, {
            "group_key": group.key, "sender_domain": group.sender_domain,
            "is_subscription": bool(r.get("is_subscription")),
            "service_name": (r.get("service_name") or "").strip() or group.sender_domain,
            "plan_name": (r.get("plan_name") or "").strip(), "ai_status": r.get("status"),
            "stated_period": r.get("period"), "amount_email_id": first["id"] if chosen else None,
            "amount_value": chosen["value"] if chosen else None,
            "amount_currency": chosen["currency"] if chosen else None,
            "amount_label": chosen["label"] if chosen else None,
            "confidence": float(r.get("confidence") or 0.0), "raw_model_response": r})
        saved += 1
    return saved


def find_subscriptions(conn, provider: "ai.AIProvider", batch_size: int = 8, max_groups: int | None = None,
                       control=None, depth=None) -> dict:
    """Ask the AI about every shortlisted billing sender not yet checked. Saved after every request."""
    d = run_options.get_depth(depth)
    done = db.subscription_done_keys(conn)
    groups = [g for g in collect_groups(conn).values() if g.key not in done and g.worth_asking()]
    groups.sort(key=lambda g: g.key)
    if max_groups:
        groups = groups[:max_groups]
    if not groups:
        print("Nothing to check - no new billing senders that look like subscriptions.")
        jobcontrol.report(control, 1, 1)
        return {"groups": 0, "answered": 0, "requests": 0, "failed_batches": 0, "subscriptions_found": 0}
    before = db.counts(conn)["subscriptions_found"]
    stats = ai_batches.run_batches(
        provider, groups, batch_size=batch_size, build_message=lambda b: build_user_message(b, d),
        tool=SUBSCRIPTION_TOOL, essential=ESSENTIAL_KEYS, fallbacks=ENUM_FALLBACKS, system_prompt=SYSTEM_PROMPT,
        max_tokens=MAX_TOKENS, save=lambda b, r: _save_batch(conn, b, r, d), noun="senders", control=control)
    stats["subscriptions_found"] = db.counts(conn)["subscriptions_found"] - before
    print(f"{stats['subscriptions_found']} subscription(s) confirmed.")
    return stats


# ---------------------------------------------------------------------------
# Step 4: status and totals, worked out by code when shown
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AddressUse:
    """How one of your addresses figures in a sender's billing emails."""
    address: str
    charges: int                      # charge receipts that reached this address
    last_charge: date | None          # the newest of them
    last_seen: date | None            # the newest billing email of any kind (a cancellation, a failed payment...)


@dataclass
class SubRecord:
    key: str
    service: str
    plan: str
    domain: str
    state: str                       # billing_math.STATE_*
    note: str                        # plain sentences saying why
    amount: float | None
    currency: str
    cycle: str                       # "monthly", "yearly", ... or "unknown"
    cycle_source: str                # "observed" | "stated" | ""
    period_days: float | None
    charges: int
    last_charge: date | None
    next_expected: date | None
    per_month: float | None
    per_year: float | None
    confidence: float
    last_subject: str
    ai_status: str
    by_address: tuple = ()           # one AddressUse for each of your addresses that got this sender's billing emails

    @property
    def addresses(self) -> tuple:
        return tuple(use.address for use in self.by_address)


_STATE_ORDER = {bm.STATE_ACTIVE: 0, bm.STATE_STOPPED: 1, bm.STATE_UNCLEAR: 2, bm.STATE_TRIAL: 3, bm.STATE_CANCELLED: 4}


def _current_amount(row, group: BillGroup) -> tuple:
    """(value, currency, label) of the recurring charge. It is the amount the AI chose; but if a
    newer receipt has arrived since (a price rise), the same kind of amount from the newest receipt
    is used instead, so the figure matches the last charge date shown."""
    if row["amount_value"] is None:
        return None, "", ""
    value, currency, label = row["amount_value"], row["amount_currency"] or "", row["amount_label"] or ""
    newest = group.charges[-1] if group.charges else None
    if newest is not None and newest["id"] != row["amount_email_id"]:
        for a in _amounts(newest):
            if a["currency"] == currency and a["label"] == label:
                return a["value"], currency, label
    return value, currency, label


def _address_uses(group: BillGroup) -> tuple:
    """Each of your addresses that received this sender's billing emails, in alphabetical order."""
    uses = []
    for address in sorted({r["email_address_used"] for r in group.rows if r["email_address_used"]}):
        rows = [r for r in group.rows if r["email_address_used"] == address]
        charge_days = [bm.to_date(r["date_iso"]) for r in rows if r["billing_kind"] == "charge"]
        seen_days = [bm.to_date(r["date_iso"]) for r in rows]
        uses.append(AddressUse(address=address, charges=len(charge_days),
                               last_charge=max((d for d in charge_days if d), default=None),
                               last_seen=max((d for d in seen_days if d), default=None)))
    return tuple(uses)


def subscription_records(conn, min_confidence: float = 0.0) -> list:
    """Every confirmed subscription with its code-judged status, Active ones first."""
    groups = collect_groups(conn)
    ends = db.mailbox_ends(conn)
    records = []
    for row in db.subscription_rows(conn, min_confidence):
        group = groups.get(row["group_key"])
        if group is None:
            continue
        cadence = bm.with_stated_period(group.cadence, row["stated_period"] or "")
        newest = max((bm.to_date(ends.get(r["account_name"])) for r in group.rows
                      if bm.to_date(ends.get(r["account_name"]))), default=None)
        verdict = bm.judge_state(last_charge=group.last_charge, cadence=cadence, mailbox_end=newest,
                                 cancel_date=group.cancel_date, ai_status=row["ai_status"] or "unclear")
        value, currency, _label = _current_amount(row, group)
        last_subject = (group.charges[-1] if group.charges else group.rows[-1])["subject"] or ""
        records.append(SubRecord(
            key=row["group_key"], service=row["service_name"] or row["sender_domain"], plan=row["plan_name"] or "",
            domain=row["sender_domain"], state=verdict.state, note=verdict.note, amount=value, currency=currency,
            cycle=cadence.label, cycle_source=cadence.source if cadence.known else "", period_days=cadence.period_days,
            charges=len(group.charges), last_charge=group.last_charge, next_expected=verdict.next_expected,
            per_month=bm.per_month(value, cadence.period_days), per_year=bm.per_year(value, cadence.period_days),
            confidence=row["confidence"] or 0.0, last_subject=last_subject, ai_status=row["ai_status"] or "",
            by_address=_address_uses(group)))
    records.sort(key=lambda r: (_STATE_ORDER.get(r.state, 9), r.service.lower()))
    return records


def totals(records: list) -> dict:
    """What the ACTIVE subscriptions add up to. Returns
       {"per_currency": {cur: (per_month, per_year)}, "counted": n, "uncounted_active": n,
        "possibly_stopped": n}
    Only subscriptions whose status is Active are added; a Possibly-stopped one is counted
    separately so it is visible but never inflates the figure."""
    active = [r for r in records if r.state == bm.STATE_ACTIVE]
    per_currency, skipped = bm.add_totals((r.amount, r.currency, r.period_days) for r in active)
    return {"per_currency": per_currency, "counted": len(active) - skipped, "uncounted_active": skipped,
            "possibly_stopped": sum(1 for r in records if r.state == bm.STATE_STOPPED)}


def totals_sentence(records: list) -> str:
    """One readable line for the Results page, the Markdown export and the command line."""
    t = totals(records)
    if not records:
        return ""
    if t["per_currency"]:
        text = (f"Active subscriptions ({t['counted']}): "
                + "   +   ".join(f"{bm.money(m, c)} / month  \u00b7  {bm.money(y, c)} / year"
                                 for c, (m, y) in sorted(t["per_currency"].items())))
    else:
        text = "No active subscription has both a known amount and a known cycle, so no total can be shown."
    extras = []
    if t["uncounted_active"]:
        extras.append(f"{t['uncounted_active']} active one(s) left out: amount or cycle unknown")
    if t["possibly_stopped"]:
        extras.append(f"{t['possibly_stopped']} possibly stopped, not counted")
    return text + (f"   ({'; '.join(extras)})" if extras else "")
