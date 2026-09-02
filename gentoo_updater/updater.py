"""The update pipeline. Each step is a named phase you can run, skip, or report
on its own. It drives portage's own tools; it never reimplements them."""

from __future__ import annotations

import glob
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

_log = logging.getLogger("gentoo_updater.updater")

from . import __version__
from .runner import CommandRunner, CommandResult
from .parse import parse_pretend, PretendPlan, parse_emerge_progress
from .snapshot import SnapshotManager
from . import advise
from . import audit as _audit
from . import picker
from . import ui

# portage's per-package elog messages; we surface new ones after a run.
ELOG_DIR = "/var/log/portage/elog"

# Warn below this much free space in the build dir (toolchain builds are hungry).
LOW_SPACE_GIB = 5.0

# Unread-news state and the news items themselves. Module constants so tests can
# point them at a fixture tree.
NEWS_UNREAD_GLOB = "/var/lib/gentoo/news/news-*.unread"
NEWS_REPOS_GLOB = "/var/db/repos/*/metadata/news"


# Toolchain, init, kernel, core libs. Upgrades here are the ones most likely to
# need a reboot or break something subtly, so we call them out.
HIGH_RISK_ATOMS = (
    "sys-devel/gcc",
    "sys-libs/glibc",
    "sys-apps/systemd",
    "sys-kernel/",           # any kernel source/bin
    "sys-devel/binutils",
    "sys-devel/clang",
    "llvm-core/llvm",
    "dev-lang/rust",
    "dev-lang/python",
)


def _pkg_label(change) -> str:
    # Checklist row: "U cat/pkg  old → new".
    tag = ("N" if change.is_new else "U" if change.is_upgrade
           else "R" if change.is_rebuild else " ")
    core = change.atom.split("::", 1)[0]
    prefix = change.name + "-"
    ver = core[len(prefix):] if core.startswith(prefix) else core
    old = change.old_version.split("::", 1)[0]  # drop any ::repo suffix
    if old:
        return f"{tag}  {change.name}  {old} → {ver}"
    return f"{tag}  {change.name}  {ver}"


@dataclass
class PhaseResult:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class UpdateReport:
    phases: list[PhaseResult] = field(default_factory=list)
    plan: PretendPlan | None = None
    snapshot_id: str | None = None
    reboot_pkgs: list[str] = field(default_factory=list)

    def add(self, result: PhaseResult) -> None:
        self.phases.append(result)

    @property
    def failed(self) -> bool:
        return any(not p.ok and not p.skipped for p in self.phases)


