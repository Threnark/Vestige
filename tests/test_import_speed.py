"""Tests for the import speed-ups (batch 5 of the review):

  * the money amounts in an email are only searched for when the pre-filter will treat the email as a billing
    email (it decides from the subject alone). The search reads up to 20,000 characters of every email and costs
    2-4 ms each; for any email that was not a receipt the result was deleted again by the next pre-filter run.
    The receipts keep every amount, including ones far beyond the short snippet;
  * the mbox importer reads only the first 256 KB of each message, instead of reading and parsing a message and
    all its attachments in full. Which emails are stored, and every stand-in Message-ID, are unchanged.
"""
import base64
import contextlib
import io
import json
import mailbox
import os
import shutil
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

import billing_math
import db
import module_a_eml as eml_mod
import module_a_imap as imap_mod
import module_a_mbox as mbox_mod
import module_a_pst as pst_mod
import module_b_prefilter as prefilter
from models import Account
from tests.test_imap_robustness import ImapCase, body_reply
from tests.test_mail_import import FakeMessage

ACCT = Account("a", "other", "me@x.com")
BILLING = ["Your receipt from Acme", "Your subscription has been renewed", "Payment failed - update your card",
           "Invoice #1234 is ready", "Your monthly plan payment"]
NOT_BILLING = ["Weekly digest", "50% off everything", "Welcome to Acme", "Verify your email", "Hello"]


