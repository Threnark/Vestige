"""Tests for the reduced downloads (batch 6 of the review):

  * the "download email text" step fetches only the emails the AI will actually READ. For sign-ups and closed
    accounts that is a few per sender (how many depends on the depth), not every shortlisted email. The set is
    worked out by the same grouping code the AI steps use, so the two cannot disagree, and a test below checks
    that every email the AI will be shown has its text;
  * the AI steps say so when emails they are about to read have no text (the depth was raised after the download);
  * for Gmail, the INBOX is not scanned a second time when "All Mail" (which contains it) was just read in full.

The expected downloads are worked out BY HAND from how the mailbox below is built, not by asking the code under test.
"""
import contextlib
import imaplib
import io
import unittest
from unittest import mock

import ai_batches
import db
import models
import module_a_imap as imap_mod
import module_b_prefilter as prefilter
import module_c_classify
import module_f_closures
import pipeline
import run_options
from models import Account
from tests.test_imap_robustness import NO_FETCH, ImapCase, body_reply

ACCT = Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x")
RAW = (b"From: a@acme.com\r\nSubject: x\r\nContent-Type: text/plain\r\n\r\n"
       b"Thanks for joining. Your account is ready to use.")
VERIFY, WELCOME = "Verify your email address", "Welcome to Beta - confirm your account"
CHANGED, CLOSED, DIGEST = "Your email address was changed", "Your account has been closed", "Weekly digest"


