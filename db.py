"""
db.py
-----
All SQLite access lives here, so the schema only ever needs to change in one
place.

IN MEMORY BY DEFAULT
    `open_database()` with no path creates a database that exists only in RAM.
    When the program closes it is gone - nothing was ever written to disk. A real
    file is only used when the user turns on "write mode" (see session.py) or
    when the command-line tool is pointed at a --db file.

RESUMABLE
    Every pipeline stage reads rows left in a particular state by the previous
    stage and writes its results back, so any stage can be re-run safely. With a
    database file (write mode) that means a crash or accidental close doesn't
    cost you the scanning or the AI calls already done.

THREADS
    The connection is opened with check_same_thread=False so a background job
    thread can use it. The GUI guarantees only ONE thread touches it at a time
    (the UI does not read it while a job is running), so no extra locking is
    needed. Don't share one connection between threads that run at the same time.

DATA MODEL (schema version 2)
    emails           one row per message header seen (never the full body)
    classifications  one row per "group" the AI judged: usually a sender domain,
                     or - for identity providers such as accounts.google.com,
                     which send notices about MANY different apps - one row per
                     individual email. See group_key below.
    email_changes    one row per email-change notice the AI examined
    run_log          a small audit trail of which stages ran
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_VERSION = 2


class DatabaseVersionError(Exception):
    """The file was made by a different (older) version of this program."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    account_name        TEXT NOT NULL,
    email_address_used  TEXT,          -- the exact address the mail was sent TO (incl. plus-aliases)
    message_id          TEXT,          -- Message-ID header; used to avoid duplicates
    uid                 TEXT,          -- IMAP UID (NULL for MBOX-imported rows)
    folder              TEXT,
    sender_raw          TEXT,          -- full "From" header
    sender_email        TEXT,
    sender_domain       TEXT,
    subject             TEXT,
    date_raw            TEXT,
    date_iso            TEXT,          -- parsed, ISO-8601 in UTC where possible
    list_unsubscribe    TEXT,
    body_snippet        TEXT,          -- short text excerpt, filled in only for candidates
    prefilter_score     REAL,          -- sign-up score from Module B
    prefilter_label     TEXT,          -- 'candidate' | 'noise' | NULL (not scored yet)
    prefilter_signal    TEXT,
    change_score        REAL,          -- "email address changed" score from Module B
    change_candidate    INTEGER DEFAULT 0,
    change_signal       TEXT,
    body_fetched        INTEGER DEFAULT 0,
    fetched_at          TEXT,
    -- The same message can appear in several Gmail folders (INBOX + All Mail);
    -- the Message-ID identifies it, so we keep just one copy.
    UNIQUE(account_name, message_id)
);

CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(sender_domain);
CREATE INDEX IF NOT EXISTS idx_emails_prefilter ON emails(prefilter_label);
CREATE INDEX IF NOT EXISTS idx_emails_change ON emails(change_candidate);

-- group_key is the sender domain for normal senders. For identity providers it
-- is "<domain>#<email id>" so each notice ("you signed in to Notion with Google")
-- is judged separately and can resolve to a DIFFERENT service.
CREATE TABLE IF NOT EXISTS classifications (
    group_key               TEXT PRIMARY KEY,
    sender_domain           TEXT NOT NULL,
    is_signup               INTEGER,
    service_name            TEXT,
    category                TEXT,
    signup_method           TEXT,
    confidence              REAL,
    reasoning               TEXT,
    representative_email_id INTEGER,
    signup_date_iso         TEXT,
    classified_at           TEXT,
    raw_model_response      TEXT
);

