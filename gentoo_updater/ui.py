"""All terminal output goes through here. Uses rich for colour/tables when it's
installed, otherwise plain print(). The live spinner is hand-rolled (one thread,
raw ANSI on a single line) rather than a rich widget -- that way it can animate
through a long blocking command without a rich refresh thread fighting the
subprocess, and suspend() can hand the terminal cleanly to a child or the picker."""

from __future__ import annotations

import contextlib
import itertools
import shutil
import sys
import threading
import time

try:
    from rich.console import Console
    from rich.table import Table
    from rich.prompt import Confirm
    _console: "Console | None" = Console()
    _HAVE_RICH = True
except Exception:  # noqa: BLE001 - rich is optional
    _console = None
    _HAVE_RICH = False


# Live-spinner state. Module-level because both the updater and the runner talk
# to ui, and the runner needs suspend() to pause the spinner while a subprocess
# owns the terminal.
_PLAIN = False              # --plain
_active = False             # is the spinner in use this run?
_paused = 0                 # depth: >0 means someone else owns the terminal
_current: "str | None" = None  # name of the phase currently running
_run_start: float = 0.0
_lock = threading.Lock()    # serialises every write to the terminal
_anim_thread: "threading.Thread | None" = None
_anim_stop: "threading.Event | None" = None

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Pinned-dashboard state: the ordered phase list, each phase's (state, detail),
# how many screen lines the block currently occupies (so we can move the cursor
# up and redraw it in place), and the current spinner frame. All read/written
# under _lock, same as the spinner fields above.
_phases: "list[str]" = []
_status: "dict[str, tuple[str, str]]" = {}
_dash_lines = 0
_frame = _FRAMES[0]


def set_plain(plain: bool) -> None:
    global _PLAIN
    _PLAIN = plain


def _dashboard_wanted() -> bool:
    return _HAVE_RICH and not _PLAIN and sys.stdout.isatty()


def _emit(render) -> None:
    """Run a print. When the dashboard is up, tear the pinned block down first
    (under the lock) so the message scrolls into history above it, then redraw
    the block below. When idle, just print."""
    if _active:
        with _lock:
            _clear_block_locked()
            render()
            _repaint_locked()
    else:
        render()


# -- basic messages ------------------------------------------------------

def _plain(msg: str) -> None:
    print(msg)


def info(msg: str) -> None:
    _emit(lambda: _console.print(msg) if _HAVE_RICH else _plain(msg))


def dim(msg: str) -> None:
    _emit(lambda: _console.print(f"[dim]{msg}[/dim]") if _HAVE_RICH else _plain(msg))


def warn(msg: str) -> None:
    _emit(lambda: _console.print(f"[yellow]! {msg}[/yellow]") if _HAVE_RICH
          else _plain(f"! {msg}"))


def error(msg: str) -> None:
    _emit(lambda: _console.print(f"[bold red]x {msg}[/bold red]") if _HAVE_RICH
          else _plain(f"x {msg}"))


def hint(msg: str) -> None:
    _emit(lambda: _console.print(f"[cyan]  -> {msg}[/cyan]") if _HAVE_RICH
          else _plain(f"  -> {msg}"))


def phase_header(name: str) -> None:
    # The spinner already names the current phase, so skip the rule when it's up.
    if _active:
        return
    label = f" {name.upper()} "
    if _HAVE_RICH:
        _console.rule(f"[bold]{label}[/bold]")
    else:
        _plain("\n" + "=" * 8 + label + "=" * 8)


# -- prompts -------------------------------------------------------------

def confirm(prompt: str, *, default: bool = False) -> bool:
    # A prompt needs the terminal to itself, so pause the spinner while we ask.
    with suspend():
        if _HAVE_RICH:
            try:
                return Confirm.ask(prompt, default=default)
            except (EOFError, KeyboardInterrupt):
                return False
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            ans = input(prompt + suffix).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not ans:
            return default
        return ans in ("y", "yes")