def quietly(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def mbox_file(folder, name, messages) -> str:
    """messages: (subject, text) pairs, written without Message-IDs so nothing else is in play."""
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for subject, text in messages:
            handle.write("From sender@x.com Mon Jan  1 10:00:00 2024\nFrom: Acme <no-reply@acme.com>\nTo: me@x.com\n"
                         f"Subject: {subject}\nDate: Mon, 01 Jan 2024 10:00:00 +0000\n"
                         f"Message-ID: <{abs(hash((subject, text)))}@t>\n\n{text}\n\n")
    return path


class FileImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = db.open_database(None)
        self.real_extract = billing_math.extract_amounts
        spy = mock.patch.object(imap_mod.billing_math, "extract_amounts", wraps=self.real_extract)
        self.spy = spy.start()
        self.addCleanup(spy.stop)

    def amounts(self) -> dict:
        return {r["subject"]: r["amounts_json"] for r in self.conn.execute("SELECT subject, amounts_json FROM emails")}


class TheBillingQuestion(unittest.TestCase):
    def test_import_and_the_prefilter_can_never_disagree(self):
        conn = db.open_database(None)
        for i, subject in enumerate(BILLING + NOT_BILLING):
            db.insert_email(conn, imap_mod.build_header_row(ACCT, "INBOX", str(i), "Acme <a@acme.com>", "me@x.com",
                                                            subject, "Mon, 01 Jan 2024 10:00:00 +0000", f"<{i}@t>", None))
        conn.commit()
        prefilter.run_prefilter(conn)
        rows = conn.execute("SELECT * FROM emails").fetchall()
        self.assertGreaterEqual(sum(r["billing_candidate"] for r in rows), 3, "the sample should include real billing mail")
        for row in rows:
            self.assertEqual(bool(row["billing_candidate"]), imap_mod.wants_amounts(row), row["subject"])
            self.assertEqual(bool(row["billing_candidate"]), imap_mod._stored_row_wants_amounts(row), row["subject"])

    def test_a_stored_row_goes_by_the_flag_the_prefilter_recorded(self):
        """The later stages go by the recorded flag, so that is what decides, whatever the subject says."""
        conn = db.open_database(None)
        conn.execute("INSERT INTO emails(account_name, message_id, subject, billing_candidate) "
                     "VALUES ('a','<1>','Weekly digest',1), ('a','<2>','Your receipt from Acme',0)")
        digest, receipt = conn.execute("SELECT * FROM emails ORDER BY id").fetchall()
        self.assertTrue(imap_mod._stored_row_wants_amounts(digest))
        self.assertFalse(imap_mod._stored_row_wants_amounts(receipt))

    def test_a_row_without_the_flag_falls_back_to_its_subject(self):
        self.assertTrue(imap_mod._stored_row_wants_amounts({"subject": "Your receipt from Acme"}))
        self.assertFalse(imap_mod._stored_row_wants_amounts({"subject": "Weekly digest"}))

    def test_body_extraction_skips_the_search_when_asked(self):
        text = "Thanks for your order. Total: $17.99 charged today."
        self.assertEqual(imap_mod.body_from_texts(text, None).amounts, billing_math.extract_amounts(text))
        skipped = imap_mod.body_from_texts(text, None, want_amounts=False)
        self.assertEqual(skipped.amounts, [])
        self.assertEqual(skipped.snippet, imap_mod.body_from_texts(text, None).snippet)


class MboxAmounts(FileImportCase):
    def test_only_billing_emails_are_searched_for_amounts(self):
        path = mbox_file(self.tmp, "a.mbox", [(s, "You paid $9.99 and 12.50 EUR today.") for s in BILLING + NOT_BILLING])
        quietly(mbox_mod.import_mbox, ACCT, path, self.conn)
        wanted = sum(imap_mod.wants_amounts({"subject": s}) for s in BILLING + NOT_BILLING)
        self.assertEqual(self.spy.call_count, wanted)
        self.assertLess(wanted, len(BILLING + NOT_BILLING))
        for subject, stored in self.amounts().items():
            self.assertEqual(stored is not None, imap_mod.wants_amounts({"subject": subject}), subject)

    def test_a_receipt_keeps_amounts_far_beyond_the_short_snippet(self):
        text = "Thanks for being a customer. " * 200 + "Total charged: $42.50"
        path = mbox_file(self.tmp, "a.mbox", [("Your receipt from Acme", text)])
        quietly(mbox_mod.import_mbox, ACCT, path, self.conn)
        self.assertGreater(len(text), 5000)
        before = json.loads(self.amounts()["Your receipt from Acme"])
        self.assertTrue(any(abs(a["value"] - 42.50) < 0.001 for a in before), before)
        prefilter.run_prefilter(self.conn)                           # the next stage must not lose or change it
        self.assertEqual(json.loads(self.amounts()["Your receipt from Acme"]), before)

    def test_the_prefilter_ends_with_the_same_amounts_as_before(self):
        """The old way (search everything, delete the non-billing results later) and the new way end up equal."""
        path = mbox_file(self.tmp, "a.mbox", [(s, "You paid $9.99 today.") for s in BILLING + NOT_BILLING])
        quietly(mbox_mod.import_mbox, ACCT, path, self.conn)
        prefilter.run_prefilter(self.conn)
        new_way = self.amounts()
        old_way_conn = db.open_database(None)
        with mock.patch.object(imap_mod, "wants_amounts", return_value=True):         # search every email, as before
            quietly(mbox_mod.import_mbox, ACCT, path, old_way_conn)
        prefilter.run_prefilter(old_way_conn)
        old_way = {r["subject"]: r["amounts_json"] for r in old_way_conn.execute("SELECT subject, amounts_json FROM emails")}
        self.assertEqual(new_way, old_way)


class EmlAndPstAmounts(FileImportCase):
    def test_eml_files_are_searched_only_when_billing(self):
        folder = os.path.join(self.tmp, "eml")
        os.makedirs(folder)
        for i, subject in enumerate(["Your receipt from Acme", "Weekly digest", "Hello"]):
            with open(os.path.join(folder, f"{i}.eml"), "w", encoding="utf-8") as handle:
                handle.write(f"From: Acme <no-reply@acme.com>\nTo: me@x.com\nSubject: {subject}\n"
                             f"Date: Mon, 01 Jan 2024 10:00:00 +0000\nMessage-ID: <{i}@t>\n\nTotal: $17.99\n")
        quietly(eml_mod.import_eml, ACCT, [folder], self.conn)
        self.assertEqual(self.spy.call_count, 1)
        amounts = self.amounts()
        self.assertIsNotNone(amounts["Your receipt from Acme"])
        self.assertIsNone(amounts["Weekly digest"])
        self.assertIsNone(amounts["Hello"])

    def test_pst_messages_are_searched_only_when_billing(self):
        for subject, expected_calls in (("Your receipt from Acme", 1), ("Weekly digest", 0)):
            self.spy.reset_mock()
            message = FakeMessage(subject=subject, sender_name="Acme", plain="Total: $17.99",
                                  mapi={0x5D01: "billing@acme.com"})
            row, snippet = pst_mod.message_to_row(ACCT, "Inbox", 0, message)
            body = snippet()
            self.assertEqual(self.spy.call_count, expected_calls, subject)
            self.assertEqual(bool(body.amounts), bool(expected_calls), subject)
            self.assertIn("17.99", body.snippet)                                   # the text itself is kept either way


class ImapAmounts(ImapCase):
    RAW = b"From: a@acme.com\r\nSubject: x\r\nContent-Type: text/plain\r\n\r\nYour total is $17.99 thanks for your order."

    def test_amounts_follow_the_pre_filters_recorded_answer(self):
        self.add_candidates(["10", "11"])
        self.conn.execute("UPDATE emails SET billing_candidate=0")
        self.conn.execute("UPDATE emails SET billing_candidate=1 WHERE uid='11'")
        self.conn.commit()
        self.server.on_uid = lambda c, cmd, uid_set, items: body_reply(uid_set, {"10": self.RAW, "11": self.RAW})
        with mock.patch.object(imap_mod.billing_math, "extract_amounts", wraps=billing_math.extract_amounts) as spy:
            stored, _ = self.fetch_bodies()
        self.assertEqual(stored, 2)
        self.assertEqual(spy.call_count, 1)
        got = {r["uid"]: r["amounts_json"] for r in self.conn.execute("SELECT uid, amounts_json FROM emails")}
        self.assertIsNone(got["10"])
        self.assertTrue(any(abs(a["value"] - 17.99) < 0.001 for a in json.loads(got["11"])))
        snippets = {r["uid"]: r["body_snippet"] for r in self.conn.execute("SELECT uid, body_snippet FROM emails")}
        self.assertIn("17.99", snippets["10"])                                    # both still get their text


class MboxReadsOnlyTheStart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = db.open_database(None)
        self.path = os.path.join(self.tmp, "big.mbox")

    def write(self, messages) -> None:
        box = mailbox.mbox(self.path)
        for message in messages:
            box.add(message)
        box.flush()
        box.close()

    @staticmethod
    def with_attachment(subject, text, size=1_500_000, message_id=None) -> EmailMessage:
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "Acme <no-reply@acme.com>", "me@x.com", subject
        m["Date"] = "Mon, 01 Jan 2024 10:00:00 +0000"
        if message_id:
            m["Message-ID"] = message_id
        m.set_content(text)
        m.add_attachment(os.urandom(size), maintype="application", subtype="pdf", filename="a.pdf")
        return m

    def run_import(self):
        return quietly(mbox_mod.import_mbox, ACCT, self.path, self.conn)

    def test_no_message_is_read_in_full(self):
        self.write([self.with_attachment(f"Receipt {i}", "Thanks! Total: $5.00", message_id=f"<{i}@t>") for i in range(5)])
        returned = []
        real = mailbox._PartialFile.read

        def spy(file, size=None):
            data = real(file, size)
            returned.append(len(data))
            return data
        with mock.patch.object(mailbox._PartialFile, "read", spy), \
                mock.patch.object(mailbox.mbox, "get_bytes", side_effect=AssertionError("whole messages must not be read")):
            self.assertEqual(self.run_import(), 5)
        self.assertEqual(len(returned), 5)
        self.assertLessEqual(max(returned), mbox_mod.MBOX_READ_BYTES)
        self.assertGreater(os.path.getsize(self.path), 5 * 1_500_000)           # the messages really were large

    def test_the_subject_and_the_text_still_come_from_a_message_with_a_big_attachment(self):
        self.write([self.with_attachment("Your receipt from Acme", "Thanks for your order. Total: $17.99", message_id="<1@t>")])
        self.run_import()
        row = self.conn.execute("SELECT subject, body_snippet, amounts_json FROM emails").fetchone()
        self.assertEqual(row["subject"], "Your receipt from Acme")
        self.assertIn("Total: $17.99", row["body_snippet"])
        self.assertTrue(any(abs(a["value"] - 17.99) < 0.001 for a in json.loads(row["amounts_json"])))

    def test_a_text_part_cut_off_by_the_limit_does_not_stop_the_import(self):
        long_text = base64.encodebytes(b"hello world " * 40000).decode()                 # about 640 KB of base64
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "Acme <no-reply@acme.com>", "me@x.com", "Your receipt"
        m["Date"], m["Message-ID"] = "Mon, 01 Jan 2024 10:00:00 +0000", "<long@t>"
        m["Content-Type"], m["Content-Transfer-Encoding"] = "text/plain; charset=utf-8", "base64"
        m.set_payload(long_text)
        self.write([m, self.with_attachment("Second message", "still arrives", message_id="<2@t>")])
        self.assertEqual(self.run_import(), 2)
        snippet = self.conn.execute("SELECT body_snippet FROM emails WHERE subject='Your receipt'").fetchone()[0]
        self.assertTrue(snippet.startswith("hello world hello world"))

    def test_the_stand_in_id_is_the_one_a_whole_message_read_would_give(self):
        """Messages without a Message-ID must keep the same ID as before, or a re-import would add them again."""
        self.write([self.with_attachment("No ID here", "the body of a message with no id")])
        self.run_import()
        stored = self.conn.execute("SELECT message_id FROM emails").fetchone()[0]
        box = mailbox.mbox(self.path, create=False)
        whole = box.get_bytes(next(iter(box.iterkeys())))                          # the old way: everything
        box.close()
        msg = imap_mod.email.message_from_bytes(whole)
        expected = imap_mod.stand_in_message_id(
            imap_mod._decode_header_value(msg.get("From")), imap_mod._decode_header_value(msg.get("To")),
            imap_mod._decode_header_value(msg.get("Subject")), msg.get("Date"), imap_mod.body_start(whole))
        self.assertEqual(stored, expected)
        self.assertEqual(self.run_import(), 0, "importing the same file again must add nothing")

    def test_windows_style_line_endings_give_the_same_bytes_as_before(self):
        with open(self.path, "wb") as handle:
            handle.write(b"From sender@x.com Mon Jan  1 10:00:00 2024\r\nFrom: Acme <a@acme.com>\r\nTo: me@x.com\r\n"
                         b"Subject: Hello\r\nDate: Mon, 01 Jan 2024 10:00:00 +0000\r\n\r\nline one\r\nline two\r\n\r\n")
        box = mailbox.mbox(self.path, create=False)
        key = next(iter(box.iterkeys()))
        self.assertEqual(mbox_mod._read_message_start(box, key), box.get_bytes(key))
        box.close()

    def test_a_small_message_is_read_whole(self):
        with open(self.path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("From s@x.com Mon Jan  1 10:00:00 2024\nSubject: Small\n\nbody\n\n")
        box = mailbox.mbox(self.path, create=False)
        key = next(iter(box.iterkeys()))
        self.assertEqual(mbox_mod._read_message_start(box, key), box.get_bytes(key))
        box.close()


class MessagesCutOffMidBase64(unittest.TestCase):
    """When only the START of a message is read, it can end in the middle of its base64 text. Python then returns
    the base64 text itself, undecoded, for a quarter of the possible cut points: the snippet came out as
    "aGVsbG8gd29ybGQ...". (The IMAP download and the .eml importer are cut off the same way, so this was
    reachable there before the mbox change.)"""

    @staticmethod
    def message(cut: int) -> bytes:
        body = base64.encodebytes(b"hello world " * 3000)                    # about 48 KB of base64 text
        whole = (b"From: a@acme.com\nSubject: x\nContent-Type: text/plain; charset=utf-8\n"
                 b"Content-Transfer-Encoding: base64\n\n" + body)
        return whole[:cut]

    def snippet(self, cut: int) -> str:
        return imap_mod._extract_body(imap_mod.email.message_from_bytes(self.message(cut))).snippet

    def test_every_possible_cut_point_gives_readable_text(self):
        header_length = len(self.message(10 ** 6)) - len(base64.encodebytes(b"hello world " * 3000))
        for extra in range(2000, 2012):                                      # covers every length remainder, with and without a newline
            cut = header_length + extra
            with self.subTest(cut=cut):
                snippet = self.snippet(cut)
                self.assertTrue(snippet.startswith("hello world hello world"), snippet[:40])
                self.assertNotIn("aGVsbG8", snippet)

    def test_a_complete_message_decodes_exactly_as_before(self):
        whole = self.message(10 ** 6)
        msg = imap_mod.email.message_from_bytes(whole)
        self.assertEqual(imap_mod._decode_part(msg), b"hello world " * 3000)
        self.assertEqual(imap_mod._decode_part(msg), msg.get_payload(decode=True))

    def test_padding_at_the_end_is_handled(self):
        for text in (b"a", b"ab", b"abc", b"abcd", b"hello"):
            raw = (b"Content-Type: text/plain\nContent-Transfer-Encoding: base64\n\n" + base64.encodebytes(text))
            msg = imap_mod.email.message_from_bytes(raw)
            self.assertEqual(imap_mod._decode_part(msg), text, text)

    def test_other_encodings_and_plain_text_are_untouched(self):
        plain = imap_mod.email.message_from_bytes(b"Content-Type: text/plain\n\njust text\n")
        self.assertEqual(imap_mod._decode_part(plain), b"just text\n")
        qp = imap_mod.email.message_from_bytes(
            b"Content-Type: text/plain\nContent-Transfer-Encoding: quoted-printable\n\ncaf=C3=A9 =\ncontinued\n")
        self.assertEqual(imap_mod._decode_part(qp), qp.get_payload(decode=True))

    def test_junk_that_only_claims_to_be_base64_does_not_crash(self):
        raw = b"Content-Type: text/plain\nContent-Transfer-Encoding: base64\n\n!!!! ???? ====\n"
        self.assertIsNotNone(imap_mod._decode_part(imap_mod.email.message_from_bytes(raw)) or b"")


if __name__ == "__main__":
    unittest.main()
