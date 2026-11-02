"""Regression tests for MBOX / EML importing robustness.

Background: a real 37,000-message export stopped at about message 10,000 with
"UnicodeDecodeError: 'ascii' codec can't decode byte 0xf0". Python's mailbox module decodes each
message's "From " SEPARATOR line as ASCII, and one message had a non-ASCII byte there. These tests
pin down the fix: such files import completely, and one unreadable message can never stop an import."""
import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

import db
import jobcontrol
import module_a_eml
import module_a_mbox
from models import Account

ACCOUNT = Account(name="t", provider="gmail", email="me@gmail.com")


def message(n: int, separator: bytes = b"From 1111@xxx Mon Jan 01 00:00:00 2024", subject: str = None) -> bytes:
    """One mbox entry. `separator` is the 'From ' line, which is where the real-world bug lived."""
    subject = subject or f"Welcome to Shop {n}"
    return (separator + b"\nFrom: Shop <hi@shop.example>\nTo: me@gmail.com\nSubject: " + subject.encode("utf-8") +
            b"\nDate: Mon, 01 Jan 2024 10:00:00 +0000\nMessage-ID: <" + str(n).encode() + b"@x>\n\nhello\n\n")


class MboxRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = db.open_database(None)

    def write(self, *entries: bytes) -> str:
        path = os.path.join(self.tmp, "mail.mbox")
        with open(path, "wb") as f:
            f.write(b"".join(entries))
        return path

    def run_import(self, path, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inserted = module_a_mbox.import_mbox(ACCOUNT, path, self.conn, **kw)
        return inserted, out.getvalue()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    # ---- the reported bug ------------------------------------------------------------------
    def test_non_ascii_byte_in_the_separator_line_no_longer_stops_the_import(self):
        emoji_line = "From \U0001F600 sender Mon Jan 01 00:00:00 2024".encode("utf-8")      # contains byte 0xf0
        path = self.write(message(1), message(2, separator=emoji_line), message(3))
        inserted, log = self.run_import(path)
        self.assertEqual(inserted, 3)                       # the message AFTER the odd one is imported too
        self.assertEqual(self.count(), 3)
        self.assertNotIn("skipped", log)                    # and nothing had to be skipped

    def test_separator_lines_in_other_encodings_also_work(self):
        latin1_line = "From Caf\u00e9 Mon Jan 01 00:00:00 2024".encode("latin-1")             # not valid UTF-8
        garbage_line = b"From \xff\xfe\xfd Mon Jan 01 00:00:00 2024"
        path = self.write(message(1, separator=latin1_line), message(2, separator=garbage_line), message(3))
        inserted, _ = self.run_import(path)
        self.assertEqual(inserted, 3)

    def test_non_ascii_headers_and_bodies_still_import(self):
        path = self.write(message(1, subject="Welcome \U0001F389 to Caf\u00e9"))
        inserted, _ = self.run_import(path)
        self.assertEqual(inserted, 1)
        subject = self.conn.execute("SELECT subject FROM emails").fetchone()[0]
        self.assertIn("Welcome", subject)

    # ---- one bad message never stops the rest ------------------------------------------------------
    def test_one_unreadable_message_is_skipped_counted_and_reported(self):
        path = self.write(message(1), message(2, subject="BOOM"), message(3), message(4))
        real = module_a_mbox.build_header_row

        def flaky(account, folder, uid, **kw):
            if "BOOM" in kw.get("subject", ""):
                raise ValueError("simulated unreadable message")
            return real(account, folder, uid, **kw)

        module_a_mbox.build_header_row = flaky
        try:
            inserted, log = self.run_import(path)
        finally:
            module_a_mbox.build_header_row = real
        self.assertEqual(inserted, 3)                       # 1, 3 and 4 were imported
        self.assertIn("skipped message #2", log)            # which one, and why
        self.assertIn("1 skipped", log)
        self.assertIn("#2", log.split("skipped message numbers:")[1])

    def test_a_stop_request_is_not_swallowed(self):
        path = self.write(message(1), message(2))
        control = jobcontrol.JobControl()
        control.cancel()
        with self.assertRaises(jobcontrol.Cancelled):
            self.run_import(path, control=control)

    def test_database_errors_are_not_swallowed(self):
        path = self.write(message(1), message(2))
        real = module_a_mbox.store_message

        def broken(*a, **k):
            raise sqlite3.OperationalError("disk full")

        module_a_mbox.store_message = broken
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.run_import(path)
        finally:
            module_a_mbox.store_message = real

    def test_many_skips_are_counted_but_not_all_listed(self):
        path = self.write(*[message(n, subject="BOOM") for n in range(1, 31)])
        real = module_a_mbox.build_header_row
        module_a_mbox.build_header_row = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad"))
        try:
            inserted, log = self.run_import(path)
        finally:
            module_a_mbox.build_header_row = real
        self.assertEqual(inserted, 0)
        self.assertEqual(log.count("skipped message #"), module_a_mbox.SKIP_DETAILS_SHOWN)
        self.assertIn("30 skipped", log)
        self.assertIn("and 10 more", log)

    def test_re_importing_still_adds_nothing_new(self):
        path = self.write(message(1), message(2))
        self.run_import(path)
        inserted, _ = self.run_import(path)
        self.assertEqual(inserted, 0)


class EmlRobustness(unittest.TestCase):
    def test_one_odd_eml_file_does_not_stop_the_folder(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for n in (1, 2, 3):
            with open(os.path.join(tmp, f"{n}.eml"), "wb") as f:
                f.write(b"From: Shop <hi@shop.example>\nTo: me@gmail.com\nSubject: Welcome to Shop " +
                        str(n).encode() + b"\nMessage-ID: <" + str(n).encode() + b"@x>\n\nhello\n")
        conn = db.open_database(None)
        real = module_a_eml.build_header_row

        def flaky(account, folder, uid, **kw):
            if "Shop 2" in kw.get("subject", ""):
                raise ValueError("simulated")
            return real(account, folder, uid, **kw)

        module_a_eml.build_header_row = flaky
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                inserted = module_a_eml.import_eml(ACCOUNT, [tmp], conn)
        finally:
            module_a_eml.build_header_row = real
        self.assertEqual(inserted, 2)
        self.assertIn("skipped 2.eml", out.getvalue())


if __name__ == "__main__":
    unittest.main()