# -- structured views ----------------------------------------------------

def show_plan(plan) -> None:
    def render():
        if plan.total == 0:
            (_console.print("[green]System is up to date -- nothing to do.[/green]")
             if _HAVE_RICH else _plain("System is up to date -- nothing to do."))
            return
        if _HAVE_RICH:
            table = Table(title="Pending update", show_edge=False, header_style="bold")
            table.add_column("Category")
            table.add_column("Count", justify="right")
            table.add_row("New", str(len(plan.new)))
            table.add_row("Upgrades", str(len(plan.upgrades)))
            table.add_row("Rebuilds", str(len(plan.rebuilds)))
            table.add_row("From binhost", str(len(plan.from_binary)))
            table.add_row("Need keyword (~)", str(len(plan.needs_keywords)))
            table.add_row("[bold]Total[/bold]", f"[bold]{plan.total}[/bold]")
            _console.print(table)
            if plan.download_size:
                _console.print(f"[dim]Download size: {plan.download_size}[/dim]")
            if plan.needs_keywords:
                _console.print("[yellow]Packages needing a testing keyword:[/yellow]")
                for c in plan.needs_keywords:
                    _console.print(f"  [yellow]~[/yellow] {c.atom}")
        else:
            _plain(f"Pending update: {plan.total} package(s)")
            _plain(f"  new={len(plan.new)} upgrades={len(plan.upgrades)} "
                   f"rebuilds={len(plan.rebuilds)} binhost={len(plan.from_binary)} "
                   f"need-keyword={len(plan.needs_keywords)}")
            if plan.download_size:
                _plain(f"  download size: {plan.download_size}")
    _emit(render)


def show_autounmask(suggestion) -> None:
    def render():
        if _HAVE_RICH:
            _console.print("[yellow]! The update needs configuration changes "
                           "before it can proceed.[/yellow]")
            _console.print("[bold]Add the following to /etc/portage[/bold] "
                           "(review each line before applying):")
        else:
            _plain("! The update needs configuration changes before it can proceed.")
            _plain("Add the following to /etc/portage (review each line before applying):")
        for change in suggestion.changes:
            header = f"# {change.target_file}"
            if _HAVE_RICH:
                _console.print(f"[cyan]{header}[/cyan]")
                for ln in change.lines:
                    _console.print(f"  {ln}")
            else:
                _plain(header)
                for ln in change.lines:
                    _plain(f"  {ln}")
        msg = "Once written, run 'dispatch-conf' if needed, then re-run gup."
        _console.print(f"[cyan]  -> {msg}[/cyan]") if _HAVE_RICH else _plain(f"  -> {msg}")
    _emit(render)


def show_snapshots(snaps) -> None:
    if _HAVE_RICH:
        table = Table(title="Pre-update snapshots", header_style="bold")
        table.add_column("#", justify="right")
        table.add_column("Id")
        table.add_column("When")
        table.add_column("Description", overflow="fold")
        for idx, s in enumerate(snaps, 1):
            table.add_row(str(idx), s.ident, s.when, s.description)
        _console.print(table)
    else:
        _plain("Pre-update snapshots:")
        for idx, s in enumerate(snaps, 1):
            _plain(f"  {idx}) {s.ident}  {s.when}  {s.description}")


def select_snapshot(snaps):
    # Pick by number; blank/invalid cancels.
    prompt = f"Snapshot to roll back to [1-{len(snaps)}, or blank to cancel]: "
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        error("Not a number.")
        return None
    if not 1 <= idx <= len(snaps):
        error("Out of range.")
        return None
    return snaps[idx - 1]


