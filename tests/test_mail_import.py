"""Tests for importing exported mail files (.pst / .eml / .mbox), the Outlook/Hotmail
handling, and the table size arithmetic.

IMPORTANT ABOUT THE PST TESTS: they use a small stand-in for the `pypff` module that
mimics the parts of its interface the importer relies on. They prove Vestige's own
logic (folder skipping, header parsing, fallbacks, de-duplication, error handling).
They do NOT prove the real libpff library reads a real .pst the way the stand-in
does; that needs a real Outlook export (see README "Testing the PST import")."""
import imaplib
import mailbox
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime
from email.message import EmailMessage
from unittest import mock

import db
import module_a_files as files
import module_a_imap as imap_mod
import module_a_pst as pst_mod
import pipeline
import ui_metrics as metrics
from models import MICROSOFT_PASSWORD_NOTE, Account, ImportProblem, is_microsoft_mail

ME = Account(name="hotmail", provider="outlook", email="me@hotmail.com")


# ---------------------------------------------------------------------------
# A stand-in for pypff
# ---------------------------------------------------------------------------
class FakeEntry:
    def __init__(self, text):
        self.text = text

    def get_data_as_string(self):
        return self.text


class FakeRecordSet:
    def __init__(self, props):
        self.props = props

    def get_entry_by_type(self, tag):
        return FakeEntry(self.props[tag]) if tag in self.props else None


class FakeMessage:
    def __init__(self, headers=None, subject=None, sender_name=None, plain=None, html=None,
                 delivery_time=None, mapi=None, broken=False):
        self.transport_headers = headers
        self.subject = subject
        self.sender_name = sender_name
        self.plain_text_body = plain
        self.html_body = html
        self.delivery_time = delivery_time
        self.number_of_record_sets = 1 if mapi else 0
        self._mapi = mapi or {}
        self._broken = broken

    def get_record_set(self, index):
        return FakeRecordSet(self._mapi)

    def __getattribute__(self, name):
        if name == "transport_headers" and object.__getattribute__(self, "_broken"):
            raise OSError("libpff: unable to retrieve transport headers")
        return object.__getattribute__(self, name)


class FakeFolder:
    def __init__(self, name, messages=(), subfolders=()):
        self.name = name
        self._messages, self._subfolders = list(messages), list(subfolders)
        self.number_of_sub_messages = len(self._messages)
        self.number_of_sub_folders = len(self._subfolders)

    def get_sub_message(self, i):
        m = self._messages[i]
        if isinstance(m, Exception):
            raise m
        return m

    def get_sub_folder(self, i):
        return self._subfolders[i]


class FakePstFile:
    root = None
    open_error = None

    def open(self, path):
        if FakePstFile.open_error:
            raise FakePstFile.open_error

    def get_root_folder(self):
        return FakePstFile.root

    def close(self):
        pass


def fake_pypff_module():
    module = types.ModuleType("pypff")
    module.file = FakePstFile
    return module


def headers(frm, to, subject, mid, date="Thu, 14 Mar 2019 10:00:00 +0000", extra=""):
    return (f"From: {frm}\r\nTo: {to}\r\nSubject: {subject}\r\nDate: {date}\r\n"
            f"Message-ID: {mid}\r\n{extra}\r\n")


