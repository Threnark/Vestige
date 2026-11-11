"""Tests of the new interface behaviour (pipeline tick boxes, depth, the pinned progress bar, the
resizable log, the four results tables), built against the stand-in for Tk (tests/fake_tk.py).

As with the other interface tests, these catch wiring mistakes: they can't show what anything looks
like on a real screen."""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import db
import pipeline
import run_options
from models import MODE_CLOUD, MODE_LOCAL, Account, ProviderSettings
from tests import fake_tk
from tests.test_closures_subs import FakeAI, build_scenario


class GuiCase(unittest.TestCase):
    def setUp(self):
        manager = fake_tk.installed(permissive=True)
        self.env = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        for name in ("gui_setup", "gui_run", "gui_results"):
            sys.modules.pop(name, None)
        self.run_mod = importlib.import_module("gui_run")
        self.results_mod = importlib.import_module("gui_results")
        self.accounts = [Account(name="gm", provider="gmail", email="me@gmail.com", password="x")]
        self.local_settings = ProviderSettings(mode=MODE_LOCAL, base_url="http://localhost:11434/v1")
        session = mock.MagicMock()
        session.write_mode = False
        session.accounts = self.accounts

        class App(fake_tk.FakeWidget):
            busy = False
        self.root = fake_tk.FakeToplevel(None)
        self.app = App(self.root)
        self.app.session = session
        self.app.run_job = mock.Mock()
        self.app.request_stop = mock.Mock()


