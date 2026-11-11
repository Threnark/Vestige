"""Tests for the interface fixes (batch 4 of the review):

  * the window's update loop (App._poll) keeps running when one queued update raises. It used to stop for
    good, which froze the log and progress bar and left the window "busy" for ever;
  * the Stop button is not offered during an export, and a finished job's control is never "stopped" later;
  * the "send email text to a remote AI?" agreement is remembered per DESTINATION, not once for everything.

These run the real App / page methods against the stand-in for Tk (tests/fake_tk.py). As with the other
interface tests, that catches wiring mistakes; it can't show how anything looks on a real screen.
"""
import dataclasses
import importlib
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import ai_providers as ai
import jobcontrol
from models import MODE_CLOUD, MODE_LOCAL, KIND_OPENAI, ProviderSettings
from tests import fake_tk
from tests.test_gui_new import GuiCase


class AppCase(GuiCase):
    """A bare App (its __init__, which builds the whole window, is not run) wired to mock pages."""

    def setUp(self):
        super().setUp()
        sys.modules.pop("gui_app", None)
        self.gui_app = importlib.import_module("gui_app")
        app = self.gui_app.App.__new__(self.gui_app.App)
        app.busy, app.stoppable, app.control, app._ui_problems = False, False, None, 0
        app.gui_queue = queue.Queue()
        app.after = mock.Mock()
        app.run_page, app.results_page, app.setup_page = mock.Mock(), mock.Mock(), mock.Mock()
        self.real = app

    def logged(self) -> list:
        return [call.args[0] for call in self.real.run_page.append_log.call_args_list]

    def run_to_the_end(self, job, timeout=5.0):
        """Start `job` and run the update loop until the window is no longer busy."""
        self.real.run_job(job)
        deadline = time.time() + timeout
        while self.real.busy and time.time() < deadline:
            self.real._poll()
            time.sleep(0.005)
        self.assertFalse(self.real.busy, "the job never finished")