def quietly(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class Mailbox(ImapCase):
    """A mailbox whose shortlists are known exactly:

      acme.com            8 sign-up candidates (uids 100-107, oldest first)    Balanced reads the oldest 3
      beta.com            2 sign-up candidates (200-201)                        both
      accounts.google.com 3 sign-up candidates (300-302), an identity provider  each is judged on its own: all 3
      delta.com           2 email-change notices (400-401)                      every notice is read: both
      gamma.com           5 closure notices (500-504, oldest first)             Balanced reads the newest 2
      news.com            5 newsletters (600-604)                               not shortlisted at all
    """

    def setUp(self):
        super().setUp()
        plan = [("acme.com", VERIFY, range(100, 108)), ("beta.com", WELCOME, range(200, 202)),
                ("accounts.google.com", VERIFY, range(300, 303)), ("delta.com", CHANGED, range(400, 402)),
                ("gamma.com", CLOSED, range(500, 505)), ("news.com", DIGEST, range(600, 605))]
        for domain, subject, uids in plan:
            for day, uid in enumerate(uids, start=1):
                db.insert_email(self.conn, imap_mod.build_header_row(
                    ACCT, "INBOX", str(uid), f"Sender <n@{domain}>", "me@gmail.com", subject,
                    f"{day:02d} Jun 2017 09:00:00 +0000", f"<{uid}@t>", None))
        self.conn.commit()
        prefilter.run_prefilter(self.conn)
        self.server.on_uid = lambda c, cmd, uid_set, items: body_reply(uid_set, {u: RAW for u in uid_set.split(",")})

    def download(self, pipelines=None, depth="balanced") -> set:
        quietly(pipeline.stage_fetch_bodies, self.conn, [ACCT], None, pipelines, depth)
        return {uid for uid_set in self.server.fetches() for uid in uid_set.split(",")}

    @staticmethod
    def uids(*ranges) -> set:
        return {str(u) for r in ranges for u in r}

    def fetched(self) -> set:
        return {r["uid"] for r in self.conn.execute("SELECT uid FROM emails WHERE body_fetched=1")}


class OnlyWhatTheAiWillRead(Mailbox):
    def test_balanced(self):
        expected = self.uids(range(100, 103), range(200, 202), range(300, 303), range(400, 402), range(503, 505))
        self.assertEqual(self.download(), expected)
        self.assertEqual(len(expected), 12)

    def test_quick_reads_less(self):
        expected = self.uids([100], [200], range(300, 303), range(400, 402), [504])
        self.assertEqual(self.download(depth="quick"), expected)

    def test_thorough_reads_more(self):
        expected = self.uids(range(100, 105), range(200, 202), range(300, 303), range(400, 402), range(502, 505))
        self.assertEqual(self.download(depth="thorough"), expected)

    def test_it_is_a_good_deal_fewer_than_everything_shortlisted(self):
        before = len(db.emails_missing_body(self.conn, ("signups", "changes", "closed")))
        self.assertEqual(before, 20)                            # what the old code downloaded for this mailbox
        self.assertEqual(len(self.download()), 12)

    def test_the_log_says_what_was_left_out(self):
        _, log = quietly(pipeline.stage_fetch_bodies, self.conn, [ACCT], None, None, "balanced")
        self.assertIn("Not downloading 8 other shortlisted emails", log)

    def test_a_second_run_downloads_nothing_more(self):
        self.download()
        self.server.calls.clear()
        _, log = quietly(pipeline.stage_fetch_bodies, self.conn, [ACCT], None, None, "balanced")
        self.assertEqual(self.server.fetches(), [])
        self.assertIn("No email bodies need fetching", log)

    def test_only_the_ticked_pipelines_are_downloaded(self):
        self.assertEqual(self.download(("changes",)), self.uids(range(400, 402)))

    def test_signups_alone_do_not_download_the_other_pipelines_emails(self):
        self.assertEqual(self.download(("signups",)), self.uids(range(100, 103), range(200, 202), range(300, 303)))

    def test_closed_accounts_alone_download_the_newest_notices(self):
        self.assertEqual(self.download(("closed",)), self.uids(range(503, 505)))

    def test_unshortlisted_mail_is_never_downloaded(self):
        self.assertFalse(self.download(depth="thorough") & self.uids(range(600, 605)))

    def test_a_sender_already_judged_is_not_downloaded_again(self):
        """Once a sender has a verdict it is never shown to the AI again, so its emails need no text."""
        self.conn.execute("INSERT INTO classifications(group_key, sender_domain, is_signup, confidence, "
                          "service_name, signup_date_iso, representative_email_id, reasoning, raw_model_response) "
                          "VALUES ('acme.com','acme.com',1,0.9,'Acme',NULL,NULL,'','')")
        self.conn.commit()
        self.assertEqual(self.download(("signups",)), self.uids(range(200, 202), range(300, 303)))


class EveryEmailTheAiWillBeShownHasItsText(Mailbox):
    """The guard that matters most: narrowing the download must never leave the AI reading an empty snippet."""

    def check(self, depth_name):
        depth = run_options.get_depth(depth_name)
        self.download(depth=depth_name)
        shown = [row for group in prefilter.build_groups(self.conn, depth.signup_samples) for row in group.samples]
        shown += [row for group in module_f_closures.build_groups(self.conn, depth) for row in group.samples]
        shown += list(self.conn.execute("SELECT * FROM emails WHERE change_candidate=1"))
        self.assertGreaterEqual(len(shown), 8)
        for row in shown:
            self.assertEqual(row["body_fetched"], 1, f"uid {row['uid']} ({row['subject']}) will be read with no text")
            self.assertIn("account is ready", row["body_snippet"])
        _, output = quietly(ai_batches.note_missing_text, shown)
        self.assertEqual(output, "")

    def test_quick(self):
        self.check("quick")

    def test_balanced(self):
        self.check("balanced")

    def test_thorough(self):
        self.check("thorough")


class RaisingTheDepthAfterTheDownload(Mailbox):
    def test_the_ai_steps_say_which_emails_have_no_text(self):
        self.download(depth="quick")
        depth = run_options.get_depth("thorough")
        rows = [row for group in prefilter.build_groups(self.conn, depth.signup_samples) for row in group.samples]
        missing = sum(1 for r in rows if not r["body_fetched"])
        self.assertGreater(missing, 0)
        _, output = quietly(ai_batches.note_missing_text, rows)
        self.assertIn(f"{missing:,} of the {len(rows):,}", output)
        self.assertIn('"Download email text"', output)

    def test_the_sign_up_step_prints_it_in_a_real_run(self):
        self.download(depth="quick")
        provider = mock.Mock(last_usage=None)
        provider.structured_call.return_value = []
        _, output = quietly(module_c_classify.classify_all, self.conn, provider, batch_size=5, depth="thorough")
        self.assertIn("NOTE: the text of", output)

    def test_the_closed_accounts_step_prints_it_too(self):
        self.download(("closed",), depth="quick")
        provider = mock.Mock(last_usage=None)
        provider.structured_call.return_value = []
        _, output = quietly(module_f_closures.check_closed, self.conn, provider, batch_size=5, depth="thorough")
        self.assertIn("NOTE: the text of", output)

    def test_nothing_is_said_when_every_email_has_its_text(self):
        self.download(depth="thorough")
        provider = mock.Mock(last_usage=None)
        provider.structured_call.return_value = []
        _, output = quietly(module_c_classify.classify_all, self.conn, provider, batch_size=5, depth="balanced")
        self.assertNotIn("NOTE", output)

    def test_mail_that_came_from_a_file_never_triggers_it(self):
        rows = [{"uid": None, "body_fetched": 1}, {"uid": None, "body_fetched": 0}]
        self.assertEqual(quietly(ai_batches.note_missing_text, rows)[1], "")


# ---------------------------------------------------------------------------------------------------
# Gmail: All Mail already contains the INBOX
# ---------------------------------------------------------------------------------------------------
def header_reply(uid_set, ids):
    out = []
    for uid in uid_set.split(","):
        raw = (f"From: Acme <a@acme.com>\r\nTo: me@gmail.com\r\nSubject: Hello\r\n"
               f"Date: Mon, 05 Jun 2017 09:00:00 +0000\r\nMessage-ID: {ids[uid]}\r\n\r\n").encode()
        out.append((f"{uid} (UID {uid} BODY[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] {{{len(raw)}}}".encode(), raw))
        out.append(b")")
    return "OK", out


class GmailFolders(ImapCase):
    ALL_MAIL = imap_mod.GMAIL_ALL_MAIL
    GMAIL = Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x", folders=["INBOX", imap_mod.GMAIL_ALL_MAIL])
    # Gmail numbers messages separately in each folder; the three in the INBOX are also in All Mail.
    IN_ALL_MAIL = {"11": "<a@x>", "12": "<b@x>", "13": "<c@x>", "14": "<d@x>", "15": "<e@x>", "16": "<f@x>"}
    IN_INBOX = {"1": "<a@x>", "2": "<b@x>", "3": "<c@x>"}

    def setUp(self):
        super().setUp()
        self.selected = []
        self.refuse = lambda folder, uid_set: False
        self.search_answer = None
        self.can_open = lambda folder: True

        def on_select(conn, folder):
            if not self.can_open(folder):
                raise imaplib.IMAP4.error("SELECT command error: BAD [NONEXISTENT] Unknown Mailbox")
            self.selected.append(folder)
            return "OK", [b"1"]

        def on_uid(conn, command, *args):
            ids = self.IN_ALL_MAIL if conn.folder == self.ALL_MAIL else self.IN_INBOX
            if command == "search":
                if self.search_answer and conn.folder == self.ALL_MAIL:
                    return self.search_answer
                return "OK", [" ".join(ids).encode()]
            return NO_FETCH if self.refuse(conn.folder, args[0]) else header_reply(args[0], ids)
        self.server.on_select, self.server.on_uid = on_select, on_uid

    def scan(self, account=None):
        return quietly(imap_mod.fetch_headers_for_account, account or self.GMAIL, self.conn)

    def stored(self) -> set:
        return {r["message_id"] for r in self.conn.execute("SELECT message_id FROM emails")}

    def searched_folders(self) -> list:
        return [c for c in self.selected]

    def test_all_mail_is_read_first_and_the_inbox_is_then_skipped(self):
        inserted, log = self.scan()
        self.assertEqual(self.selected, [self.ALL_MAIL])
        self.assertEqual(inserted, 6)
        self.assertEqual(self.stored(), {"<a@x>", "<b@x>", "<c@x>", "<d@x>", "<e@x>", "<f@x>"})
        self.assertIn("[INBOX] skipped", log)
        self.assertIn("is also in", log)

    def test_the_inbox_messages_are_not_downloaded_a_second_time(self):
        self.scan()
        downloaded = {uid for uid_set in self.server.fetches() for uid in uid_set.split(",")}
        self.assertEqual(downloaded, set(self.IN_ALL_MAIL))
        self.assertFalse(downloaded & set(self.IN_INBOX))

    def test_everything_is_found_exactly_as_when_both_were_scanned(self):
        """The skipped folder never held anything that All Mail did not."""
        self.scan()
        skipping = self.stored()
        self.conn.execute("DELETE FROM emails")
        with mock.patch.object(imap_mod, "_scan_order", lambda account: (list(account.folders), {})):
            self.scan()
        self.assertEqual(self.stored(), skipping)

    def test_the_inbox_is_scanned_when_all_mail_cannot_be_opened(self):
        """All Mail can be hidden from IMAP in Gmail's settings, and is named differently in other languages."""
        self.can_open = lambda folder: folder != self.ALL_MAIL
        inserted, log = self.scan()
        self.assertEqual(self.selected, ["INBOX"])
        self.assertEqual(self.stored(), {"<a@x>", "<b@x>", "<c@x>"})
        self.assertIn("skipping folder", log)

    def test_the_inbox_is_scanned_when_the_search_of_all_mail_is_refused(self):
        self.search_answer = NO_FETCH
        inserted, log = self.scan()
        self.assertEqual(self.selected, [self.ALL_MAIL, "INBOX"])
        self.assertEqual(self.stored(), {"<a@x>", "<b@x>", "<c@x>"})

    def test_the_inbox_is_scanned_when_all_mail_was_only_partly_read(self):
        self.refuse = lambda folder, uid_set: folder == self.ALL_MAIL and "11" in uid_set.split(",")
        inserted, log = self.scan()
        self.assertEqual(self.selected, [self.ALL_MAIL, "INBOX"])
        self.assertEqual(self.stored(), {"<a@x>", "<b@x>", "<c@x>", "<d@x>", "<e@x>", "<f@x>"},
                         "message <a@x> was refused in All Mail but is recovered from the INBOX")
        self.assertIn("Scan again", log)

    def test_other_providers_are_not_affected(self):
        other = Account("o", "other", "me@x.com", "imap.x.com", password="x", folders=["INBOX", self.ALL_MAIL])
        self.scan(other)
        self.assertEqual(sorted(self.selected), sorted(["INBOX", self.ALL_MAIL]))

    def test_a_gmail_account_with_only_the_inbox_or_only_all_mail_is_not_affected(self):
        for folders in (["INBOX"], [self.ALL_MAIL]):
            self.selected.clear()
            self.scan(Account("gm", "gmail", "me@gmail.com", "imap.gmail.com", password="x", folders=folders))
            self.assertEqual(self.selected, folders)

    def test_the_order_is_only_changed_for_the_gmail_pair(self):
        self.assertEqual(imap_mod._scan_order(self.GMAIL), ([self.ALL_MAIL, "INBOX"], {"INBOX": self.ALL_MAIL}))
        plain = Account("o", "other", "me@x.com", "imap.x.com", password="x", folders=["INBOX", "Archive"])
        self.assertEqual(imap_mod._scan_order(plain), (["INBOX", "Archive"], {}))

    def test_the_folder_name_matches_the_one_the_app_offers_for_gmail(self):
        self.assertIn(imap_mod.GMAIL_ALL_MAIL, models.DEFAULT_FOLDERS["gmail"])


# ---------------------------------------------------------------------------------------------------
# The per-sender lookups
# ---------------------------------------------------------------------------------------------------
class PerSenderQueries(unittest.TestCase):
    """SQLite picked the index on a flag column (a few distinct values) instead of the sender index for these
    lookups, so each one scanned every shortlisted email: 5-7 ms per sender instead of 0.03 ms, which added up to
    ~11 s for 2,000 senders each time the sign-up or closure step grouped its emails."""

    @classmethod
    def setUpClass(cls):
        import random
        random.seed(3)
        cls.conn = db.open_database(None)
        subjects = ["Weekly digest", "Welcome to Acme", "Verify your email", "Your account has been closed", "Hello"]
        rows = [dict(account_name="a", email_address_used="me@x.com", message_id=f"<{i}@x>", uid=str(i), folder="INBOX",
                     sender_raw="s", sender_email=f"n@d{i % 1000}.com", sender_domain=f"d{i % 1000}.com",
                     subject=random.choice(subjects), date_raw=None,
                     date_iso=f"2024-{1 + i % 3:02d}-{1 + i % 5:02d}T00:00:00+00:00",     # few distinct dates: plenty of ties
                     list_unsubscribe=None, fetched_at="x") for i in range(30_000)]
        db.bulk_insert_emails(cls.conn, rows)
        prefilter.run_prefilter(cls.conn)

    def plan_of_the_query_run_by(self, call) -> str:
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            call()
        finally:
            self.conn.set_trace_callback(None)
        select = next(sql for sql in seen if sql.lstrip().upper().startswith("SELECT"))
        return " | ".join(row[3] for row in self.conn.execute("EXPLAIN QUERY PLAN " + select))

    def test_the_signup_lookup_uses_the_sender_index(self):
        plan = self.plan_of_the_query_run_by(lambda: db.candidate_emails_for_domain(self.conn, "d5.com", limit=3))
        self.assertIn("idx_emails_domain", plan)
        self.assertNotIn("idx_emails_prefilter", plan)

    def test_the_closure_lookup_uses_the_sender_index(self):
        plan = self.plan_of_the_query_run_by(lambda: db.closure_candidate_emails(self.conn, "d5.com", limit=2))
        self.assertIn("idx_emails_domain", plan)
        self.assertNotIn("idx_emails_closure", plan)

    def test_the_address_lookup_uses_the_sender_index(self):
        cls_row = {"group_key": "d5.com", "sender_domain": "d5.com", "representative_email_id": None}
        plan = self.plan_of_the_query_run_by(lambda: db.signup_addresses(self.conn, cls_row))
        self.assertIn("idx_emails_domain", plan)
        self.assertNotIn("idx_emails_prefilter", plan)

    def test_grouping_every_sender_is_fast(self):
        import time
        started = time.perf_counter()
        groups = prefilter.build_groups(self.conn, 3)
        elapsed = time.perf_counter() - started
        self.assertGreater(len(groups), 500)
        # 0.04 s here; the old queries took 2.4 s for the same 1,000 senders. The limit leaves a wide margin
        # for a slow machine and still fails the old behaviour.
        self.assertLess(elapsed, 0.5, f"{len(groups)} senders took {elapsed:.2f}s")

    def test_the_results_are_what_they_were_apart_from_a_fixed_tie_break(self):
        for domain in ("d5.com", "d17.com", "d400.com"):
            rows = self.conn.execute("SELECT * FROM emails WHERE sender_domain=? AND prefilter_label='candidate'", (domain,)).fetchall()
            expected = sorted(rows, key=lambda r: (r["date_iso"] is None, r["date_iso"] or "", r["id"]))
            self.assertEqual([r["id"] for r in db.candidate_emails_for_domain(self.conn, domain)], [r["id"] for r in expected])
            self.assertEqual([r["id"] for r in db.candidate_emails_for_domain(self.conn, domain, limit=2)],
                             [r["id"] for r in expected[:2]])
            closing = self.conn.execute("SELECT * FROM emails WHERE sender_domain=? AND closure_candidate=1", (domain,)).fetchall()
            wanted = sorted(closing, key=lambda r: (r["date_iso"] is None, r["date_iso"] or "", r["id"]))
            newest_first = [r["id"] for r in sorted(closing, key=lambda r: (r["date_iso"] or "", r["id"]), reverse=True)]
            self.assertEqual([r["id"] for r in db.closure_candidate_emails(self.conn, domain)], newest_first)

    def test_the_same_call_always_picks_the_same_emails(self):
        """Emails with the same date used to come back in no guaranteed order, so the step that downloads the text
        and the step that reads it could in principle have chosen different ones."""
        first = [r["id"] for r in db.candidate_emails_for_domain(self.conn, "d5.com", limit=3)]
        for _ in range(5):
            self.assertEqual([r["id"] for r in db.candidate_emails_for_domain(self.conn, "d5.com", limit=3)], first)
        self.assertEqual(first, sorted(first, key=lambda i: (self.conn.execute("SELECT date_iso FROM emails WHERE id=?", (i,)).fetchone()[0], i)))


if __name__ == "__main__":
    unittest.main()