class RunPageChoices(GuiCase):
    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.depth_control = types.SimpleNamespace(get=lambda: self.depth, set=lambda v: None,
                                                        configure=lambda **k: None)
        self.depth = "Balanced"

    def test_every_pipeline_is_ticked_at_first(self):
        self.assertEqual(self.page.chosen_pipelines(), run_options.PIPELINE_ORDER)
        self.assertIn("Sign-ups, Email changes, Closed accounts, Subscriptions", self.page.plan_var.get())
        self.assertNotIn("Skipped", self.page.plan_var.get())

    def test_unticking_says_what_will_be_skipped(self):
        self.page.pipeline_vars["changes"].set(False)
        self.page.pipeline_vars["closed"].set(False)
        self.page._plan_changed()
        self.assertEqual(self.page.chosen_pipelines(), ("signups", "subscriptions"))
        text = self.page.plan_var.get()
        self.assertIn("The AI will be used for: Sign-ups, Subscriptions.", text)
        self.assertIn("Skipped: Email changes, Closed accounts", text)

    def test_the_depth_note_follows_the_choice(self):
        for name in run_options.DEPTH_NAMES:
            self.page._depth_changed(name)
            self.assertEqual(self.page.depth_var.get(), run_options.get_depth(name).blurb)

    def test_nothing_ticked_runs_nothing(self):
        for var in self.page.pipeline_vars.values():
            var.set(False)
        self.page._plan_changed()
        self.assertIn("Nothing is ticked", self.page.plan_var.get())
        with mock.patch.object(self.run_mod.dialogs, "showinfo") as info:
            self.page._do_all()
        info.assert_called_once()
        self.app.run_job.assert_not_called()

    def test_run_selected_passes_the_choices_to_the_pipeline(self):
        self.page.pipeline_vars["changes"].set(False)
        self.depth = "Quick"
        self.page._provider = lambda: (object(), self.local_settings)
        self.page._announce = lambda settings: None
        self.page._do_all()
        job = self.app.run_job.call_args[0][0]
        with mock.patch.object(self.run_mod.pipeline, "run_full") as run_full:
            job(None)
        kwargs = run_full.call_args.kwargs
        self.assertEqual(kwargs["pipelines"], ("signups", "closed", "subscriptions"))
        self.assertEqual(kwargs["depth"], "Quick")

    def test_each_ai_button_uses_the_chosen_depth(self):
        self.depth = "Thorough"
        self.page._provider = lambda: (object(), self.local_settings)
        self.page._announce = lambda settings: None
        for button, stage in ((self.page._do_classify, "stage_classify"), (self.page._do_changes, "stage_detect_changes"),
                              (self.page._do_closed, "stage_check_closed"),
                              (self.page._do_subscriptions, "stage_find_subscriptions")):
            self.app.run_job.reset_mock()
            button()
            job = self.app.run_job.call_args[0][0]
            with mock.patch.object(self.run_mod.pipeline, stage) as called:
                job(None)
            self.assertEqual(called.call_args[0][-1], "Thorough", stage)

    def test_fetching_bodies_follows_the_ticks(self):
        self.page.pipeline_vars["subscriptions"].set(False)
        self.page._do_bodies()
        job = self.app.run_job.call_args[0][0]
        with mock.patch.object(self.run_mod.pipeline, "stage_fetch_bodies") as bodies:
            job(None)
        self.assertEqual(bodies.call_args[0][3], ("signups", "changes", "closed"))

    def test_every_step_has_a_button(self):
        names = {b.kw.get("text") for b in self.page.stage_buttons}
        self.assertEqual(len(names), 7)
        for label in ("6. AI: closed accounts", "7. AI: subscriptions"):
            self.assertIn(label, names)

    def test_importing_a_mail_file_is_not_a_step_on_this_page(self):
        names = {b.kw.get("text") for b in self.page.stage_buttons + self.page.run_controls}
        self.assertFalse([n for n in names if n and "Import mail file" in n])

    def test_run_and_stop_sit_together_with_the_progress_bar(self):
        self.assertIn(self.page.run_button, self.page.run_controls)
        self.assertEqual(self.page.stop_button.kw["state"], "disabled")
        self.assertEqual(self.page.progress_title_var.get(), "Ready. Nothing is running.")

    def test_everything_is_locked_while_a_job_runs_except_stop(self):
        self.page.set_busy_state(True)
        for widget in self.page.stage_buttons + self.page.run_controls:
            self.assertEqual(widget.kw["state"], "disabled")
        self.assertEqual(self.page.stop_button.kw["state"], "normal")
        self.page.set_busy_state(False)
        for widget in self.page.stage_buttons + self.page.run_controls:
            self.assertEqual(widget.kw["state"], "normal")
        self.assertEqual(self.page.stop_button.kw["state"], "disabled")

    def test_the_status_shows_every_counter_the_database_reports(self):
        self.app.session.conn = db.open_database(None)
        self.app.results_page = mock.Mock()
        self.page.refresh_status()
        self.assertEqual(len(self.page.status_counters), 10)
        self.assertLessEqual(set(self.page.status_counters), set(db.counts(self.app.session.conn)))
        shown = {label.kw["text"] for label, _ in self.page.status_counters.values()}
        for wanted in ("Emails scanned: 0", "Closure notices spotted: 0", "Closed accounts found: 0",
                       "Billing emails spotted: 0", "Subscriptions found: 0"):
            self.assertIn(wanted, shown)

    def test_the_status_counters_follow_the_numbers(self):
        counts = {key: n for n, key in enumerate(self.page.status_counters, start=1)}
        self.app.results_page = mock.Mock()
        with mock.patch.object(self.run_mod.db, "counts", return_value=counts):
            self.page.refresh_status()
        label, text = self.page.status_counters["closure_notices"]
        self.assertEqual(label.kw["text"], f"{text}: {counts['closure_notices']}")
        self.assertEqual(self.page.status_var.get(), "")

    def test_the_status_is_left_alone_while_a_job_runs(self):
        self.app.busy = True
        before = {k: label.kw["text"] for k, (label, _) in self.page.status_counters.items()}
        self.page.refresh_status()
        self.assertIn("A job is running", self.page.status_var.get())
        self.assertEqual({k: label.kw["text"] for k, (label, _) in self.page.status_counters.items()}, before)

    def _some_mail_is_loaded(self) -> None:
        conn = db.open_database(None)
        db.insert_email(conn, {"account_name": "gm", "folder": "INBOX", "uid": "1", "message_id": "<1>",
                               "sender_name": "A", "sender_email": "a@x.com", "sender_domain": "x.com",
                               "subject": "Hi", "date": "2026-01-01", "email_address_used": "me@gmail.com"})
        self.app.session.conn = conn

    def test_a_full_run_skips_the_fetch_when_no_account_has_a_password_and_mail_is_loaded(self):
        self.accounts[0].password = ""
        self._some_mail_is_loaded()
        with mock.patch.object(self.run_mod.dialogs, "showinfo") as info, \
                mock.patch.object(self.page, "_provider", return_value=(object(), self.local_settings)), \
                mock.patch.object(self.run_mod.pipeline, "run_full") as run_full, \
                mock.patch.object(self.run_mod.RunPage, "_announce"):
            self.page._do_all()
            job = self.app.run_job.call_args[0][0]
            job(None)
        info.assert_not_called()
        self.assertFalse(run_full.call_args.kwargs["fetch"])

    def test_a_full_run_with_no_password_and_no_mail_explains_what_to_do(self):
        self.accounts[0].password = ""
        self.app.session.conn = db.open_database(None)
        with mock.patch.object(self.run_mod.dialogs, "showinfo") as info, \
                mock.patch.object(self.page, "_provider", return_value=(object(), self.local_settings)):
            self.page._do_all()
        info.assert_called_once()
        self.assertIn("Setup tab", info.call_args[0][1])
        self.app.run_job.assert_not_called()

    def test_unticking_the_fetch_stops_a_full_run_fetching_even_with_a_password(self):
        self.page.fetch_var.set(False)
        self._some_mail_is_loaded()
        with mock.patch.object(self.page, "_provider", return_value=(object(), self.local_settings)), \
                mock.patch.object(self.run_mod.pipeline, "run_full") as run_full, \
                mock.patch.object(self.run_mod.RunPage, "_announce"):
            self.page._do_all()
            self.app.run_job.call_args[0][0](None)
        self.assertFalse(run_full.call_args.kwargs["fetch"])


