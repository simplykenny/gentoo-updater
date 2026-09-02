import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gentoo_updater.runner import CommandRunner, CommandResult


PRESERVED = ["emerge", "-p", "@preserved-rebuild"]
ETC_SCAN = ["find", "/etc", "-name", "._cfg????_*", "-type", "f"]


class NeedsRoot(unittest.TestCase):
    def test_pretend_emerge_is_not_root_by_default(self):
        # The plain heuristic: a --pretend/-p emerge is read-only, so no sudo.
        self.assertFalse(CommandRunner._needs_root(PRESERVED))
        self.assertFalse(CommandRunner._needs_root(
            ["emerge", "--pretend", "@world"]))

    def test_mutating_emerge_needs_root(self):
        self.assertTrue(CommandRunner._needs_root(["emerge", "@world"]))

    def test_find_is_not_root_by_default(self):
        self.assertFalse(CommandRunner._needs_root(ETC_SCAN))


class ForceRoot(unittest.TestCase):
    """force_root adds sudo for read-only commands that must read root-only
    state (the preserved-libs registry, 0700 dirs under /etc) -- but never
    under --dry-run, which stays sudo-free."""

    def test_force_root_adds_sudo_on_real_run(self):
        run = CommandRunner()
        self.assertEqual(run._prep(PRESERVED, force_root=True)[0], "sudo")
        self.assertEqual(run._prep(ETC_SCAN, force_root=True)[0], "sudo")

    def test_force_root_is_noop_without_flag(self):
        run = CommandRunner()
        self.assertNotIn("sudo", run._prep(PRESERVED))
        self.assertNotIn("sudo", run._prep(ETC_SCAN))

    def test_force_root_stays_sudo_free_under_dry_run(self):
        run = CommandRunner(dry_run=True)
        self.assertNotIn("sudo", run._prep(PRESERVED, force_root=True))
        self.assertNotIn("sudo", run._prep(ETC_SCAN, force_root=True))

    def test_force_root_respects_no_sudo(self):
        run = CommandRunner(use_sudo=False)
        self.assertNotIn("sudo", run._prep(PRESERVED, force_root=True))

    def test_force_root_does_not_change_dry_run_skip(self):
        # _needs_root still says False for these read-only commands, so capture
        # runs them under --dry-run rather than skipping -- force_root only
        # governs sudo, not the skip decision.
        self.assertFalse(CommandRunner._needs_root(PRESERVED))
        self.assertFalse(CommandRunner._needs_root(ETC_SCAN))


class RunLive(unittest.TestCase):
    """Quiet mode pipes output line-by-line to on_line and returns it; verbose
    mode hands off to stream() instead. Uses tiny real subprocesses."""

    def test_quiet_pipes_lines_to_callback(self):
        run = CommandRunner(use_sudo=False, verbose=False)
        seen = []
        res = run.run_live(["printf", "a\\nb\\nc\\n"], on_line=seen.append)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertIn("b", res.stdout)

    def test_quiet_returns_child_exit_code(self):
        run = CommandRunner(use_sudo=False, verbose=False)
        self.assertEqual(run.run_live(["false"]).returncode, 1)

    def test_missing_binary_is_127_not_a_crash(self):
        run = CommandRunner(use_sudo=False, verbose=False)
        self.assertEqual(run.run_live(["gup-no-such-binary-xyz"]).returncode, 127)

    def test_bad_callback_never_kills_the_run(self):
        run = CommandRunner(use_sudo=False, verbose=False)

        def boom(_):
            raise RuntimeError("callback blew up")

        res = run.run_live(["printf", "x\\n"], on_line=boom)
        self.assertEqual(res.returncode, 0)  # swallowed, run completed

    def test_verbose_delegates_to_stream(self):
        run = CommandRunner(use_sudo=False, verbose=True)
        called = {}

        def fake_stream(cmd):
            called["cmd"] = cmd
            return CommandResult(0, stdout="")

        run.stream = fake_stream
        run.run_live(["emerge", "@world"], on_line=lambda _: None)
        self.assertEqual(called["cmd"], ["emerge", "@world"])


if __name__ == "__main__":
    unittest.main()
