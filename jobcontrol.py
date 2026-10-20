"""
jobcontrol.py
-------------
Two small helpers that every long-running stage shares:

1. STOP  - lets the GUI's Stop button (or Ctrl+C in the CLI) ask a stage to
   finish politely. Stages call `control.check()` between units of work
   (between IMAP batches, between AI requests). If a stop was requested,
   `check()` raises `Cancelled`, the stage unwinds, and everything already
   stored stays stored. Nothing is killed mid-write.

2. PROGRESS - a general-purpose "how far through are we?" signal that drives the
   progress bar on the Run Pipeline page.

HOW PROGRESS WORKS
  A job is a list of weighted STAGES (e.g. fetch 25%, pre-filter 5%, bodies 15%,
  classify 40%, changes 15% for the full pipeline). Inside a stage, the code
  reports `done` out of `total` whenever it knows both. The tracker combines the
  two into one overall figure between 0 and 1.

  * When a stage does NOT yet know its total (e.g. an MBOX file is still being
    indexed, or the mail server hasn't said how many messages there are), it
    reports "indeterminate" and the bar shows a moving marquee instead of a
    number. The moment a total is known it switches to a real percentage.
  * Overall progress never goes backwards, even if a later folder or batch
    reveals that there was more work than first thought. (The bar simply pauses
    until the new total catches up.)
  * Stage weights are estimates of relative time, not exact. They only exist so
    the bar moves at a roughly sensible pace; edit `pipeline.STAGES` to tune them.

Everything here is plain Python with no GUI code, so it is easy to test. A
listener (the GUI) is called with a small dict snapshot; it may be called from the
job's thread, so the GUI forwards it to its own thread safely.
"""

from __future__ import annotations

import threading
import time


class Cancelled(Exception):
    """Raised inside a stage when the user asked it to stop."""


class Progress:
    """Weighted, monotonic progress for one job. See the module docstring."""

    def __init__(self, listener=None, min_interval: float = 0.12):
        self.listener = listener            # called with a snapshot dict; may be None
        self.min_interval = min_interval    # minimum seconds between non-forced updates
        self._plan: list = []               # [(key, label, weight), ...]
        self._index = -1                    # index of the current stage in the plan
        self._fraction = 0.0                # 0..1 inside the current stage
        self._slice = (0, 1)                # (i, n): this stage is split into n equal slices
        self._indeterminate = False
        self._detail = ""
        self._label = ""
        self._floor = 0.0                   # highest overall value shown so far
        self._finished = False
        self._last_emit = 0.0
        self._last_sent = None

    # -- planning ----------------------------------------------------------
    def plan(self, stages: list) -> None:
        """Declare the stages of this job: [(key, label, weight), ...]."""
        self._plan = list(stages)
        self._index, self._fraction, self._floor = -1, 0.0, 0.0
        self._finished = False
        self._emit(force=True)

    # -- reporting (called by stages) -------------------------------------------
    def begin(self, key: str, label: str = "", weight: float = 1.0) -> None:
        """Start a stage; any earlier stage is treated as complete."""
        keys = [k for k, _, _ in self._plan]
        if key not in keys:                 # an un-planned single-stage run
            self._plan.append((key, label or key, weight))
            keys.append(key)
        self._index = keys.index(key)
        self._label = self._plan[self._index][1]
        self._fraction, self._slice, self._detail = 0.0, (0, 1), ""
        self._indeterminate = False
        self._emit(force=True)

    def slice(self, index: int, count: int) -> None:
        """Split the current stage into `count` equal parts (e.g. one per mail
        account) and say which one we are in."""
        self._slice = (max(0, index), max(1, count))
        self._fraction = self._slice[0] / self._slice[1]
        self._emit()

    def update(self, done: float, total: float, detail: str = "") -> None:
        """Report `done` of `total` for the current stage (or current slice)."""
        if total and total > 0:
            inner = min(1.0, max(0.0, done / total))
        else:
            inner = 1.0                     # nothing to do counts as complete
        i, n = self._slice
        self._fraction = (i + inner) / n
        self._indeterminate = False
        if detail:
            self._detail = detail
        self._emit(force=inner >= 1.0 and i == n - 1)

    def indeterminate(self, detail: str = "") -> None:
        """The total isn't known yet: show a marquee instead of a number."""
        self._indeterminate = True
        if detail:
            self._detail = detail
        self._emit(force=True)

    def finish(self) -> None:
        self._finished, self._indeterminate = True, False
        self._emit(force=True)

    # -- reading --------------------------------------------------------------------
    @property
    def overall(self) -> float:
        if self._finished:
            return 1.0
        total = sum(w for _, _, w in self._plan) or 1.0
        done = sum(w for _, _, w in self._plan[:self._index]) if self._index > 0 else 0.0
        current = self._plan[self._index][2] * self._fraction if self._index >= 0 else 0.0
        value = (done + current) / total
        self._floor = max(self._floor, min(1.0, value))        # never go backwards
        return self._floor

    def snapshot(self) -> dict:
        return {
            "overall": self.overall,
            "label": self._label,
            "step": self._index + 1 if self._index >= 0 else 0,
            "steps": len(self._plan),
            "detail": self._detail,
            "indeterminate": self._indeterminate and not self._finished,
            "finished": self._finished,
        }

    # -- emitting -------------------------------------------------------------------------
    def _emit(self, force: bool = False) -> None:
        if self.listener is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < self.min_interval:
            return
        snap = self.snapshot()
        key = (round(snap["overall"], 3), snap["label"], snap["detail"], snap["indeterminate"], snap["finished"])
        if not force and key == self._last_sent:
            return
        self._last_emit, self._last_sent = now, key
        self.listener(snap)


class JobControl:
    """Carries the Stop request and the Progress tracker through a job."""

    def __init__(self, listener=None):
        self._event = threading.Event()
        self.progress = Progress(listener)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


# ---------------------------------------------------------------------------
# Convenience wrappers. Passing control=None is always fine (CLI, tests), so
# stages can call these without caring whether anyone is listening.
# ---------------------------------------------------------------------------

def check(control) -> None:
    if control is not None:
        control.check()


def begin(control, key: str, label: str = "", weight: float = 1.0) -> None:
    if control is not None:
        control.progress.begin(key, label, weight)


def slice_(control, index: int, count: int) -> None:
    if control is not None:
        control.progress.slice(index, count)


def report(control, done: float, total: float, detail: str = "") -> None:
    if control is not None:
        control.progress.update(done, total, detail)


def indeterminate(control, detail: str = "") -> None:
    if control is not None:
        control.progress.indeterminate(detail)
