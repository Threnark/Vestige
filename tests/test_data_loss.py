"""Tests for the data-loss fixes (batch 2 of the review):

  * turning write mode off must never leave the session pointing at a closed database, even when the saved
    file can't be deleted (Windows: antivirus or a cloud-sync app holding it);
  * an email with no Message-ID gets a stand-in ID built from what the message SAYS, so it is neither lost
    (the first no-ID message of a second file used to be thrown away as a "duplicate" of the first no-ID
    message of the first file) nor duplicated (moving a folder made every message look new);
  * the run log says how every stage ended, including stages that were stopped or failed.
"""
import mailbox
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import db
import jobcontrol
import module_a_eml as eml_mod
import module_a_imap as imap_mod
import module_a_mbox as mbox_mod
import module_a_pst as pst_mod
import pipeline
import session as session_mod
from models import Account
from session import Session, SessionError
from tests.test_mail_import import FakeMessage, headers

ACCT = Account("a", "other", "me@x.com")


# ---------------------------------------------------------------------------------------------------
# Write mode
# ---------------------------------------------------------------------------------------------------
class WriteModeIsNeverLeftBroken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.session = Session()
        self.session.conn.execute(
            "INSERT INTO emails(account_name, message_id, sender_domain, subject) VALUES ('a','<1>','x.com','hello')")
        self.session.conn.commit()
        self.path = self.session.enable_write_mode(self.tmp)

    def locked(self):
        """os.remove refuses to delete the session file, as Windows does while something else holds it."""
        real = os.remove

        def remove(path, *args, **kwargs):
            if str(path).endswith("session.db"):
                raise PermissionError(13, "The process cannot access the file because it is being used by another process")
            return real(path, *args, **kwargs)
        return mock.patch.object(session_mod.os, "remove", remove)

    def emails(self) -> int:
        return self.session.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]

    def test_a_file_that_cannot_be_deleted_does_not_take_the_results_with_it(self):
        with self.locked(), self.assertRaises(SessionError) as caught:
            self.session.disable_write_mode(delete_file=True)
        self.assertFalse(self.session.write_mode)
        self.assertIsNone(self.session.db_path)
        self.assertEqual(self.emails(), 1, "the results must still be reachable")
        self.session.conn.execute("INSERT INTO emails(account_name, message_id) VALUES ('a','<2>')")   # and usable
        self.assertEqual(self.emails(), 2)
        message = str(caught.exception)
        self.assertIn("safe in memory", message)
        self.assertIn(self.path, message)                       # says where the file is
        self.assertIn("Delete saved data", message)             # and what to do about it
        self.assertTrue(os.path.exists(self.path))

    def test_the_other_files_are_still_deleted_when_one_cannot_be(self):
        journal = self.path + "-journal"
        open(journal, "wb").close()
        with self.locked(), self.assertRaises(SessionError) as caught:
            self.session.disable_write_mode(delete_file=True)
        self.assertFalse(os.path.exists(journal))
        self.assertIn("session.db", str(caught.exception))

    def test_write_mode_can_be_turned_back_on_once_the_file_is_free(self):
        with self.locked(), self.assertRaises(SessionError):
            self.session.disable_write_mode(delete_file=True)
        again = self.session.enable_write_mode(self.tmp, overwrite=True)      # the lock has gone
        self.assertTrue(self.session.write_mode)
        self.assertEqual(again, self.path)
        self.assertEqual(self.emails(), 1)

    def test_deleting_normally_still_works(self):
        removed = self.session.disable_write_mode(delete_file=True)
        self.assertIn(self.path, removed)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(self.session.write_mode)
        self.assertEqual(self.emails(), 1)

    def test_keeping_the_file_still_works(self):
        self.assertEqual(self.session.disable_write_mode(delete_file=False), [])
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.emails(), 1)

    def test_a_failed_copy_reports_the_real_problem_not_a_leftover_file(self):
        fresh = Session()
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        with mock.patch.object(session_mod.db, "copy_database", side_effect=RuntimeError("disk full")), \
                self.locked(), self.assertRaises(RuntimeError) as caught:
            fresh.enable_write_mode(folder)
        self.assertIn("disk full", str(caught.exception))
        self.assertFalse(fresh.write_mode)


