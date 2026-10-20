"""
module_b_prefilter.py
---------------------
Module B: pure rule-based scoring of subject lines. No network, no AI, no cost.

It does two independent jobs on every header row Module A stored:

  1. SIGN-UP score   -> label 'candidate' / 'noise'
        Only candidates ever get a body fetched or sent to the AI for sign-up
        classification. This is what keeps AI usage small.
  2. EMAIL-CHANGE score -> change_candidate yes/no
        Flags notices such as "Your email address was changed" or "Confirm your
        new email". An email can be both (or neither). These are the only
        emails Module E ever looks at for possible email changes.

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

_SIGNUP_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in POSITIVE_PATTERNS]
_SIGNUP_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in NEGATIVE_PATTERNS]
_CHANGE_POS = [(re.compile(p, re.IGNORECASE), w) for p, w in CHANGE_POSITIVE_PATTERNS]
_CHANGE_NEG = [(re.compile(p, re.IGNORECASE), w) for p, w in CHANGE_NEGATIVE_PATTERNS]

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


def run_prefilter(conn, control=None) -> dict:
    """Score every not-yet-scored email. Safe to re-run: only new rows are touched.
    `control` is optional and only used to report progress (this stage is fast
    and is never interrupted half-way)."""
    rows = conn.execute("SELECT id, subject, sender_domain FROM emails WHERE prefilter_label IS NULL").fetchall()
    candidates = noise = change = 0
    total = len(rows)
    for count, row in enumerate(rows, start=1):
        if count % 1000 == 0:
            jobcontrol.report(control, count, total, f"{count:,} of {total:,} emails")
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
    conn.commit()
    return {"scored": len(rows), "candidates": candidates, "noise": noise, "change_notices": change}


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
    samples              `emails` rows to show the AI (earliest first)
    """
    key: str
    sender_domain: str
    is_identity_provider: bool
    samples: list


def build_groups(conn, samples_per_group: int = 3) -> list:
    """Build the not-yet-classified groups. Normal senders are judged once per
    domain (all their emails together); identity-provider senders once per
    notice. Anything already in `classifications` is skipped, so re-running
    after adding another mailbox only classifies what's new."""
    done = db.classified_keys(conn)
    groups = []
    for domain in sorted(db.candidate_domains(conn)):
        if domain in IDENTITY_PROVIDER_DOMAINS:
            for row in db.candidate_emails_for_domain(conn, domain):
                key = f"{domain}#{row['id']}"
                if key not in done:
                    groups.append(Group(key, domain, True, [row]))
        elif domain not in done:
            samples = db.candidate_emails_for_domain(conn, domain, limit=samples_per_group)
            if samples:
                groups.append(Group(domain, domain, False, samples))
    return groups