def show_summary(report) -> None:
    if _HAVE_RICH:
        table = Table(title="Run summary", header_style="bold")
        table.add_column("Phase")
        table.add_column("Result")
        table.add_column("Detail", overflow="fold")
        for p in report.phases:
            if p.skipped:
                status = "[dim]skipped[/dim]"
            elif p.ok:
                status = "[green]ok[/green]"
            else:
                status = "[red]FAIL[/red]"
            table.add_row(p.name, status, p.detail)
        _console.print(table)
        if report.snapshot_id:
            _console.print(f"[dim]Snapshot: {report.snapshot_id}[/dim]")
        if report.reboot_pkgs:
            _console.print("[bold yellow]Reboot recommended — updated: "
                           + ", ".join(report.reboot_pkgs) + "[/bold yellow]")
        if report.failed:
            _console.print("[bold red]Run completed with failures.[/bold red]")
        else:
            _console.print("[bold green]Run completed successfully.[/bold green]")
    else:
        _plain("\n--- Run summary ---")
        for p in report.phases:
            status = "skipped" if p.skipped else ("ok" if p.ok else "FAIL")
            _plain(f"  {p.name:<14} {status:<8} {p.detail}")
        if report.snapshot_id:
            _plain(f"Snapshot: {report.snapshot_id}")
        if report.reboot_pkgs:
            _plain("Reboot recommended -- updated: " + ", ".join(report.reboot_pkgs))
        _plain("Run completed with failures." if report.failed
               else "Run completed successfully.")


# -- live dashboard ------------------------------------------------------
#
# run_all calls begin_run -> phase_start / phase_done* -> end_run; the runner
# and picker wrap terminal-owning work in suspend(). A background thread keeps a
# checklist of every phase pinned to the bottom of the screen and redraws it in
# place each tick, so the running phase animates and the clock ticks. Permanent
# output (warnings, the plan table, the summary) scrolls above it via _emit.
#
# During a real merge the runner calls suspend(): the block is torn down so
# emerge owns the terminal and its build output scrolls freely, then the block
# is repainted below that output when the phase returns. That hand-off is why
# this is a pinned checklist and not a full-screen frame -- we never fight the
# child for the terminal.


