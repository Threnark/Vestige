"""
billing_math.py
---------------
The arithmetic behind the Subscriptions pipeline. Pure Python: no AI, no network, no database,
no GUI, so every rule here is predictable and easy to test.

WHO DOES WHAT (the philosophy)
  The AI is only asked things that need reading: "is this a recurring subscription?", "what is
  the plan called?", "what do the emails say about its status?", and "which of these numbered
  amounts is the recurring charge?". EVERYTHING that is arithmetic is done here, by code:

      * finding the amounts printed in an email              extract_amounts()
      * how often it is charged (the "cycle")                 compute_cadence()
      * the date of the last charge and the next expected one judge_state()
      * whether it still looks active                         judge_state()
      * the monthly and yearly totals                         add_totals()

  The AI never types a number of its own. It can only point at a NUMBERED amount that this file
  found in the email, so it cannot invent a price.

HONEST LIMITS
  * "Active" means "the emails fit a subscription that is still being charged", not "I checked
    with the company". A charge notice that was deleted, or that went to another mailbox, looks
    like a missed payment.
  * "Today" is never used. Everything is measured against the newest email in the same mailbox,
    so an old export (say a 2019 mailbox) is not wrongly reported as lapsed.
  * Amounts are added up per currency, exactly as printed. A bare "$" is kept as "$" because an
    email that only says "$" doesn't say which dollar it is, and a bare "\u00a5" likewise (it is the
    sign for both the yen and the yuan). An explicit code such as JPY or CNY is used as written.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Finding amounts in text
# ---------------------------------------------------------------------------
MAX_AMOUNTS = 8             # how many different amounts are kept (and shown to the AI) per email
MAX_SCAN_CHARS = 20000      # only the start of a long email is searched
MONTH_DAYS = 30.4375        # 365.25 / 12
YEAR_DAYS = 365.25

_ISO = "USD|EUR|GBP|NZD|AUD|CAD|CHF|JPY|CNY|INR|SGD|HKD|SEK|NOK|DKK|PLN|ZAR|MXN|BRL|KRW"
_SYMBOLS = {"nz$": "NZD", "au$": "AUD", "a$": "AUD", "us$": "USD", "ca$": "CAD", "c$": "CAD",
            "hk$": "HKD", "s$": "SGD", "r$": "BRL", "\u00a3": "GBP", "\u20ac": "EUR",
            "\u20b9": "INR", "$": "$", "\u00a5": "\u00a5"}      # "$" and "\u00a5" are ambiguous: kept as printed
_BARE_SYMBOLS = ("$", "\u00a5")

# 1,234.56  1.234,56  9,99  9.99  1 234,56 (thin/no-break space)  17
_NUM = r"\d{1,3}(?:[,.\u202f\u00a0]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_PRE = rf"(?P<pre>NZ\$|AU\$|US\$|CA\$|HK\$|R\$|A\$|C\$|S\$|{_ISO}|[\u00a3\u20ac\u00a5\u20b9$])\s?"
_POST = rf"\s?(?P<post>{_ISO}|[\u00a3\u20ac\u00a5\u20b9])(?![A-Za-z])"
_AMOUNT_RE = re.compile(
    rf"(?<![A-Za-z])(?:{_PRE}(?P<n1>{_NUM})|(?P<n2>{_NUM}){_POST})", re.IGNORECASE)

# What the words just BEFORE an amount say it is.
_LABEL_WORDS = re.compile(
    r"sub-?total|grand total|total|amount charged|amount paid|amount due|you paid|charged|billed|"
    r"paid|payment|tax|gst|vat|discount|credit|balance|price|plan|renew\w*", re.IGNORECASE)
_LABEL_MAP = {"subtotal": "subtotal", "sub-total": "subtotal", "tax": "tax", "gst": "tax", "vat": "tax",
              "discount": "discount", "credit": "credit", "balance": "balance",
              "price": "price", "plan": "price"}
_INCLUDING = re.compile(r"\(?\s*(?:incl|including|inc|excl|excluding)\.?\s*(?:tax|gst|vat)\s*\)?", re.IGNORECASE)
_NOT_THE_CHARGE = {"subtotal", "tax", "discount", "credit", "balance"}


def parse_number(text: str) -> float:
    """'1,234.56' -> 1234.56, '1.234,56' -> 1234.56, '9,99' -> 9.99, '1,234' -> 1234."""
    s = text.replace("\u202f", "").replace("\u00a0", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + "." + tail if len(tail) in (1, 2) else s.replace(",", "")
    elif "." in s:
        head, _, tail = s.rpartition(".")
        if len(tail) == 3 and head:                     # 1.234 or 12.345.678: dots are thousands
            s = s.replace(".", "")
    return float(s)


def _label_before(text: str, start: int, floor: int = 0) -> str:
    """The kind of amount that starts at `start`, from the words just before it (but never from
    words that belong to the previous amount, which end at `floor`)."""
    window = _INCLUDING.sub(" ", text[max(floor, start - 40):start])
    found = _LABEL_WORDS.findall(window)
    if not found:
        return ""
    word = found[-1].lower()                            # the closest word wins
    if word.startswith("renew"):
        word = "plan"
    return _LABEL_MAP.get(word, "total")                # total, amount paid, charged, billed ... all mean "total"


def extract_amounts(text, limit: int = MAX_AMOUNTS) -> list:
    """Every money amount printed in `text` (the start of it), in order of appearance, as
    [{"value": 17.99, "currency": "NZD", "label": "total"}, ...]. `label` is "" when the words
    before the amount don't say what it is. Identical amounts are listed once; zero is ignored."""
    if not text:
        return []
    text = text[:MAX_SCAN_CHARS]
    found, seen = [], set()
    previous_end = 0
    for match in _AMOUNT_RE.finditer(text):
        raw_cur = (match.group("pre") or match.group("post") or "").lower()
        currency = _SYMBOLS.get(raw_cur, raw_cur.upper())
        try:
            value = round(parse_number(match.group("n1") or match.group("n2")), 2)
        except ValueError:
            continue
        if value <= 0 or value > 10_000_000:
            continue
        label = _label_before(text, match.start(), previous_end)
        previous_end = match.end()
        key = (value, currency, label)
        if key in seen:
            continue
        seen.add(key)
        found.append({"value": value, "currency": currency, "label": label})
        if len(found) >= limit:
            break
    return found