class RemoteAiConfirmation(GuiCase):
    """The first AI run of a session says where email text is going, unless that is this computer."""

    CLOUD = ProviderSettings(mode=MODE_CLOUD, api_key="k")
    LAN = ProviderSettings(mode=MODE_LOCAL, base_url="http://192.168.1.20:11434/v1")

    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.depth_control = types.SimpleNamespace(get=lambda: "Balanced", set=lambda v: None,
                                                        configure=lambda **kw: None)

    def ask(self, settings, answer="Run", limit=None) -> mock.Mock:
        with mock.patch.object(self.run_mod.dialogs, "ask_choice", return_value=answer) as asked:
            self.result = self.page._confirm_remote_ai(settings, 6, limit)
        return asked

    def test_cloud_ai_asks_and_says_where_the_text_goes_and_how_to_limit_the_cost(self):
        asked = self.ask(self.CLOUD, limit=5)
        self.assertTrue(self.result)
        message = asked.call_args[0][1]
        self.assertIn("api.anthropic.com", message)
        self.assertIn("Depth: Balanced", message)
        self.assertIn("Senders per request: 6", message)
        self.assertIn("Test-run limit: 5 senders", message)
        self.assertIn("charges for every request", message)
        self.assertEqual(asked.call_args[0][2], ["Run", "Cancel"])

    def test_no_limit_is_stated_plainly(self):
        self.assertIn("Test-run limit: none", self.ask(self.CLOUD).call_args[0][1])

    def test_agreeing_once_covers_the_rest_of_the_session(self):
        self.ask(self.CLOUD)
        self.assertFalse(self.ask(self.CLOUD).called)
        self.assertTrue(self.result)

    def test_cancelling_stops_the_run_and_asks_again_next_time(self):
        self.ask(self.CLOUD, answer="Cancel")
        self.assertFalse(self.result)
        self.assertTrue(self.ask(self.CLOUD, answer="Run").called)

    def test_closing_the_window_counts_as_no(self):
        self.ask(self.CLOUD, answer=None)
        self.assertFalse(self.result)

    def test_an_ai_on_this_computer_never_asks(self):
        self.assertFalse(self.ask(self.local_settings).called)
        self.assertTrue(self.result)

    def test_an_ai_elsewhere_on_your_network_asks_but_does_not_talk_about_charges(self):
        asked = self.ask(self.LAN)
        self.assertTrue(asked.called)
        self.assertNotIn("charges", asked.call_args[0][1])

    def test_a_cancelled_confirmation_starts_no_job_from_either_button(self):
        self.page._provider = lambda: (object(), self.CLOUD)
        with mock.patch.object(self.run_mod.dialogs, "ask_choice", return_value="Cancel"):
            self.page._do_all()
            self.page._do_classify()
        self.app.run_job.assert_not_called()


