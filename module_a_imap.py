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
  * The connection heals itself. If it drops part-way through a big mailbox the scan
    reconnects, re-opens the folder and carries on (see _Mailbox).
  * Every reply status is checked. "OK" is the only answer treated as success: a NO or BAD
    is never mistaken for "no mail" or "message deleted". A batch the server refuses is
    split to isolate the message it chokes on, anything still refused is left as it was so
    the next run tries it again, and nothing is recorded as done that wasn't.
  * The mail server's TLS certificate AND host name are always verified before the
    password is sent (see _tls_context). A server that fails the check is refused,
    clearly reported and not retried. There is deliberately no option to turn this off.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.header
import email.utils
import hashlib
import html
import imaplib
import re
import ssl
import time
from datetime import date, timezone
from html.parser import HTMLParser
from typing import NamedTuple

import billing_math
import db
import jobcontrol
import module_b_prefilter
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


class ServerRefused(Exception):
    """The mail server answered a command with NO or BAD (or gave the same refusal every time we
    asked). That is not a broken connection: whoever was asking skips what it was doing, says so,
    and carries on with the rest."""


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
    """Run `fn`, retrying temporary connection problems with growing delays.

    A plain `imaplib.IMAP4.error` is the server saying BAD: the request itself is wrong, so asking
    again can't help and it is raised at once. (`IMAP4.abort`, a dropped connection, IS retried.)"""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except (imaplib.IMAP4.abort, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                _pause(attempt, str(exc))
    raise last_exc


def _pause(attempt: int, reason: str) -> None:
    wait = 2 ** attempt
    print(f"    mail server problem ({reason}); retrying in {wait}s [{attempt}/{MAX_RETRIES}]")
    time.sleep(wait)


def _reply_text(data) -> str:
    """The words of a NO answer, shortened, for the log (e.g. ': Some messages could not be FETCHed')."""
    first = data[0] if data else None
    if isinstance(first, bytes):
        first = first.decode("utf-8", errors="replace")
    text = " ".join(str(first).split())[:120] if first else ""
    return f": {text}" if text else ""


def _chunk(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# The "only mail since" date
# ---------------------------------------------------------------------------

# IMAP dates always use these English month names, whatever language the computer is set to, so
# the text is built by hand instead of with strftime (which follows the computer's language).
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_FULL = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December")
_MONTH_NUMBER = {**{name.lower(): n for n, name in enumerate(_MONTH_ABBR, start=1)},
                 **{name.lower(): n for n, name in enumerate(_MONTH_FULL, start=1)}, "sept": 9}
_SINCE_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_SINCE_TEXT = re.compile(r"(\d{1,2})[-\s]+([A-Za-z]+)[-\s]+(\d{4})")


def parse_since(text) -> str | None:
    """Turn what a person typed into the date form an IMAP server understands ('01-Jan-2024'),
    or None when nothing was typed. Accepts 01-Jan-2024, 1 January 2024 and 2024-01-31. Anything
    else raises ValueError with a message that says what to write. (A day/month/year order such as
    01/02/2024 is refused on purpose: it means different dates in different countries.)"""
    value = text.strip() if isinstance(text, str) else ""
    if not value:
        return None
    year = month = day = None
    match = _SINCE_ISO.fullmatch(value)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = _SINCE_TEXT.fullmatch(value)
        if match:
            day, year = int(match.group(1)), int(match.group(3))
            month = _MONTH_NUMBER.get(match.group(2).lower())
    try:
        date(year, month, day)                      # also rejects 31-Feb and the like
    except (TypeError, ValueError):
        raise ValueError(f"'{value}' isn't a date Vestige can use. Write it like 01-Jan-2024 or "
                         f"2024-01-31 (or leave the box empty for all mail).") from None
    return f"{day:02d}-{_MONTH_ABBR[month - 1]}-{year:04d}"


# ---------------------------------------------------------------------------
# One connection that repairs itself
# ---------------------------------------------------------------------------

class _Mailbox:
    """An IMAP connection that survives a drop.

    Mail servers close idle or long-running connections, and a mailbox of 100,000 messages takes
    long enough for that to happen. `imaplib` cannot reuse a connection once it has failed, so the
    old approach of calling the same command again could only fail again. This class instead throws
    the dead connection away, logs in again, re-opens the folder that was open, and repeats the
    command. Commands go through `uid()` and folders through `select()`.

    Three kinds of trouble are told apart:
      * the connection broke  -> reconnect and try again (up to MAX_RETRIES times, then raise);
      * the server said BAD   -> the request is wrong; ServerRefused at once, no point retrying;
      * the server said NO    -> try again a few times, then ServerRefused.
    Only an "OK" answer is ever returned as success."""

    def __init__(self, account: Account):
        self.account = account
        self.imap = None
        self.folder = None                 # the folder that should be open (re-opened after a reconnect)
        self._opened_before = False

    # -- the connection itself ----------------------------------------------------------------
    def open(self) -> None:
        """Connect and log in (AccountLoginError if the login is refused), then re-open the folder
        that was open before, if any."""
        if self._opened_before:
            print("    reconnecting to the mail server ...")
        self.imap = _retry(connect, self.account)      # has its own retries; raises if the server can't be reached
        self._opened_before = True
        if self.folder is not None:
            try:
                self._open_folder(self.folder)
            except ServerRefused:
                self.folder = None         # the connection is fine; that folder is not
                raise
            except (imaplib.IMAP4.abort, OSError, TimeoutError):
                self._drop()               # it broke again straight away: don't keep a dead connection
                raise

    def close(self) -> None:
        imap, self.imap = self.imap, None
        if imap is None:
            return
        try:
            imap.logout()
        except Exception:
            self._shut(imap)

    def _drop(self) -> None:
        """Forget a connection that has failed."""
        imap, self.imap = self.imap, None
        if imap is not None:
            self._shut(imap)

    @staticmethod
    def _shut(imap) -> None:
        try:
            imap.shutdown()
        except Exception:
            pass

    # -- commands -------------------------------------------------------------------------------
    def _open_folder(self, folder: str) -> None:
        """SELECT `folder` read-only on the current connection (read-only: nothing can be changed)."""
        try:
            typ, _data = self.imap.select(folder, readonly=True)
        except imaplib.IMAP4.abort:
            raise                          # the connection broke: the caller reconnects
        except imaplib.IMAP4.error:
            raise ServerRefused("does not exist on this account") from None
        if typ != "OK":
            raise ServerRefused("cannot select")

    def select(self, folder: str) -> None:
        """Open `folder` read-only. Raises ServerRefused if the server won't open it."""
        self.folder = None
        drops = 0
        while True:
            if self.imap is None:
                self.open()
            try:
                self._open_folder(folder)
            except (imaplib.IMAP4.abort, OSError, TimeoutError) as exc:
                self._drop()
                drops += 1
                if drops >= MAX_RETRIES:
                    raise
                _pause(drops, f"connection lost: {exc}")
                continue
            self.folder = folder
            return

    def uid(self, *args, refusal_tries: int = MAX_RETRIES):
        """Run `imap.uid(*args)` and return its data. Heals a dropped connection; raises ServerRefused
        for a BAD answer, or for a NO that was repeated `refusal_tries` times."""
        drops = refusals = 0
        while True:
            if self.imap is None:
                self.open()                            # reconnects; raises if the server can't be reached
            try:
                typ, data = self.imap.uid(*args)
            except (imaplib.IMAP4.abort, OSError, TimeoutError) as exc:
                self._drop()
                drops += 1
                if drops >= MAX_RETRIES:
                    raise
                _pause(drops, f"connection lost: {exc}")
                continue
            except imaplib.IMAP4.error as exc:         # (abort is a kind of error: it was caught above)
                raise ServerRefused(f"the server rejected the request ({exc})") from None
            if typ == "OK":
                return data
            refusals += 1
            why = f"the server answered {typ}{_reply_text(data)}"
            if refusals >= refusal_tries:
                raise ServerRefused(why)
            _pause(refusals, why)


MAX_REFUSED_BATCHES_IN_A_ROW = 3     # a server refusing everything: stop that folder instead of crawling through it


def _fetch_uids(box: _Mailbox, uids: list, items: str, tries: int = MAX_RETRIES, budget: list | None = None):
    """UID FETCH `items` for `uids` (strings). Returns (data parts, refused uids).

    If the server refuses the whole batch (Gmail does this with "Some messages could not be FETCHed"
    when ONE message is a problem) the batch is split in halves and each half is asked for again, so a
    single bad message costs only itself. The number of extra requests is capped, so a server that
    refuses everything can't turn one batch into hundreds of requests."""
    if budget is None:
        budget = [2 * len(uids).bit_length() + 2]
    try:
        return list(box.uid("fetch", ",".join(uids), items, refusal_tries=tries) or []), []
    except ServerRefused:
        if len(uids) == 1 or budget[0] <= 0:
            return [], list(uids)
    budget[0] -= 1
    middle = len(uids) // 2
    parts, refused = [], []
    for half in (uids[:middle], uids[middle:]):
        got, bad = _fetch_uids(box, half, items, tries=1, budget=budget)
        parts += got
        refused += bad
    return parts, refused


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


# ---------------------------------------------------------------------------
# Stand-in Message-IDs
# ---------------------------------------------------------------------------
# Emails are kept once per (account, Message-ID), which is how re-running a scan, or importing the same
# mail twice, adds nothing new. A few messages have no Message-ID (or a blank one). They still need an
# ID, and it has to be the SAME one every time that message is read, and different for different
# messages. An ID built from where the message was found (its position in a file, or the file's path)
# fails both ways: the first no-ID message of a second file got the same ID as the first no-ID message
# of the first file, and was silently thrown away, while moving a folder made every message "new".
# So the stand-in is built from what the message SAYS instead.

STAND_IN_CONTENT_BYTES = 2000        # how much of the start of a message's content goes into its stand-in ID
_BODY_START = re.compile(rb"\r?\n\r?\n")


def tidy_message_id(value) -> str:
    """A Message-ID header with the spaces around it removed; "" when there is none or it is blank."""
    return str(value).strip() if value else ""


def body_start(raw: bytes) -> bytes:
    """The first part of a raw message's content (what comes after the header block), used with the
    headers to tell two messages apart that have the same sender, subject and date."""
    match = _BODY_START.search(raw)
    return raw[match.end():match.end() + STAND_IN_CONTENT_BYTES] if match else b""


def stand_in_message_id(*identity, where: str = "") -> str:
    """The Message-ID to use for a message that has none. `identity` is what the message says: its
    From, To, Subject and Date, then the start of its content (text or bytes). The same message always
    gives the same ID, wherever and however often it is read: the same file twice, the same message in two
    exports, a folder that has moved. Two different messages give different IDs, so neither is lost.

    `where` (a file path, a position) is used ONLY when the message says nothing at all, so that a pile
    of completely blank messages isn't merged into one."""
    digest, informative = hashlib.sha256(), False
    for part in identity:
        data = part if isinstance(part, bytes) else str(part or "").strip().encode("utf-8", "replace")
        informative = informative or bool(data)
        digest.update(len(data).to_bytes(8, "big"))       # so ("ab", "c") and ("a", "bc") differ
        digest.update(data)
    if not informative:
        digest.update(where.encode("utf-8", "replace"))
    return f"<no-id-{digest.hexdigest()[:32]}@vestige>"


def build_header_row(account: Account, folder: str, uid, from_raw, to_raw, subject,
                     date_raw, message_id, list_unsub) -> dict:
    """Assemble one `emails` row from already-decoded header values."""
    sender_addrs = email.utils.getaddresses([from_raw]) if from_raw else []
    sender_email = sender_addrs[0][1].lower() if sender_addrs else ""
    sender_domain = sender_email.split("@")[-1] if "@" in sender_email else None
    return {
        "account_name": account.name,
        "email_address_used": _best_recipient_address(to_raw, account, sender_domain),
        # No Message-ID (or a blank one)? Make a stable stand-in. The folder is part of it because
        # UIDs are only unique within one folder. (Mail files build their own: see stand_in_message_id.)
        "message_id": tidy_message_id(message_id) or f"<no-id-{account.name}-{folder}-{uid}>",
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

    `snippet` may be a string, a Body, or a zero-argument function that returns either. A
    function is only called for NEW messages, so re-importing a big file doesn't re-parse every
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
    amounts = None
    if isinstance(snippet, Body):
        snippet, amounts = snippet.snippet, snippet.amounts
    db.set_body_snippet(conn, email_id, (snippet or "")[:BODY_SNIPPET_CHARS], amounts)
    return True


# ---------------------------------------------------------------------------
# Stage 1: headers
# ---------------------------------------------------------------------------

GMAIL_ALL_MAIL = '"[Gmail]/All Mail"'


def _scan_order(account: Account) -> tuple:
    """(the folders in the order to scan them, {folder: a folder that holds every message it holds}).

    In Gmail, "All Mail" contains every message that is in INBOX (a label, not a separate place), so reading
    both downloads the headers of every inbox message twice, and the copies are thrown away afterwards as
    duplicates. All Mail is scanned FIRST, and INBOX is then skipped if All Mail was read completely. If All
    Mail can't be opened (it can be hidden from IMAP in Gmail's settings, and is named differently in other
    languages) or wasn't read completely, INBOX is scanned as usual, so nothing is ever missed."""
    folders = list(account.folders)
    covered_by: dict = {}
    if account.provider == "gmail" and "INBOX" in folders and GMAIL_ALL_MAIL in folders:
        folders.remove(GMAIL_ALL_MAIL)
        folders.insert(0, GMAIL_ALL_MAIL)
        covered_by["INBOX"] = GMAIL_ALL_MAIL
    return folders, covered_by


def fetch_headers_for_account(account: Account, conn, since: str | None = None, control=None) -> int:
    """Fetch headers for every folder of one account. `since` limits how far back to look; it may be
    written 01-Jan-2020 or 2020-01-01 (see parse_since). Returns rows inserted."""
    since = parse_since(since)             # a bad date is refused here, before anything connects
    print(f"[{account.name}] connecting to {account.imap_server} ...")
    jobcontrol.indeterminate(control, f"Connecting to {account.imap_server}...")
    box = _Mailbox(account)
    box.open()
    total_inserted = 0
    # Progress: the server only tells us a folder's size when we search it, so the
    # total grows as folders are discovered. The tracker never lets the bar go back.
    messages_total = messages_done = 0
    incomplete = False
    folders, covered_by = _scan_order(account)
    read_completely: set = set()
    try:
        for folder in folders:
            jobcontrol.check(control)
            if covered_by.get(folder) in read_completely:
                print(f"  [{folder}] skipped: every message in it is also in {covered_by[folder]}, which was just read.")
                continue
            folder_complete = True
            try:
                box.select(folder)
            except ServerRefused as exc:
                print(f"  (skipping folder {folder}: {exc})")
                continue

            search_cmd = f"SINCE {since}" if since else "ALL"
            try:
                data = box.uid("search", None, search_cmd)
            except ServerRefused as exc:
                print(f"  [{folder}] skipped: the server refused the search ({exc}).")
                incomplete = True
                continue                       # (not read: so a folder that covers for it is still scanned)
            uids = data[0].split() if data and data[0] else []
            print(f"  [{folder}] {len(uids)} messages")
            messages_total += len(uids)
            if messages_total:
                jobcontrol.report(control, messages_done, messages_total,
                                  f"{messages_done:,} of {messages_total:,} messages")

            refused_in_a_row = 0
            for batch_num, batch in enumerate(_chunk(uids, IMAP_BATCH_SIZE), start=1):
                jobcontrol.check(control)
                parts, refused = _fetch_uids(box, [u.decode() for u in batch], HEADER_FIELDS)
                rows = _parse_fetch_batch(parts, account, folder)
                inserted = db.bulk_insert_emails(conn, rows)
                total_inserted += inserted
                messages_done += len(batch)
                jobcontrol.report(control, messages_done, messages_total,
                                  f"{messages_done:,} of {messages_total:,} messages")
                note = ""
                if refused:
                    incomplete = True
                    folder_complete = False
                    note = f" ({len(refused)} refused by the server)"
                print(f"    batch {batch_num}: {len(rows)} read, {inserted} new{note}")
                refused_in_a_row = refused_in_a_row + 1 if len(refused) == len(batch) else 0
                if refused_in_a_row >= MAX_REFUSED_BATCHES_IN_A_ROW:
                    print(f"  [{folder}] the server keeps refusing; leaving the rest of this folder for next time.")
                    messages_done += max(0, len(uids) - batch_num * IMAP_BATCH_SIZE)    # keep the bar moving
                    folder_complete = False
                    break
            if folder_complete:
                read_completely.add(folder)
    finally:
        box.close()
    if incomplete:
        print(f"[{account.name}] Some mail could not be read this time because the server refused it (see above). "
              f"Scan again to pick it up; mail already saved is not downloaded twice.")
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


class Body(NamedTuple):
    """What is kept from one email's text: a short snippet (sent to the AI in small pieces) and
    the money amounts printed anywhere in the first part of the text (see billing_math.py).
    The amounts are found HERE, while the whole text is in hand, because the snippet is too short
    to be sure of containing them. They are only searched for in emails that need them (see
    wants_amounts); for every other email `amounts` is empty."""
    snippet: str
    amounts: list


def wants_amounts(row) -> bool:
    """Should the money amounts be searched for in this email's text? Only if the pre-filter will treat it as
    a billing email, which it decides from the subject line alone, so asking the same question here gives
    the same answer.

    Searching every email instead cost 2-4 ms each (the search reads up to 20,000 characters), which is
    3-7 minutes for a 100,000-message mailbox, and for every email that was not a receipt the result was
    thrown away again by the next pre-filter run."""
    return module_b_prefilter.is_billing_subject(row["subject"])


def _stored_row_wants_amounts(row) -> bool:
    """The same question for a row already in the database: the pre-filter's own recorded answer, which is
    what the later stages go by. (Falls back to the subject for a row that was never pre-filtered.)"""
    try:
        flag = row["billing_candidate"]
    except (KeyError, IndexError):
        flag = None
    return bool(flag) if flag is not None else wants_amounts(row)


def _decode_part(part):
    """The decoded content of one MIME part (None if it has none).

    Base64 is decoded here, not by get_payload(decode=True), because of what that does with a message that
    ends part-way through its base64 text, which is exactly what happens whenever only the START of a message
    is read (the IMAP download is capped at BODY_FETCH_BYTES, a file import at 256 KB): when the number of
    characters is one more than a multiple of four, Python gives back the base64 text ITSELF, undecoded, as if
    it were the content, and the snippet reads "aGVsbG8gd29ybGQ..." (a quarter of the time a cut lands that
    way). The incomplete last group of characters is dropped instead."""
    if str(part.get("Content-Transfer-Encoding", "")).strip().lower() == "base64":
        text = part.get_payload()
        if isinstance(text, str):
            data = re.sub(r"[^A-Za-z0-9+/=]", "", text)
            data = data[:len(data) - len(data) % 4]
            try:
                return base64.b64decode(data)
            except (binascii.Error, ValueError):
                pass                                   # not plain base64 after all: let the standard library try
    return part.get_payload(decode=True)


def _extract_body(msg, want_amounts: bool = True) -> Body:
    """Best-effort readable text from a parsed email message: a snippet trimmed to
    BODY_SNIPPET_CHARS plus (when `want_amounts`) the amounts found. Prefers the text/plain part,
    falls back to HTML."""
    plain = html_part = None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = _decode_part(part)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text

    return body_from_texts(plain, html_part, want_amounts)


def body_from_texts(plain: str | None, html_part: str | None, want_amounts: bool = True) -> Body:
    """Pick the readable text from an email's plain and HTML versions; keep a snippet of it
    (BODY_SNIPPET_CHARS) and, when `want_amounts`, the amounts found in it. Prefers the plain text,
    but falls back to the HTML when the plain part is only a "view in browser" stub."""
    body = _WS_RE.sub(" ", _INVISIBLE_RE.sub("", plain)).strip() if plain else ""
    if len(body) < 40 and html_part:                  # a "view in browser" stub - use the HTML instead
        body = _html_to_text(html_part) or body
    return Body(body[:BODY_SNIPPET_CHARS], billing_math.extract_amounts(body) if want_amounts else [])


# ---------------------------------------------------------------------------
# Stage 3: body snippets (only for candidates)
# ---------------------------------------------------------------------------

def fetch_body_snippets(account: Account, conn, email_rows: list, control=None) -> int:
    """Download the start of each candidate message and store a clean snippet.
    `email_rows` are `emails` rows for this account that still lack a body.
    Returns how many were stored.

    An email is only marked as done when the server really answered for it: either with its text,
    or with an OK that leaves it out (it has been deleted or moved). If the server REFUSES a request
    (NO/BAD), or the folder can't be opened, those emails are left exactly as they were, so the next
    run tries them again instead of keeping an empty snippet for ever."""
    by_folder: dict = {}
    for row in email_rows:
        by_folder.setdefault(row["folder"], []).append(row)
    if not by_folder:
        return 0

    jobcontrol.indeterminate(control, f"Connecting to {account.imap_server}...")
    box = _Mailbox(account)
    box.open()
    stored = done = skipped = 0
    total = len(email_rows)
    try:
        for folder, rows in by_folder.items():
            try:
                box.select(folder)
            except ServerRefused as exc:
                print(f"  (skipping {len(rows)} email(s) in folder {folder}: {exc})")
                skipped += len(rows)
                done += len(rows)
                jobcontrol.report(control, done, total, f"{done:,} of {total:,} emails")
                continue
            refused_in_a_row = 0
            for batch in _chunk(rows, BODY_BATCH_SIZE):
                jobcontrol.check(control)
                uid_map = {r["uid"]: r["id"] for r in batch if r["uid"]}
                if not uid_map:
                    continue
                wants = {r["uid"]: _stored_row_wants_amounts(r) for r in batch if r["uid"]}
                if refused_in_a_row >= MAX_REFUSED_BATCHES_IN_A_ROW:
                    skipped += len(uid_map)         # the server keeps refusing this folder: leave the rest
                    done += len(batch)
                    jobcontrol.report(control, done, total, f"{done:,} of {total:,} emails")
                    continue
                # BODY.PEEK[]<0.N>: the first N bytes of the WHOLE message (headers
                # + MIME structure), not marked as read. Parsing a real message
                # lets us pick the text part properly instead of guessing.
                parts, refused = _fetch_uids(box, list(uid_map), f"(UID BODY.PEEK[]<0.{BODY_FETCH_BYTES}>)")
                seen = set()
                for part in parts:
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
                        body = _extract_body(email.message_from_bytes(raw), wants[uid])
                    except Exception:
                        body = Body("", [])
                    db.set_body_snippet(conn, email_id, body.snippet, body.amounts)
                    seen.add(uid)
                    stored += 1
                # A message that has since been deleted/moved never comes back, although the server
                # answered OK. Mark it done so it isn't requested again on every run. A message the
                # server REFUSED is not in that position, so it is left for the next run.
                refused_uids = set(refused)
                for uid, email_id in uid_map.items():
                    if uid not in seen and uid not in refused_uids:
                        db.set_body_snippet(conn, email_id, "")
                if refused_uids:
                    skipped += len(refused_uids)
                    print(f"    {len(refused_uids)} email(s) refused by the server; they will be tried again next run")
                refused_in_a_row = refused_in_a_row + 1 if len(refused_uids) == len(uid_map) else 0
                conn.commit()
                done += len(batch)
                jobcontrol.report(control, done, total, f"{done:,} of {total:,} emails")
    finally:
        box.close()
    if skipped:
        print(f"[{account.name}] {skipped} email(s) could not be downloaded this time (the server refused them). "
              f"They are kept as they were and will be tried again the next time you download email text.")
    return stored
