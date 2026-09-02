#!/usr/bin/env python3
"""Visual demo of gup's live dashboard -- no real emerge required.

    python contrib/ui-demo.py           # the pretty pinned dashboard
    python contrib/ui-demo.py --plain   # the plain-text fallback
    python contrib/ui-demo.py --fail    # make a phase fail, to see the FAIL row
    python contrib/ui-demo.py --verbose # stream fake build output (terminal takeover)

It fakes the phase sequence with sleeps, emits a couple of warnings so you can
watch permanent output scroll *above* the pinned checklist, and fakes an emerge
that takes over the terminal during `apply` (via ui.suspend()) so you can see
the block tear down and repaint below the build output.

Needs a real terminal for the animated version; without a TTY (or with --plain)
it uses the linear fallback, same as the tool itself.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gentoo_updater import ui  # noqa: E402


@dataclass
class FakeResult:
    name: str
    ok: bool = True
    detail: str = ""
    skipped: bool = False


@dataclass
class FakeReport:
    phases: list = field(default_factory=list)
    snapshot_id: str | None = None
    reboot_pkgs: list = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(not p.ok and not p.skipped for p in self.phases)


# (name, seconds to fake; None => fake an emerge takeover, detail)
_STEPS = [
    ("preflight", 0.6, "42.1 GiB free; eix, glsa-check present"),
    ("news", 0.5, "no unread news"),
    ("sync", 1.3, "all repos synced"),
    ("plan", 0.9, "17 package(s) pending; 2 news item(s) apply"),
    ("glsa", 0.5, "no known vulnerabilities"),
    ("snapshot", 0.7, "created snapper#42"),
    ("apply", None, "world updated"),
    ("config", 0.5, "no config updates"),
    ("post-update", 0.9, "preserved-rebuild rc=0, module-rebuild rc=0"),
    ("elog", 0.4, "no new elog messages"),
    ("verify", 0.9, "linkage:ok, preserved:clean"),
]

_FAKE_MERGE = [
    "sys-libs/glibc-2.40-r1",
    "dev-lang/rust-1.83.0",
    "sys-devel/gcc-14.2.0",
]


def _fake_merge_quiet() -> None:
    # Quiet default: no build spam -- just the per-package progress row updating
    # on the pinned 'apply' line, driven the same way run_live drives it live.
    total = len(_FAKE_MERGE)
    for i, pkg in enumerate(_FAKE_MERGE, 1):
        ui.set_phase_progress(f"{i}/{total}  {pkg}")
        time.sleep(0.7)


def _fake_merge_verbose() -> None:
    # Verbose: the terminal is handed over and emerge streams its own output.
    total = len(_FAKE_MERGE)
    for i, pkg in enumerate(_FAKE_MERGE, 1):
        print(f">>> Emerging ({i} of {total}) {pkg}")
        time.sleep(0.5)
        print(f">>> Installing ({i} of {total}) {pkg}")


def main(argv: list[str]) -> int:
    ui.set_plain("--plain" in argv)
    make_fail = "--fail" in argv
    verbose = "--verbose" in argv

    names = [name for name, _, _ in _STEPS]
    ui.begin_run(names)

    results: list[FakeResult] = []
    for name, dur, detail in _STEPS:
        ui.phase_start(name)
        ui.phase_header(name)  # prints a rule in --plain; a no-op under the dashboard

        if name == "plan":
            time.sleep(0.4)
            # Permanent output: should land above the pinned block.
            ui.warn("This update touches high-risk packages: "
                    "sys-devel/gcc, dev-lang/rust")
            ui.hint("Read it before applying:  eselect news read")
            time.sleep(0.4)

        if dur is None:
            if verbose:
                with ui.suspend():
                    _fake_merge_verbose()
            else:
                _fake_merge_quiet()   # pinned dashboard stays up, progress row moves
            res = FakeResult(name, ok=True, detail=detail)
        else:
            time.sleep(dur)
            if make_fail and name == "verify":
                res = FakeResult(name, ok=False, detail="linkage:BROKEN")
            else:
                res = FakeResult(name, ok=True, detail=detail)

        results.append(res)
        ui.phase_done(res)

    report = FakeReport(
        phases=results,
        snapshot_id="snapper#42",
        reboot_pkgs=["sys-libs/glibc"],
    )
    ui.end_run(report)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