class Updater:
    def __init__(
        self,
        runner: CommandRunner,
        snapshots: SnapshotManager,
        *,
        interactive: bool = True,
        assume_yes: bool = False,
        low_space_gib: float = LOW_SPACE_GIB,
        include_depclean: bool = False,
        select: bool = False,
        selector=None,
        audit_log=None,
        notifier=None,
    ):
        self.run = runner
        self.snapshots = snapshots
        self.interactive = interactive
        self.assume_yes = assume_yes
        self.low_space_gib = low_space_gib
        self.include_depclean = include_depclean
        # --select: pick packages to update, the rest go to emerge --exclude.
        # selector is injectable for tests: [(name, label)] -> checked names/None.
        self.select = select
        self.selector = selector or picker.pick
        self._excludes: list[str] = []
        # Optional, injectable, both default off. See audit.py / notify.py.
        self.audit_log = audit_log
        self.notifier = notifier
        self.report = UpdateReport()
        self._started = time.time()  # to find elog files written during this run

    def _confirm(self, prompt: str, *, default: bool = False) -> bool:
        if self.assume_yes:
            return True
        if not self.interactive:
            return default
        return ui.confirm(prompt, default=default)

    # -- phases ----------------------------------------------------------

    def phase_preflight(self) -> PhaseResult:
        missing = [t for t in ("emerge", "emaint") if shutil.which(t) is None]
        if missing:
            return PhaseResult(
                "preflight", ok=False,
                detail=f"missing required tools: {', '.join(missing)}",
            )
        # nice-to-haves; just note whether they're around
        optional = {t: shutil.which(t) is not None
                    for t in ("eix", "revdep-rebuild", "eselect", "glsa-check")}
        note = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in optional.items())

        free = self._portage_free_gib()
        space = ""
        if free is not None:
            space = f"; build space: {free:.1f} GiB free"
            if free < self.low_space_gib:
                ui.warn(f"Only {free:.1f} GiB free in the portage build dir "
                        f"(< {self.low_space_gib:.0f} GiB). A large source build may fail.")
        return PhaseResult("preflight", ok=True,
                           detail=f"optional tools: {note}{space}")

    @staticmethod
    def _portage_build_dir() -> str:
        base = os.environ.get("PORTAGE_TMPDIR", "/var/tmp")
        candidate = os.path.join(base, "portage")
        return candidate if os.path.isdir(candidate) else base

    def _portage_free_gib(self) -> float | None:
        path = self._portage_build_dir()
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            return None
        return usage.free / (1024 ** 3)

    def phase_news(self) -> PhaseResult:
        # Unread news can carry "do X before updating"; surface it and let an
        # interactive user stop to read it.
        if shutil.which("eselect") is None:
            return PhaseResult("news", ok=True, skipped=True,
                               detail="eselect not present")
        res = self.run.capture(["eselect", "news", "count", "new"])
        count = res.stdout.strip()
        try:
            n = int(count)
        except ValueError:
            n = 0
        if n == 0:
            return PhaseResult("news", ok=True, detail="no unread news")

        ui.warn(f"{n} unread news item(s). These may contain pre-update warnings.")
        if self.interactive and not self.assume_yes:
            if ui.confirm("Read news now (opens 'eselect news read')?", default=True):
                self.run.interactive(["eselect", "news", "read", "new"])
                if not ui.confirm("Continue with the update?", default=True):
                    return PhaseResult("news", ok=False,
                                       detail="user stopped to act on news")
        return PhaseResult("news", ok=True, detail=f"{n} unread (acknowledged)")

    def phase_sync(self) -> PhaseResult:
        self.run.sudo_warmup()  # first mutating phase: prompt cleanly, once
        res = self.run.run_live(["emaint", "sync", "-a"], on_line=self._sync_progress)
        if res.returncode != 0:
            # emaint returns nonzero if any repo failed, and doesn't say which,
            # so warn and let the user decide.
            ui.warn("At least one repository failed to sync.")
            if not self._confirm("Continue despite sync failure?", default=False):
                return PhaseResult("sync", ok=False, detail="sync failed, user aborted")
            return PhaseResult("sync", ok=True,
                               detail="sync had failures (continued by choice)")
        if shutil.which("eix-update"):
            self.run.run_live(["eix-update"])
        return PhaseResult("sync", ok=True, detail="all repos synced")

    @staticmethod
    def _pretend_cmd(excludes: list[str] | None = None) -> list[str]:
        cmd = ["emerge", "--pretend", "--verbose", "--update",
               "--deep", "--newuse", "--with-bdeps=y", "@world"]
        if excludes:
            cmd += ["--exclude", " ".join(excludes)]  # one space-separated list
        return cmd

    def phase_plan(self) -> PhaseResult:
        res = self.run.capture(self._pretend_cmd())
        plan = parse_pretend(res.stdout, high_risk_atoms=HIGH_RISK_ATOMS)
        self.report.plan = plan

        if res.returncode != 0:
            # nonzero usually means blockers or needed keyword/USE changes
            ui.error("emerge could not resolve the update cleanly.")
            suggestion = advise.parse_autounmask(res.stdout + "\n" + res.stderr)
            if suggestion.any:
                ui.show_autounmask(suggestion)
                return PhaseResult(
                    "plan", ok=False,
                    detail="needs config changes (see keyword/USE suggestions)",
                )
            ui.hint("Common causes: blockers, slot conflicts, or needed "
                    "keyword/USE changes. Re-run 'emerge -pv @world' to see detail.")
            ui.show_plan(plan)
            return PhaseResult("plan", ok=False,
                               detail="dependency resolution failed")

        ui.show_plan(plan)
        if plan.total == 0:
            return PhaseResult("plan", ok=True, detail="nothing to update")

        if self.select and self.interactive:
            selected = self._apply_selection(plan)
            if selected is not None:
                return selected  # picker re-planned; use that result

        news_hits = self._plan_advisories(plan)
        return PhaseResult("plan", ok=True,
                           detail=self._plan_detail(plan.total, news_hits))

    def _selectable_items(self, plan: PretendPlan) -> list[tuple[str, str]]:
        # One (name, label) row per distinct package.
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for c in plan.changes:
            if c.name in seen:
                continue
            seen.add(c.name)
            items.append((c.name, _pkg_label(c)))
        return items

    def _apply_selection(self, plan: PretendPlan) -> PhaseResult | None:
        # Run the picker. If anything was unchecked, re-pretend with --exclude
        # and return the new result. None = nothing changed, caller carries on.
        items = self._selectable_items(plan)
        # The picker (curses, or the plain prompt) must own the terminal, so
        # drop the spinner while it's up -- otherwise the live line draws on top
        # of it.
        with ui.suspend():
            checked = self.selector(items)
        if checked is None:
            ui.info("Selection cancelled -- keeping all packages.")
            return None
        checked_set = set(checked)
        excludes = [name for name, _ in items if name not in checked_set]
        if not excludes:
            return None

        self._excludes = excludes
        ui.info(f"Excluding {len(excludes)} package(s): " + ", ".join(excludes))
        res = self.run.capture(self._pretend_cmd(excludes))
        plan2 = parse_pretend(res.stdout, high_risk_atoms=HIGH_RISK_ATOMS)
        self.report.plan = plan2

        if res.returncode != 0:
            ui.error("Excluding those packages breaks dependency resolution.")
            ui.hint("Something you kept needs one you skipped. Re-run and keep "
                    "it, or resolve the conflict by hand.")
            ui.show_plan(plan2)
            return PhaseResult("plan", ok=False,
                               detail="exclusion caused a dependency conflict")

        ui.show_plan(plan2)
        if plan2.total == 0:
            return PhaseResult("plan", ok=True,
                               detail="all pending packages were excluded")
        news_hits = self._plan_advisories(
            plan2, risk_prefix="Still touches high-risk packages: ")
        detail = self._plan_detail(plan2.total, news_hits, suffix="after exclusions")
        return PhaseResult("plan", ok=True, detail=detail)

    @staticmethod
    def _plan_detail(total: int, news_hits: int, *, suffix: str = "") -> str:
        base = f"{total} package(s) pending"
        if suffix:
            base = f"{total} pending {suffix}"
        if news_hits:
            base += f"; {news_hits} news item(s) apply"
        return base

    def _plan_advisories(self, plan: PretendPlan, *,
                         risk_prefix: str = "This update touches high-risk "
                                            "packages: ") -> int:
        # High-risk warning + relevant-news warning. Returns the news count.
        if plan.high_risk:
            ui.warn(risk_prefix + ", ".join(sorted(plan.high_risk)))
        return self._warn_relevant_news(plan)

    def _warn_relevant_news(self, plan: PretendPlan) -> int:
        # Unread news whose Display-If-Installed package is in this update.
        names = {c.name for c in plan.changes}
        if not names:
            return 0
        matches = advise.relevant_news(self._unread_news_items(), names)
        for title, pkgs in matches:
            ui.warn(f'Unread news applies to this update: "{title}" '
                    f'({", ".join(pkgs)})')
        if matches:
            ui.hint("Read it before applying:  eselect news read")
        return len(matches)

    def _unread_news_items(self) -> list:
        # Best-effort read of the unread items; any I/O trouble -> empty list.
        ids: set[str] = set()
        for state in glob.glob(NEWS_UNREAD_GLOB):
            try:
                with open(state, encoding="utf-8", errors="replace") as fh:
                    ids.update(ln.strip() for ln in fh if ln.strip())
            except OSError:
                continue
        items = []
        for nid in sorted(ids):
            text = self._read_news_file(nid)
            if text:
                items.append(advise.parse_news_item(text))
        return items

    @staticmethod
    def _read_news_file(news_id: str) -> str:
        # Find the item in whatever repo has it; prefer the English text.
        for base in glob.glob(NEWS_REPOS_GLOB):
            item_dir = os.path.join(base, news_id)
            candidates = (sorted(glob.glob(os.path.join(item_dir, "*.en.txt")))
                          or sorted(glob.glob(os.path.join(item_dir, "*.txt"))))
            for path in candidates:
                try:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        return fh.read()
                except OSError:
                    continue
        return ""

    def phase_glsa(self) -> PhaseResult:
        # Known security advisories. Informational; never blocks the update.
        if shutil.which("glsa-check") is None:
            return PhaseResult("glsa", ok=True, skipped=True,
                               detail="glsa-check not present (install gentoolkit)")
        res = self.run.capture(["glsa-check", "--test", "all"])
        ids = advise.parse_glsa_ids(res.stdout + "\n" + res.stderr)
        if ids:
            ui.warn(f"{len(ids)} security advisory(ies) affect this system: "
                    + ", ".join(ids))
            ui.hint("Review with: glsa-check --list affected  "
                    "(fix with: glsa-check --fix <id>)")
            return PhaseResult("glsa", ok=True,
                               detail=f"{len(ids)} affected: {', '.join(ids)}")
        return PhaseResult("glsa", ok=True, detail="no known vulnerabilities")

    def phase_elog(self) -> PhaseResult:
        # Post-merge messages that scrolled past during the build.
        pkgs = self._new_elog_packages()
        if not pkgs:
            return PhaseResult("elog", ok=True, detail="no new elog messages")
        ui.warn(f"{len(pkgs)} package(s) left post-install messages "
                f"(in {ELOG_DIR}):")
        for p in pkgs:
            ui.hint(p)
        return PhaseResult("elog", ok=True,
                           detail=f"{len(pkgs)} elog message(s): {', '.join(pkgs)}")

    def _new_elog_packages(self) -> list[str]:
        # elog files written since the run started.
        try:
            entries = os.scandir(ELOG_DIR)
        except OSError:
            return []
        found: list[str] = []
        with entries:
            for e in entries:
                try:
                    if not e.is_file() or e.stat().st_mtime < self._started:
                        continue
                except OSError:
                    continue
                pkg = advise.elog_package_from_filename(e.name)
                if pkg:
                    found.append(pkg)
        return sorted(set(found))

    def phase_snapshot(self) -> PhaseResult:
        if not self.snapshots.available:
            return PhaseResult("snapshot", ok=True, skipped=True,
                               detail="btrfs/snapper not available")
        if not self._confirm("Take a pre-update snapshot?", default=True):
            return PhaseResult("snapshot", ok=True, skipped=True,
                               detail="declined by user")
        try:
            snap_id = self.snapshots.create("pre-update (gentoo-updater)")
            self.report.snapshot_id = snap_id
            return PhaseResult("snapshot", ok=True, detail=f"created {snap_id}")
        except Exception as exc:  # noqa: BLE001 - report any snapshot failure
            ui.warn(f"Snapshot failed: {exc}")
            if not self._confirm("Continue without a snapshot?", default=False):
                return PhaseResult("snapshot", ok=False, detail=str(exc))
            return PhaseResult("snapshot", ok=True, skipped=True,
                               detail=f"failed, continued: {exc}")

    def phase_apply(self) -> PhaseResult:
        # The real update, streamed so you see the build live.
        if self.report.plan and self.report.plan.total == 0:
            return PhaseResult("apply", ok=True, skipped=True,
                               detail="nothing to do")
        if not self._confirm("Apply the update now?", default=False):
            return PhaseResult("apply", ok=False, detail="user declined apply")

        cmd = ["emerge", "--update", "--deep", "--newuse",
               "--with-bdeps=y", "--keep-going", "@world"]
        if self._excludes:
            cmd += ["--exclude", " ".join(self._excludes)]  # same as the plan step
        self.run.sudo_warmup()  # re-prompt cleanly if the timestamp lapsed
        res = self.run.run_live(cmd, on_line=self._merge_progress)
        if self.run.dry_run:
            # stream() short-circuits under dry-run and reports rc=0; don't let
            # that masquerade as a merge that actually happened (in the summary,
            # the audit log, or reboot advice).
            return PhaseResult("apply", ok=True, skipped=True,
                               detail="dry-run: not applied")
        if res.returncode != 0:
            ui.error("emerge @world exited non-zero. Some packages may have failed.")
            ui.hint("Inspect with: emerge --resume  (or --resume --skipfirst)")
            return PhaseResult("apply", ok=False, detail="emerge failed")
        return PhaseResult("apply", ok=True, detail="world updated")

    def phase_postupdate(self) -> PhaseResult:
        # Rebuild consumers of replaced libs, plus out-of-tree modules.
        overall_ok = True
        details = []

        self.run.sudo_warmup()  # a long @world merge may have outlived the cache
        pres = self.run.run_live(["emerge", "@preserved-rebuild"],
                                 on_line=self._merge_progress)
        details.append(f"preserved-rebuild rc={pres.returncode}")
        overall_ok &= pres.returncode == 0

        # @module-rebuild is a fast no-op when there are no external modules.
        mres = self.run.run_live(["emerge", "@module-rebuild"],
                                 on_line=self._merge_progress)
        details.append(f"module-rebuild rc={mres.returncode}")
        overall_ok &= mres.returncode == 0

        return PhaseResult("post-update", ok=overall_ok, detail=", ".join(details))

    def phase_config(self) -> PhaseResult:
        # dispatch-conf is interactive; unattended we only report, never auto-merge.
        pending = self._count_pending_configs()
        if pending == 0:
            return PhaseResult("config", ok=True, detail="no config updates")
        if self.interactive and not self.assume_yes:
            if ui.confirm(f"{pending} config update(s) pending. Run dispatch-conf?",
                          default=True):
                self.run.interactive(["dispatch-conf"])
                return PhaseResult("config", ok=True, detail="dispatch-conf run")
        ui.warn(f"{pending} config file update(s) pending -- run 'dispatch-conf'.")
        return PhaseResult("config", ok=True, detail=f"{pending} pending (deferred)")

    def phase_depclean(self) -> PhaseResult:
        # Remove orphans, carefully: pretend first, confirm before removing.
        # depclean can drop packages you use but never added to @world (emerge
        # still refuses anything that'd break a reverse dep).
        res = self.run.capture(["emerge", "--depclean", "--pretend"])
        removable = advise.parse_depclean_count(res.stdout)
        if removable == 0:
            return PhaseResult("depclean", ok=True, detail="no orphaned packages")

        ui.warn(f"{removable} orphaned package(s) could be removed by depclean.")
        ui.hint("Review carefully: depclean removes anything not pulled in by "
                "@world. List it with 'emerge --depclean --pretend'.")
        if not self._confirm("Run 'emerge --depclean' now?", default=False):
            return PhaseResult("depclean", ok=True,
                               detail=f"{removable} removable (deferred)")

        self.run.sudo_warmup()
        r = self.run.run_live(["emerge", "--depclean"], on_line=self._merge_progress)
        if r.returncode != 0:
            return PhaseResult("depclean", ok=False, detail="depclean failed")
        return PhaseResult("depclean", ok=True,
                           detail=f"removed up to {removable} package(s)")

    def phase_verify(self) -> PhaseResult:
        # Read-only health checks after the update.
        details = []
        ok = True

        if shutil.which("revdep-rebuild"):
            r = self.run.capture(["revdep-rebuild", "-p", "-i"])
            broken = "broken" in r.stdout.lower() and "no broken" not in r.stdout.lower()
            details.append("linkage:BROKEN" if broken else "linkage:ok")
            ok &= not broken
            if broken:
                ui.warn("Broken library linkage detected. Run: sudo revdep-rebuild")
        else:
            details.append("linkage:skipped")

        r2 = self.run.capture(["emerge", "-p", "@preserved-rebuild"],
                              force_root=True)
        if r2.returncode != 0:
            # Reading the preserved-libs registry needs root; under --dry-run we
            # stay sudo-free, so this can't be determined. Inconclusive, not a
            # failure -- don't turn a permission gap into "run failed".
            details.append("preserved:unknown")
            ui.warn(f"Couldn't check @preserved-rebuild (emerge exited "
                    f"{r2.returncode}); skipping that check.")
        else:
            needs = parse_pretend(r2.stdout).total > 0
            details.append("preserved:pending" if needs else "preserved:clean")
            ok &= not needs
            if needs:
                ui.warn("Preserved libraries still need rebuilding. "
                        "Run: sudo emerge @preserved-rebuild")

        return PhaseResult("verify", ok=ok, detail=", ".join(details))

    def _merge_progress(self, line: str) -> None:
        # Feed each emerge output line to the parser; when it's a
        # ">>> Emerging/Installing (N of M) atom" marker, update the phase row.
        prog = parse_emerge_progress(line)
        if prog is not None:
            ui.set_phase_progress(prog.label)

    def _sync_progress(self, line: str) -> None:
        # emaint/emerge --sync prints ">>> Syncing repository 'gentoo' ...".
        s = line.strip()
        if s.startswith(">>> Syncing repository"):
            ui.set_phase_progress(s[len(">>> Syncing repository "):].strip(" .'\""))

    def _count_pending_configs(self) -> int:
        # ._cfg files live all over CONFIG_PROTECT dirs; find is simpler and more
        # robust than poking at portage internals. force_root so it can descend
        # into 0700 dirs (ssl/private, portage/gnupg, ...) instead of silently
        # skipping them and under-reporting pending config changes.
        res = self.run.capture(
            ["find", "/etc", "-name", "._cfg????_*", "-type", "f"],
            force_root=True,
        )
        return len([ln for ln in res.stdout.splitlines() if ln.strip()])

    # -- orchestration ---------------------------------------------------

    def run_all(self, *, skip_snapshot: bool = False,
                skip_sync: bool = False) -> UpdateReport:
        sequence = [
            ("preflight", self.phase_preflight, True),
            ("news", self.phase_news, True),
            ("sync", self.phase_sync, not skip_sync),
            ("plan", self.phase_plan, True),
            ("glsa", self.phase_glsa, True),
            ("snapshot", self.phase_snapshot, not skip_snapshot),
            ("apply", self.phase_apply, True),
            ("config", self.phase_config, True),
            ("post-update", self.phase_postupdate, True),
        ]
        # depclean is opt-in; only add it when enabled so the common run doesn't
        # carry a "skipped" row for a feature that's off.
        if self.include_depclean:
            sequence.append(("depclean", self.phase_depclean, True))
        sequence += [
            ("elog", self.phase_elog, True),
            ("verify", self.phase_verify, True),
        ]

        ui.begin_run([name for name, _, _ in sequence])
        try:
            for name, fn, enabled in sequence:
                ui.phase_start(name)
                if not enabled:
                    skipped = PhaseResult(name, ok=True, skipped=True,
                                          detail="skipped by flag")
                    self.report.add(skipped)
                    ui.phase_done(skipped)
                    _log.info("phase %s: skipped by flag", name)
                    continue

                _log.info("phase %s: start", name)
                ui.phase_header(name)
                result = fn()
                self.report.add(result)
                ui.phase_done(result)
                _log.info("phase %s: %s (%s)", name,
                          "skip" if result.skipped else "ok" if result.ok
                          else "FAIL", result.detail)

                # A failed fatal phase stops the run. apply is not fatal, so we
                # still reach verify to report the damage.
                if not result.ok and not result.skipped:
                    if name in ("preflight", "news", "sync", "plan", "snapshot"):
                        ui.error(f"Phase '{name}' failed: {result.detail}. Stopping.")
                        break
        finally:
            self._compute_reboot_advice()
            ui.end_run(self.report)

        self._finalize(command="update")
        return self.report

    def _finalize(self, *, command: str) -> None:
        # Audit log + notification. Both best-effort: the update already happened,
        # so neither is allowed to raise.
        if self.audit_log is not None:
            try:
                record = _audit.build_record(
                    self.report, started=self._started, finished=time.time(),
                    version=__version__, command=command,
                )
                self.audit_log.record(record)
            except Exception as exc:  # noqa: BLE001
                ui.dim(f"(audit log skipped: {exc})")
        if self.notifier is not None:
            try:
                self.notifier.maybe_notify(self.report, command=command)
            except Exception as exc:  # noqa: BLE001
                ui.dim(f"(notification skipped: {exc})")

    def _compute_reboot_advice(self) -> None:
        # Flag a reboot if the applied update touched kernel/glibc/systemd/dbus.
        applied = next((p for p in self.report.phases if p.name == "apply"), None)
        if not applied or applied.skipped or not applied.ok:
            return
        if not self.report.plan:
            return
        names = [c.name for c in self.report.plan.changes]
        self.report.reboot_pkgs = advise.packages_needing_reboot(names)

    def run_depclean(self) -> int:
        # Standalone `gup depclean`: preflight then depclean, nothing else.
        pre = self.phase_preflight()
        self.report.add(pre)
        if not pre.ok:
            ui.error(f"Preflight failed: {pre.detail}")
            ui.show_summary(self.report)
            return 1
        ui.phase_header("depclean")
        result = self.phase_depclean()
        self.report.add(result)
        ui.show_summary(self.report)
        self._finalize(command="depclean")
        return 0 if result.ok else 1

    # -- rollback --------------------------------------------------------

    def run_rollback(self) -> int:
        if not self.snapshots.available:
            ui.error("No snapshot backend (snapper/btrfs) available -- nothing to "
                     "roll back to.")
            return 1
        snaps = self.snapshots.list_snapshots()
        if not snaps:
            ui.info("No pre-update snapshots created by gentoo-updater were found.")
            return 0

        ui.show_snapshots(snaps)
        choice = self._choose_snapshot(snaps)
        if choice is None:
            ui.info("Rollback cancelled.")
            return 0

        if not self._confirm(
                f"Roll back to {choice.ident} ({choice.when})? "
                "This changes what your system boots into.", default=False):
            ui.info("Rollback cancelled.")
            return 0
        try:
            msg = self.snapshots.rollback(choice.ident)
        except Exception as exc:  # noqa: BLE001 - surface any rollback failure
            ui.error(f"Rollback failed: {exc}")
            return 1
        ui.info(msg)
        return 0

    def _choose_snapshot(self, snaps):
        # Newest under -y/non-interactive, otherwise ask.
        if self.assume_yes or not self.interactive:
            return snaps[-1]
        return ui.select_snapshot(snaps)
