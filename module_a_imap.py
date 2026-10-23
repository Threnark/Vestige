"""
module_a_imap.py
----------------
Module A: connects to each configured mailbox over IMAP and pulls HEADERS ONLY
for every message (fast, cheap, read-only). Bodies are fetched later, in a
separate pass, only for the small number of messages that survive Module B's
pre-filter (see fetch_body_snippets).

Everything uses BODY.PEEK[...], so messages are never marked as read, and this
module only ever READS mail: it never sends, moves, flags or deletes anything.

Design notes
  * UIDs are fetched in batches (IMAP_BATCH_SIZE) with one comma-joined
    UID FETCH per batch - ~200 round trips for a 100k-message mailbox instead
    of 100k.
  * Re-running is safe: emails are unique per (account, Message-ID), so
    already-stored messages are silently skipped.
  * Gmail: searching '[Gmail]/All Mail' as well as INBOX catches archived and
    labelled mail. The same message appears in both, but its Message-ID makes the
    duplicate collapse into one row.
  * A wrong password is reported once, clearly - it is NOT retried (retrying a
    bad login just risks the provider locking the account).
  * The mail server's TLS certificate AND host name are always verified before the
    password is sent (see _tls_context). A server that fails the check is refused,
    clearly reported and not retried. There is deliberately no option to turn this off.
"""

from __future__ import annotations

import email
import email.header
import email.utils
import html
import imaplib
import re
import ssl
import time
from datetime import timezone
from html.parser import HTMLParser

import db
import jobcontrol
from models import MICROSOFT_PASSWORD_NOTE, Account, is_microsoft_mail

IMAP_BATCH_SIZE = 500
IMAP_TIMEOUT = 60          # seconds; passed to the socket, so a dead connection can't hang forever
MAX_RETRIES = 3
BODY_SNIPPET_CHARS = 800   # how much body text is kept per candidate email
BODY_FETCH_BYTES = 50000   # only the first ~50 KB of each candidate message is downloaded
BODY_BATCH_SIZE = 50       # bodies are bigger than headers, so use smaller batches

HEADER_FIELDS = "(UID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE LIST-UNSUBSCRIBE MESSAGE-ID)])"


class AccountLoginError(Exception):
    """Login was refused. Not retried."""


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

def _tls_context() -> ssl.SSLContext:
    """TLS settings for the mail connection: the server's certificate and host name
    are always checked, so nobody on the network can pose as the mail server and
    collect the app password.

    `imaplib.IMAP4_SSL` called WITHOUT a context encrypts the connection but does not
    check who is on the other end, so a context is always passed in explicitly. The
    computer's own trusted certificates are used, plus the `certifi` bundle when it is
    installed, because packaged programs can't always find the system's list.
    """
    ctx = ssl.create_default_context()      # CERT_REQUIRED + check_hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass                                 # the system's own certificates still apply
    return ctx


def _certificate_message(account: Account, exc: ssl.SSLCertVerificationError) -> str:
    reason = getattr(exc, "verify_message", "") or getattr(exc, "reason", "") or str(exc)
    return (
        f"Vestige could not verify the identity of the mail server for '{account.name}' "
        f"({account.imap_server}:{account.imap_port}): {reason}.\n"
        "Your password was NOT sent. Check that the IMAP server name is correct. If you are on "
        "public or shared Wi-Fi, try another network, because someone may be intercepting the "
        "connection. If this is your own mail server with a self-signed certificate, add its "
        "certificate authority to your computer's trusted certificates; Vestige does not skip "
        "this check."
    )


