"""Tests for the update orchestration (updater.py) and the runner's dry-run guard.

These are the risky, stateful parts of the tool -- the phase pipeline and the
"never mutate under --dry-run" promise -- so we pin their behaviour with fakes
rather than touching a real system.
"""

import io
import os
import sys
import contextlib
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gentoo_updater.runner import CommandResult, CommandRunner
from gentoo_updater.updater import Updater, HIGH_RISK_ATOMS


# --- test doubles -------------------------------------------------------

# A pretend plan with something to do (one upgrade), used for happy-path runs.
PRETEND_NONEMPTY = (
    "These are the packages that would be merged, in order:\n\n"
    "[ebuild     U   ] app-editors/vim-9.1.0  1234 KiB\n\n"
    "Total: 1 package, Size of downloads: 1,234 KiB\n"
)

# A pretend plan with nothing pending.
PRETEND_EMPTY = "Calculating dependencies... done!\n\nTotal: 0 packages, Size of downloads: 0 KiB\n"

# What `emerge -p @preserved-rebuild` prints when nothing needs rebuilding.
PRESERVED_CLEAN = "Calculating dependencies... done!\n\nTotal: 0 packages, Size of downloads: 0 KiB\n"


class FakeRunner:
    """Stand-in for CommandRunner that returns canned results and records calls.

    `handler(cmd) -> CommandResult | None` lets a test override the result for a
    given command; returning None (the default) yields a successful empty result.
    """

    def __init__(self, handler=None, dry_run=False, verbose=False):
        self._handler = handler or (lambda cmd: None)
        self.dry_run = dry_run
        self.verbose = verbose
        self.calls: list[list[str]] = []
        self.warmups = 0

    def _result(self, cmd):
        self.calls.append(list(cmd))
        res = self._handler(cmd)
        return res if res is not None else CommandResult(returncode=0, stdout="")

    def capture(self, cmd, *, force_root=False):
        return self._result(cmd)

    def stream(self, cmd):
        return self._result(cmd)

    def interactive(self, cmd):
        return self._result(cmd)

    def run_live(self, cmd, *, on_line=None):
        res = self._result(cmd)
        if on_line is not None:
            for line in (res.stdout or "").splitlines():
                on_line(line)
        return res

    def sudo_warmup(self):
        self.warmups += 1


class FakeSnapshots:
    def __init__(self, available=False, snapshots=None):
        self.available = available
        self.created: list[str] = []
        self.rolled_back: list[str] = []
        self._snapshots = snapshots or []

    def create(self, description):
        self.created.append(description)
        return "fake#1"

    def list_snapshots(self):
        return list(self._snapshots)

    def rollback(self, ident):
        self.rolled_back.append(ident)
        return f"rolled back to {ident}"


def _happy_handler(cmd):
    """Default command outcomes for a clean, successful run."""
    if cmd[:2] == ["emerge", "--pretend"]:
        return CommandResult(0, stdout=PRETEND_NONEMPTY)
    if cmd[:1] == ["emerge"] and "-p" in cmd and "@preserved-rebuild" in cmd:
        return CommandResult(0, stdout=PRESERVED_CLEAN)
    if cmd[:3] == ["eselect", "news", "count"]:
        return CommandResult(0, stdout="0\n")
    if cmd[:1] == ["find"]:
        return CommandResult(0, stdout="")  # no pending ._cfg files
    return None  # everything else: success, empty output


def _which_stub(present):
    """Return a shutil.which replacement: tools in `present` resolve, others don't."""
    return lambda tool: (f"/usr/bin/{tool}" if tool in present else None)


def make_updater(handler=_happy_handler, *, snapshots_available=False,
                 snapshots=None, interactive=False, assume_yes=True,
                 include_depclean=False, select=False, selector=None,
                 dry_run=False):
    runner = FakeRunner(handler, dry_run=dry_run)
    snaps = FakeSnapshots(available=snapshots_available, snapshots=snapshots)
    updater = Updater(runner, snaps, interactive=interactive, assume_yes=assume_yes,
                      include_depclean=include_depclean, select=select,
                      selector=selector)
    return updater, runner