-- One row per email-change notice examined (also the "no, not a change" ones,
-- so they are never sent to the AI twice).
CREATE TABLE IF NOT EXISTS email_changes (
    email_id            INTEGER PRIMARY KEY,
    is_email_change     INTEGER,
    change_type         TEXT,
    service_name        TEXT,
    sender_domain       TEXT,
    recipient_address   TEXT,
    recipient_role      TEXT,
    old_address_printed TEXT,
    new_address_printed TEXT,
    old_candidates      TEXT,          -- JSON list of your known addresses that fit
    new_candidates      TEXT,
    likely_old_address  TEXT,
    likely_new_address  TEXT,
    match_quality       TEXT,
    confidence          REAL,
    summary             TEXT,
    notice_date_iso     TEXT,
    subject             TEXT,
    reasoning           TEXT,
    classified_at       TEXT,
    raw_model_response  TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    detail      TEXT
);
"""


# ---------------------------------------------------------------------------
# Opening / closing
# ---------------------------------------------------------------------------

def _init_schema(conn: sqlite3.Connection) -> None:
    has_emails = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='emails'"
    ).fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if has_emails and version != SCHEMA_VERSION:
        raise DatabaseVersionError(
            "This database file was created by an older version of the program "
            "and can't be opened by this one. Delete it and scan again "
            "(nothing else is lost - it only holds scan progress)."
        )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def prepare_private_file(path: str | None) -> None:
    """Make sure a database file is readable and writable by its owner only (mode 600 on
    macOS and Linux) BEFORE anything is stored in it, instead of the usual default that
    lets other users of the computer read it. It holds sender addresses, subjects and
    parts of email text. Does nothing on Windows (access there follows the folder's
    permissions) or for an in-memory database, and never raises: this is a best-effort
    extra protection that must not stop a scan."""
    if os.name == "nt" or not path or path == ":memory:":
        return
    try:
        if os.path.exists(path):
            os.chmod(path, 0o600)
        else:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except OSError:
        pass


def open_database(path: str | None = None) -> sqlite3.Connection:
    """Open (or create) the database. `path=None` means in-memory only."""
    prepare_private_file(path)
    conn = sqlite3.connect(path if path else ":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        _init_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def connection(path: str | None = None):
    """`with db.connection(path) as conn:` for scripts (the CLI)."""
    conn = open_database(path)
    try:
        yield conn
    finally:
        conn.close()


def copy_database(src: sqlite3.Connection, dest: sqlite3.Connection) -> None:
    """Copy the whole database from one connection to another (memory<->file).
    Uses SQLite's own online-backup API, which is safe and consistent."""
    src.commit()
    src.backup(dest)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# emails
# ---------------------------------------------------------------------------

_EMAIL_COLS = [
    "account_name", "email_address_used", "message_id", "uid", "folder",
    "sender_raw", "sender_email", "sender_domain", "subject",
    "date_raw", "date_iso", "list_unsubscribe", "fetched_at",
]


def insert_email(conn: sqlite3.Connection, row: dict):
    """Insert one header record. Returns the new row id, or None if that
    message (same account + Message-ID) was already stored."""
    cur = conn.execute(
        f"INSERT OR IGNORE INTO emails ({','.join(_EMAIL_COLS)}) "
        f"VALUES ({','.join('?' * len(_EMAIL_COLS))})",
        [row.get(c) for c in _EMAIL_COLS],
    )
    return cur.lastrowid if cur.rowcount else None


def bulk_insert_emails(conn: sqlite3.Connection, rows: list) -> int:
    inserted = sum(1 for r in rows if insert_email(conn, r))
    conn.commit()
    return inserted


def set_prefilter_result(conn, email_id, score, label, signal,
                         change_score=0.0, change_candidate=False, change_signal=""):
    conn.execute(
        "UPDATE emails SET prefilter_score=?, prefilter_label=?, prefilter_signal=?, "
        "change_score=?, change_candidate=?, change_signal=? WHERE id=?",
        (score, label, signal, change_score, 1 if change_candidate else 0, change_signal, email_id),
    )


def emails_missing_body(conn: sqlite3.Connection) -> list:
    """IMAP-sourced emails worth reading (sign-up OR email-change candidates)
    whose body text we haven't fetched yet. MBOX rows have no UID and already
    carry their body, so they are never listed here."""
    return conn.execute(
        "SELECT * FROM emails WHERE body_fetched=0 AND uid IS NOT NULL "
        "AND (prefilter_label='candidate' OR change_candidate=1)"
    ).fetchall()