def connect(account: Account) -> imaplib.IMAP4_SSL:
    if not account.imap_server:
        raise AccountLoginError(f"'{account.name}' has no IMAP server set.")
    try:
        conn = imaplib.IMAP4_SSL(account.imap_server, account.imap_port,
                                 ssl_context=_tls_context(), timeout=IMAP_TIMEOUT)
    except ssl.SSLCertVerificationError as exc:
        # login() is never reached, so the password is never sent to an unverified server.
        # AccountLoginError is not retried (see _retry).
        raise AccountLoginError(_certificate_message(account, exc)) from None
    try:
        conn.login(account.email, account.password)
    except imaplib.IMAP4.error as exc:
        try:
            conn.shutdown()
        except Exception:
            pass
        if is_microsoft_mail(account.email, account.imap_server):
            # Not a typo in the password: Microsoft rejects every password-based IMAP login.
            raise AccountLoginError(f"Login failed for {account.email}. {MICROSOFT_PASSWORD_NOTE}\n"
                                    f"(The server said: {exc})") from None
        raise AccountLoginError(
            f"Login failed for {account.email}. Use an APP password (not your normal "
            f"password) and make sure IMAP access is enabled for the account. "
            f"The server said: {exc}"
        ) from None
    return conn


def _retry(fn, *args, **kwargs):
    """Retry temporary network problems with growing delays."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, TimeoutError, OSError) as exc:
            last_exc = exc
            wait = 2 ** attempt
            print(f"    mail server problem ({exc}); retrying in {wait}s [{attempt}/{MAX_RETRIES}]")
            time.sleep(wait)
    raise last_exc


def _chunk(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Header parsing helpers (shared with module_a_mbox.py)
# ---------------------------------------------------------------------------

def _decode_header_value(raw) -> str:
    if not raw:
        return ""
    raw = str(raw)           # MBOX parsing can hand back Header objects
    try:
        decoded = []
        for text, enc in email.header.decode_header(raw):
            if isinstance(text, bytes):
                decoded.append(text.decode(enc or "utf-8", errors="replace"))
            else:
                decoded.append(text)
        return "".join(decoded).strip()
    except Exception:
        return raw.strip()


def _parse_date(raw):
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def _best_recipient_address(to_header, account: Account, sender_domain: str | None = None) -> str:
    """Work out which of YOUR addresses an email was sent to. This matters twice:
    for "which address did I use for this service?" and for email-change
    detection, where the notice usually goes to an OLD address.

    Rules, in order:
      1. an address on the mailbox's own domain (including plus-aliases such as
         you+spotify@gmail.com) - the normal case;
      2. otherwise, if the email names exactly ONE recipient and it isn't on the
         sender's own domain, that is almost certainly another address of yours
         that forwards into this mailbox (an old address you still receive mail
         for) - exactly where change notices tend to land;
      3. otherwise the mailbox's own address.
    """
    account_local, _, account_domain = account.email.lower().partition("@")
    addresses = []
    if to_header:
        addresses = [a.lower().strip() for _, a in email.utils.getaddresses([str(to_header)]) if a and "@" in a]
    for addr in addresses:
        local, _, domain = addr.partition("@")
        if domain == account_domain and (local == account_local or local.startswith(account_local + "+")):
            return addr
    for addr in addresses:
        if addr.endswith("@" + account_domain):
            return addr
    if len(addresses) == 1 and addresses[0].split("@")[-1] != (sender_domain or ""):
        return addresses[0]
    return account.email.lower()


def build_header_row(account: Account, folder: str, uid, from_raw, to_raw, subject,
                     date_raw, message_id, list_unsub) -> dict:
    """Assemble one `emails` row from already-decoded header values."""
    sender_addrs = email.utils.getaddresses([from_raw]) if from_raw else []
    sender_email = sender_addrs[0][1].lower() if sender_addrs else ""
    sender_domain = sender_email.split("@")[-1] if "@" in sender_email else None
    return {
        "account_name": account.name,
        "email_address_used": _best_recipient_address(to_raw, account, sender_domain),
        # No Message-ID? Make a stable stand-in. The folder is part of it because
        # UIDs are only unique within one folder.
        "message_id": (str(message_id).strip() if message_id else f"<no-id-{account.name}-{folder}-{uid}>"),
        "uid": uid, "folder": folder,
        "sender_raw": from_raw, "sender_email": sender_email, "sender_domain": sender_domain,
        "subject": subject, "date_raw": str(date_raw) if date_raw else None,
        "date_iso": _parse_date(date_raw),
        "list_unsubscribe": str(list_unsub) if list_unsub else None,
        "fetched_at": db.now_iso(),
    }


def store_message(conn, row: dict, snippet="") -> bool:
    """Store one message that arrived from a FILE (MBOX, PST, EML) rather than from IMAP,
    with its body snippet. Returns True if it was new, False if that message (same
    account + Message-ID) was already stored.

    `snippet` may be a string or a zero-argument function that returns one. A function
    is only called for NEW messages, so re-importing a big file doesn't re-parse every
    body it already has. If the function raises, the snippet is simply left empty.

    body_fetched is set to 1 even when the snippet is empty: a file-imported row has
    no IMAP UID, so there is nothing more that could ever be fetched for it."""
    email_id = db.insert_email(conn, row)
    if not email_id:
        return False
    if callable(snippet):
        try:
            snippet = snippet()
        except Exception:
            snippet = ""
    conn.execute("UPDATE emails SET body_snippet=?, body_fetched=1 WHERE id=?",
                 ((snippet or "")[:BODY_SNIPPET_CHARS], email_id))
    return True


# ---------------------------------------------------------------------------
# Stage 1: headers
# ---------------------------------------------------------------------------

def fetch_headers_for_account(account: Account, conn, since: str | None = None, control=None) -> int:
    """Fetch headers for every folder of one account. `since` is an IMAP date
    like '01-Jan-2020' to limit how far back to look. Returns rows inserted."""
    print(f"[{account.name}] connecting to {account.imap_server} ...")
    jobcontrol.indeterminate(control, f"Connecting to {account.imap_server}...")
    imap = _retry(connect, account)
    total_inserted = 0
    # Progress: the server only tells us a folder's size when we search it, so the
    # total grows as folders are discovered. The tracker never lets the bar go back.
    messages_total = messages_done = 0
    try:
        for folder in account.folders:
            jobcontrol.check(control)
            try:
                typ, _ = imap.select(folder, readonly=True)       # readonly: can't change anything
                if typ != "OK":
                    print(f"  (skipping folder {folder}: cannot select)")
                    continue
            except imaplib.IMAP4.error:
                print(f"  (skipping folder {folder}: does not exist on this account)")
                continue

            search_cmd = f"SINCE {since}" if since else "ALL"
            typ, data = _retry(imap.uid, "search", None, search_cmd)
            uids = data[0].split() if data and data[0] else []
            print(f"  [{folder}] {len(uids)} messages")
            messages_total += len(uids)
            if messages_total:
                jobcontrol.report(control, messages_done, messages_total,
                                  f"{messages_done:,} of {messages_total:,} messages")

            for batch_num, batch in enumerate(_chunk(uids, IMAP_BATCH_SIZE), start=1):
                jobcontrol.check(control)
                typ, data = _retry(imap.uid, "fetch", b",".join(batch).decode(), HEADER_FIELDS)
                rows = _parse_fetch_batch(data, account, folder)
                inserted = db.bulk_insert_emails(conn, rows)
                total_inserted += inserted
                messages_done += len(batch)
                jobcontrol.report(control, messages_done, messages_total,
                                  f"{messages_done:,} of {messages_total:,} messages")
                print(f"    batch {batch_num}: {len(rows)} read, {inserted} new")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return total_inserted


def _parse_fetch_batch(data, account: Account, folder: str) -> list:
    rows = []
    for part in data:
        if not isinstance(part, tuple):
            continue                                  # the closing ")" lines
        meta, header_bytes = part
        uid_match = re.search(rb"UID (\d+)", meta)
        uid = uid_match.group(1).decode() if uid_match else None
        msg = email.message_from_bytes(header_bytes)
        rows.append(build_header_row(
            account, folder, uid,
            from_raw=_decode_header_value(msg.get("From")),
            to_raw=_decode_header_value(msg.get("To")),
            subject=_decode_header_value(msg.get("Subject")),
            date_raw=msg.get("Date"), message_id=msg.get("Message-ID"),
            list_unsub=msg.get("List-Unsubscribe"),
        ))
    return rows


# ---------------------------------------------------------------------------
# Turning a message body into a short, clean text snippet
# (also used by module_a_mbox.py, which has the whole message already)
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
# Invisible characters marketing emails use as "preheader padding".
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff\u034f\u00ad]")


class _TextExtractor(HTMLParser):
    """Collects visible text from HTML, skipping <style> and <script> blocks.
    (Naively removing tags would leave pages of CSS in the snippet, wasting AI
    tokens and hurting accuracy.)"""

    _SKIP = {"style", "script"}
    _BREAK = {"br", "p", "div", "tr", "li", "td", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def _html_to_text(raw_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw_html)       # last resort for badly broken HTML
        text = html.unescape(text)
    return _WS_RE.sub(" ", _INVISIBLE_RE.sub("", text)).strip()


def _extract_snippet(msg) -> str:
    """Best-effort readable text from a parsed email message, trimmed to
    BODY_SNIPPET_CHARS. Prefers the text/plain part, falls back to HTML."""
    plain = html_part = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text

    return snippet_from_texts(plain, html_part)


def snippet_from_texts(plain: str | None, html_part: str | None) -> str:
    """Pick the readable text from an email's plain and HTML versions and trim it to
    BODY_SNIPPET_CHARS. Prefers the plain text, but falls back to the HTML when the
    plain part is only a "view in browser" stub."""
    body = _WS_RE.sub(" ", _INVISIBLE_RE.sub("", plain)).strip() if plain else ""
    if len(body) < 40 and html_part:                  # a "view in browser" stub - use the HTML instead
        body = _html_to_text(html_part) or body
    return body[:BODY_SNIPPET_CHARS]


# ---------------------------------------------------------------------------
# Stage 3: body snippets (only for candidates)
# ---------------------------------------------------------------------------

def fetch_body_snippets(account: Account, conn, email_rows: list, control=None) -> int:
    """Download the start of each candidate message and store a clean snippet.
    `email_rows` are `emails` rows for this account that still lack a body.
    Returns how many were stored."""
    by_folder: dict = {}
    for row in email_rows:
        by_folder.setdefault(row["folder"], []).append(row)
    if not by_folder:
        return 0

    jobcontrol.indeterminate(control, f"Connecting to {account.imap_server}...")
    imap = _retry(connect, account)
    stored = done = 0
    total = len(email_rows)
    try:
        for folder, rows in by_folder.items():
            imap.select(folder, readonly=True)
            for batch in _chunk(rows, BODY_BATCH_SIZE):
                jobcontrol.check(control)
                uid_map = {r["uid"]: r["id"] for r in batch if r["uid"]}
                if not uid_map:
                    continue
                # BODY.PEEK[]<0.N>: the first N bytes of the WHOLE message (headers
                # + MIME structure), not marked as read. Parsing a real message
                # lets us pick the text part properly instead of guessing.
                typ, data = _retry(imap.uid, "fetch", ",".join(uid_map),
                                   f"(UID BODY.PEEK[]<0.{BODY_FETCH_BYTES}>)")
                seen = set()
                for part in data or []:
                    if not isinstance(part, tuple):
                        continue
                    meta, raw = part
                    uid_match = re.search(rb"UID (\d+)", meta)
                    if not uid_match:
                        continue
                    uid = uid_match.group(1).decode()
                    email_id = uid_map.get(uid)
                    if email_id is None:
                        continue
                    try:
                        snippet = _extract_snippet(email.message_from_bytes(raw))
                    except Exception:
                        snippet = ""
                    db.set_body_snippet(conn, email_id, snippet)
                    seen.add(uid)
                    stored += 1
                # A message that has since been deleted/moved never comes back.
                # Mark it done so it isn't requested again on every run.
                for uid, email_id in uid_map.items():
                    if uid not in seen:
                        db.set_body_snippet(conn, email_id, "")
                conn.commit()
                done += len(batch)
                jobcontrol.report(control, done, total, f"{done:,} of {total:,} emails")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stored