# ---------------------------------------------------------------------------------------------------
# Stand-in Message-IDs
# ---------------------------------------------------------------------------------------------------
class StandInHelpers(unittest.TestCase):
    def test_the_same_message_always_gets_the_same_id(self):
        a = imap_mod.stand_in_message_id("A <a@x.com>", "me@x.com", "Hello", "Mon, 01 Jan 2024", b"body")
        b = imap_mod.stand_in_message_id("A <a@x.com>", "me@x.com", "Hello", "Mon, 01 Jan 2024", b"body")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("<no-id-") and a.endswith("@vestige>"))

    def test_different_messages_get_different_ids(self):
        base = ["A <a@x.com>", "me@x.com", "Hello", "Mon, 01 Jan 2024", b"body"]
        ids = {imap_mod.stand_in_message_id(*base)}
        for position, changed in enumerate(("B <b@x.com>", "you@x.com", "Hi", "Tue, 02 Jan 2024", b"other")):
            variant = list(base)
            variant[position] = changed
            ids.add(imap_mod.stand_in_message_id(*variant))
        self.assertEqual(len(ids), 6)

    def test_fields_cannot_run_into_each_other(self):
        self.assertNotEqual(imap_mod.stand_in_message_id("ab", "c"), imap_mod.stand_in_message_id("a", "bc"))

    def test_spaces_around_a_field_do_not_matter(self):
        self.assertEqual(imap_mod.stand_in_message_id(" Hello ", "x"), imap_mod.stand_in_message_id("Hello", "x"))

    def test_where_it_was_found_is_ignored_when_the_message_says_something(self):
        a = imap_mod.stand_in_message_id("A", "B", where="/one/file.mbox#3")
        b = imap_mod.stand_in_message_id("A", "B", where="/two/other.mbox#99")
        self.assertEqual(a, b)

    def test_where_it_was_found_keeps_completely_blank_messages_apart(self):
        a = imap_mod.stand_in_message_id("", None, "", "", b"", where="/f.mbox#1")
        b = imap_mod.stand_in_message_id("", None, "", "", b"", where="/f.mbox#2")
        self.assertNotEqual(a, b)
        self.assertEqual(a, imap_mod.stand_in_message_id("", None, "", "", b"", where="/f.mbox#1"))

    def test_a_blank_message_id_header_counts_as_missing(self):
        self.assertEqual(imap_mod.tidy_message_id(None), "")
        self.assertEqual(imap_mod.tidy_message_id("   "), "")
        self.assertEqual(imap_mod.tidy_message_id("  <a@b>  "), "<a@b>")

    def test_the_start_of_the_content_is_found_after_the_headers(self):
        self.assertEqual(imap_mod.body_start(b"A: 1\r\nB: 2\r\n\r\nhello"), b"hello")
        self.assertEqual(imap_mod.body_start(b"A: 1\nB: 2\n\nhello"), b"hello")
        self.assertEqual(imap_mod.body_start(b"A: 1\r\nB: 2\r\n"), b"")
        self.assertEqual(len(imap_mod.body_start(b"A: 1\n\n" + b"x" * 5000)), imap_mod.STAND_IN_CONTENT_BYTES)

    def test_an_imap_message_with_a_blank_id_gets_a_stand_in_not_an_empty_id(self):
        row = imap_mod.build_header_row(ACCT, "INBOX", "7", "A <a@x.com>", "me@x.com", "s", None, "   ", None)
        self.assertEqual(row["message_id"], "<no-id-a-INBOX-7>")


def mbox_text(*messages) -> str:
    """The text of an mbox file. Each message is (subject, body, message_id) with message_id None to leave the
    header out altogether, or a string (possibly blank) to write it."""
    out = []
    for subject, body, message_id in messages:
        lines = ["From sender@x.com Mon Jan  1 10:00:00 2024", "From: Acme <no-reply@acme.com>", "To: me@x.com",
                 f"Subject: {subject}", "Date: Mon, 01 Jan 2024 10:00:00 +0000"]
        if message_id is not None:
            lines.append(f"Message-ID: {message_id}")
        out.append("\n".join(lines) + f"\n\n{body}\n\n")
    return "".join(out)


class MboxWithoutMessageIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = db.open_database(None)

    def write(self, name, *messages) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(mbox_text(*messages))
        return path

    def run_import(self, path) -> int:
        with mock.patch("builtins.print"):
            return mbox_mod.import_mbox(ACCT, path, self.conn)

    def subjects(self) -> list:
        return sorted(r["subject"] for r in self.conn.execute("SELECT subject FROM emails"))

    def test_the_first_message_of_a_second_file_is_no_longer_thrown_away(self):
        """The bug: both files' first no-ID message was called <no-id-...-mbox-0>, so the second was dropped."""
        first = self.write("a.mbox", ("Welcome to Alpha", "Thanks for joining Alpha.", None))
        second = self.write("b.mbox", ("Welcome to Beta", "Thanks for joining Beta.", None))
        self.assertEqual(self.run_import(first), 1)
        self.assertEqual(self.run_import(second), 1)
        self.assertEqual(self.subjects(), ["Welcome to Alpha", "Welcome to Beta"])

    def test_the_same_message_in_two_files_is_kept_once(self):
        """Takeout-style exports put one message in the file of every label it carries."""
        one = self.write("inbox.mbox", ("Your receipt", "Total paid: 9.99", None))
        two = self.write("important.mbox", ("Your receipt", "Total paid: 9.99", None))
        self.assertEqual(self.run_import(one), 1)
        self.assertEqual(self.run_import(two), 0)
        self.assertEqual(self.subjects(), ["Your receipt"])

    def test_two_different_messages_with_the_same_headers_are_both_kept(self):
        path = self.write("codes.mbox", ("Your code", "Your code is 111111", None), ("Your code", "Your code is 222222", None))
        self.assertEqual(self.run_import(path), 2)

    def test_importing_the_same_file_again_adds_nothing(self):
        path = self.write("a.mbox", ("One", "first", None), ("Two", "second", None))
        self.assertEqual(self.run_import(path), 2)
        self.assertEqual(self.run_import(path), 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 2)

    def test_a_message_added_at_the_front_of_a_later_export_is_the_only_new_one(self):
        """A later export of the same mailbox with one new message at the top. Position-based IDs shifted by one:
        the new message was dropped (it took the place of 'One') and 'Two' was stored a second time."""
        self.run_import(self.write("a.mbox", ("One", "first", None), ("Two", "second", None)))
        later = self.write("b.mbox", ("Three", "third", None), ("One", "first", None), ("Two", "second", None))
        self.assertEqual(self.run_import(later), 1)
        self.assertEqual(self.subjects(), ["One", "Three", "Two"])

    def test_a_blank_message_id_header_is_treated_as_missing(self):
        path = self.write("blank.mbox", ("One", "first", "   "), ("Two", "second", "   "))
        self.assertEqual(self.run_import(path), 2, "two messages must not share the ID ''")
        self.assertNotIn("", [r["message_id"] for r in self.conn.execute("SELECT message_id FROM emails")])

    def test_a_real_message_id_is_used_exactly_as_before(self):
        self.run_import(self.write("a.mbox", ("One", "first", "<real-1@acme.com>")))
        self.assertEqual(self.conn.execute("SELECT message_id FROM emails").fetchone()[0], "<real-1@acme.com>")


class EmlWithoutMessageIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conn = db.open_database(None)

    def save(self, folder, name, subject, body) -> str:
        os.makedirs(os.path.join(self.tmp, folder), exist_ok=True)
        path = os.path.join(self.tmp, folder, name)
        with open(path, "w", encoding="utf-8", newline="\r\n") as handle:
            handle.write(f"From: Acme <no-reply@acme.com>\nTo: me@x.com\nSubject: {subject}\n"
                         f"Date: Mon, 01 Jan 2024 10:00:00 +0000\n\n{body}\n")
        return path

    def run_import(self, folder) -> int:
        with mock.patch("builtins.print"):
            return eml_mod.import_eml(ACCT, [os.path.join(self.tmp, folder)], self.conn)

    def test_the_same_email_saved_in_two_folders_is_kept_once(self):
        self.save("inbox", "1.eml", "Welcome to Acme", "Thanks for joining.")
        self.save("backup", "copy of 1.eml", "Welcome to Acme", "Thanks for joining.")
        self.assertEqual(self.run_import("inbox"), 1)
        self.assertEqual(self.run_import("backup"), 0)

    def test_moving_the_folder_does_not_make_everything_new(self):
        """The old ID contained the file's path."""
        self.save("old-place", "1.eml", "Welcome to Acme", "Thanks for joining.")
        self.save("old-place", "2.eml", "Welcome to Beta", "Thanks for joining Beta.")
        self.assertEqual(self.run_import("old-place"), 2)
        shutil.move(os.path.join(self.tmp, "old-place"), os.path.join(self.tmp, "new-place"))
        self.assertEqual(self.run_import("new-place"), 0)

    def test_different_emails_are_all_kept(self):
        self.save("mail", "1.eml", "Welcome to Acme", "Thanks for joining.")
        self.save("mail", "2.eml", "Welcome to Beta", "Thanks for joining Beta.")
        self.save("mail", "3.eml", "Welcome to Beta", "A different body.")
        self.assertEqual(self.run_import("mail"), 3)


