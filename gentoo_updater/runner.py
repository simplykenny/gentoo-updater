"""Runs system commands. capture() collects output, stream() lets it hit the
terminal, interactive() is stream() with a name that says "this grabs stdin".
dry_run short-circuits anything that would change the system."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass

from . import ui

_log = logging.getLogger("gentoo_updater.runner")


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _c_locale_env() -> dict[str, str]:
    # We grep portage/eselect output for strings like "no broken" and
    # "Total: 0 packages". On a non-English box those are localised and the
    # checks silently fail, so force C locale + no colour for parsed commands.
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["NOCOLOR"] = "true"
    env["NO_COLOR"] = "1"
    return env


class CommandRunner:
    def __init__(self, *, dry_run: bool = False, use_sudo: bool = True,
                 verbose: bool = False):
        self.dry_run = dry_run
        self.use_sudo = use_sudo
        self.verbose = verbose

    def _prep(self, cmd: list[str], *, force_root: bool = False) -> list[str]:
        # force_root marks a *read-only* command that still needs root to read
        # root-only state (e.g. `emerge -p @preserved-rebuild` reads the
        # preserved-libs registry; scanning /etc crosses 0700 dirs). We add
        # sudo for it on a real run, but never under --dry-run, which stays
        # sudo-free -- the caller handles the resulting inconclusive result.
        if self.use_sudo and (self._needs_root(cmd)
                              or (force_root and not self.dry_run)):
            return ["sudo", *cmd]
        return cmd

    @staticmethod
    def _needs_root(cmd: list[str]) -> bool:
        # Does this command need root *and* change the system? Both questions
        # have the same answer here, and one predicate answers both: whether to
        # sudo, and whether dry_run should skip it. It has to look at
        # subcommands, not just the program, so that read-only calls (emerge
        # --pretend, snapper list, find) still run under --dry-run while their
        # mutating siblings (snapper create, btrfs subvolume snapshot) don't.
        prog = cmd[0]
        if prog == "emerge":
            readonly = {"-p", "--pretend", "-s", "--search", "-S", "--searchdesc"}
            return not any(a in readonly for a in cmd)
        if prog in ("emaint", "dispatch-conf", "revdep-rebuild", "eix-update",
                    "mkdir"):
            return True
        if prog == "snapper":
            return any(sub in cmd for sub in ("create", "rollback", "delete",
                                              "modify"))
        if prog == "btrfs":
            return "snapshot" in cmd or "delete" in cmd
        return False  # eselect news, find, findmnt, ... fine as a normal user

    def capture(self, cmd: list[str], *, force_root: bool = False) -> CommandResult:
        full = self._prep(cmd, force_root=force_root)
        if self.dry_run and self._needs_root(cmd):
            ui.dim(f"[dry-run] would run: {' '.join(full)}")
            return CommandResult(returncode=0)
        _log.debug("capture: %s", " ".join(full))
        start = time.time()
        try:
            # No suspend() here: capture uses pipes, so the child never touches
            # the terminal and the spinner can keep animating over the wait.
            proc = subprocess.run(full, capture_output=True, text=True,
                                  check=False, env=_c_locale_env())
        except FileNotFoundError as exc:
            _log.debug("capture: %s -> not found", full[0])
            return CommandResult(returncode=127, stderr=str(exc))
        _log.debug("capture done: rc=%d (%.1fs) out=%dB err=%dB",
                   proc.returncode, time.time() - start,
                   len(proc.stdout), len(proc.stderr))
        if proc.returncode != 0 and proc.stderr.strip():
            _log.debug("stderr: %s", proc.stderr.strip()[:2000])
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    def stream(self, cmd: list[str]) -> CommandResult:
        full = self._prep(cmd)
        if self.dry_run and self._needs_root(cmd):
            ui.dim(f"[dry-run] would run: {' '.join(full)}")
            return CommandResult(returncode=0)
        ui.dim(f"$ {' '.join(full)}")
        _log.debug("stream: %s", " ".join(full))
        start = time.time()
        try:
            # ui.suspend() drops the spinner so the child gets the terminal to
            # itself, then puts it back.
            with ui.suspend():
                proc = subprocess.run(full, check=False)
        except FileNotFoundError as exc:
            _log.debug("stream: %s -> not found", full[0])
            return CommandResult(returncode=127, stderr=str(exc))
        _log.debug("stream done: rc=%d (%.1fs)", proc.returncode,
                   time.time() - start)
        return CommandResult(proc.returncode)

    def interactive(self, cmd: list[str]) -> CommandResult:
        return self.stream(cmd)

    def run_live(self, cmd: list[str], *, on_line=None) -> CommandResult:
        """Run a long mutating command (sync, the world merge, rebuilds).

        Verbose mode hands the terminal to the child via stream(), so you see
        its native, coloured output live -- today's behaviour. The quiet default
        instead *pipes* the output: every line goes to the debug log and to
        on_line() (which drives the dashboard's progress row), while the pinned
        checklist stays on screen because we never suspend(). The build text
        itself isn't echoed -- it's in the debug log if you want to tail it."""
        if self.verbose:
            return self.stream(cmd)

        full = self._prep(cmd)
        if self.dry_run and self._needs_root(cmd):
            ui.dim(f"[dry-run] would run: {' '.join(full)}")
            return CommandResult(returncode=0)
        _log.debug("run_live: %s", " ".join(full))
        start = time.time()
        try:
            proc = subprocess.Popen(
                full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=_c_locale_env(),
            )
        except FileNotFoundError as exc:
            _log.debug("run_live: %s -> not found", full[0])
            return CommandResult(returncode=127, stderr=str(exc))
        tail: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            _log.debug("| %s", line)
            tail.append(line)
            if len(tail) > 200:            # keep only the tail for error context
                tail.pop(0)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:          # noqa: BLE001 - a bad callback must
                    pass                   # never kill the merge
        proc.wait()
        _log.debug("run_live done: rc=%d (%.1fs)", proc.returncode,
                   time.time() - start)
        return CommandResult(proc.returncode, "\n".join(tail))

    def sudo_warmup(self) -> None:
        """Prime sudo's credential cache once, cleanly, before piped commands.

        run_live pipes output, so a cold `sudo` prompt would land under the
        pinned dashboard. Warming up first (with the terminal handed over) keeps
        the prompt clean; calling it again before a later phase re-prompts only
        if the timestamp has since expired -- e.g. after a multi-hour compile.
        A no-op when sudo is off or under --dry-run."""
        if not self.use_sudo or self.dry_run:
            return
        # Already valid? `sudo -n -v` succeeds silently -> no prompt, no flicker.
        try:
            probe = subprocess.run(["sudo", "-n", "-v"], capture_output=True)
        except FileNotFoundError:
            return  # no sudo binary; the mutating commands will fail loudly later
        if probe.returncode == 0:
            return
        with ui.suspend():
            subprocess.run(["sudo", "-v"], check=False)