class FoldableSteps(GuiCase):
    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.steps_area = mock.Mock()

    def test_the_steps_start_folded_away(self):
        fresh = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.assertFalse(fresh._steps_shown)
        self.assertEqual(fresh.steps_toggle.kw["text"], "Show steps")

    def test_the_button_shows_and_hides_them(self):
        self.page._toggle_steps()
        self.page.steps_area.grid.assert_called_once_with()
        self.assertEqual(self.page.steps_toggle.kw["text"], "Hide steps")
        self.page._toggle_steps()
        self.page.steps_area.grid_remove.assert_called_once_with()
        self.assertEqual(self.page.steps_toggle.kw["text"], "Show steps")

    def test_the_step_buttons_are_still_locked_while_a_job_runs_even_when_folded(self):
        self.page.set_busy_state(True)
        self.assertTrue(all(b.kw["state"] == "disabled" for b in self.page.stage_buttons))


class SetupNextStep(GuiCase):
    """The line at the top of Setup that says what to do next, from what is set up so far."""

    GOOD_AI = ProviderSettings(mode=MODE_CLOUD, api_key="k")

    def setUp(self):
        super().setUp()
        sys.modules.pop("gui_app", None)
        self.setup_mod = importlib.import_module("gui_setup")
        self.app.busy = False
        self.page = self.setup_mod.SetupPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.get_provider_settings = lambda: self.ai
        self.ai = self.GOOD_AI
        self.app.session.conn = db.open_database(None)

    def step(self):
        self.page.refresh_next_step()
        return self.page.next_step_var.get(), self.page.next_step_label.kw["text_color"]

    def test_with_no_account_the_first_step_is_to_add_one(self):
        self.accounts.clear()
        text, _ = self.step()
        self.assertIn("add an email account", text)

    def test_with_an_account_but_no_ai_it_says_what_the_ai_still_needs(self):
        self.ai = ProviderSettings(mode=MODE_CLOUD, api_key="")
        text, _ = self.step()
        self.assertIn("set up the AI", text)
        self.assertIn("API key", text)

    def test_an_account_with_a_password_and_an_ai_is_ready_to_fetch(self):
        text, colour = self.step()
        self.assertIn("Ready", text)
        self.assertIn("fetch your mail", text)
        self.assertEqual(colour, self.setup_mod.theme.ROW_COLORS["good"])

    def test_a_file_only_account_with_no_mail_is_told_to_import(self):
        self.accounts[0].password = ""
        text, colour = self.step()
        self.assertIn("Import mail file", text)
        self.assertEqual(colour, self.setup_mod.theme.TEXT)

    def test_loaded_mail_is_reported_and_counts_as_ready(self):
        self.accounts[0].password = ""
        db.insert_email(self.app.session.conn, {"account_name": "gm", "folder": "INBOX", "uid": "1",
                                                "message_id": "<1>", "sender_email": "a@x.com",
                                                "sender_domain": "x.com", "subject": "Hi", "date": "2026-01-01",
                                                "email_address_used": "me@gmail.com"})
        text, _ = self.step()
        self.assertIn("Ready: 1 emails loaded", text)

    def test_the_line_is_left_alone_while_a_job_runs(self):
        self.step()
        before = self.page.next_step_var.get()
        self.app.busy = True
        self.accounts.clear()
        self.page.refresh_next_step()
        self.assertEqual(self.page.next_step_var.get(), before)

    def test_changing_the_accounts_or_the_ai_fields_refreshes_it(self):
        self.page.refresh_accounts()
        self.assertTrue(self.page.next_step_var.get())
        self.page.next_step_var.set("stale")
        self.page._update_privacy()
        self.assertNotEqual(self.page.next_step_var.get(), "stale")