def set_body_snippet(conn, email_id: int, snippet: str) -> None:
    conn.execute("UPDATE emails SET body_snippet=?, body_fetched=1 WHERE id=?", (snippet, email_id))


def candidate_domains(conn) -> list:
    rows = conn.execute(
        "SELECT DISTINCT sender_domain FROM emails "
        "WHERE prefilter_label='candidate' AND sender_domain IS NOT NULL"
    ).fetchall()
    return [r["sender_domain"] for r in rows]


def candidate_emails_for_domain(conn, domain: str, limit: int | None = None) -> list:
    """Candidate emails from one sender, earliest first (the earliest is the most
    likely to be the original welcome / verification message)."""
    sql = ("SELECT * FROM emails WHERE sender_domain=? AND prefilter_label='candidate' "
           "ORDER BY date_iso IS NULL, date_iso ASC")
    params: list = [domain]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def known_addresses(conn) -> set:
    """Every address of yours that appeared as a recipient in the scanned mail
    (this includes plus-aliases like you+spotify@gmail.com)."""
    rows = conn.execute(
        "SELECT DISTINCT lower(email_address_used) AS a FROM emails "
        "WHERE email_address_used IS NOT NULL AND email_address_used != ''"
    ).fetchall()
    return {r["a"] for r in rows}


# ---------------------------------------------------------------------------
# classifications (sign-ups)
# ---------------------------------------------------------------------------

def classified_keys(conn) -> set:
    return {r["group_key"] for r in conn.execute("SELECT group_key FROM classifications")}


def save_classification(conn, result: dict) -> None:
    conn.execute(
        """
        INSERT INTO classifications (
            group_key, sender_domain, is_signup, service_name, category, signup_method,
            confidence, reasoning, representative_email_id, signup_date_iso,
            classified_at, raw_model_response
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(group_key) DO UPDATE SET
            sender_domain=excluded.sender_domain, is_signup=excluded.is_signup,
            service_name=excluded.service_name, category=excluded.category,
            signup_method=excluded.signup_method, confidence=excluded.confidence,
            reasoning=excluded.reasoning,
            representative_email_id=excluded.representative_email_id,
            signup_date_iso=excluded.signup_date_iso,
            classified_at=excluded.classified_at,
            raw_model_response=excluded.raw_model_response
        """,
        (
            result["group_key"], result["sender_domain"], int(bool(result["is_signup"])),
            result.get("service_name"), result.get("category"), result.get("signup_method"),
            result.get("confidence"), result.get("reasoning"),
            result.get("representative_email_id"), result.get("signup_date_iso"),
            now_iso(), json.dumps(result.get("raw_model_response", {})),
        ),
    )
    conn.commit()


def all_signups(conn, min_confidence: float = 0.0) -> list:
    return conn.execute(
        "SELECT * FROM classifications WHERE is_signup=1 AND confidence >= ? "
        "ORDER BY service_name COLLATE NOCASE",
        (min_confidence,),
    ).fetchall()


def signup_addresses(conn, cls_row) -> list:
    """Which of YOUR addresses were used for this sign-up, with the date of the
    earliest candidate email for each. Returns [(address, first_date_iso), ...].

    This is what makes the output "one row per service AND address" and is the
    foundation for email-change detection (it tells us where an account used to
    live). Derived on demand from the emails table, so it can never go stale.
    """
    if "#" in cls_row["group_key"]:
        # Identity-provider notice: exactly one email is the evidence.
        row = conn.execute(
            "SELECT email_address_used AS address, date_iso AS first_date FROM emails WHERE id=?",
            (cls_row["representative_email_id"],),
        ).fetchone()
        return [(row["address"], row["first_date"])] if row and row["address"] else []
    rows = conn.execute(
        "SELECT email_address_used AS address, MIN(date_iso) AS first_date FROM emails "
        "WHERE sender_domain=? AND prefilter_label='candidate' AND email_address_used IS NOT NULL "
        "GROUP BY email_address_used ORDER BY first_date IS NULL, first_date",
        (cls_row["sender_domain"],),
    ).fetchall()
    return [(r["address"], r["first_date"]) for r in rows]