def main_amount_index(amounts: list) -> int:
    """Code's own best guess at which amount is the charge (-1 if none). Used to tell apart
    different subscriptions that arrive from one payment processor, and as a fallback.
    Preference: a 'total', then a 'price', then the first amount that isn't a subtotal/tax/etc."""
    for wanted in ("total", "price"):
        for i, a in enumerate(amounts):
            if a.get("label") == wanted:
                return i
    for i, a in enumerate(amounts):
        if a.get("label") not in _NOT_THE_CHARGE:
            return i
    return -1


def money(value, currency: str = "") -> str:
    """17.99, 'NZD' -> 'NZD 17.99'; 17.99, '$' -> '$17.99'; 1500, '\u00a5' -> '\u00a51,500.00'; None -> ''."""
    if value is None:
        return ""
    cur = currency or ""
    number = f"{value:,.2f}"
    return f"{cur}{number}" if cur in _BARE_SYMBOLS or not cur else f"{cur} {number}"


def amount_line(amounts: list) -> str:
    """The numbered list shown to the AI: '1) total NZD 17.99; 2) tax NZD 2.35'. Numbers start
    at 1 so that 0 can mean 'none of these'."""
    return "; ".join(f"{i}) {(a['label'] + ' ') if a.get('label') else ''}{money(a['value'], a['currency'])}"
                     for i, a in enumerate(amounts, start=1))


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def to_date(iso):
    """'2026-09-14T10:00:00+00:00' (or just '2026-09-14') -> date, or None."""
    if not iso:
        return None
    try:
        return date.fromisoformat(str(iso)[:10])
    except ValueError:
        return None


