"""
run_options.py
--------------
WHAT a run does (which pipelines) and HOW DEEPLY it looks (the depth setting).

This is plain data with no GUI code and no side effects, so the desktop app, the command line
and the tests all read the same facts and the numbers are never typed in two places.

PIPELINES
  Vestige has four AI pipelines. They SHARE the free steps (fetching headers, the rule-based
  pre-filter and fetching email bodies) and differ only in their last step: each has its own
  small prompt and only ever sees the emails the free rules shortlisted for it.

      signups         Which services did I sign up to?                 (module_c_classify.py)
      changes         Did the email on an account change, and to what? (module_e_email_changes.py)
      closed          Was an account closed or deactivated?            (module_f_closures.py)
      subscriptions   What am I paying for, and is it still running?   (module_g_subscriptions.py)

  The checkboxes on the Run Pipeline page choose which of these run when you press
  "Run selected pipelines". Leaving one out means it costs nothing: its emails are not even
  downloaded or sent to the AI.

DEPTH
  Depth is how much of each shortlisted sender's mail the AI READS: how many sample emails and
  how many characters of each. It is the main lever on token use.

      Quick       the least text. A first look; a sender whose first sample isn't convincing
                  may be missed.
      Balanced    the default, and exactly what Vestige did before depth existed.
      Thorough    the most text per sender. Slower and dearer, with a better chance of a correct
                  answer on unusual emails.

  Depth does NOT change the free rules (what gets shortlisted) and it is not remembered between
  runs: answers already given are kept, so running again at a different depth only fills in
  what has not been answered yet.

  TO TUNE: edit the numbers in DEPTHS below. Nothing else needs to change. (Email bodies are
  only kept up to module_a_imap.BODY_SNIPPET_CHARS characters, so no depth can send more than
  that for one email.)
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------
PIPELINE_SIGNUPS = "signups"
PIPELINE_CHANGES = "changes"
PIPELINE_CLOSED = "closed"
PIPELINE_SUBSCRIPTIONS = "subscriptions"

# The order they run in (and the order the checkboxes appear in).
PIPELINE_ORDER = (PIPELINE_SIGNUPS, PIPELINE_CHANGES, PIPELINE_CLOSED, PIPELINE_SUBSCRIPTIONS)

# The names shown on the checkboxes, in the command line and in the log.
PIPELINE_LABELS = {
    PIPELINE_SIGNUPS: "Sign-ups",
    PIPELINE_CHANGES: "Email changes",
    PIPELINE_CLOSED: "Closed accounts",
    PIPELINE_SUBSCRIPTIONS: "Subscriptions",
}

def normalize_pipelines(selected=None) -> tuple:
    """Turn what the caller chose into a tuple of valid pipeline keys in running order.
    None means "all of them". Unknown names raise ValueError (a typo must not silently run
    nothing). An empty choice also raises: there would be nothing to do."""
    if selected is None:
        return PIPELINE_ORDER
    wanted = {str(s).strip().lower() for s in selected}
    unknown = wanted - set(PIPELINE_ORDER)
    if unknown:
        raise ValueError(f"Unknown pipeline(s): {', '.join(sorted(unknown))}. "
                         f"Choose from: {', '.join(PIPELINE_ORDER)}.")
    if not wanted:
        raise ValueError("Choose at least one pipeline.")
    return tuple(p for p in PIPELINE_ORDER if p in wanted)


def parse_pipeline_list(text: str) -> tuple:
    """For the command line: 'signups,subscriptions' -> ('signups', 'subscriptions').
    'all' (or an empty string) means every pipeline."""
    parts = [p.strip().lower() for p in (text or "").replace(";", ",").split(",") if p.strip()]
    if not parts or parts == ["all"]:
        return PIPELINE_ORDER
    return normalize_pipelines(parts)


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Depth:
    """How much text one AI request reads about each shortlisted sender."""
    name: str
    blurb: str                 # one sentence shown under the control on the Run Pipeline page
    signup_samples: int        # sample emails per sender (sign-ups)
    signup_chars: int          # characters of each
    change_chars: int          # characters of each email-change notice
    closure_samples: int       # sample notices per sender (closed accounts)
    closure_chars: int
    billing_samples: int       # sample emails per sender (subscriptions): see module_g.sample_rows
    billing_chars: int
    processor_receipts: int    # newest receipts read per payment processor (Apple, PayPal...) when
                               # downloading them over IMAP, because one sender covers many services


QUICK = Depth(
    "Quick", "Cheapest and fastest: one short sample per sender. Good for a first look; it can miss unusual emails.",
    signup_samples=1, signup_chars=250, change_chars=500,
    closure_samples=1, closure_chars=200, billing_samples=1, billing_chars=160,
    processor_receipts=6)

BALANCED = Depth(
    "Balanced", "The default: a few samples per sender, each cut short.",
    signup_samples=3, signup_chars=400, change_chars=800,
    closure_samples=2, closure_chars=350, billing_samples=2, billing_chars=260,
    processor_receipts=12)

THOROUGH = Depth(
    "Thorough", "Most careful: more and longer samples per sender. The slowest and most expensive.",
    signup_samples=5, signup_chars=800,
    change_chars=800,          # same as Balanced on purpose: a change notice is short, and 800 characters is all that
                               # is kept of any email (module_a_imap.BODY_SNIPPET_CHARS), so there is nothing more to read

    closure_samples=3, closure_chars=600, billing_samples=3, billing_chars=450,
    processor_receipts=24)

DEPTHS = {d.name: d for d in (QUICK, BALANCED, THOROUGH)}
DEPTH_NAMES = list(DEPTHS)                     # the order of the segmented control
DEFAULT_DEPTH = BALANCED


def get_depth(value) -> Depth:
    """Accept a Depth, a name in any letter case ('quick'), or None (= the default)."""
    if value is None or value == "":
        return DEFAULT_DEPTH
    if isinstance(value, Depth):
        return value
    for name, depth in DEPTHS.items():
        if name.lower() == str(value).strip().lower():
            return depth
    raise ValueError(f"Unknown depth '{value}'. Choose from: {', '.join(n.lower() for n in DEPTHS)}.")