def sample_tree():
    inbox = FakeFolder("Inbox", [
        FakeMessage(headers("Spotify <no-reply@spotify.com>", "me@hotmail.com",
                            "Welcome to Spotify", "<1@s>", extra="List-Unsubscribe: <mailto:u@spotify.com>\r\n"),
                    plain="Hi, thanks for joining Spotify. Please confirm your account to get started today."),
        FakeMessage(headers("Dropbox <no-reply@dropbox.com>", "old@yahoo.com", "Welcome to Dropbox", "<2@d>"),
                    plain="Hi", html="<p>Your Dropbox account was created successfully. Welcome aboard, friend!</p>"),
        # no internet headers: sender comes from Outlook's own properties
        FakeMessage(subject="Welcome to Notion", sender_name="Notion", plain="Welcome!",
                    delivery_time=datetime(2021, 7, 2, 10, 0, 0),
                    mapi={0x5D01: "team@mail.notion.so"}),
        # no headers and no sender address at all (a calendar item, a draft...)
        FakeMessage(subject="Meeting", sender_name="Someone"),
        # sent from the account's own address
        FakeMessage(headers("Me <me@hotmail.com>", "friend@x.com", "Hello", "<own@me>")),
        # a damaged item
        OSError("damaged item"),
        # same Message-ID again (a duplicate)
        FakeMessage(headers("Spotify <no-reply@spotify.com>", "me@hotmail.com", "Welcome to Spotify", "<1@s>")),
    ])
    deleted = FakeFolder("Deleted Items", [
        FakeMessage(headers("Figma <hi@figma.com>", "me@hotmail.com", "Welcome to Figma", "<3@f>"), plain="x")])
    sent = FakeFolder("Sent Items", [FakeMessage(headers("a@b.com", "c@d.com", "Sent thing", "<s1>"))])
    calendar = FakeFolder("Calendar", [FakeMessage(subject="Event")])
    archive = FakeFolder("Archive", [], subfolders=[
        FakeFolder("2018", [FakeMessage(headers("GitHub <noreply@github.com>", "me@hotmail.com",
                                                "Welcome to GitHub", "<4@g>"), plain="y")])])
    top = FakeFolder("Top of Outlook data file", [], [inbox, deleted, sent, calendar, archive])
    return FakeFolder("Root - Mailbox", [], [top])


