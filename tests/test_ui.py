"""Tests for the live-status helpers in ui.py.

They run without a real terminal, so the spinner stays inactive; the phase-line
output is checked by forcing a StringIO console. Rich assertions are guarded on
rich being importable.
"""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gentoo_updater import ui
from gentoo_updater.updater import UpdateReport, PhaseResult


class Fmt(unittest.TestCase):
    def test_minutes_seconds(self):
        self.assertEqual(ui._fmt(0), "00:00")
        self.assertEqual(ui._fmt(75), "01:15")

    def test_hours(self):
        self.assertEqual(ui._fmt(3661), "1:01:01")


class InactiveWithoutTty(unittest.TestCase):
    """In tests stdout isn't a terminal, so the spinner never starts and the
    phase calls are all no-ops that must not raise."""

    def setUp(self):
        ui.set_plain(False)

    def tearDown(self):
        with contextlib.redirect_stdout(io.StringIO()):
            ui.end_run(UpdateReport())
        ui.set_plain(False)

    def test_lifecycle_is_a_noop_and_does_not_start_a_spinner(self):
        ui.begin_run(["preflight", "sync", "apply"])
        self.assertFalse(ui._active)
        self.assertIsNone(ui._anim_thread)  # no animator thread without a tty
        ui.phase_start("preflight")
        ui.phase_done(PhaseResult("preflight", ok=True, detail="ok"))
        with ui.suspend():  # must not raise, must not start a spinner
            pass
        self.assertIsNone(ui._anim_thread)

    def test_nested_suspend_does_not_resume_early(self):
        # Force the spinner "active" so suspend() takes its real path, and check
        # the pause depth: the inner exit must not un-pause the outer.
        ui._active = True
        # suspend() writes an erase-line sequence; keep it out of test output.
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                with ui.suspend():
                    self.assertEqual(ui._paused, 1)
                    with ui.suspend():
                        self.assertEqual(ui._paused, 2)
                    self.assertEqual(ui._paused, 1,
                                     "inner exit un-paused too early")
                self.assertEqual(ui._paused, 0)
            finally:
                ui._active = False

    def test_end_run_resets_state(self):
        ui.begin_run(["preflight"])
        with contextlib.redirect_stdout(io.StringIO()):
            ui.end_run(UpdateReport())
        self.assertFalse(ui._active)
        self.assertIsNone(ui._current)


class PhaseBlock(unittest.TestCase):
    """Force the dashboard active and capture the raw-ANSI block written to
    stdout when a phase finishes. The block is the checklist, so a finished
    phase shows its mark, name, and detail there (no rich needed)."""

    def _block(self, result):
        buf = io.StringIO()
        orig_active, orig_phases, orig_status, orig_lines = (
            ui._active, ui._phases, ui._status, ui._dash_lines)
        ui._active = True
        ui._phases = [result.name]
        ui._status = {result.name: ("running", "")}
        ui._dash_lines = 0
        ui._run_start = 0.0
        try:
            with contextlib.redirect_stdout(buf):
                ui.phase_done(result)
        finally:
            (ui._active, ui._phases, ui._status, ui._dash_lines) = (
                orig_active, orig_phases, orig_status, orig_lines)
        return buf.getvalue()

    def test_ok_row(self):
        out = self._block(PhaseResult("plan", ok=True, detail="17 pending"))
        self.assertIn("✔", out)
        self.assertIn("plan", out)
        self.assertIn("17 pending", out)

    def test_fail_row(self):
        self.assertIn("✘", self._block(PhaseResult("sync", ok=False, detail="boom")))

    def test_skip_row(self):
        out = self._block(PhaseResult("snapshot", ok=True, skipped=True, detail="x"))
        self.assertIn("╌", out)


class DashboardRender(unittest.TestCase):
    """The pure block renderer and the in-place cursor accounting, both
    checkable without a real terminal."""

    def setUp(self):
        ui._run_start = 0.0
        ui._frame = "@"  # a distinctive running mark for the assertion
        ui._phases = ["preflight", "apply", "verify"]
        ui._status = {
            "preflight": ("ok", "all good"),
            "apply": ("running", ""),
            "verify": ("pending", ""),
        }

    def test_lines_cover_header_plus_every_phase(self):
        lines = ui._dashboard_lines()
        self.assertEqual(len(lines), 1 + len(ui._phases))  # header + one per phase
        body = "\n".join(lines)
        for name in ui._phases:
            self.assertIn(name, body)
        self.assertIn("gentoo-updater", lines[0])
        self.assertIn("@", body)   # the running phase shows the frame
        self.assertIn("✔", body)   # the ok phase shows its mark

    def test_repaint_tracks_line_count_and_redraws_in_place(self):
        orig_active, orig_lines = ui._active, ui._dash_lines
        ui._active = True
        ui._dash_lines = 0
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ui._refresh()                     # first paint: no cursor-up
                first = ui._dash_lines
                self.assertEqual(first, 1 + len(ui._phases))
                self.assertNotIn("\033[", buf.getvalue().split("\n")[0][:1] or "")
                ui._refresh()                     # second paint steps up first
        finally:
            ui._active, ui._dash_lines = orig_active, orig_lines
        # the second repaint must move the cursor up over the first block
        self.assertIn(f"\033[{first}A", buf.getvalue())


    def test_set_phase_progress_updates_running_row(self):
        orig_active, orig_current = ui._active, ui._current
        ui._active = True
        ui._current = "apply"
        ui._status["apply"] = ("running", "")
        try:
            ui.set_phase_progress("6/13  dev-lang/rust-1.83.0")
            state, detail = ui._status["apply"]
            self.assertEqual(state, "running")            # state preserved
            self.assertEqual(detail, "6/13  dev-lang/rust-1.83.0")
            body = "\n".join(ui._dashboard_lines())
            self.assertIn("6/13", body)
        finally:
            ui._active, ui._current = orig_active, orig_current

    def test_set_phase_progress_is_noop_when_inactive(self):
        orig_active = ui._active
        ui._active = False
        ui._current = "apply"
        ui._status["apply"] = ("running", "")
        try:
            ui.set_phase_progress("should not appear")
            self.assertEqual(ui._status["apply"], ("running", ""))
        finally:
            ui._active = orig_active


if __name__ == "__main__":
    unittest.main(verbosity=2)