class UpdateLoop(AppCase):
    def test_an_update_that_raises_does_not_stop_the_others(self):
        ran = []
        self.real.gui_queue.put(lambda: ran.append("first"))
        self.real.gui_queue.put(lambda: 1 / 0)
        self.real.gui_queue.put(lambda: ran.append("third"))
        self.real._poll()
        self.assertEqual(ran, ["first", "third"])

    def test_the_loop_is_always_scheduled_again(self):
        """The bug: the exception escaped _poll before it reached the line that schedules the next look."""
        self.real.gui_queue.put(lambda: 1 / 0)
        self.real._poll()
        self.real.after.assert_called_once_with(100, self.real._poll)

    def test_even_an_unusual_exception_cannot_end_the_loop(self):
        def interrupt():
            raise KeyboardInterrupt
        self.real.gui_queue.put(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.real._poll()
        self.real.after.assert_called_once_with(100, self.real._poll)

    def test_the_failure_is_reported_in_the_log_not_swallowed_silently(self):
        self.real.gui_queue.put(lambda: {}["missing"])
        self.real._poll()
        [message] = self.logged()
        self.assertIn("a window update failed", message)
        self.assertIn("KeyError", message)

    def test_an_update_that_fails_on_every_tick_cannot_fill_the_log(self):
        for _ in range(40):
            self.real.gui_queue.put(lambda: 1 / 0)
        self.real._poll()
        self.assertLessEqual(len(self.logged()), self.gui_app.App.UI_PROBLEMS_SHOWN + 1)
        self.assertIn("not listed one by one", self.logged()[-1])

    def test_a_log_that_is_itself_broken_does_not_break_the_loop(self):
        self.real.run_page.append_log.side_effect = RuntimeError("the log widget is gone")
        ran = []
        self.real.gui_queue.put(lambda: 1 / 0)
        self.real.gui_queue.put(lambda: ran.append("after"))
        self.real._poll()
        self.assertEqual(ran, ["after"])
        self.real.after.assert_called_once()

    def test_updates_run_in_the_order_they_were_queued(self):
        ran = []
        for n in range(5):
            self.real.gui_queue.put(lambda n=n: ran.append(n))
        self.real._poll()
        self.assertEqual(ran, [0, 1, 2, 3, 4])


class JobEnd(AppCase):
    def test_one_part_of_the_finishing_step_failing_does_not_skip_the_others(self):
        self.real.busy = True
        self.real.run_page.end_progress.side_effect = RuntimeError("progress bar gone")
        self.real._job_finished("finished")
        self.assertFalse(self.real.busy)
        self.real.run_page.refresh_status.assert_called_once()
        self.real.setup_page.refresh_next_step.assert_called_once()
        self.assertTrue(any("a window update failed" in line for line in self.logged()))

    def test_a_job_that_fails_still_ends_in_a_usable_window(self):
        def broken(control):
            raise ValueError("boom")
        self.run_to_the_end(broken)
        self.real.run_page.end_progress.assert_called_once_with("problem")
        self.assertTrue(any("ERROR: ValueError: boom" in line for line in self.logged()))

    def test_a_job_that_finishes_ends_with_the_control_cleared(self):
        self.run_to_the_end(lambda control: None)
        self.real.run_page.end_progress.assert_called_once_with("finished")
        self.assertIsNone(self.real.control)
        self.assertFalse(self.real.stoppable)


class StopButton(AppCase):
    def test_stop_during_a_job_cancels_it(self):
        started = threading.Event()

        def job(control):
            started.set()
            while True:
                jobcontrol.check(control)
                time.sleep(0.005)
        self.real.run_job(job)
        self.assertTrue(started.wait(2))
        self.real.request_stop()
        deadline = time.time() + 5
        while self.real.busy and time.time() < deadline:
            self.real._poll()
            time.sleep(0.005)
        self.assertFalse(self.real.busy)
        self.real.run_page.end_progress.assert_called_once_with("stopped")
        self.assertIn("Stop requested - finishing the current step...", self.logged())

    def test_stop_during_an_export_does_nothing_and_says_nothing(self):
        stale = jobcontrol.JobControl()                    # the control of some earlier, finished job
        self.real.control = stale
        self.real.set_busy(True, stoppable=False)
        self.real.request_stop()
        self.assertFalse(stale.cancelled)
        self.assertNotIn("Stop requested - finishing the current step...", self.logged())

    def test_stop_after_a_job_has_ended_does_nothing(self):
        self.run_to_the_end(lambda control: None)
        self.real.run_page.append_log.reset_mock()
        self.real.request_stop()
        self.assertEqual(self.logged(), [])

    def test_stop_with_nothing_running_does_nothing(self):
        self.real.request_stop()
        self.assertEqual(self.logged(), [])

    def test_set_busy_tells_the_run_page_whether_stop_is_possible(self):
        self.real.set_busy(True)
        self.real.run_page.set_busy_state.assert_called_with(True, True)
        self.assertTrue(self.real.stoppable)
        self.real.set_busy(True, stoppable=False)
        self.real.run_page.set_busy_state.assert_called_with(True, False)
        self.assertFalse(self.real.stoppable)
        self.real.set_busy(False)
        self.assertFalse(self.real.stoppable)
        self.real.results_page.set_busy_state.assert_called_with(False)


class StopButtonOnTheRunPage(GuiCase):
    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.stop_button = mock.Mock()

    def test_stop_is_offered_while_a_job_runs(self):
        self.page.set_busy_state(True)
        self.page.stop_button.configure.assert_called_with(state="normal")

    def test_stop_stays_greyed_out_during_an_export(self):
        self.page.set_busy_state(True, stoppable=False)
        self.page.stop_button.configure.assert_called_with(state="disabled")
        self.assertTrue(all(b.kw["state"] == "disabled" for b in self.page.stage_buttons),
                        "the step buttons are still locked while busy")

    def test_stop_is_greyed_out_when_nothing_runs(self):
        self.page.set_busy_state(False)
        self.page.stop_button.configure.assert_called_with(state="disabled")


class ExportMarksTheWindowBusyWithoutStop(GuiCase):
    def test_exporting_asks_for_a_busy_window_that_cannot_be_stopped(self):
        self.app.set_busy = mock.Mock()
        self.app.gui_queue = queue.Queue()
        page = self.results_mod.ResultsPage(fake_tk.FakeWidget(self.root), self.app)
        folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, folder, ignore_errors=True)
        page.folder_var.set(folder)

        class Immediately:                                  # run the export's worker now instead of on a thread
            def __init__(self, target=None, **kwargs):
                self.target = target

            def start(self):
                self.target()
        done = mock.Mock(files=[], folder=folder)
        with mock.patch.object(self.results_mod.threading, "Thread", Immediately), \
                mock.patch.object(self.results_mod.export, "export_files", return_value=done):
            page._export()
        self.assertEqual(self.app.set_busy.call_args_list[0], mock.call(True, stoppable=False))