@contextlib.contextmanager
def env(tools):
    """Patch tool discovery and isolate the elog scan + news globs so run_all is
    hermetic (never reads the real /var/log or /var/lib/gentoo on the box)."""
    with mock.patch("gentoo_updater.updater.shutil.which",
                    side_effect=_which_stub(tools)), \
         mock.patch("gentoo_updater.updater.os.scandir",
                    side_effect=FileNotFoundError), \
         mock.patch("gentoo_updater.updater.NEWS_UNREAD_GLOB",
                    "/nonexistent-gup-test/*"), \
         mock.patch("gentoo_updater.updater.NEWS_REPOS_GLOB",
                    "/nonexistent-gup-test/*"):
        yield


@contextlib.contextmanager
def _quiet():
    """Silence the tool's terminal output during a test."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def _phase_names(report):
    return [p.name for p in report.phases]


def _phase(report, name):
    return next(p for p in report.phases if p.name == name)


# Tools present in a "normal" environment for these tests. eselect present so
# news runs (and reports 0 unread); revdep-rebuild/eix-update absent to keep
# the happy path from depending on them.
BASE_TOOLS = {"emerge", "emaint", "eselect"}


class RunAllPipeline(unittest.TestCase):

    def test_happy_path_runs_every_phase_and_succeeds(self):
        updater, runner = make_updater()
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertFalse(report.failed)
        # every phase in the pipeline reported in order
        self.assertEqual(
            _phase_names(report),
            ["preflight", "news", "sync", "plan", "glsa", "snapshot",
             "apply", "config", "post-update", "elog", "verify"],
        )
        # the real world update actually got issued
        self.assertTrue(
            any(c[:1] == ["emerge"] and "@world" in c and "--pretend" not in c
                for c in runner.calls),
            "expected a real 'emerge ... @world' apply call",
        )

    def test_plan_failure_is_fatal_and_stops_before_apply(self):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                return CommandResult(1, stdout="")  # dependency resolution failed
            return _happy_handler(cmd)

        updater, runner = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertTrue(report.failed)
        self.assertIn("plan", _phase_names(report))
        # pipeline stopped: apply and everything after was never reached
        self.assertNotIn("apply", _phase_names(report))
        self.assertFalse(
            any("@world" in c and "--pretend" not in c for c in runner.calls),
            "apply must not run after a failed plan",
        )

    def test_sync_failure_declined_is_fatal(self):
        def handler(cmd):
            if cmd[:2] == ["emaint", "sync"]:
                return CommandResult(1)  # a repo failed to sync
            return _happy_handler(cmd)

        # interactive=False, assume_yes=False -> _confirm returns the default,
        # which for "continue despite sync failure?" is False -> abort.
        updater, _ = make_updater(handler, interactive=False, assume_yes=False)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertTrue(report.failed)
        self.assertFalse(_phase(report, "sync").ok)
        self.assertNotIn("plan", _phase_names(report))  # stopped at sync

    def test_empty_plan_skips_apply_but_run_succeeds(self):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                return CommandResult(0, stdout=PRETEND_EMPTY)
            return _happy_handler(cmd)

        updater, runner = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertFalse(report.failed)
        self.assertTrue(_phase(report, "apply").skipped)
        self.assertFalse(
            any("@world" in c and "--pretend" not in c for c in runner.calls),
            "nothing to do -> no real emerge",
        )

    def test_dry_run_reports_apply_skipped_not_applied(self):
        # A glibc upgrade is pending; under dry-run the merge never runs, so
        # apply must report skipped (not a phantom "world updated") and no
        # reboot should be advised off an update that didn't happen.
        pretend = (
            "[ebuild     U   ] sys-libs/glibc-2.40-r1  0 KiB\n"
            "Total: 1 package, Size of downloads: 0 KiB\n"
        )

        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                return CommandResult(0, stdout=pretend)
            return _happy_handler(cmd)

        updater, _ = make_updater(handler, dry_run=True)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        apply = _phase(report, "apply")
        self.assertTrue(apply.skipped)
        self.assertIn("dry-run", apply.detail)
        self.assertFalse(report.failed)
        self.assertEqual(report.reboot_pkgs, [])

    def test_preserved_check_error_is_inconclusive_not_a_failure(self):
        # Regression: `emerge -p @preserved-rebuild` needs root to read the
        # preserved-libs registry; when it can't (permission denied, rc!=0) the
        # verify phase must report it as unknown, not treat the empty output as
        # "packages pending" and fail the whole run.
        def handler(cmd):
            if cmd[:1] == ["emerge"] and "-p" in cmd and "@preserved-rebuild" in cmd:
                return CommandResult(13, stdout="",
                                     stderr="Permission denied: "
                                            "'/var/lib/portage/preserved_libs_registry'")
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        verify = _phase(report, "verify")
        self.assertTrue(verify.ok)              # inconclusive != failure
        self.assertFalse(report.failed)
        self.assertIn("preserved:unknown", verify.detail)

    def test_preserved_check_pending_still_fails(self):
        # A genuine (rc==0) pending result must still fail verify.
        def handler(cmd):
            if cmd[:1] == ["emerge"] and "-p" in cmd and "@preserved-rebuild" in cmd:
                return CommandResult(0, stdout=PRETEND_NONEMPTY)
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertTrue(report.failed)
        self.assertIn("preserved:pending", _phase(report, "verify").detail)

    def test_config_scan_runs_with_root(self):
        # Regression: the /etc scan must be forced to root so it can descend
        # into 0700 dirs instead of silently under-counting pending configs.
        seen = {}

        def handler(cmd):
            if cmd[:1] == ["find"]:
                seen["force_root"] = cmd  # recorded; assert via capture wrapper below
            return _happy_handler(cmd)

        updater, runner = make_updater(handler)
        # wrap capture to record the force_root flag the phase passes
        flags = []
        orig = runner.capture
        runner.capture = lambda cmd, *, force_root=False: (
            flags.append((cmd[0], force_root)) or orig(cmd, force_root=force_root))
        with _quiet(), env(BASE_TOOLS):
            updater.run_all()

        find_flags = [fr for prog, fr in flags if prog == "find"]
        self.assertTrue(find_flags and all(find_flags))  # find was called force_root=True

    def test_apply_drives_progress_row_and_warms_sudo(self):
        # emerge output carries (N of M) markers; apply should feed them to the
        # dashboard, and each mutating phase should warm sudo first.
        merge_out = (
            "Calculating dependencies... done!\n"
            ">>> Emerging (1 of 2) sys-libs/glibc-2.40-r1::gentoo\n"
            ">>> Installing (1 of 2) sys-libs/glibc-2.40-r1::gentoo\n"
            ">>> Emerging (2 of 2) dev-lang/rust-1.83.0::gentoo\n"
            ">>> Installing (2 of 2) dev-lang/rust-1.83.0::gentoo\n"
        )

        def handler(cmd):
            if (cmd[:1] == ["emerge"] and "@world" in cmd
                    and "--pretend" not in cmd):
                return CommandResult(0, stdout=merge_out)
            return _happy_handler(cmd)

        seen = []
        import gentoo_updater.ui as ui_mod
        orig = ui_mod.set_phase_progress
        ui_mod.set_phase_progress = seen.append
        try:
            updater, runner = make_updater(handler)
            with _quiet(), env(BASE_TOOLS):
                report = updater.run_all()
        finally:
            ui_mod.set_phase_progress = orig

        self.assertFalse(report.failed)
        # last progress line reflects the final package
        self.assertIn("2/2  dev-lang/rust-1.83.0", seen)
        self.assertGreaterEqual(runner.warmups, 1)  # sudo warmed before mutating

    def test_apply_failure_is_not_fatal_verify_still_runs(self):
        def handler(cmd):
            # the real world merge fails, but pretend/preserved stay healthy
            if (cmd[:1] == ["emerge"] and "@world" in cmd
                    and "--pretend" not in cmd):
                return CommandResult(1)
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertTrue(report.failed)
        self.assertFalse(_phase(report, "apply").ok)
        # apply is non-fatal: the pipeline continues so we still report health
        self.assertIn("verify", _phase_names(report))

    def test_skip_flags_mark_phases_skipped_without_running_them(self):
        updater, runner = make_updater()
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all(skip_snapshot=True, skip_sync=True)

        self.assertTrue(_phase(report, "sync").skipped)
        self.assertTrue(_phase(report, "snapshot").skipped)
        self.assertFalse(report.failed)
        # sync was skipped by flag -> no emaint sync issued
        self.assertFalse(any(c[:2] == ["emaint", "sync"] for c in runner.calls))

    def test_snapshot_taken_when_backend_available(self):
        updater, _ = make_updater(snapshots_available=True)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertFalse(report.failed)
        snap = _phase(report, "snapshot")
        self.assertTrue(snap.ok)
        self.assertFalse(snap.skipped)
        self.assertEqual(report.snapshot_id, "fake#1")

    def test_glsa_phase_flags_affected_advisories(self):
        def handler(cmd):
            if cmd[:1] == ["glsa-check"]:
                return CommandResult(0, stdout="202501-07\n202412-03\n")
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS | {"glsa-check"}):
            report = updater.run_all()

        glsa = _phase(report, "glsa")
        self.assertTrue(glsa.ok)  # informational, never fails the run
        self.assertIn("202501-07", glsa.detail)
        self.assertIn("202412-03", glsa.detail)

    def test_glsa_skipped_when_tool_absent(self):
        updater, _ = make_updater()
        with _quiet(), env(BASE_TOOLS):  # no glsa-check
            report = updater.run_all()
        self.assertTrue(_phase(report, "glsa").skipped)

    def test_reboot_advice_when_toolchain_updated(self):
        pretend = (
            "[ebuild     U   ] sys-libs/glibc-2.40-r1  0 KiB\n"
            "[ebuild     U   ] app-editors/vim-9.1.0  0 KiB\n"
            "Total: 2 packages, Size of downloads: 0 KiB\n"
        )

        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                return CommandResult(0, stdout=pretend)
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertIn("sys-libs/glibc", report.reboot_pkgs)
        self.assertNotIn("app-editors/vim", report.reboot_pkgs)

    def test_no_reboot_advice_when_apply_skipped(self):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                return CommandResult(0, stdout=PRETEND_EMPTY)  # nothing to do
            return _happy_handler(cmd)

        updater, _ = make_updater(handler)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertEqual(report.reboot_pkgs, [])


DEPCLEAN_PRETEND = (
    ">>> These are the packages that would be unmerged:\n\n"
    " app-misc/foo\n"
    "    selected: 1.0\n"
    "    protected: none\n\n"
    " dev-libs/bar\n"
    "    selected: 2.0\n\n"
)


class DepcleanPhase(unittest.TestCase):
    def test_absent_from_default_pipeline(self):
        updater, _ = make_updater()
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertNotIn("depclean", _phase_names(report))

    def test_included_and_removes_when_enabled_and_confirmed(self):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--depclean"] and "--pretend" in cmd:
                return CommandResult(0, stdout=DEPCLEAN_PRETEND)
            return _happy_handler(cmd)

        updater, runner = make_updater(handler, include_depclean=True,
                                       assume_yes=True)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertIn("depclean", _phase_names(report))
        self.assertTrue(_phase(report, "depclean").ok)
        # assume_yes -> confirmed -> the real (non-pretend) depclean was issued
        self.assertTrue(
            any(c[:2] == ["emerge", "--depclean"] and "--pretend" not in c
                for c in runner.calls),
            "expected a real 'emerge --depclean' after confirmation",
        )

    def test_no_orphans_does_not_remove(self):
        # default handler returns empty stdout for depclean pretend -> 0 removable
        updater, runner = make_updater(include_depclean=True)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertIn("no orphaned", _phase(report, "depclean").detail)
        self.assertFalse(
            any(c[:2] == ["emerge", "--depclean"] and "--pretend" not in c
                for c in runner.calls),
            "nothing removable -> no real depclean",
        )

    def test_deferred_when_declined(self):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--depclean"] and "--pretend" in cmd:
                return CommandResult(0, stdout=DEPCLEAN_PRETEND)
            return _happy_handler(cmd)

        # interactive=False, assume_yes=False -> _confirm returns default (False)
        updater, runner = make_updater(handler, include_depclean=True,
                                       interactive=False, assume_yes=False)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertIn("deferred", _phase(report, "depclean").detail)
        self.assertFalse(
            any(c[:2] == ["emerge", "--depclean"] and "--pretend" not in c
                for c in runner.calls))


PRETEND_THREE = (
    "These are the packages that would be merged, in order:\n\n"
    "[ebuild     U  ] app-editors/vim-9.1.0 [9.0.1]  0 KiB\n"
    "[ebuild     U  ] sys-libs/glibc-2.40 [2.39]  0 KiB\n"
    "[ebuild     U  ] dev-lang/python-3.14 [3.13]  0 KiB\n"
    "Total: 3 packages, Size of downloads: 0 KiB\n"
)
PRETEND_TWO = (
    "[ebuild     U  ] app-editors/vim-9.1.0 [9.0.1]  0 KiB\n"
    "[ebuild     U  ] dev-lang/python-3.14 [3.13]  0 KiB\n"
    "Total: 2 packages, Size of downloads: 0 KiB\n"
)


def _apply_call(runner):
    return next((c for c in runner.calls
                 if c[:1] == ["emerge"] and "@world" in c
                 and "--pretend" not in c), None)


class SelectPackages(unittest.TestCase):
    def _handler(self, two=PRETEND_TWO, exclude_rc=0):
        def handler(cmd):
            if cmd[:2] == ["emerge", "--pretend"]:
                if "--exclude" in cmd:
                    return CommandResult(exclude_rc, stdout=two)
                return CommandResult(0, stdout=PRETEND_THREE)
            return _happy_handler(cmd)
        return handler

    def test_unchecking_excludes_from_replan_and_apply(self):
        # selector unchecks glibc (keeps the other two)
        selector = lambda items: [n for n, _ in items if n != "sys-libs/glibc"]
        updater, runner = make_updater(
            self._handler(), interactive=True, assume_yes=True,
            select=True, selector=selector)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()

        self.assertFalse(report.failed)
        # a re-pretend with --exclude was issued
        self.assertTrue(any(c[:2] == ["emerge", "--pretend"] and "--exclude" in c
                            for c in runner.calls))
        # the real apply carries the same exclusion
        apply_cmd = _apply_call(runner)
        self.assertIsNotNone(apply_cmd)
        self.assertIn("--exclude", apply_cmd)
        self.assertEqual(apply_cmd[apply_cmd.index("--exclude") + 1],
                         "sys-libs/glibc")
        # the report reflects the post-exclusion plan (2 packages)
        self.assertEqual(report.plan.total, 2)

    def test_keeping_all_adds_no_exclusion(self):
        selector = lambda items: [n for n, _ in items]  # nothing unchecked
        updater, runner = make_updater(
            self._handler(), interactive=True, assume_yes=True,
            select=True, selector=selector)
        with _quiet(), env(BASE_TOOLS):
            updater.run_all()
        self.assertFalse(any("--exclude" in c for c in runner.calls))
        self.assertIn("@world", _apply_call(runner))

    def test_cancel_keeps_all(self):
        selector = lambda items: None  # user hit 'q'
        updater, runner = make_updater(
            self._handler(), interactive=True, assume_yes=True,
            select=True, selector=selector)
        with _quiet(), env(BASE_TOOLS):
            updater.run_all()
        self.assertFalse(any("--exclude" in c for c in runner.calls))

    def test_exclusion_conflict_is_fatal_and_stops_before_apply(self):
        # re-pretend with --exclude fails -> plan phase fails -> no apply
        selector = lambda items: [n for n, _ in items if n != "sys-libs/glibc"]
        updater, runner = make_updater(
            self._handler(exclude_rc=1), interactive=True, assume_yes=True,
            select=True, selector=selector)
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertTrue(report.failed)
        self.assertFalse(_phase(report, "plan").ok)
        self.assertIsNone(_apply_call(runner))

    def test_selector_not_called_without_select_flag(self):
        called = []
        selector = lambda items: called.append(1) or [n for n, _ in items]
        updater, _ = make_updater(
            self._handler(), interactive=True, assume_yes=True,
            select=False, selector=selector)
        with _quiet(), env(BASE_TOOLS):
            updater.run_all()
        self.assertEqual(called, [])


class NewsRelevance(unittest.TestCase):
    PRETEND = (
        "[ebuild     U  ] sys-libs/glibc-2.40 [2.39]  0 KiB\n"
        "[ebuild     U  ] app-editors/vim-9.1.0  0 KiB\n"
        "Total: 2 packages, Size of downloads: 0 KiB\n"
    )

    def _handler(self, cmd):
        if cmd[:2] == ["emerge", "--pretend"]:
            return CommandResult(0, stdout=self.PRETEND)
        return _happy_handler(cmd)

    def test_plan_flags_unread_news_for_an_updated_package(self):
        from gentoo_updater import advise
        updater, _ = make_updater(self._handler)
        updater._unread_news_items = lambda: [
            advise.NewsItem("glibc 2.40 migration notes", ["sys-libs/glibc"])]
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertIn("news item(s) apply", _phase(report, "plan").detail)

    def test_plan_ignores_news_for_untouched_package(self):
        from gentoo_updater import advise
        updater, _ = make_updater(self._handler)
        updater._unread_news_items = lambda: [
            advise.NewsItem("openssh change", ["net-misc/openssh"])]
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertNotIn("news item", _phase(report, "plan").detail)


class AuditAndNotify(unittest.TestCase):
    def test_finalize_records_audit_and_notifies(self):
        recorded = []
        notified = []

        class FakeAudit:
            def record(self, entry):
                recorded.append(entry)
                return "/tmp/history.jsonl"

        class FakeNotifier:
            def maybe_notify(self, report, *, command="update"):
                notified.append(command)
                return ["desktop"]

        runner = FakeRunner(_happy_handler)
        snaps = FakeSnapshots()
        updater = Updater(runner, snaps, interactive=False, assume_yes=True,
                          audit_log=FakeAudit(), notifier=FakeNotifier())
        with _quiet(), env(BASE_TOOLS):
            updater.run_all()

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["command"], "update")
        self.assertIn("phases", recorded[0])
        self.assertEqual(notified, ["update"])

    def test_audit_failure_does_not_break_the_run(self):
        class BoomAudit:
            def record(self, entry):
                raise OSError("disk full")

        runner = FakeRunner(_happy_handler)
        snaps = FakeSnapshots()
        updater = Updater(runner, snaps, interactive=False, assume_yes=True,
                          audit_log=BoomAudit())
        with _quiet(), env(BASE_TOOLS):
            report = updater.run_all()
        self.assertFalse(report.failed)  # logging blew up, run still fine


class RollbackFlow(unittest.TestCase):
    def _snaps(self):
        from gentoo_updater.snapshot import SnapshotInfo
        return [
            SnapshotInfo("snapper#10", "snapper", "2026-08-14 09:00", "pre-update (gentoo-updater)"),
            SnapshotInfo("snapper#12", "snapper", "2026-08-15 09:00", "pre-update (gentoo-updater)"),
        ]

    def test_rollback_no_backend(self):
        updater, _ = make_updater(snapshots_available=False)
        with _quiet():
            rc = updater.run_rollback()
        self.assertEqual(rc, 1)

    def test_rollback_no_snapshots(self):
        updater, _ = make_updater(snapshots_available=True, snapshots=[])
        with _quiet():
            rc = updater.run_rollback()
        self.assertEqual(rc, 0)

    def test_rollback_unattended_picks_newest_and_restores(self):
        updater, _ = make_updater(snapshots_available=True, snapshots=self._snaps(),
                                  assume_yes=True)
        with _quiet():
            rc = updater.run_rollback()
        self.assertEqual(rc, 0)
        # newest (last) snapshot chosen and rolled back
        self.assertEqual(updater.snapshots.rolled_back, ["snapper#12"])


class RunnerDryRun(unittest.TestCase):
    """The --dry-run promise: mutating commands are described, never executed."""

    def test_dry_run_does_not_execute_mutating_command(self):
        runner = CommandRunner(dry_run=True, use_sudo=False)
        with mock.patch("gentoo_updater.runner.subprocess.run") as sp, _quiet():
            res = runner.stream(["emerge", "--update", "@world"])
        sp.assert_not_called()
        self.assertEqual(res.returncode, 0)

    def test_dry_run_still_runs_readonly_command(self):
        # a pretend/search emerge is side-effect-free, so it should really run
        runner = CommandRunner(dry_run=True, use_sudo=False)
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="ok", stderr=""))
        with mock.patch("gentoo_updater.runner.subprocess.run", fake), _quiet():
            runner.capture(["emerge", "--pretend", "@world"])
        fake.assert_called_once()

    def test_dry_run_guards_snapshot_creation(self):
        # snapshot *creation* mutates the system, so --dry-run must not run it.
        runner = CommandRunner(dry_run=True, use_sudo=False)
        with mock.patch("gentoo_updater.runner.subprocess.run") as sp, _quiet():
            r1 = runner.capture(["btrfs", "subvolume", "snapshot", "-r", "/", "/d"])
            r2 = runner.capture(["snapper", "-c", "root", "create", "--print-number"])
            r3 = runner.capture(["mkdir", "-p", "/.snapshots"])
        sp.assert_not_called()
        for r in (r1, r2, r3):
            self.assertEqual(r.returncode, 0)

    def test_dry_run_still_lists_snapshots(self):
        # listing/detection is read-only, so it must really run even in dry-run
        # (otherwise `gup rollback --dry-run` would see no snapshots).
        runner = CommandRunner(dry_run=True, use_sudo=False)
        fake = mock.Mock(return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        with mock.patch("gentoo_updater.runner.subprocess.run", fake), _quiet():
            runner.capture(["snapper", "-c", "root", "list"])
            runner.capture(["find", "/.snapshots"])
        self.assertEqual(fake.call_count, 2)

    def test_snapshot_mutation_is_sudo_but_listing_is_not(self):
        runner = CommandRunner(dry_run=False, use_sudo=True)
        seen = []

        def fake_run(cmd, **kw):
            seen.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("gentoo_updater.runner.subprocess.run", side_effect=fake_run):
            runner.capture(["snapper", "-c", "root", "create", "--print-number"])
            runner.capture(["snapper", "-c", "root", "list"])
        self.assertEqual(seen[0][0], "sudo")       # create -> privileged
        self.assertEqual(seen[1][0], "snapper")    # list   -> unprivileged

    def test_capture_pins_c_locale_for_parsed_output(self):
        runner = CommandRunner(dry_run=False, use_sudo=False)
        captured_env = {}

        def fake_run(cmd, **kw):
            captured_env.update(kw.get("env") or {})
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("gentoo_updater.runner.subprocess.run", side_effect=fake_run):
            runner.capture(["emerge", "--pretend", "@world"])
        self.assertEqual(captured_env.get("LC_ALL"), "C")
        self.assertEqual(captured_env.get("NO_COLOR"), "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
