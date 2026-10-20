"""Tests for Module A's parsing: snippets, recipients, header batches, and the
body-fetch flow against a fake IMAP connection (no real mail server)."""
import email
import unittest
from email.message import EmailMessage

import db
import module_a_imap as imap_mod
from models import Account

ACCT = Account(name="gmail", provider="gmail", email="me@gmail.com", imap_server="imap.gmail.com", password="x")


class Snippets(unittest.TestCase):
    def test_html_only_mail_drops_css_and_scripts(self):
        m = EmailMessage()
        m.set_content("<html><head><style>body{margin:0} .x{color:red}</style></head>"
                      "<body><script>track()</script><p>Welcome to <b>Acme</b>!</p>"
                      "<div>Please verify&nbsp;your email.</div></body></html>", subtype="html")
        snippet = imap_mod._extract_snippet(m)
        self.assertIn("Welcome to Acme", snippet)
        self.assertNotIn("margin", snippet)
        self.assertNotIn("track()", snippet)
        self.assertIn("verify", snippet)

    def test_multipart_prefers_plain_text(self):
        m = EmailMessage()
        m.set_content("Plain version: thanks for signing up to Acme, please confirm your account today.")
        m.add_alternative("<p>HTML version</p>", subtype="html")
        self.assertTrue(imap_mod._extract_snippet(m).startswith("Plain version"))

    def test_stub_plain_part_falls_back_to_html(self):
        m = EmailMessage()
        m.set_content("View in browser")
        m.add_alternative("<p>Your Acme account was created successfully. Welcome aboard, friend!</p>", subtype="html")
        self.assertIn("account was created", imap_mod._extract_snippet(m))

    def test_attachments_are_ignored_and_length_is_capped(self):
        m = EmailMessage()
        m.set_content("x" * 5000)
        m.add_attachment(b"secret", maintype="application", subtype="octet-stream", filename="a.bin")
        self.assertEqual(len(imap_mod._extract_snippet(m)), imap_mod.BODY_SNIPPET_CHARS)

    def test_invisible_padding_is_removed(self):
        self.assertEqual(imap_mod._html_to_text("<p>a\u200b\u034f b</p>"), "a b")


class Recipients(unittest.TestCase):
    def addr(self, to, sender_domain="shop.com"):
        return imap_mod._best_recipient_address(to, ACCT, sender_domain)

    def test_plus_alias_and_primary(self):
        self.assertEqual(self.addr("Me <me+shop@gmail.com>"), "me+shop@gmail.com")
        self.assertEqual(self.addr("me@gmail.com"), "me@gmail.com")

    def test_forwarded_single_recipient_is_kept(self):
        self.assertEqual(self.addr("old@yahoo.com"), "old@yahoo.com")

    def test_lists_bcc_and_missing_fall_back_to_the_mailbox(self):
        self.assertEqual(self.addr("a@x.com, b@y.com"), "me@gmail.com")          # several recipients
        self.assertEqual(self.addr("undisclosed-recipients:;"), "me@gmail.com")
        self.assertEqual(self.addr(None), "me@gmail.com")
        self.assertEqual(self.addr("info@shop.com", "shop.com"), "me@gmail.com")  # addressed to the sender's own domain


class HeaderBatch(unittest.TestCase):
    def test_parse_fetch_batch_like_imaplib_returns_it(self):
        raw = (b"From: =?utf-8?q?Caf=C3=A9?= <hi@cafe.example>\r\nTo: me@gmail.com\r\n"
               b"Subject: Welcome to Caf=?utf-8?q?=C3=A9?=\r\nDate: Mon, 05 Jun 2017 09:00:00 +0000\r\n"
               b"Message-ID: <m1@cafe>\r\n\r\n")
        data = [(b"1 (UID 42 BODY[HEADER.FIELDS (FROM TO SUBJECT)] {200}", raw), b")"]
        row = imap_mod._parse_fetch_batch(data, ACCT, "INBOX")[0]
        self.assertEqual(row["uid"], "42")
        self.assertEqual(row["sender_domain"], "cafe.example")
        self.assertEqual(row["date_iso"], "2017-06-05T09:00:00+00:00")
        self.assertEqual(row["message_id"], "<m1@cafe>")

    def test_missing_message_id_gets_a_stable_per_folder_stand_in(self):
        a = imap_mod.build_header_row(ACCT, "INBOX", "7", "A <a@x.com>", "me@gmail.com", "s", None, None, None)
        b = imap_mod.build_header_row(ACCT, "Archive", "7", "A <a@x.com>", "me@gmail.com", "s", None, None, None)
        self.assertNotEqual(a["message_id"], b["message_id"])


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL for fetch_body_snippets."""

    def __init__(self, messages):
        self.messages, self.fetch_calls, self.selected = messages, [], []

    def select(self, folder, readonly=False):
        assert readonly, "mailbox must only ever be opened read-only"
        self.selected.append(folder)
        return "OK", [b"1"]

    def uid(self, command, uid_set, items):
        assert command == "fetch" and "PEEK" in items, "must use BODY.PEEK so mail isn't marked read"
        self.fetch_calls.append((uid_set, items))
        out = []
        for uid in uid_set.split(","):
            if uid in self.messages:
                out.append((f"1 (UID {uid} BODY[]<0> {{99}}".encode(), self.messages[uid]))
                out.append(b")")
        return "OK", out

    def logout(self):
        pass


class BodyFetch(unittest.TestCase):
    def test_snippets_are_stored_and_deleted_messages_are_marked_done(self):
        conn = db.open_database(None)
        for uid in ("10", "11", "12"):
            db.insert_email(conn, imap_mod.build_header_row(
                ACCT, "INBOX", uid, "Acme <a@acme.com>", "me@gmail.com", "Welcome to Acme",
                "Mon, 05 Jun 2017 09:00:00 +0000", f"<{uid}@acme>", None))
        conn.execute("UPDATE emails SET prefilter_label='candidate'")
        conn.commit()
        good = (b"From: a@acme.com\r\nSubject: Welcome\r\nContent-Type: text/plain\r\n\r\n"
                b"Thanks for joining Acme. Your account is ready.")
        fake = FakeImap({"10": good, "11": good})                     # uid 12 was deleted on the server
        original = imap_mod.connect
        imap_mod.connect = lambda account: fake
        try:
            rows = db.emails_missing_body(conn)
            self.assertEqual(len(rows), 3)
            imap_mod.fetch_body_snippets(ACCT, conn, rows)
        finally:
            imap_mod.connect = original
        got = {r["uid"]: (r["body_snippet"], r["body_fetched"]) for r in conn.execute("SELECT * FROM emails")}
        self.assertIn("account is ready", got["10"][0])
        self.assertEqual(got["12"], ("", 1))                          # not requested again next run
        self.assertEqual(db.emails_missing_body(conn), [])
        self.assertIn("BODY.PEEK[]<0.", fake.fetch_calls[0][1])


if __name__ == "__main__":
    unittest.main()