class RemoteAiDestinations(GuiCase):
    """Agreeing to send email text to one AI must not count as agreeing to send it to another."""

    CLOUD = ProviderSettings(mode=MODE_CLOUD, api_key="k")
    OPENAI = ProviderSettings(mode=MODE_CLOUD, kind=KIND_OPENAI, base_url="https://api.openai.com/v1",
                              api_key="k", model="gpt")
    LAN_A = ProviderSettings(mode=MODE_LOCAL, base_url="http://192.168.1.20:11434/v1")
    LAN_B = ProviderSettings(mode=MODE_LOCAL, base_url="http://192.168.1.21:11434/v1")
    ELSEWHERE = ProviderSettings(mode=MODE_LOCAL, base_url="http://ai.example.org:8000/v1")

    def setUp(self):
        super().setUp()
        self.page = self.run_mod.RunPage(fake_tk.FakeWidget(self.root), self.app)
        self.page.depth_control = mock.Mock(get=lambda: "Balanced")

    def ask(self, settings, answer="Run"):
        with mock.patch.object(self.run_mod.dialogs, "ask_choice", return_value=answer) as asked:
            result = self.page._confirm_remote_ai(settings, 6, None)
        return result, asked.called

    def test_agreeing_for_one_cloud_provider_does_not_cover_another(self):
        self.assertEqual(self.ask(self.CLOUD), (True, True))
        self.assertEqual(self.ask(self.OPENAI), (True, True), "a different company: it must ask again")

    def test_agreeing_for_one_machine_on_the_network_does_not_cover_another(self):
        self.ask(self.LAN_A)
        self.assertEqual(self.ask(self.LAN_B), (True, True))

    def test_agreeing_for_the_cloud_does_not_cover_a_server_elsewhere_on_the_internet(self):
        """The case that prompted this: approve Anthropic, then point the 'local' address at someone's server."""
        self.ask(self.CLOUD)
        self.assertEqual(self.ask(self.ELSEWHERE), (True, True))

    def test_the_same_destination_is_not_asked_about_twice(self):
        self.ask(self.CLOUD)
        self.assertEqual(self.ask(self.CLOUD), (True, False))
        self.ask(self.LAN_A)
        self.assertEqual(self.ask(self.LAN_A), (True, False))

    def test_changing_only_the_model_does_not_ask_again(self):
        self.ask(self.CLOUD)
        self.assertEqual(self.ask(dataclasses.replace(self.CLOUD, model="another-model")), (True, False))

    def test_changing_the_port_asks_again(self):
        self.ask(self.LAN_A)
        other_port = dataclasses.replace(self.LAN_A, base_url="http://192.168.1.20:8080/v1")
        self.assertEqual(self.ask(other_port), (True, True))

    def test_each_approved_destination_stays_approved(self):
        self.ask(self.CLOUD)
        self.ask(self.OPENAI)
        self.assertEqual(self.ask(self.CLOUD), (True, False))
        self.assertEqual(self.ask(self.OPENAI), (True, False))

    def test_cancelling_is_not_remembered(self):
        self.assertEqual(self.ask(self.CLOUD, answer="Cancel"), (False, True))
        self.assertEqual(self.ask(self.CLOUD), (True, True))

    def test_a_server_on_this_computer_is_never_asked_about(self):
        here = ProviderSettings(mode=MODE_LOCAL, base_url="http://localhost:11434/v1")
        self.assertEqual(self.ask(here), (True, False))


class DestinationLabel(unittest.TestCase):
    def test_anthropic(self):
        self.assertEqual(ai.destination_of(ProviderSettings(mode=MODE_CLOUD, api_key="k")), "api.anthropic.com")

    def test_the_host_and_port_of_an_address(self):
        for address, expected in (("http://Localhost:11434/v1", "localhost:11434"),
                                  ("192.168.1.20:11434", "192.168.1.20:11434"),
                                  ("https://API.OpenAI.com/v1", "api.openai.com"),
                                  ("https://api.openai.com/v1/chat/completions", "api.openai.com"),
                                  ("http://host:notaport/v1", "host"),
                                  ("", "")):
            settings = ProviderSettings(mode=MODE_CLOUD, kind=KIND_OPENAI, base_url=address)
            self.assertEqual(ai.destination_of(settings), expected, address)

    def test_the_same_server_written_differently_is_the_same_destination(self):
        a = ProviderSettings(mode=MODE_LOCAL, base_url="http://AI.example.org:8000/v1")
        b = ProviderSettings(mode=MODE_LOCAL, base_url="ai.example.org:8000")
        self.assertEqual(ai.destination_of(a), ai.destination_of(b))


if __name__ == "__main__":
    unittest.main()