def signups_for_service(conn, service_name: str, sender_domain: str) -> list:
    """Confirmed sign-ups matching a service name or sender domain (used to
    show 'originally signed up with ...' next to an email-change notice)."""
    return conn.execute(
        "SELECT * FROM classifications WHERE is_signup=1 AND "
        "(lower(service_name)=lower(?) OR sender_domain=?)",
        (service_name or "", sender_domain or ""),
    ).fetchall()


# ---------------------------------------------------------------------------
# email_changes
# ---------------------------------------------------------------------------

def change_notices_pending(conn) -> list:
    """Email-change candidates the AI hasn't examined yet."""
    return conn.execute(
        "SELECT * FROM emails WHERE change_candidate=1 "
        "AND id NOT IN (SELECT email_id FROM email_changes) "
        "ORDER BY date_iso IS NULL, date_iso"
    ).fetchall()


def save_email_change(conn, rec: dict) -> None:
    cols = [
        "email_id", "is_email_change", "change_type", "service_name", "sender_domain",
        "recipient_address", "recipient_role", "old_address_printed", "new_address_printed",
        "old_candidates", "new_candidates", "likely_old_address", "likely_new_address",
        "match_quality", "confidence", "summary", "notice_date_iso", "subject",
        "reasoning", "classified_at", "raw_model_response",
    ]
    values = []
    for c in cols:
        v = rec.get(c)
        if c in ("old_candidates", "new_candidates"):
            v = json.dumps(v or [])
        elif c == "raw_model_response":
            v = json.dumps(v or {})
        elif c == "classified_at":
            v = now_iso()
        elif c == "is_email_change":
            v = int(bool(v))
        values.append(v)
    conn.execute(
        f"INSERT OR REPLACE INTO email_changes ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        values,
    )
    conn.commit()


def change_rows(conn, min_confidence: float = 0.0) -> list:
    return conn.execute(
        "SELECT * FROM email_changes WHERE is_email_change=1 AND confidence >= ? "
        "ORDER BY notice_date_iso DESC",
        (min_confidence,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

def log_stage(conn, stage: str, detail: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO run_log (stage, started_at, detail) VALUES (?,?,?)", (stage, now_iso(), detail)
    )
    conn.commit()
    return cur.lastrowid


def finish_stage(conn, log_id: int, detail: str = "") -> None:
    conn.execute("UPDATE run_log SET finished_at=?, detail=? WHERE id=?", (now_iso(), detail, log_id))
    conn.commit()


def counts(conn) -> dict:
    """Headline numbers for the status line."""
    def one(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "total_emails": one("SELECT COUNT(*) FROM emails"),
        "candidates": one("SELECT COUNT(*) FROM emails WHERE prefilter_label='candidate'"),
        "noise": one("SELECT COUNT(*) FROM emails WHERE prefilter_label='noise'"),
        "unscored": one("SELECT COUNT(*) FROM emails WHERE prefilter_label IS NULL"),
        "candidate_domains": one(
            "SELECT COUNT(DISTINCT sender_domain) FROM emails WHERE prefilter_label='candidate'"),
        "classified_groups": one("SELECT COUNT(*) FROM classifications"),
        "confirmed_signups": one("SELECT COUNT(*) FROM classifications WHERE is_signup=1"),
        "change_notices": one("SELECT COUNT(*) FROM emails WHERE change_candidate=1"),
        "email_changes_found": one("SELECT COUNT(*) FROM email_changes WHERE is_email_change=1"),
    }


def has_results(conn) -> bool:
    """True if there is anything worth exporting (used for the close warning)."""
    c = counts(conn)
    return bool(c["confirmed_signups"] or c["email_changes_found"])