def day(d) -> str:
    return d.isoformat() if d else ""


# ---------------------------------------------------------------------------
# Cadence: how often is it charged?
# ---------------------------------------------------------------------------
# (name, nominal days, shortest, longest) of the gap between two charges.
CYCLES = (
    ("weekly", 7.0, 5, 9),
    ("every 2 weeks", 14.0, 12, 16),
    ("monthly", MONTH_DAYS, 26, 35),
    ("every 2 months", 60.9, 55, 67),
    ("every 3 months", 91.3, 84, 98),
    ("every 6 months", 182.6, 170, 196),
    ("yearly", YEAR_DAYS, 350, 380),
)
MERGE_DAYS = 3                  # two charge emails this close are the same charge (receipt + invoice)
RECENT_INTERVALS = 6            # only the latest gaps count, so an old change of plan is forgotten
REGULAR_SHARE = 0.6             # this share of gaps must fit the cycle (a missed email still fits)

# What the AI may say the emails state about how often it bills (never used when code could work it out).
STATED_PERIOD_DAYS = {"weekly": 7.0, "monthly": MONTH_DAYS, "quarterly": 91.3, "yearly": YEAR_DAYS}


@dataclass(frozen=True)
class Cadence:
    label: str                  # "monthly", "yearly", ... or "irregular" or "unknown"
    period_days: float | None   # None unless the cycle is known
    charges: int                # distinct charge days after merging duplicates
    source: str = "observed"    # "observed" (from the dates) or "stated" (from the email's wording)

    @property
    def known(self) -> bool:
        return self.period_days is not None


def charge_days(dates) -> list:
    """Sorted distinct charge days, with charges within MERGE_DAYS of the previous one dropped."""
    merged = []
    for d in sorted({d for d in dates if d}):
        if not merged or (d - merged[-1]).days > MERGE_DAYS:
            merged.append(d)
    return merged


def compute_cadence(dates) -> Cadence:
    """Work out the billing cycle from the dates of the charge emails."""
    days = charge_days(dates)
    if len(days) < 2:
        return Cadence("unknown", None, len(days))
    gaps = [(b - a).days for a, b in zip(days, days[1:])][-RECENT_INTERVALS:]
    middle = statistics.median(gaps)
    for label, period, low, high in CYCLES:
        if low <= middle <= high:
            fits = sum(1 for g in gaps if low <= g <= high or 2 * low <= g <= 2 * high)
            if fits / len(gaps) >= REGULAR_SHARE:
                return Cadence(label, period, len(days))
            break
    return Cadence("irregular", None, len(days))


def with_stated_period(cadence: Cadence, stated: str) -> Cadence:
    """If the dates could not give a cycle but the email's own wording did, use that, labelled so."""
    if cadence.known or stated not in STATED_PERIOD_DAYS:
        return cadence
    label = "every 3 months" if stated == "quarterly" else stated
    return Cadence(label, STATED_PERIOD_DAYS[stated], cadence.charges, source="stated")


# ---------------------------------------------------------------------------
# State: does it still look like it is running?
# ---------------------------------------------------------------------------
STATE_ACTIVE = "Active"
STATE_STOPPED = "Possibly stopped"
STATE_CANCELLED = "Cancelled"
STATE_TRIAL = "Trial"
STATE_UNCLEAR = "Unclear"

CANCEL_GRACE_DAYS = 3           # a charge this soon after a cancellation notice is the last one


@dataclass(frozen=True)
class StateResult:
    state: str
    note: str                   # plain sentences saying why, shown in the details
    next_expected: date | None = None


