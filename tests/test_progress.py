"""Tests for the progress system behind the progress bar."""
import os
import shutil
import tempfile
import unittest

import db
import jobcontrol
import pipeline
from jobcontrol import JobControl, Progress
from models import Account
from tests.test_pipeline import FakeProvider, build_mbox


def tracker(**kw):
    events = []
    return Progress(listener=events.append, min_interval=0, **kw), events


class ProgressMath(unittest.TestCase):
    def test_weighted_stages(self):
        p, _ = tracker()
        p.plan([("a", "Stage A", 1), ("b", "Stage B", 3)])
        p.begin("a")
        p.update(1, 2)
        self.assertAlmostEqual(p.overall, 0.125)           # half of a quarter
        p.begin("b")
        self.assertAlmostEqual(p.overall, 0.25)            # A counts as complete
        p.update(1, 2)
        self.assertAlmostEqual(p.overall, 0.625)
        p.finish()
        self.assertEqual(p.overall, 1.0)

    def test_never_goes_backwards_when_the_total_grows(self):
        p, _ = tracker()
        p.plan([("a", "A", 1)])
        p.begin("a")
        p.update(5, 10)
        high = p.overall
        p.update(5, 20)                                      # a second folder revealed more work
        self.assertGreaterEqual(p.overall, high)
        p.update(20, 20)
        self.assertEqual(round(p.overall, 3), 1.0)

    def test_slices_split_a_stage_between_accounts(self):
        p, _ = tracker()
        p.plan([("fetch", "Fetch", 1)])
        p.begin("fetch")
        p.slice(0, 2)
        p.update(1, 2)
        self.assertAlmostEqual(p.overall, 0.25)
        p.slice(1, 2)
        self.assertAlmostEqual(p.overall, 0.5)
        p.update(2, 2)
        self.assertAlmostEqual(p.overall, 1.0)

    def test_indeterminate_until_a_total_is_known(self):
        p, events = tracker()
        p.plan([("import", "Import", 1)])
        p.begin("import")
        p.indeterminate("Indexing...")
        self.assertTrue(events[-1]["indeterminate"])
        p.update(0, 1000, "0 of 1,000")
        self.assertFalse(events[-1]["indeterminate"])
        self.assertEqual(events[-1]["detail"], "0 of 1,000")

    def test_zero_total_counts_as_done(self):
        p, _ = tracker()
        p.plan([("a", "A", 1)])
        p.begin("a")
        p.update(0, 0)
        self.assertEqual(round(p.overall, 3), 1.0)

    def test_unplanned_single_stage_works(self):
        p, events = tracker()
        p.begin("prefilter", "Pre-filtering", 0.05)
        p.update(1, 4)
        self.assertAlmostEqual(p.overall, 0.25)
        self.assertEqual(events[-1]["label"], "Pre-filtering")
        self.assertEqual((events[-1]["step"], events[-1]["steps"]), (1, 1))

    def test_updates_are_throttled_but_milestones_are_not(self):
        events = []
        p = Progress(listener=events.append, min_interval=60)
        p.plan([("a", "A", 1)])
        p.begin("a")
        for i in range(1, 50):
            p.update(i, 100)                                  # all within the throttle window
        sent_during = len(events)
        p.update(100, 100)                                    # completion is always delivered
        p.finish()
        self.assertLess(sent_during, 6)
        self.assertTrue(events[-1]["finished"])
        self.assertEqual(events[-1]["overall"], 1.0)

    def test_helpers_accept_no_control(self):
        jobcontrol.begin(None, "x", "X")
        jobcontrol.report(None, 1, 2)
        jobcontrol.indeterminate(None)
        jobcontrol.slice_(None, 0, 1)


class PipelineReportsProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mbox = os.path.join(self.tmp, "mail.mbox")
        build_mbox(self.mbox)
        self.acct = Account(name="gmail", provider="gmail", email="me@gmail.com")
        self.work = Account(name="work", provider="other", email="work@acme.com")
        self.conn = db.open_database(None)

    def control(self):
        self.events = []
        return JobControl(listener=self.events.append)

    def test_mbox_import_shows_a_marquee_then_a_real_count(self):
        control = self.control()
        control.progress.min_interval = 0
        pipeline.stage_import_mbox(self.conn, self.acct, self.mbox, control)
        flags = [e["indeterminate"] for e in self.events]
        self.assertTrue(flags[0] or any(flags))                  # started in marquee mode ...
        self.assertFalse(self.events[-1]["indeterminate"])       # ... and ended with a real figure
        self.assertEqual(self.events[-1]["label"], "Importing mail from file")   # the stage now reads .pst/.eml too
        self.assertIn("9 of 9", self.events[-1]["detail"])
        self.assertEqual(round(self.events[-1]["overall"], 3), 1.0)

    def test_full_run_walks_through_every_step_in_order(self):
        control = self.control()
        control.progress.min_interval = 0
        pipeline.stage_import_mbox(self.conn, self.acct, self.mbox)
        pipeline.run_full(self.conn, [self.acct, self.work], FakeProvider(), fetch=False, batch_size=6, control=control)
        control.progress.finish()
        labels = []
        for e in self.events:
            if e["label"] and (not labels or labels[-1] != e["label"]):
                labels.append(e["label"])
        self.assertEqual(labels, ["Pre-filtering (free, no AI)", "Fetching email bodies",
                                  "AI: classifying sign-ups", "AI: finding possible email changes"])
        self.assertEqual({e["steps"] for e in self.events if e["steps"]}, {4})
        values = [e["overall"] for e in self.events]
        self.assertEqual(values, sorted(values))                  # monotonic
        self.assertEqual(values[-1], 1.0)

    def test_full_run_with_fetch_has_five_steps(self):
        control = self.control()
        control.progress.min_interval = 0
        pipeline.run_full(self.conn, [self.acct], FakeProvider(), fetch=True, control=control)   # no password: fetch skipped
        self.assertEqual(self.events[0]["steps"], 5)
        self.assertIn("Fetching email headers", {e["label"] for e in self.events})

    def test_stop_still_works_alongside_progress(self):
        control = self.control()
        pipeline.stage_import_mbox(self.conn, self.acct, self.mbox)
        pipeline.stage_prefilter(self.conn)
        control.cancel()
        with self.assertRaises(jobcontrol.Cancelled):
            pipeline.stage_classify(self.conn, FakeProvider(), 1, control=control)


if __name__ == "__main__":
    unittest.main()