class PstImport(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_database(None)
        FakePstFile.root, FakePstFile.open_error = sample_tree(), None
        patcher = mock.patch.dict(sys.modules, {"pypff": fake_pypff_module()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "export.pst")
        with open(self.path, "wb") as fh:
            fh.write(b"!BDN" + b"\0" * 100)

    def rows(self):
        return {r["subject"]: r for r in self.conn.execute("SELECT * FROM emails")}

    def test_imports_mail_and_skips_what_it_should(self):
        inserted = pst_mod.import_pst(ME, self.path, self.conn)
        rows = self.rows()
        self.assertEqual(inserted, 5)
        self.assertEqual(set(rows), {"Welcome to Spotify", "Welcome to Dropbox", "Welcome to Notion",
                                     "Welcome to Figma", "Welcome to GitHub"})     # incl. Deleted Items + a sub-folder
        self.assertNotIn("Sent thing", rows)                                        # Sent Items skipped
        self.assertNotIn("Hello", rows)                                             # from own address
        self.assertNotIn("Meeting", rows)                                           # no sender address

    def test_headers_are_used_when_present(self):
        pst_mod.import_pst(ME, self.path, self.conn)
        spotify = self.rows()["Welcome to Spotify"]
        self.assertEqual(spotify["sender_email"], "no-reply@spotify.com")
        self.assertEqual(spotify["sender_domain"], "spotify.com")
        self.assertEqual(spotify["email_address_used"], "me@hotmail.com")
        self.assertEqual(spotify["date_iso"], "2019-03-14T10:00:00+00:00")
        self.assertIn("mailto:u@spotify.com", spotify["list_unsubscribe"])
        self.assertEqual(spotify["uid"], None)
        self.assertTrue(spotify["folder"].startswith("pst:"))
        self.assertEqual(spotify["body_fetched"], 1)                                # nothing more to fetch
        self.assertIn("thanks for joining", spotify["body_snippet"])

    def test_forwarded_old_address_is_kept(self):
        pst_mod.import_pst(ME, self.path, self.conn)
        self.assertEqual(self.rows()["Welcome to Dropbox"]["email_address_used"], "old@yahoo.com")

    def test_html_body_is_used_when_plain_is_a_stub(self):
        pst_mod.import_pst(ME, self.path, self.conn)
        self.assertIn("account was created", self.rows()["Welcome to Dropbox"]["body_snippet"])

    def test_message_without_headers_uses_outlook_properties(self):
        pst_mod.import_pst(ME, self.path, self.conn)
        notion = self.rows()["Welcome to Notion"]
        self.assertEqual(notion["sender_email"], "team@mail.notion.so")
        self.assertEqual(notion["sender_domain"], "mail.notion.so")
        self.assertEqual(notion["date_iso"], "2021-07-02T10:00:00+00:00")
        self.assertTrue(notion["message_id"].startswith("<no-id-hotmail-pst-"))

    def test_reimport_adds_nothing(self):
        pst_mod.import_pst(ME, self.path, self.conn)
        self.assertEqual(pst_mod.import_pst(ME, self.path, self.conn), 0)

    def test_folder_walk_reports_skipped_folders(self):
        folders, skipped = pst_mod.mail_folders(sample_tree())
        labels = [label for _, label in folders]
        self.assertIn("Inbox", labels)                                  # no "Root - Mailbox/Top of ..." noise
        self.assertIn("Archive/2018", labels)
        names = [label.split("/")[-1] for label in labels]
        self.assertIn("Inbox", names)
        self.assertIn("Deleted Items", names)
        self.assertIn("2018", names)
        self.assertEqual(sorted(s.split("/")[-1] for s in skipped), ["Calendar", "Sent Items"])

    def test_a_skipped_folders_children_are_skipped_too(self):
        tree = FakeFolder("Root", [], [FakeFolder("Sent Items", [], [FakeFolder("Old", [FakeMessage(headers(
            "a@b.com", "c@d.com", "Deep", "<d>"))])])])
        folders, skipped = pst_mod.mail_folders(tree)
        self.assertEqual(len(folders), 1)                              # only the root itself
        self.assertEqual(skipped, ["Sent Items"])

    def test_missing_library_gives_a_clear_message(self):
        with mock.patch.dict(sys.modules, {"pypff": None}):          # makes `import pypff` raise ImportError
            with self.assertRaises(ImportProblem) as ctx:
                pst_mod.import_pst(ME, self.path, self.conn)
        self.assertIn("pip install libpff-python", str(ctx.exception))
        self.assertIn(".mbox", str(ctx.exception))                   # offers the alternative

    def test_unopenable_file_gives_a_clear_message(self):
        FakePstFile.open_error = OSError("invalid file signature")
        with self.assertRaises(ImportProblem) as ctx:
            pst_mod.import_pst(ME, self.path, self.conn)
        self.assertIn("Couldn't open", str(ctx.exception))

    def test_a_property_that_raises_does_not_stop_the_import(self):
        FakePstFile.root = FakeFolder("Root", [], [FakeFolder("Inbox", [
            FakeMessage(broken=True, subject="Broken", sender_name="X"),
            FakeMessage(headers("A <a@shop.com>", "me@hotmail.com", "Fine", "<f>"))])])
        self.assertEqual(pst_mod.import_pst(ME, self.path, self.conn), 1)

    def test_progress_and_cancel_are_honoured(self):
        from jobcontrol import Cancelled, JobControl
        control = JobControl()
        control.cancel()
        with self.assertRaises(Cancelled):
            pst_mod.import_pst(ME, self.path, self.conn, control=control)


# ---------------------------------------------------------------------------
# EML + MBOX + the dispatcher
# ---------------------------------------------------------------------------
def eml_bytes(frm, subject, mid, body="Thanks for signing up to Acme. Please verify your email address now."):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = (
        frm, "me@hotmail.com", subject, "Fri, 01 May 2020 10:00:00 +0000", mid)
    m.set_content(body)
    return m.as_bytes()


class FileImport(unittest.TestCase):
    def setUp(self):
        self.conn = db.open_database(None)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, relative, data):
        path = os.path.join(self.tmp, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_folder_of_eml_files_is_searched_recursively(self):
        self.write("a/1.eml", eml_bytes("Acme <hi@acme.com>", "Welcome to Acme", "<1@a>"))
        self.write("a/deeper/2.EML", eml_bytes("Beta <hi@beta.com>", "Welcome to Beta", "<2@b>"))
        self.write("a/notes.txt", b"not mail")
        self.assertEqual(files.import_mail(ME, self.tmp, self.conn), 2)
        subjects = {r["subject"] for r in self.conn.execute("SELECT subject FROM emails")}
        self.assertEqual(subjects, {"Welcome to Acme", "Welcome to Beta"})
        row = self.conn.execute("SELECT * FROM emails WHERE subject='Welcome to Acme'").fetchone()
        self.assertEqual(row["folder"], "eml")
        self.assertIn("verify your email", row["body_snippet"])
        self.assertEqual(files.import_mail(ME, self.tmp, self.conn), 0)           # re-import is a no-op

    def test_chosen_files_are_read_whatever_they_are_called(self):
        path = self.write("saved_message", eml_bytes("Acme <hi@acme.com>", "Welcome to Acme", "<1@a>"))
        self.assertEqual(files.import_mail(ME, [path], self.conn), 1)

    def test_a_file_that_is_not_an_email_is_ignored_not_fatal(self):
        good = self.write("good.eml", eml_bytes("Acme <hi@acme.com>", "Welcome", "<1@a>"))
        bad = self.write("bad.eml", b"just some text with no headers at all")
        self.assertEqual(files.import_mail(ME, [good, bad], self.conn), 1)

    def test_eml_without_message_id_gets_a_stable_stand_in(self):
        m = EmailMessage()
        m["From"], m["Subject"] = "Acme <hi@acme.com>", "No id"
        m.set_content("hello there, thanks for signing up")
        path = self.write("noid.eml", m.as_bytes())
        self.assertEqual(files.import_mail(ME, path, self.conn), 1)
        self.assertEqual(files.import_mail(ME, path, self.conn), 0)

    def test_empty_folder_explains_itself(self):
        with self.assertRaises(ImportProblem) as ctx:
            files.import_mail(ME, self.tmp, self.conn)
        self.assertIn(".eml", str(ctx.exception))

    def test_mbox_still_imports_through_the_dispatcher(self):
        path = os.path.join(self.tmp, "mail.mbox")
        box = mailbox.mbox(path)
        m = EmailMessage()
        m["From"], m["To"], m["Subject"], m["Message-ID"] = "Acme <hi@acme.com>", "me@hotmail.com", "Hi", "<m1>"
        m.set_content("hello")
        box.add(m)
        box.flush()
        box.close()
        self.assertEqual(files.import_mail(ME, path, self.conn), 1)
        self.assertEqual(pipeline.stage_import_mbox(self.conn, ME, path), 0)       # the old name still works

    def test_several_sources_in_one_go(self):
        mbox = os.path.join(self.tmp, "x.mbox")
        box = mailbox.mbox(mbox)
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"] = "A <a@a.com>", "In mbox", "<mm>"
        m.set_content("body")
        box.add(m)
        box.flush()
        box.close()
        eml = self.write("one.eml", eml_bytes("Acme <hi@acme.com>", "In eml", "<ee>"))
        self.assertEqual(files.import_mail(ME, [mbox, eml], self.conn), 2)


class Recognising(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_contents_beat_the_file_name(self):
        self.assertEqual(files.sniff(self.make("renamed.mbox", b"!BDN\x00\x00")), "pst")
        self.assertEqual(files.sniff(self.make("Inbox", b"From - Mon Jan 1 00:00:00 2024\nFrom: a@b.com\n")), "mbox")
        self.assertEqual(files.sniff(self.make("noext", b"Received: from x\nFrom: a@b.com\n")), "eml")
        self.assertEqual(files.sniff(self.make("a.txt", b"hello")), "unknown")

    def test_names_are_used_when_the_contents_say_nothing(self):
        self.assertEqual(files.sniff(self.make("empty.pst", b"")), "pst")           # reader explains what's wrong
        self.assertEqual(files.sniff(self.make("x.mbox", b"")), "mbox")

    def test_unsupported_formats_say_what_to_do(self):
        cases = {"mac.olm": (b"PK\x03\x04rest", "Outlook for Mac"),
                 "one.msg": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", ".pst"),
                 "export.csv": (b"Subject,Body\n", "CSV"),
                 "takeout.zip": (b"PK\x03\x04rest", "Unzip")}
        for name, (data, expected) in cases.items():
            with self.assertRaises(ImportProblem) as ctx:
                files.plan_import([self.make(name, data)])
            self.assertIn(expected, str(ctx.exception), name)

    def test_everything_is_checked_before_anything_is_imported(self):
        good = self.make("a.eml", eml_bytes("Acme <hi@acme.com>", "S", "<1>"))
        conn = db.open_database(None)
        with self.assertRaises(ImportProblem):
            files.import_mail(ME, [good, self.make("b.csv", b"x")], conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0], 0)

    def test_missing_file_and_empty_choice(self):
        with self.assertRaises(ImportProblem):
            files.plan_import([os.path.join(self.tmp, "nope.pst")])
        with self.assertRaises(ImportProblem):
            files.plan_import([])


# ---------------------------------------------------------------------------
# Outlook / Hotmail handling
# ---------------------------------------------------------------------------
class MicrosoftMail(unittest.TestCase):
    def test_consumer_domains_in_every_country_variant(self):
        for addr in ("a@outlook.com", "a@hotmail.com", "a@hotmail.co.uk", "a@live.com.au", "a@msn.com",
                     "a@outlook.co.nz", "A@HOTMAIL.CO.NZ", "a@live.nl"):
            self.assertTrue(is_microsoft_mail(addr), addr)

    def test_other_addresses_are_not_flagged(self):
        for addr in ("a@gmail.com", "a@outlook.mycompany.com", "a@notlive.com", "a@live.com.au.evil.com", ""):
            self.assertFalse(is_microsoft_mail(addr), addr)

    def test_microsoft_imap_hosts_are_flagged(self):
        self.assertTrue(is_microsoft_mail("me@myowndomain.com", "outlook.office365.com"))
        self.assertFalse(is_microsoft_mail("me@myowndomain.com", "imap.fastmail.com"))

    def test_account_property(self):
        self.assertTrue(Account(name="n", provider="outlook", email="x@example.com").is_microsoft)
        self.assertTrue(Account(name="n", provider="other", email="x@live.com").is_microsoft)
        self.assertFalse(Account(name="n", provider="gmail", email="x@gmail.com").is_microsoft)

    def test_imap_login_failure_explains_why(self):
        account = Account(name="h", provider="other", email="me@hotmail.com", imap_server="outlook.office365.com",
                          password="app-password")
        fake = mock.Mock()
        fake.login.side_effect = imaplib.IMAP4.error("LOGIN failed.")
        with mock.patch.object(imap_mod.imaplib, "IMAP4_SSL", return_value=fake):
            with self.assertRaises(imap_mod.AccountLoginError) as ctx:
                imap_mod.connect(account)
        self.assertIn("OAuth", str(ctx.exception))
        self.assertIn(".pst", str(ctx.exception))
        self.assertIn("LOGIN failed", str(ctx.exception))
        self.assertIn(MICROSOFT_PASSWORD_NOTE.splitlines()[0], str(ctx.exception))

    def test_other_providers_keep_the_normal_login_message(self):
        account = Account(name="g", provider="gmail", email="me@gmail.com", imap_server="imap.gmail.com",
                          password="wrong")
        fake = mock.Mock()
        fake.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        with mock.patch.object(imap_mod.imaplib, "IMAP4_SSL", return_value=fake):
            with self.assertRaises(imap_mod.AccountLoginError) as ctx:
                imap_mod.connect(account)
        self.assertIn("APP password", str(ctx.exception))
        self.assertNotIn("OAuth", str(ctx.exception))

    def test_fetch_skips_a_passwordless_microsoft_account_with_an_explanation(self):
        import contextlib
        import io
        conn = db.open_database(None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pipeline.stage_fetch(conn, [ME])
        self.assertIn("Import your exported .pst", out.getvalue())


# ---------------------------------------------------------------------------
# Table sizes
# ---------------------------------------------------------------------------
class TableMetrics(unittest.TestCase):
    def test_table_text_is_smaller_than_the_old_10pt_and_in_pixels(self):
        body, head = metrics.table_font_specs("Segoe UI", False, 1.0)
        self.assertEqual(body, ("Segoe UI", -12))                 # negative = pixels
        self.assertEqual(head, ("Segoe UI", -12, "bold"))

    def test_semibold_faces_are_not_bolded_again(self):
        _, head = metrics.table_font_specs("Yu Gothic UI Semibold", True, 1.0)
        self.assertEqual(head, ("Yu Gothic UI Semibold", -12))

    def test_text_follows_the_display_scale(self):
        self.assertEqual(metrics.table_font_specs("F", False, 1.5)[0][1], -18)
        self.assertEqual(metrics.scaled(140, 1.25), 175)

    def test_nonsense_scale_values_are_made_safe(self):
        for bad in (None, "x", float("nan"), 0, -3, 99):
            self.assertTrue(metrics.MIN_SCALE <= metrics.clamp_scale(bad) <= metrics.MAX_SCALE)
        self.assertEqual(metrics.scaled(0, 1.0), 1)


if __name__ == "__main__":
    unittest.main()