def grace_days(period_days: float) -> int:
    """How late a charge may be before it counts as missed: a fifth of the cycle, at least a week."""
    return max(7, round(period_days * 0.2))


def judge_state(*, last_charge, cadence: Cadence, mailbox_end, cancel_date=None,
                ai_status: str = "unclear") -> StateResult:
    """Decide Active / Possibly stopped / Cancelled / Trial / Unclear.

    last_charge   date of the latest charge email, or None
    cadence       from compute_cadence() (possibly with_stated_period())
    mailbox_end   date of the newest email in the mailbox(es) these emails came to
    cancel_date   date of the latest "cancelled/ended" notice found by the rules, or None
    ai_status     what the AI says the emails state: active|cancelled|trial|unclear
    """
    ended = ai_status == "cancelled"
    after_cancel = None
    if cancel_date and ended:
        if last_charge is None or last_charge <= cancel_date + timedelta(days=CANCEL_GRACE_DAYS):
            return StateResult(STATE_CANCELLED,
                               f"A cancellation notice is dated {day(cancel_date)}"
                               + (f", and the last charge email is {day(last_charge)}." if last_charge
                                  else ", and no charge email was found.")
                               + " No later charge was seen.")
        after_cancel = (f"A cancellation notice from {day(cancel_date)} was followed by a charge on "
                        f"{day(last_charge)}, so it may have been restarted. ")
    if ai_status == "trial" and last_charge is None:
        return StateResult(STATE_TRIAL, "The emails describe a free trial and no charge email was found.")
    if last_charge is None:
        return StateResult(STATE_UNCLEAR, "No charge email was found, only other notices.")
    if not cadence.known:
        why = ("only one charge email was found" if cadence.label == "unknown"
               else "the charges are not regular enough to tell the cycle")
        return StateResult(STATE_UNCLEAR, f"The last charge email is dated {day(last_charge)}, but {why}.")
    if mailbox_end is None:
        return StateResult(STATE_UNCLEAR, f"The last charge email is dated {day(last_charge)}.")

    next_expected = last_charge + timedelta(days=round(cadence.period_days))
    deadline = next_expected + timedelta(days=grace_days(cadence.period_days))
    how = f"{cadence.label} ({'stated in the email' if cadence.source == 'stated' else 'seen in the dates'})"
    base = (after_cancel or "") + f"Charged {how}; the last charge email is dated {day(last_charge)}. "
    if ended and not cancel_date:
        return StateResult(STATE_STOPPED, base + "The emails use wording that says it was cancelled or ended, "
                                           "but no dated cancellation notice was found.", next_expected)
    if mailbox_end <= deadline:
        return StateResult(STATE_ACTIVE, base + f"The next charge is expected around {day(next_expected)}; "
                                         f"your mail runs to {day(mailbox_end)}.", next_expected)
    return StateResult(STATE_STOPPED, base + f"The next charge was expected around {day(next_expected)}, "
                                      f"but your mail continues to {day(mailbox_end)} with no charge email. "
                                      f"It may have stopped, or the emails may be missing.", next_expected)


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def per_month(value, period_days):
    return None if value is None or not period_days else value * MONTH_DAYS / period_days


def per_year(value, period_days):
    return None if value is None or not period_days else value * YEAR_DAYS / period_days


def add_totals(items) -> tuple:
    """items: (amount, currency, period_days) for each subscription to be counted.
    Returns ({currency: (per_month, per_year)}, how_many_could_not_be_counted). A subscription
    without a known amount or cycle cannot be added up, and is counted in the second number."""
    totals: dict = {}
    skipped = 0
    for amount, currency, period in items:
        m, y = per_month(amount, period), per_year(amount, period)
        if m is None:
            skipped += 1
            continue
        cur_m, cur_y = totals.get(currency or "", (0.0, 0.0))
        totals[currency or ""] = (cur_m + m, cur_y + y)
    return {c: (round(m, 2), round(y, 2)) for c, (m, y) in totals.items()}, skipped
