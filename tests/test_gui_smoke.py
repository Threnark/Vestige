"""Smoke tests for the interface: build the REAL pages and pop-up windows against the stand-in for
Tk (tests/fake_tk.py) and check that the [?] help boxes are there and point where they should, and
that the Outlook/Hotmail handling in the Add account window reacts as it should.

These catch typos and wiring mistakes (a missing name, a button pointing nowhere). They can't show
what anything looks like on a real screen; that still needs a person to open the program."""
import importlib
import sys
import unittest
from unittest import mock

from models import Account
from tests import fake_tk


class Smoke(unittest.TestCase):
    def setUp(self):
        manager = fake_tk.installed(permissive=True)
        self.env = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        for name in ("gui_setup", "gui_run", "gui_results"):          # import them against the fake Tk
            sys.modules.pop(name, None)
        self.setup_mod = importlib.import_module("gui_setup")
        self.run_mod = importlib.import_module("gui_run")
        self.results_mod = importlib.import_module("gui_results")
        self.accounts = [Account(name="hot", provider="outlook", email="me@hotmail.com"),
                         Account(name="gm", provider="gmail", email="me@gmail.com", password="x")]
        session = mock.MagicMock()
        session.write_mode = False
        session.accounts = self.accounts

        class App(fake_tk.FakeWidget):
            run_page = None
        self.root = fake_tk.FakeToplevel(None)
        self.app = App(self.root)
        self.app.session = session

    @staticmethod
    def question_marks(widget):
        return [w for w in widget.walk() if w.kw.get("text") == "?"]

    def test_setup_page_has_help_boxes_that_open_real_pages(self):
        page = self.setup_mod.SetupPage(fake_tk.FakeWidget(self.root), self.app)
        boxes = self.question_marks(page)
        self.assertGreaterEqual(len(boxes), 5)                       # Storage, AI heading, Model, API key, Accounts
        opened = set()
        for box in boxes:
            box.kw["command"]()
            opened.add(self.env.help_ui._window.topic_id)
        self.assertEqual(opened, {"privacy", "ai", "start"})

    def test_switching_to_local_ai_works_with_the_key_help_box_in_place(self):
        page = self.setup_mod.SetupPage(fake_tk.FakeWidget(self.root), self.app)
        page.mode_var.set("local")
        page._on_mode_change()                                       # must not raise (it hides the key row incl. its [?])
        self.assertIsNotNone(page.key_help)

    def test_run_page_has_help_boxes(self):
        page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        boxes = self.question_marks(page)
        self.assertEqual(len(boxes), 5)                              # Options, Accounts, Stages, Progress, Log
        for box in boxes:
            box.kw["command"]()
            self.assertEqual(self.env.help_ui._window.topic_id, "pipeline")

    def test_results_page_has_help_boxes(self):
        page = self.results_mod.ResultsPage(fake_tk.FakeWidget(self.root), self.app)
        boxes = self.question_marks(page)
        self.assertEqual(len(boxes), 2)                              # next to the top bar, and the Export heading
        for box in boxes:
            box.kw["command"]()
            self.assertEqual(self.env.help_ui._window.topic_id, "results")

    def test_add_account_window_help_follows_the_provider(self):
        dialog = self.setup_mod.AccountDialog(self.root, None)
        boxes = self.question_marks(dialog)
        self.assertEqual(len(boxes), 3)                              # Provider, App password, IMAP server
        for provider, topic in (("gmail", "gmail"), ("yahoo", "yahoo"), ("icloud", "icloud"),
                                ("outlook", "microsoft"), ("other", "other")):
            dialog.provider_var.set(provider)
            boxes[1].kw["command"]()
            self.assertEqual(self.env.help_ui._window.topic_id, topic, provider)

    def test_add_account_window_help_notices_a_microsoft_address(self):
        dialog = self.setup_mod.AccountDialog(self.root, None)
        dialog.provider_var.set("other")
        dialog.email_var.set("someone@outlook.com")
        self.assertEqual(dialog._help_topic(), "microsoft")

    def test_add_account_window_turns_the_password_off_for_microsoft_addresses(self):
        dialog = self.setup_mod.AccountDialog(self.root, None)
        dialog.password_var.set("typed-before-the-address")
        dialog.email_var.set("me@hotmail.com")                       # the trace on the field reacts
        self.assertEqual(dialog.pw_entry.kw["state"], "disabled")
        self.assertEqual(dialog.password_var.get(), "")              # nothing useless kept
        self.assertIn("Microsoft blocks passwords", dialog.pw_hint.kw["text"])
        dialog.email_var.set("me@gmail.com")
        dialog.provider_var.set("gmail")
        self.assertEqual(dialog.pw_entry.kw["state"], "normal")
        self.assertNotIn("Microsoft", dialog.pw_hint.kw["text"])

    def test_import_window_help_points_microsoft_accounts_at_their_page(self):
        dialog = self.run_mod.ImportDialog(self.root, self.accounts)
        self.assertEqual(len(self.question_marks(dialog)), 1)
        dialog.var.set("hot")
        self.assertEqual(dialog._help_topic(), "microsoft")
        dialog.var.set("gm")
        self.assertEqual(dialog._help_topic(), "pipeline")

    def test_help_opened_from_a_dialog_belongs_to_that_dialog(self):
        """A pop-up form locks the rest of the program, so the Help window opened from it must belong to it."""
        dialog = self.setup_mod.AccountDialog(self.root, None)
        self.question_marks(dialog)[0].kw["command"]()
        self.assertIs(self.env.help_ui._window.owner, dialog)


if __name__ == "__main__":
    unittest.main()