class ImportFromSetup(GuiCase):
    """The Setup page's "Import mail file..." button: pick an account and files, then the import runs as a job
    and the app switches to Run Pipeline, where the progress bar and the log are."""

    def setUp(self):
        super().setUp()
        sys.modules.pop("gui_app", None)
        self.setup_mod = importlib.import_module("gui_setup")
        self.app.show_run_page = mock.Mock()
        self.page = self.setup_mod.SetupPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.tree = types.SimpleNamespace(selection=lambda: ("gm",))
        self.dialog = mock.patch.object(self.setup_mod, "ImportDialog")
        self.dialog_class = self.dialog.start()
        self.addCleanup(self.dialog.stop)
        self.dialog_class.return_value.result = ("gm", ["/mail/export.mbox"])

    def test_the_chosen_files_are_imported_for_the_chosen_account(self):
        self.page._import_mail()
        self.app.run_job.assert_called_once()
        conn, account = self.app.session.conn, self.accounts[0]
        with mock.patch.object(self.setup_mod.pipeline, "stage_import_mail") as imported:
            self.app.run_job.call_args[0][0]("the job control")
        imported.assert_called_once_with(conn, account, ["/mail/export.mbox"], "the job control")

    def test_the_app_shows_the_run_page_so_the_progress_is_visible(self):
        self.page._import_mail()
        self.app.show_run_page.assert_called_once_with()

    def test_the_highlighted_account_is_offered_first(self):
        self.page._import_mail()
        self.assertEqual(self.dialog_class.call_args[0][1:], (self.accounts, "gm"))

    def test_with_nothing_highlighted_no_account_is_preferred(self):
        self.page.tree = types.SimpleNamespace(selection=lambda: ())
        self.page._import_mail()
        self.assertIsNone(self.dialog_class.call_args[0][2])

    def test_cancelling_the_window_starts_nothing(self):
        self.dialog_class.return_value.result = None
        self.page._import_mail()
        self.app.run_job.assert_not_called()
        self.app.show_run_page.assert_not_called()

    def test_without_an_account_it_says_to_add_one_first(self):
        self.accounts.clear()
        with mock.patch.object(self.setup_mod.dialogs, "showinfo") as info:
            self.page._import_mail()
        info.assert_called_once()
        self.dialog_class.assert_not_called()
        self.app.run_job.assert_not_called()

    def test_while_another_job_runs_it_waits(self):
        self.app.busy = True
        with mock.patch.object(self.setup_mod.dialogs, "showwarning") as warned:
            self.page._import_mail()
        warned.assert_called_once()
        self.dialog_class.assert_not_called()
        self.app.run_job.assert_not_called()

    def test_the_app_can_switch_to_the_run_page(self):
        gui_app = importlib.import_module("gui_app")
        app = types.SimpleNamespace(tabs=mock.Mock(), show_page=mock.Mock())
        gui_app.App.show_run_page(app)
        app.tabs.set.assert_called_once_with("Run Pipeline")
        app.show_page.assert_called_once_with("Run Pipeline")


