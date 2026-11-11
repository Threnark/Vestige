"""
module_b_prefilter.py
---------------------
Module B: pure rule-based scoring of subject lines. No network, no AI, no cost.

It does four independent jobs on every header row Module A stored. This is the SHARED free
step: all four AI pipelines start from what it shortlists, and each pipeline's AI stage only
ever sees its own shortlist.

  1. SIGN-UP score   -> label 'candidate' / 'noise'
        Only candidates ever get a body fetched or sent to the AI for sign-up
        classification. This is what keeps AI usage small.
  2. EMAIL-CHANGE score -> change_candidate yes/no
        Flags notices such as "Your email address was changed" or "Confirm your
        new email". An email can be both (or neither). These are the only
        emails Module E ever looks at for possible email changes.
  3. CLOSURE score   -> closure_candidate yes/no
        Flags notices such as "Your account has been closed". These are the only emails
        Module F (closed accounts) ever looks at. The AI then confirms them: the rules alone
        never say an account is closed.
  4. BILLING score   -> billing_candidate yes/no, plus a KIND
        Flags receipts, invoices, renewals, failed payments, trial notices and cancellations.
        The kind (charge / cancel / failed / trial / other) is what lets Module G count
        charges and spot a cancellation using only dates, with no AI. These are the only
        emails Module G (subscriptions) ever looks at.

The rules are deliberately generous (a borderline email stays a candidate and is
left for the AI to decide) rather than silently dropping something real. Tune
the lists below against your own mailbox; `python main.py prefilter-report`
shows what was kept and dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import db
import jobcontrol

CANDIDATE_THRESHOLD = 2.0
CHANGE_THRESHOLD = 2.0
CLOSURE_THRESHOLD = 2.5      # a little higher: a missed closure notice costs less than 100 false ones
BILLING_THRESHOLD = 2.0

# ---------------------------------------------------------------------------
# Sign-up rules: (regex, weight). Positive = looks like an account being created.
# ---------------------------------------------------------------------------
POSITIVE_PATTERNS = [
    (r"\bwelcome to\b", 2.5),
    (r"\bwelcome,?\s", 1.0),
    (r"\bverify your (email|account|address)\b", 4.0),
    (r"\bconfirm your (email|account|address|registration)\b", 4.0),
    (r"\bactivate your (account|membership)\b", 4.0),
    (r"\byour account (has been|was) (created|set up)\b", 4.0),
    (r"\bthanks? for (signing up|registering|joining)\b", 3.0),
    (r"\bthank you for (signing up|registering|joining|creating an account)\b", 3.0),
    (r"\bcomplete your (registration|sign.?up|profile)\b", 2.5),
    (r"\bconfirm your subscription\b", 1.0),          # newsletter OR account - low weight
    (r"\byou(?:'|\u2019)re (in|all set|ready to go)\b", 1.5),
    (r"\bget started with\b", 1.5),
    (r"\bset (a|your) password\b", 3.0),
    (r"\bcreate(d)? your password\b", 3.0),
    (r"\bverify your (phone number|identity)\b", 1.5),
    # "Sign in with Google/Apple/..." notices: the notice comes from the identity
    # provider, and the AI extracts the real app name from the body.
    (r"\byou(?:'|\u2019)ve signed in to\b", 3.0),
    (r"\bused your (google|apple|facebook|microsoft) account to sign in\b", 3.5),
    (r"\bnew (app|device) (connected|has access|signed in)\b", 3.0),
    (r"\b(granted|allowed) access to your (google|apple|facebook|microsoft) account\b", 3.5),
    (r"\bsecurity alert.*sign.?in\b", 1.0),
    (r"\bconnected a new app\b", 3.0),
]

# Negative = marketing / transactional noise.
NEGATIVE_PATTERNS = [
    (r"\d+% off\b", -3.0),
    (r"\bsale\b", -2.0),
    (r"\bnewsletter\b", -3.0),
    (r"\bdigest\b", -2.0),
    (r"\byour order\b", -3.0),
    (r"\border (confirmation|number)\b", -3.0),
    (r"\breceipt\b", -3.0),
    (r"\binvoice\b", -3.0),
    (r"\bshipped\b|\bshipping (confirmation|update)\b", -3.0),
    (r"\btracking number\b", -3.0),
    (r"\breset your password\b|\bpassword reset\b", -1.5),
    (r"\bweekly roundup\b|\bweekly digest\b", -2.0),
    (r"\bnew post\b|\blatest (articles|posts)\b", -2.0),
    (r"\bunsubscribe(d)?\b", -1.0),
    (r"\breminder:", -1.0),
    (r"\byour (statement|bill) is (ready|available)\b", -2.5),
]

# ---------------------------------------------------------------------------
# Email-change rules: notices that the email address on an account changed.
# ---------------------------------------------------------------------------
CHANGE_POSITIVE_PATTERNS = [
    (r"\be-?mail( address)? (has been |was |is being |were )?(changed|updated|modified|removed|added)\b", 3.0),
    (r"\byour (primary |account |login |sign-?in |contact )?e-?mail( address)? (change|changed|update|updated)\b", 3.0),
    (r"\b(confirm|verify) your new e-?mail\b", 3.5),
    (r"\bnew e-?mail address\b", 2.0),
    (r"\bchange (of |your )?e-?mail( address)?\b", 2.5),
    (r"\be-?mail( address)? change (request|confirmation|notification|notice)\b", 3.0),
    (r"\be-?mail( address)? (removed|removal)\b", 3.0),
    (r"\bremoved? (an |the |your )?e-?mail( address)?\b", 2.0),
    (r"\badded? (a |an |the |your )?(new )?e-?mail( address)?\b", 2.0),
    (r"\bprimary e-?mail\b", 2.0),
]

CHANGE_NEGATIVE_PATTERNS = [
    (r"\brecovery (e-?mail|address)\b", -3.0),          # recovery email != account email
    (r"\bbackup (e-?mail|address)\b", -3.0),
    (r"\be-?mail (preferences|settings|notifications?|frequency|subscription)\b", -3.0),
    (r"\bnew e-?mail (from|message|received|in your)\b", -3.0),
    (r"\bnewsletter\b", -3.0),
    (r"\d+% off\b", -3.0),
    (r"\bsale\b", -2.0),
]

# ---------------------------------------------------------------------------
# Closed-account rules: notices that an account was closed, deactivated or deleted.
# Same format as above. Weights add up, so a subject that says it twice scores well.
# ---------------------------------------------------------------------------
CLOSURE_POSITIVE_PATTERNS = [
    (r"\b(your|the) (\w+ ){0,2}account (has been|was|is now|is being|will be|is scheduled to be|has now been) "
     r"(closed|deactivated|deleted|disabled|terminated|removed)\b", 4.0),
    (r"\bwe(?:'|\u2019)ve (closed|deactivated|deleted|disabled|removed) your (\w+ )?account\b", 4.0),
    (r"\b(account|membership|profile) (closure|deactivation|deletion|termination)\b", 3.5),
    (r"\b(confirm(ation|ed)?|complet(e|ed)) (of )?(your )?(account )?(closure|deactivation|deletion)\b", 3.5),
    (r"\b(deletion|closure|deactivation) (request|scheduled|confirmed|complete|completed)\b", 2.5),
    (r"\bscheduled (for )?(deletion|closure)\b", 3.5),
    (r"\byour data (has been|was|will be|is being) (deleted|erased|removed)\b", 3.5),
    (r"\baccount (will be|is going to be) (closed|deleted|deactivated)\b", 3.0),
    (r"\bsorry to see you (go|leave)\b", 2.5),
    (r"\bwe(?:'|\u2019)ll miss you\b", 2.0),
    (r"\b(delete|close|deactivate)d? your (\w+ )?account\b", 1.5),
    (r"\binactive account\b", 1.5),
    (r"\bgoodbye\b", 1.0),
]

CLOSURE_NEGATIVE_PATTERNS = [
    (r"\bclos(e|ing) (the )?(deal|sale|gap|out)\b", -3.0),
    (r"\bclosing (soon|time|date)\b", -3.0),
    (r"\b(how to|learn how|tips? (for|on)) (delete|close|deactivate)\b", -3.0),
    (r"\bdelete (a |your |the )?(message|email|post|file|photo|comment|draft|chat)s?\b", -3.0),
    (r"\bnewsletter\b", -3.0),
    (r"\d+% off\b", -3.0),
    (r"\bsale\b", -2.0),
]

# ---------------------------------------------------------------------------
# Billing rules: receipts, invoices, renewals, failed payments, trials, cancellations.
# ---------------------------------------------------------------------------
BILLING_POSITIVE_PATTERNS = [
    (r"\b(receipt|invoice)s?\b", 2.5),
    (r"\bpayment (received|confirmation|confirmed|successful|processed|receipt|complete|completed)\b", 3.0),
    (r"\b(thanks|thank you) for your (payment|subscription)\b", 3.0),
    (r"\b(subscription|membership)\b", 2.0),
    (r"\b(renew(s|al|ed|ing)?|auto-?renew\w*)\b", 2.5),
    (r"\b(monthly|annual|yearly|weekly) (plan|payment|charge|bill|billing|subscription|membership|statement)\b", 2.5),
    (r"\bbilling (statement|notice|receipt|summary|update|period|date|reminder|information)\b", 2.0),
    (r"\bwe(?:'|\u2019)ve (charged|received your payment|processed)\b", 3.0),
    (r"\byour (\w+ )?(payment|charge) (of|for|to)\b", 2.0),
    (r"\bpayment (failed|declined|unsuccessful|issue|problem|overdue)\b", 3.0),
    (r"\b(update|fix|confirm) your (payment|billing|card)\b", 2.5),
    (r"\btrial (is ending|has ended|ends|ending|will end|expires|expiring|expired|started|begins)\b", 2.5),
    (r"\byou(?:'|\u2019)?ve (cancell?ed|canceled)\b", 3.0),
    (r"\bauto-?renew(al)? (turned off|disabled|cancell?ed|off)\b", 3.0),
    (r"\b(will not|won(?:'|\u2019)t) renew\b", 3.0),
]

BILLING_NEGATIVE_PATTERNS = [
    (r"\b(confirm|verify|activate) your (e-?mail )?subscription\b", -3.0),   # a mailing-list sign-up
    (r"\bnewsletter\b", -3.0),
    (r"\d+% off\b", -3.0),
    (r"\bsale\b", -2.0),
    (r"\b(win|giveaway|coupon|promo(tion)?)\b", -3.0),
    (r"\b(shipped|shipping|delivered|delivery|tracking)\b", -1.5),           # a shop order, not a plan
    (r"\b(rate|review) your\b", -2.0),
]

# What KIND of billing email it is, first match wins. Order matters. This is what lets code count
# charges and notice a cancellation from the headers alone.
BILLING_KIND_PATTERNS = [
    ("failed", r"\b(failed|declined|unsuccessful|unable to (process|charge)|couldn(?:'|\u2019)t (process|charge)|overdue"
               r"|update your (payment|billing|card)|payment (issue|problem))\b"),
    ("trial", r"\btrial\b"),
    ("cancel", r"\b(cancell?ed|canceled|cancellation|ended|has expired|expired|terminated|unsubscribed"
               r"|will not renew|won(?:'|\u2019)t renew|auto-?renew(al)? (turned off|disabled|off))\b"),
    ("charge", r"\b(receipt|invoice|payment (received|confirmation|confirmed|successful|processed|complete|completed)"
               r"|thank you for your payment|thanks for your payment|charged|renewed|billing statement|your payment of)\b"),
]
# A subject that talks about a subscription/plan as such (not just "your receipt"). The Subscriptions
# pipeline sends a sender to the AI on this alone, even before two charges have been seen.
_SUBSCRIPTION_WORDS = r"\b(subscription|membership|renew\w*|auto-?renew\w*|(monthly|annual|yearly|weekly) (plan|subscription|membership|billing|payment)|your plan)\b"

_SIGNUP_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in POSITIVE_PATTERNS]
_SIGNUP_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in NEGATIVE_PATTERNS]
_CHANGE_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in CHANGE_POSITIVE_PATTERNS]
_CHANGE_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in CHANGE_NEGATIVE_PATTERNS]
_CLOSURE_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in CLOSURE_POSITIVE_PATTERNS]
_CLOSURE_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in CLOSURE_NEGATIVE_PATTERNS]
_BILLING_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in BILLING_POSITIVE_PATTERNS]
_BILLING_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in BILLING_NEGATIVE_PATTERNS]
_BILLING_KINDS = [(kind, re.compile(p, re.IGNORECASE)) for kind, p in BILLING_KIND_PATTERNS]
_SUBSCRIPTION_RE = re.compile(_SUBSCRIPTION_WORDS, re.IGNORECASE)

# Senders that notify about MANY different third-party apps. Each of their
# notices is judged on its own (see build_groups), because "you signed in to
# Notion" and "you signed in to Figma" both come from accounts.google.com.
IDENTITY_PROVIDER_DOMAINS = {
    "accounts.google.com", "accountprotection.microsoft.com", "account.microsoft.com",
    "facebookmail.com", "appleid.apple.com", "id.apple.com", "login.yahoo.com",
}


def _score(subject, positives, negatives) -> tuple:
    subject = subject or ""
    score, signals = 0.0, []
    for pattern, weight in positives + negatives:
        if pattern.search(subject):
            score += weight
            signals.append(f"{weight:+g}:{pattern.pattern[:28]}")
    return score, signals


def score_subject(subject) -> tuple:
    """Sign-up score for a subject line -> (score, [which rules fired])."""
    return _score(subject, _SIGNUP_POS, _SIGNUP_NEG)


def score_change_subject(subject) -> tuple:
    """Email-change score for a subject line -> (score, [which rules fired])."""
    return _score(subject, _CHANGE_POS, _CHANGE_NEG)


def score_closure_subject(subject) -> tuple:
    """Closed-account score for a subject line -> (score, [which rules fired])."""
    return _score(subject, _CLOSURE_POS, _CLOSURE_NEG)


def billing_kind(subject) -> str:
    """charge | cancel | failed | trial | other, from the subject alone."""
    subject = subject or ""
    for kind, pattern in _BILLING_KINDS:
        if pattern.search(subject):
            return kind
    return "other"


def is_billing_subject(subject) -> bool:
    """Would the pre-filter treat an email with this subject as a billing email? The pre-filter decides that
    from the subject line alone, so anything else that needs the same answer (see wants_amounts in
    module_a_imap) asks THIS function and can never disagree with it."""
    return score_billing_subject(subject)[0] >= BILLING_THRESHOLD


def score_billing_subject(subject) -> tuple:
    """Billing score for a subject line -> (score, kind, [signals]). The signals include
    "kind=<kind>" and, when the subject talks about a subscription/plan as such, "sub"."""
    score, signals = _score(subject, _BILLING_POS, _BILLING_NEG)
    kind = billing_kind(subject)
    signals = [f"kind={kind}"] + (["sub"] if _SUBSCRIPTION_RE.search(subject or "") else []) + signals
    return score, kind, signals


def run_prefilter(conn, control=None) -> dict:
    """Score every email that has not been scored yet. Safe to re-run: only new rows are touched.

    Two kinds of row are handled:
      * brand-new rows get all four scores (sign-up, email change, closure, billing);
      * rows scored by an older version (a saved session from before the closed-account and
        billing rules existed) keep their old scores and only get the two new ones.

    `control` is optional and only used to report progress (this stage is fast and is never
    interrupted half-way)."""
    rows = conn.execute("SELECT id, subject, sender_domain, prefilter_label FROM emails "
                        "WHERE prefilter_label IS NULL OR closure_score IS NULL").fetchall()
    candidates = noise = change = closure = billing = rechecked = 0
    billing_ids = []
    total = len(rows)
    for count, row in enumerate(rows, start=1):
        if count % 1000 == 0:
            jobcontrol.report(control, count, total, f"{count:,} of {total:,} emails")
        if row["prefilter_label"] is None:
            score, signals = score_subject(row["subject"])
            if row["sender_domain"] in IDENTITY_PROVIDER_DOMAINS:
                signals.append("identity_provider_sender")
            label = "candidate" if score >= CANDIDATE_THRESHOLD else "noise"
            c_score, c_signals = score_change_subject(row["subject"])
            is_change = c_score >= CHANGE_THRESHOLD
            candidates += label == "candidate"
            noise += label == "noise"
            change += is_change
            db.set_prefilter_result(conn, row["id"], score, label, ";".join(signals),
                                    c_score, is_change, ";".join(c_signals))
        else:
            rechecked += 1
        x_score, x_signals = score_closure_subject(row["subject"])
        is_closure = x_score >= CLOSURE_THRESHOLD
        b_score, b_kind, b_signals = score_billing_subject(row["subject"])
        is_billing = b_score >= BILLING_THRESHOLD
        closure += is_closure
        billing += is_billing
        if is_billing:
            billing_ids.append(row["id"])
        db.set_extra_track_result(conn, row["id"], x_score, is_closure, ";".join(x_signals),
                                  b_score, is_billing, b_kind, ";".join(b_signals))
    _amounts_from_snippets(conn, billing_ids)
    db.keep_amounts_for_billing_only(conn)
    conn.commit()
    return {"scored": total - rechecked, "rechecked": rechecked, "candidates": candidates, "noise": noise,
            "change_notices": change, "closure_notices": closure, "billing_emails": billing}


def _amounts_from_snippets(conn, email_ids) -> None:
    """For billing emails whose full text was not searched for amounts (a version-2 session), look
    in the saved snippet instead. It is only the start of the email, so it may find less."""
    import json

    import billing_math
    for start in range(0, len(email_ids), 500):
        chunk = email_ids[start:start + 500]
        rows = conn.execute("SELECT id, body_snippet FROM emails WHERE amounts_json IS NULL "
                            "AND body_snippet IS NOT NULL AND body_snippet != '' AND id IN (%s)"
                            % ",".join("?" * len(chunk)), chunk).fetchall()
        for row in rows:
            found = billing_math.extract_amounts(row["body_snippet"])
            if found:
                conn.execute("UPDATE emails SET amounts_json=? WHERE id=?", (json.dumps(found), row["id"]))


# ---------------------------------------------------------------------------
# Grouping candidates for the AI
# ---------------------------------------------------------------------------

@dataclass
class Group:
    """What one AI classification is about.

    key                  unique id: the sender domain, or "<domain>#<email id>"
                         for identity-provider notices
    sender_domain        the sending domain
    is_identity_provider True if the notice names a third-party app in its body
    samples              `emails` rows to show the AI (earliest first for the sign-up question,
                         newest first for the closed-account question)

    Used by both of those steps (see sender_groups).
    """
    key: str
    sender_domain: str
    is_identity_provider: bool
    samples: list


def sender_groups(domains, done: set, emails_for, samples_per_group: int) -> list:
    """Turn senders into groups for the AI, skipping what `done` already holds. Shared by the sign-up
    and closed-account steps, which differ only in where the emails come from.

    A normal sender is one group: its domain, with up to `samples_per_group` of its emails. An identity
    provider (which emails about MANY apps) is one group per notice, keyed "<domain>#<email id>", since
    one verdict for the whole domain would be meaningless.

    domains            the sender domains that have candidate emails
    done               keys already judged
    emails_for(domain, limit=None) -> `emails` rows, in the order they should be shown"""
    groups = []
    for domain in sorted(domains):
        if domain in IDENTITY_PROVIDER_DOMAINS:
            for row in emails_for(domain):
                key = f"{domain}#{row['id']}"
                if key not in done:
                    groups.append(Group(key, domain, True, [row]))
        elif domain not in done:
            samples = emails_for(domain, limit=samples_per_group)
            if samples:
                groups.append(Group(domain, domain, False, samples))
    return groups


def body_ids_wanted(conn, samples_per_group: int = 3) -> list:
    """The ids of the emails whose TEXT the sign-up question will show the AI: exactly the samples of the
    groups build_groups() makes. They come from build_groups itself, so what is downloaded and what is read
    can never disagree. (The email-changes step reads every one of its candidates, so it is not covered here.)"""
    return [row["id"] for group in build_groups(conn, samples_per_group) for row in group.samples]


def build_groups(conn, samples_per_group: int = 3) -> list:
    """Build the not-yet-classified groups. Normal senders are judged once per
    domain (all their emails together); identity-provider senders once per
    notice. Anything already in `classifications` is skipped, so re-running
    after adding another mailbox only classifies what's new."""
    return sender_groups(db.candidate_domains(conn), db.classified_keys(conn),
                         lambda domain, limit=None: db.candidate_emails_for_domain(conn, domain, limit),
                         samples_per_group)