def _fmt(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _term_width() -> int:
    try:
        return shutil.get_terminal_size((80, 24)).columns
    except Exception:  # noqa: BLE001 - never let sizing crash a redraw
        return 80


# state -> (mark, whether to bold/colour the name). The mark is one visible
# column wide so the block's cursor math stays simple.
def _row(name: str, state: str, detail: str, namew: int, width: int) -> str:
    if state == "running":
        mark, name_s = f"\033[36m{_frame}\033[0m", f"\033[1;36m{name}\033[0m"
    elif state == "ok":
        mark, name_s = "\033[32m✔\033[0m", name
    elif state == "fail":
        mark, name_s = "\033[31m✘\033[0m", f"\033[1;31m{name}\033[0m"
    elif state == "skip":
        mark, name_s = "\033[2m╌\033[0m", f"\033[2m{name}\033[0m"
    else:  # pending
        mark, name_s = "\033[2m·\033[0m", f"\033[2m{name}\033[0m"
    pad = " " * (namew - len(name))
    # visible prefix = " " + mark(1) + " " + name(namew) + "  "
    prefix_vis = 1 + 1 + 1 + namew + 2
    avail = width - 1 - prefix_vis
    shown = "" if avail <= 0 else detail[:avail]
    detail_s = f"\033[2m{shown}\033[0m" if shown else ""
    return f" {mark} {name_s}{pad}  {detail_s}"


def _dashboard_lines() -> list[str]:
    width = _term_width()
    elapsed = _fmt(time.time() - _run_start)
    left, right = "─ gentoo-updater ", f" {elapsed} ─"
    fill = max(0, width - len(left) - len(right))
    lines = [f"\033[2m{left}{'─' * fill}{right}\033[0m"]
    namew = max((len(n) for n in _phases), default=0)
    for name in _phases:
        state, detail = _status.get(name, ("pending", ""))
        lines.append(_row(name, state, detail, namew, width))
    return lines


def _clear_block_locked() -> None:
    # Move up over the pinned block and wipe it, leaving the cursor where the
    # block's first line was. Caller holds _lock.
    global _dash_lines
    if _dash_lines:
        sys.stdout.write(f"\033[{_dash_lines}A\r\033[J")
        sys.stdout.flush()
        _dash_lines = 0


def _repaint_locked() -> None:
    # Redraw the block in place: step up over the old one (if any), clear to end
    # of screen, print the fresh lines. Cursor ends just below the block. Caller
    # holds _lock.
    global _dash_lines
    if _dash_lines:
        sys.stdout.write(f"\033[{_dash_lines}A")
    lines = _dashboard_lines()
    sys.stdout.write("\r\033[J" + "\n".join(lines) + "\n")
    sys.stdout.flush()
    _dash_lines = len(lines)


def _refresh() -> None:
    if not _active:
        return
    with _lock:
        _repaint_locked()


def _animate() -> None:
    global _frame
    frames = itertools.cycle(_FRAMES)
    while _anim_stop is not None and not _anim_stop.is_set():
        if _active and not _paused:
            with _lock:
                if _current is not None:
                    _frame = next(frames)
                _repaint_locked()
        if _anim_stop is None:
            break
        _anim_stop.wait(0.1)


def begin_run(phase_names: list[str]) -> None:
    global _active, _paused, _current, _run_start, _anim_thread, _anim_stop
    global _phases, _status, _dash_lines, _frame
    _run_start = time.time()
    _current = None
    _paused = 0
    _phases = list(phase_names)
    _status = {name: ("pending", "") for name in _phases}
    _dash_lines = 0
    _frame = _FRAMES[0]
    _active = _dashboard_wanted()
    if _active:
        _anim_stop = threading.Event()
        _anim_thread = threading.Thread(target=_animate, daemon=True)
        _anim_thread.start()
        _refresh()  # paint the initial all-pending checklist right away


def phase_start(name: str) -> None:
    global _current
    _current = name
    if name in _status:
        _status[name] = ("running", "")
    _refresh()  # animator picks up the frame; this shows the switch instantly


def phase_done(result) -> None:
    global _current
    _current = None  # stop animating this phase before we mark it
    state = "skip" if result.skipped else ("ok" if result.ok else "fail")
    if result.name in _status:
        _status[result.name] = (state, result.detail)
    _refresh()


def set_phase_progress(detail: str) -> None:
    """Update the running phase's detail in place (e.g. the '6/13 rust' merge
    progress). No-op when the dashboard isn't active, so verbose/plain runs and
    non-TTY output are unaffected. The animator repaints on its next tick."""
    if not _active or _current is None:
        return
    with _lock:
        if _current in _status:
            state, _ = _status[_current]
            _status[_current] = (state, detail)


@contextlib.contextmanager
def suspend():
    # Hand the terminal to a subprocess / prompt / picker: tear the pinned block
    # down (so the child scrolls freely) and repaint it below on the way out.
    # No-op when idle. Counted, so a nested suspend doesn't repaint too early.
    global _paused
    if not _active:
        yield
        return
    _paused += 1
    with _lock:
        _clear_block_locked()
    try:
        yield
    finally:
        _paused -= 1
        if _active and not _paused:
            with _lock:
                _repaint_locked()


def end_run(report) -> None:
    global _active, _current, _anim_thread, _anim_stop, _dash_lines
    if _anim_stop is not None:
        _anim_stop.set()
        if _anim_thread is not None:
            _anim_thread.join(timeout=1)
    with _lock:
        _clear_block_locked()
    elapsed = _fmt(time.time() - _run_start) if _run_start else ""
    _active = False
    _current = None
    _anim_thread = None
    _anim_stop = None
    _dash_lines = 0
    show_summary(report)
    if elapsed:
        _console.print(f"[dim]elapsed {elapsed}[/dim]") if _HAVE_RICH \
            else _plain(f"elapsed {elapsed}")