class CloseWarning(GuiCase):
    """Closing the app warns about results that were not exported, whichever of the four tables they are in."""

    NONE = {"confirmed_signups": 0, "email_changes_found": 0, "closed_accounts_found": 0, "subscriptions_found": 0}

    def setUp(self):
        super().setUp()
        sys.modules.pop("gui_app", None)
        self.gui_app = importlib.import_module("gui_app")
        self.app = types.SimpleNamespace(busy=False, session=mock.Mock(write_mode=False), destroy=mock.Mock(),
                                         _exported_counts=(0, 0, 0, 0))
        self.app._result_counts = lambda: self.gui_app.App._result_counts(self.app)

    def close(self, counts: dict) -> mock.Mock:
        with mock.patch.object(self.gui_app.db, "counts", return_value={**self.NONE, **counts}), \
                mock.patch.object(self.gui_app.dialogs, "askyesno", return_value=False) as ask:
            self.gui_app.App._on_close(self.app)
        return ask

    def test_results_in_any_of_the_four_tables_are_warned_about(self):
        for key in self.NONE:
            with self.subTest(key):
                self.assertTrue(self.close({key: 1}).called)
                self.app.destroy.assert_not_called()          # answered "no": the app stays open

    def test_no_results_means_no_warning(self):
        self.assertFalse(self.close({}).called)
        self.app.destroy.assert_called_once()

    def test_exported_results_do_not_warn_again(self):
        with mock.patch.object(self.gui_app.db, "counts", return_value={**self.NONE, "subscriptions_found": 3}):
            self.gui_app.App.mark_exported(self.app)
        self.assertFalse(self.close({"subscriptions_found": 3}).called)

    def test_new_results_after_an_export_warn_again(self):
        with mock.patch.object(self.gui_app.db, "counts", return_value={**self.NONE, "closed_accounts_found": 1}):
            self.gui_app.App.mark_exported(self.app)
        self.assertTrue(self.close({"closed_accounts_found": 2}).called)


class ResizableLog(GuiCase):
    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.scale = max(0.5, self.page.log_grip and self.env.theme.F.scale)

    @staticmethod
    def event(y):
        return types.SimpleNamespace(y_root=y)

    def test_dragging_the_bar_resizes_the_log(self):
        start = self.page._log_height
        self.page._grip_press(self.event(500))
        self.page._grip_drag(self.event(560))
        self.assertEqual(self.page._log_height, int(start + 60 / self.scale))
        self.assertEqual(self.page.log_text.kw["height"], self.page._log_height)
        self.page._grip_drag(self.event(470))                       # back past the start: shorter
        self.assertLess(self.page._log_height, start)

    def test_the_log_stays_between_its_limits(self):
        self.page._grip_press(self.event(500))
        self.page._grip_drag(self.event(500 + 100000))
        self.assertEqual(self.page._log_height, self.run_mod.LOG_MAX_HEIGHT)
        self.page._grip_drag(self.event(500 - 100000))
        self.assertEqual(self.page._log_height, self.run_mod.LOG_MIN_HEIGHT)

    def test_moving_without_pressing_does_nothing(self):
        start = self.page._log_height
        self.page._grip_drag(self.event(900))
        self.assertEqual(self.page._log_height, start)
        self.page._grip_press(self.event(100))
        self.page._grip_release(self.event(100))
        self.page._grip_drag(self.event(900))                       # released: no longer dragging
        self.assertEqual(self.page._log_height, start)

    def test_double_click_puts_it_back(self):
        self.page.set_log_height(400)
        self.page.log_grip.bindings["<Double-Button-1>"](None)
        self.assertEqual(self.page._log_height, self.run_mod.LOG_START_HEIGHT)

    def test_the_bar_is_wired_to_the_mouse(self):
        self.assertGreaterEqual(set(self.page.log_grip.bindings),
                                {"<ButtonPress-1>", "<B1-Motion>", "<ButtonRelease-1>", "<Double-Button-1>"})
        self.assertGreaterEqual(set(self.page.log_grip.bindings), {"<Enter>", "<Leave>"})      # hover highlight

    def test_the_mouse_wheel_over_the_log_does_not_also_scroll_the_page(self):
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            handler = self.page.log_text.bindings[sequence]
            self.assertEqual(handler(types.SimpleNamespace(delta=-120, num=None)), "break")
        self.assertEqual(self.page.log_text.bindings["<Button-5>"](types.SimpleNamespace(num=5)), "break")


