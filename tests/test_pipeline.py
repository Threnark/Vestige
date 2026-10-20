"""End-to-end tests of the pipeline using a fake AI: MBOX import -> pre-filter ->
classify -> possible email changes -> export, plus write mode (save/resume)."""
import csv
import mailbox
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime
from email.message import EmailMessage

import ai_providers as ai
import db
import module_d_export as export
import module_b_prefilter as prefilter
import paths
import pipeline
from models import Account
from session import Session


def add_message(box, frm, to, subject, date, body, mid):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = frm, to, subject, date, mid
    m.set_content(body)
    box.add(m)


def build_mbox(path):
    box = mailbox.mbox(path)
    box.lock()
    add_message(box, "Spotify <no-reply@spotify.com>", "me+spotify@gmail.com",
                "Welcome to Spotify - verify your email", "Thu, 14 Mar 2019 10:00:00 +0000", "Hi, thanks for joining.", "<1@s>")
    add_message(box, "Spotify <no-reply@spotify.com>", "me@gmail.com",
                "Welcome to Spotify Premium", "Fri, 01 May 2020 10:00:00 +0000", "Premium welcome.", "<2@s>")
    add_message(box, "Spotify <news@spotify.com>", "me@gmail.com",
                "20% off Premium this week", "Sat, 02 May 2020 10:00:00 +0000", "Deals!", "<3@s>")
    add_message(box, "Google <noreply@accounts.google.com>", "me@gmail.com",
                "You used your Google Account to sign in to Notion", "Fri, 02 Jul 2021 10:00:00 +0000",
                "You used your Google Account me@gmail.com to sign in to Notion.", "<4@g>")
    add_message(box, "Google <noreply@accounts.google.com>", "me@gmail.com",
                "You used your Google Account to sign in to Figma", "Mon, 10 Jan 2022 10:00:00 +0000",
                "You used your Google Account me@gmail.com to sign in to Figma.", "<5@g>")
    add_message(box, "Dropbox <no-reply@dropbox.com>", "old@yahoo.com",          # forwarded old address
                "Welcome to Dropbox - verify your email", "Mon, 04 Apr 2016 10:00:00 +0000", "Welcome!", "<6@d>")
    add_message(box, "Dropbox <no-reply@dropbox.com>", "old@yahoo.com",
                "Your email address was changed", "Wed, 01 Feb 2023 10:00:00 +0000",
                "The email address on your Dropbox account was changed to w***@acme.com.", "<7@d>")
    add_message(box, "Amazon <auto@amazon.com>", "me@gmail.com",
                "Your order has shipped", "Tue, 03 Jan 2021 10:00:00 +0000", "Tracking number 123", "<8@a>")
    add_message(box, "Evil <x@evil.com>", "me@gmail.com",
                "Welcome to =cmd", "Tue, 05 Jan 2021 10:00:00 +0000", "Hello", "<9@e>")
    box.flush()
    box.unlock()
    box.close()


class FakeProvider(ai.AIProvider):
    """Stands in for a real AI. Decides from simple text patterns."""

    def __init__(self):
        super().__init__()
        self.requests = 0
        self.is_local = True

    NAMES = {"spotify.com": "Spotify", "dropbox.com": "Dropbox", "evil.com": "=cmd|' /C calc'!A0"}

    def structured_call(self, system_prompt, user_message, tool, essential=(), fallbacks=None, max_tokens=3000):
        self.requests += 1
        results = []
        if "GROUP" in user_message:
            for block in user_message.split("### GROUP: ")[1:]:
                key = block.splitlines()[0].split(" ")[0]
                domain = key.split("#")[0]
                if "#" in key:                                    # identity-provider notice
                    app = re.search(r"sign in to (\w+)", block)
                    name = app.group(1) if app else ""
                else:
                    name = self.NAMES.get(domain, "")
                results.append({"group_id": key, "is_signup": bool(name), "service_name": name,
                                "category": "productivity_saas", "signup_method": "native_password",
                                "confidence": 0.9, "representative_sample_index": 0, "reasoning": "fake"})
        else:
            for block in user_message.split("### EMAIL id=")[1:]:
                email_id = int(block.splitlines()[0])
                printed = re.search(r"changed to (\S+?)\.?\s*$", block, re.M)
                results.append({"email_id": email_id, "is_email_change": bool(printed),
                                "change_type": "changed", "service_name": "Dropbox",
                                "recipient_role": "old_address", "old_address_printed": "",
                                "new_address_printed": printed.group(1) if printed else "",
                                "confidence": 0.85, "reasoning": "fake"})
        return results


class PipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mbox = os.path.join(self.tmp, "mail.mbox")
        build_mbox(self.mbox)
        self.main = Account(name="gmail", provider="gmail", email="me@gmail.com")      # no password: MBOX only
        self.work = Account(name="work", provider="other", email="work@acme.com")
        self.accounts = [self.main, self.work]
        self.conn = db.open_database(None)                                              # in memory
        self.provider = FakeProvider()

    def run_all(self):
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        pipeline.stage_prefilter(self.conn)
        pipeline.stage_fetch_bodies(self.conn, self.accounts)
        pipeline.stage_classify(self.conn, self.provider, 6)
        pipeline.stage_detect_changes(self.conn, self.provider, self.accounts, 5)

    def test_import_and_prefilter(self):
        self.assertEqual(pipeline.stage_import_mbox(self.conn, self.main, self.mbox), 9)
        self.assertEqual(pipeline.stage_import_mbox(self.conn, self.main, self.mbox), 0)   # re-import is a no-op
        result = pipeline.stage_prefilter(self.conn)
        self.assertEqual(result["scored"], 9)
        labels = {r["subject"]: (r["prefilter_label"], r["change_candidate"]) for r in self.conn.execute("SELECT * FROM emails")}
        self.assertEqual(labels["Welcome to Spotify - verify your email"][0], "candidate")
        self.assertEqual(labels["20% off Premium this week"][0], "noise")
        self.assertEqual(labels["Your order has shipped"][0], "noise")
        self.assertEqual(labels["Your email address was changed"][1], 1)
        self.assertEqual(labels["Welcome to Dropbox - verify your email"][1], 0)

    def test_recipient_detection_incl_forwarded_old_address(self):
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        used = {r["subject"]: r["email_address_used"] for r in self.conn.execute("SELECT subject, email_address_used FROM emails")}
        self.assertEqual(used["Welcome to Spotify - verify your email"], "me+spotify@gmail.com")   # plus alias kept
        self.assertEqual(used["Welcome to Dropbox - verify your email"], "old@yahoo.com")          # forwarded address kept

    def test_mbox_rows_need_no_body_fetch(self):
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        pipeline.stage_prefilter(self.conn)
        self.assertEqual(db.emails_missing_body(self.conn), [])
        self.assertEqual(pipeline.stage_fetch_bodies(self.conn, self.accounts), 0)

    def test_imap_stage_skips_accounts_without_password(self):
        self.assertEqual(pipeline.stage_fetch(self.conn, self.accounts), 0)

    def test_signups_are_one_row_per_service_and_address(self):
        self.run_all()
        rows = export.build_signup_rows(self.conn, 0.5)
        pairs = {(r["Service Name"], r["Email Address Used"], r["Sign-up Confirmation Date"]) for r in rows}
        self.assertIn(("Spotify", "me+spotify@gmail.com", "2019-03-14"), pairs)
        self.assertIn(("Spotify", "me@gmail.com", "2020-05-01"), pairs)
        self.assertIn(("Dropbox", "old@yahoo.com", "2016-04-04"), pairs)
        # identity-provider notices resolve to DIFFERENT services (one row each)
        self.assertIn(("Notion", "me@gmail.com", "2021-07-02"), pairs)
        self.assertIn(("Figma", "me@gmail.com", "2022-01-10"), pairs)
        self.assertFalse(any(r["Service Name"] == "Amazon" for r in rows))
        keys = db.classified_keys(self.conn)
        self.assertEqual(sum("#" in k for k in keys), 2)

    def test_possible_email_change_is_matched_to_a_known_address(self):
        self.run_all()
        changes = export.build_change_rows(self.conn, 0.5)
        self.assertEqual(len(changes), 1)
        c = changes[0]
        self.assertEqual(c["Service Name"], "Dropbox")
        self.assertEqual(c["Notice Sent To"], "old@yahoo.com")
        self.assertEqual(c["Account May Now Use"], "work@acme.com")        # w***@acme.com matched a real address
        self.assertEqual(c["Match"], "Strong")
        self.assertEqual(c["Originally Signed Up With"], "old@yahoo.com")
        self.assertIn("matches your address work@acme.com", c["What The Notice Says"])

    def test_stages_are_resumable_and_only_do_new_work(self):
        self.run_all()
        first = self.provider.requests
        self.run_all()                                                      # nothing new -> no AI calls
        self.assertEqual(self.provider.requests, first)

    def test_unanswered_groups_are_retried_not_hidden(self):
        class Lazy(FakeProvider):
            def structured_call(self, *a, **k):
                out = super().structured_call(*a, **k)
                return out[:1]                                              # answers only the first item
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        pipeline.stage_prefilter(self.conn)
        lazy = Lazy()
        pipeline.stage_classify(self.conn, lazy, 6)
        done = len(db.classified_keys(self.conn))
        pipeline.stage_classify(self.conn, FakeProvider(), 6)
        self.assertGreater(len(db.classified_keys(self.conn)), done)

    def test_max_groups_caps_a_test_run(self):
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        pipeline.stage_prefilter(self.conn)
        pipeline.stage_classify(self.conn, self.provider, 6, max_groups=2)
        self.assertEqual(len(db.classified_keys(self.conn)), 2)

    def test_cancel_stops_cleanly(self):
        from jobcontrol import Cancelled, JobControl
        pipeline.stage_import_mbox(self.conn, self.main, self.mbox)
        pipeline.stage_prefilter(self.conn)
        control = JobControl()
        control.cancel()
        with self.assertRaises(Cancelled):
            pipeline.stage_classify(self.conn, self.provider, 1, control=control)
        self.assertEqual(self.provider.requests, 0)


class ExportRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        mbox = os.path.join(self.tmp, "mail.mbox")
        build_mbox(mbox)
        self.conn = db.open_database(None)
        acct = Account(name="gmail", provider="gmail", email="me@gmail.com")
        self.work = Account(name="work", provider="other", email="work@acme.com")
        pipeline.stage_import_mbox(self.conn, acct, mbox)
        pipeline.stage_prefilter(self.conn)
        provider = FakeProvider()
        pipeline.stage_classify(self.conn, provider, 6)
        pipeline.stage_detect_changes(self.conn, provider, [acct, self.work], 5)
        self.out = os.path.join(self.tmp, "exports")
        os.mkdir(self.out)
        self.now = datetime(2026, 10, 12, 15, 30, 5)

    def opts(self, **kw):
        base = dict(folder=self.out, formats=("csv", "md"), add_timestamp=True, use_subfolder=False)
        base.update(kw)
        return export.ExportOptions(**base)

    def test_files_are_named_with_timestamp_and_contents_are_right(self):
        result = export.export_files(self.conn, self.opts(), now=self.now)
        names = sorted(os.path.basename(f) for f in result.files)
        self.assertEqual(names, ["signups_2026-10-12_153005.csv", "signups_2026-10-12_153005.md",
                                 "signups_possible_email_changes_2026-10-12_153005.csv"])
        with open(os.path.join(self.out, "signups_2026-10-12_153005.csv"), encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0].keys()), export.SIGNUP_COLUMNS)
        md = open(os.path.join(self.out, "signups_2026-10-12_153005.md"), encoding="utf-8").read()
        self.assertIn("## Possible email changes", md)

    def test_never_overwrites(self):
        first = export.export_files(self.conn, self.opts(), now=self.now)
        second = export.export_files(self.conn, self.opts(), now=self.now)           # same second, same names
        self.assertFalse(set(first.files) & set(second.files))
        self.assertTrue(all(" (2)" in os.path.basename(f) for f in second.files))
        for f in first.files + second.files:
            self.assertTrue(os.path.exists(f))

    def test_no_timestamp_and_subfolder_options(self):
        r = export.export_files(self.conn, self.opts(add_timestamp=False, use_subfolder=True, formats=("csv",)),
                                now=self.now)
        self.assertEqual(os.path.basename(r.folder), "Vestige_2026-10-12_153005")
        self.assertEqual(sorted(os.path.basename(f) for f in r.files),
                         ["signups.csv", "signups_possible_email_changes.csv"])

    def test_spreadsheet_formulas_are_defused(self):
        r = export.export_files(self.conn, self.opts(formats=("csv",)), now=self.now)
        with open(r.files[0], encoding="utf-8-sig", newline="") as f:
            names = [row["Service Name"] for row in csv.DictReader(f)]
        self.assertTrue(any(n.startswith("'=") for n in names))
        self.assertFalse(any(n.startswith("=") for n in names))

    def test_friendly_errors(self):
        with self.assertRaises(export.ExportError):
            export.export_files(self.conn, self.opts(folder=""))
        with self.assertRaises(export.ExportError) as cm:
            export.export_files(self.conn, self.opts(folder=os.path.join(self.tmp, "missing-drive")))
        self.assertIn("does not exist", str(cm.exception))
        with self.assertRaises(export.ExportError):
            export.export_files(self.conn, self.opts(formats=()))
        with self.assertRaises(export.ExportError):
            export.export_files(db.open_database(None), self.opts())                 # nothing to export

    def test_unsafe_file_names_are_cleaned(self):
        r = export.export_files(self.conn, self.opts(base_name='my:/list*?', add_timestamp=False, formats=("csv",)))
        self.assertEqual(os.path.basename(r.files[0]), "my__list__.csv")

    def test_nothing_is_written_to_the_folder_until_export(self):
        self.assertEqual(os.listdir(self.out), [])


class WriteMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mbox = os.path.join(self.tmp, "mail.mbox")
        build_mbox(self.mbox)
        self.data = os.path.join(self.tmp, "Vestige_DATA")
        self.acct = Account(name="gmail", provider="gmail", email="me@gmail.com")

    def test_memory_only_by_default_writes_nothing(self):
        s = Session()
        pipeline.stage_import_mbox(s.conn, self.acct, self.mbox)
        pipeline.stage_prefilter(s.conn)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["mail.mbox"])
        s.close()

    def test_enable_copies_progress_and_resume_skips_finished_work(self):
        s = Session()
        pipeline.stage_import_mbox(s.conn, self.acct, self.mbox)
        pipeline.stage_prefilter(s.conn)
        path = s.enable_write_mode(self.data)
        self.assertEqual(path, os.path.join(self.data, "session.db"))
        provider = FakeProvider()
        pipeline.stage_classify(s.conn, provider, 6)
        s.close()                                                       # "crash" / accidental close

        s2 = Session()
        s2.resume_from(path)
        self.assertTrue(s2.write_mode)
        before = provider.requests
        pipeline.stage_classify(s2.conn, provider, 6)                   # nothing left to do
        self.assertEqual(provider.requests, before)
        self.assertGreater(db.counts(s2.conn)["confirmed_signups"], 0)
        s2.close()

    def test_saved_file_never_contains_credentials(self):
        s = Session()
        s.upsert_account(Account(name="g", provider="gmail", email="me@gmail.com", password="HUNTER2-APP-PW"))
        pipeline.stage_import_mbox(s.conn, self.acct, self.mbox)
        path = s.enable_write_mode(self.data)
        s.close()
        for name in os.listdir(self.data):
            with open(os.path.join(self.data, name), "rb") as f:
                self.assertNotIn(b"HUNTER2-APP-PW", f.read())

    def test_existing_session_is_not_overwritten_silently(self):
        s = Session()
        s.enable_write_mode(self.data)
        s.close()
        s2 = Session()
        with self.assertRaises(FileExistsError):
            s2.enable_write_mode(self.data)
        s2.enable_write_mode(self.data, overwrite=True)
        s2.close()

    def test_disable_keeps_data_in_memory_and_can_delete_file(self):
        s = Session()
        pipeline.stage_import_mbox(s.conn, self.acct, self.mbox)
        path = s.enable_write_mode(self.data)
        removed = s.disable_write_mode(delete_file=True)
        self.assertIn(path, removed)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(s.write_mode)
        self.assertEqual(db.counts(s.conn)["total_emails"], 9)          # still there, in memory
        s.close()

    def test_old_database_versions_are_refused_politely(self):
        import sqlite3
        old = os.path.join(self.tmp, "old.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE emails (id INTEGER)")
        c.commit()
        c.close()
        with self.assertRaises(db.DatabaseVersionError):
            db.open_database(old)


class PathHelpers(unittest.TestCase):
    def test_frozen_exe_location(self):
        import sys
        saved = (getattr(sys, "frozen", None), sys.executable)
        try:
            sys.frozen = True
            sys.executable = os.path.join(os.sep, "apps", "Vestige", "Vestige.exe")
            self.assertEqual(paths.app_base_dir(), os.path.join(os.sep, "apps", "Vestige"))
            sys.executable = os.path.join(os.sep, "Apps", "Vestige.app", "Contents", "MacOS", "Vestige")
            self.assertEqual(paths.app_base_dir(), os.path.join(os.sep, "Apps"))
            self.assertEqual(paths.default_data_dir(), os.path.join(os.sep, "Apps", "Vestige_DATA"))
        finally:
            sys.executable = saved[1]
            if saved[0] is None:
                del sys.frozen
            else:
                sys.frozen = saved[0]

    def test_cloud_sync_warning(self):
        self.assertEqual(paths.cloud_sync_warning("C:\\Users\\me\\OneDrive\\Docs"), "OneDrive")
        self.assertEqual(paths.cloud_sync_warning("/Users/me/Dropbox/tools"), "Dropbox")
        self.assertIsNone(paths.cloud_sync_warning("/home/me/tools"))
        self.assertIsNone(paths.cloud_sync_warning("/home/me/megabytes"))

    def test_check_writable(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(paths.check_writable(d)[0])
            ok, msg = paths.check_writable(os.path.join(d, "nope"))
            self.assertFalse(ok)
            self.assertIn("does not exist", msg)
            self.assertTrue(paths.check_writable(os.path.join(d, "made"), create=True)[0])


if __name__ == "__main__":
    unittest.main()