class PstWithoutMessageIds(unittest.TestCase):
    """Mail inside an Exchange organisation has no internet headers, so this is the NORMAL case for many
    Outlook data files, not a rarity."""

    def message(self, plain="Welcome!", subject="Welcome to Notion"):
        return FakeMessage(subject=subject, sender_name="Notion", plain=plain,
                           mapi={0x5D01: "team@mail.notion.so"})

    def id_of(self, folder="Inbox", index=0, **kwargs) -> str:
        row, _snippet = pst_mod.message_to_row(ACCT, folder, index, self.message(**kwargs))
        return row["message_id"]

    def test_the_id_does_not_depend_on_the_folder_or_position(self):
        self.assertEqual(self.id_of("Inbox", 0), self.id_of("Archive/2019", 417))

    def test_a_different_message_gets_a_different_id(self):
        self.assertNotEqual(self.id_of(plain="Welcome!"), self.id_of(plain="Something else entirely"))
        self.assertNotEqual(self.id_of(subject="Welcome to Notion"), self.id_of(subject="Your invoice"))

    def test_a_blank_message_id_header_counts_as_missing(self):
        blank = FakeMessage(headers("Acme <no-reply@acme.com>", "me@x.com", "Hello", "   "), plain="body")
        row, _ = pst_mod.message_to_row(ACCT, "Inbox", 0, blank)
        self.assertTrue(row["message_id"].startswith("<no-id-"))

    def test_importing_the_same_messages_from_another_file_adds_nothing(self):
        conn = db.open_database(None)
        folder_a = [self.message(plain="One"), self.message(plain="Two")]
        for index, msg in enumerate(folder_a):
            row, snippet = pst_mod.message_to_row(ACCT, "Inbox", index, msg)
            self.assertTrue(imap_mod.store_message(conn, row, snippet))
        # a second export of the same mailbox: other folder name, other order
        for index, msg in enumerate(reversed(folder_a)):
            row, snippet = pst_mod.message_to_row(ACCT, "Archive", index + 40, msg)
            self.assertFalse(imap_mod.store_message(conn, row, snippet))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 2)


# ---------------------------------------------------------------------------------------------------
# The run log
# ---------------------------------------------------------------------------------------------------
class RunLogSaysHowEveryStageEnded(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_database(None)

    def rows(self) -> list:
        return [(r["stage"], r["finished_at"], r["detail"]) for r in self.conn.execute("SELECT * FROM run_log")]

    def test_a_stage_that_finishes_records_its_result(self):
        self.assertEqual(pipeline._timed(self.conn, "demo", lambda: 5), 5)
        [(stage, finished, detail)] = self.rows()
        self.assertEqual((stage, detail), ("demo", "5"))
        self.assertIsNotNone(finished)

    def test_a_stopped_stage_is_recorded_as_stopped(self):
        def stopped():
            raise jobcontrol.Cancelled()
        with self.assertRaises(jobcontrol.Cancelled):
            pipeline._timed(self.conn, "demo", stopped)
        [(_stage, finished, detail)] = self.rows()
        self.assertEqual(detail, "stopped")
        self.assertIsNotNone(finished, "a stopped stage used to stay 'started, never finished' for ever")

    def test_a_failed_stage_is_recorded_with_the_reason_and_the_error_still_propagates(self):
        def broken():
            raise ValueError("boom")
        with self.assertRaises(ValueError):
            pipeline._timed(self.conn, "demo", broken)
        [(_stage, finished, detail)] = self.rows()
        self.assertEqual(detail, "failed: ValueError: boom")
        self.assertIsNotNone(finished)

    def test_a_closed_database_never_hides_the_real_error(self):
        """If the window is closed mid-job the connection is gone; the log must not replace the error."""
        def closes_then_fails():
            self.conn.close()
            raise ValueError("the real problem")
        with self.assertRaises(ValueError) as caught:
            pipeline._timed(self.conn, "demo", closes_then_fails)
        self.assertIn("the real problem", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