class ResultsFourViews(GuiCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "mail.mbox")
        build_scenario(path)
        acct = Account(name="gmail", provider="gmail", email="me@gmail.com")
        conn = db.open_database(None)
        self.addCleanup(conn.close)
        pipeline.stage_import_mail(conn, acct, path)
        pipeline.run_full(conn, [acct], FakeAI(), fetch=False)
        self.app.session.conn = conn
        self.page = self.results_mod.ResultsPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.detail_box = mock.Mock()
        self.trees = {}
        for name in ("signup_tree", "change_tree", "closed_tree", "sub_tree"):
            tree = mock.MagicMock()
            tree.get_children.return_value = []
            setattr(self.page, name, tree)
            self.trees[name] = tree
        self.page.refresh()

    @staticmethod
    def inserted(tree):
        return [(c.kwargs["values"], c.kwargs.get("tags", ())) for c in tree.insert.call_args_list]

    def column(self, table: str, export_column: str) -> int:
        """Where a column sits in a table's rows."""
        return [c for c, _, _ in getattr(self.results_mod, table)].index(export_column)

    def test_four_views_are_offered(self):
        self.assertEqual(self.results_mod.VIEWS, ["Sign-ups", "Possible email changes", "Closed accounts", "Subscriptions"])
        self.assertEqual(set(self.page.views), set(self.results_mod.VIEWS))

    def test_switching_shows_only_the_chosen_view(self):
        shown = {name: mock.Mock() for name in self.results_mod.VIEWS}
        self.page.views = shown
        self.page._show_view("Closed accounts")
        for name, view in shown.items():
            self.assertEqual(view.grid.called, name == "Closed accounts", name)
            self.assertEqual(view.grid_remove.called, name != "Closed accounts", name)

    def test_signups_carry_the_account_check_and_the_quiet_hint_in_separate_columns(self):
        rows = {v[0]: (v, t) for v, t in self.inserted(self.trees["signup_tree"])}
        check, hint = self.column("SIGNUP_TABLE", "Account Check"), self.column("SIGNUP_TABLE", "Activity Hint")
        old_service, tags = rows["OldService"]
        self.assertTrue(old_service[check].startswith("Likely closed"))
        self.assertEqual(old_service[hint], "")
        self.assertEqual(tags, ("flag",))                         # highlighted, but the words carry the meaning
        forum, _ = rows["OldForum"]
        self.assertEqual(forum[check], "")
        self.assertEqual(forum[hint], "No mail since 2021-04-01")

    def test_closed_accounts_table(self):
        rows = {v[0]: v for v, _ in self.inserted(self.trees["closed_tree"])}
        self.assertEqual(set(rows), {"OldService", "GameSite"})
        check, later = self.column("CLOSED_TABLE", "Check Result"), self.column("CLOSED_TABLE", "Later Mail")
        self.assertEqual(rows["OldService"][check], "Likely closed")
        self.assertEqual(rows["OldService"][later], "None found")
        self.assertEqual(rows["GameSite"][check], "Possibly closed")
        self.assertIn("1 email", rows["GameSite"][later])
        self.assertEqual(rows["GameSite"][self.column("CLOSED_TABLE", "Email Address Used")], "me@gmail.com")

    def test_subscriptions_table_and_total(self):
        rows = {v[0]: (v, t) for v, t in self.inserted(self.trees["sub_tree"])}
        status = self.column("SUB_TABLE", "Status")
        self.assertEqual((rows["StreamFlix"][0][status], rows["StreamFlix"][1]), ("Active", ("good",)))
        self.assertEqual((rows["CloudBox"][0][status], rows["CloudBox"][1]), ("Possibly stopped", ("warn",)))
        self.assertEqual((rows["GymPass"][0][status], rows["GymPass"][1]), ("Cancelled", ("dim",)))
        self.assertEqual(rows["StreamFlix"][0][self.column("SUB_TABLE", "Last Charge Amount")], "NZD 19.99")
        total = self.page.totals_var.get()
        self.assertIn("Active subscriptions (2)", total)
        self.assertIn("NZD 21.28 / month", total)
        self.assertIn("1 possibly stopped, not counted", total)

    def test_every_table_column_is_a_real_export_column_so_nothing_can_go_missing(self):
        export = self.results_mod.export
        for table, columns in (("SIGNUP_TABLE", export.SIGNUP_COLUMNS), ("CHANGE_TABLE", export.CHANGE_COLUMNS),
                               ("CLOSED_TABLE", export.CLOSED_COLUMNS), ("SUB_TABLE", export.SUBSCRIPTION_COLUMNS)):
            with self.subTest(table):
                shown = [c for c, _, _ in getattr(self.results_mod, table)]
                self.assertLessEqual(set(shown), set(columns))
                self.assertEqual(len(shown), len(set(shown)))
                self.assertEqual(shown[0], "Service Name")

    def test_the_closed_and_subscription_tables_show_the_address_early(self):
        for table in ("CLOSED_TABLE", "SUB_TABLE"):
            self.assertEqual(getattr(self.results_mod, table)[1][0], "Email Address Used")

    def test_clicking_a_row_shows_what_the_table_leaves_out(self):
        tree = mock.Mock()
        tree.selection.return_value = ("0",)
        self.page._show_detail(tree, self.page._closed, self.results_mod.CLOSED_TABLE,
                               self.results_mod.export.CLOSED_COLUMNS)
        text = self.page.detail_box.insert.call_args[0][1]
        self.assertTrue(text.startswith(self.page._closed[0]["Service Name"]))
        for left_out in ("Closed By:", "Originally Signed Up With:", "What Was Found:", "Notice Subject:"):
            self.assertIn(left_out, text)
        self.assertNotIn("Check Result:", text)                  # that one is already a column

    def test_each_address_of_a_subscription_gets_its_own_line(self):
        row = {c: "" for c in self.results_mod.export.SUBSCRIPTION_COLUMNS}
        row.update({"Service Name": "CloudBox", "Charges By Address":
                    "me+work@gmail.com: 3 charges, last 2024-03-14; me@gmail.com: 3 charges, last 2024-06-14"})
        tree = mock.Mock()
        tree.selection.return_value = ("0",)
        self.page._show_detail(tree, [row], self.results_mod.SUB_TABLE, self.results_mod.export.SUBSCRIPTION_COLUMNS)
        text = self.page.detail_box.insert.call_args[0][1]
        self.assertIn("Charges By Address: \n    me+work@gmail.com: 3 charges, last 2024-03-14\n"
                      "    me@gmail.com: 3 charges, last 2024-06-14", text)

    def test_with_nothing_selected_the_panel_is_left_alone(self):
        tree = mock.Mock()
        tree.selection.return_value = ()
        self.page.detail_box.insert.reset_mock()
        self.page._show_detail(tree, self.page._closed, self.results_mod.CLOSED_TABLE,
                               self.results_mod.export.CLOSED_COLUMNS)
        self.page.detail_box.insert.assert_not_called()

    def test_switching_table_or_refreshing_puts_the_hint_back(self):
        self.page.detail_box = mock.Mock()
        self.page._show_view("Closed accounts")
        self.assertEqual(self.page.detail_box.insert.call_args[0][1], self.results_mod.DETAIL_HINT)
        self.page.detail_box.reset_mock()
        self.page.refresh()
        self.assertEqual(self.page.detail_box.insert.call_args[0][1], self.results_mod.DETAIL_HINT)

    def test_the_summary_counts_every_table(self):
        text = self.page.count_var.get()
        for wanted in ("sign-ups", "email changes", "closed", "subscriptions"):
            self.assertIn(wanted, text)


if __name__ == "__main__":
    unittest.main()
