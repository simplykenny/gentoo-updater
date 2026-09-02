"""Config file so you don't retype the same flags every run.

Precedence, low to high: defaults -> /etc/gentoo-updater.toml ->
~/.config/gentoo-updater/config.toml -> command-line flags.

TOML via stdlib tomllib (3.11+). A missing or malformed file just means defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields

SYSTEM_PATH = "/etc/gentoo-updater.toml"


def _user_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "gentoo-updater", "config.toml")


# Recognised notification triggers.
NOTIFY_CHOICES = ("never", "failure", "reboot", "always")


@dataclass
class Config:
    # Field names match the CLI flags so the two merge by name.
    # behaviour
    yes: bool = False
    non_interactive: bool = False
    no_snapshot: bool = False
    no_sync: bool = False
    no_sudo: bool = False
    depclean: bool = False           # include a depclean step in the pipeline
    select: bool = False             # interactively cherry-pick packages to update
    verbose: bool = False            # stream full command output instead of the dashboard

    # thresholds
    low_space_gib: float = 5.0

    # notifications
    notify: str = "never"            # one of NOTIFY_CHOICES
    notify_desktop: bool = True      # use notify-send if notifying
    notify_email: str = ""           # address to mail; empty disables email

    # audit trail
    audit: bool = True               # append a JSONL record per run
    audit_path: str = ""             # empty -> AuditLog picks a default

    def merged_with_cli(self, cli: dict) -> "Config":
        # cli maps field name -> value; None means "not passed", so keep ours.
        values = {f.name: getattr(self, f.name) for f in fields(self)}
        for key, val in cli.items():
            if val is not None and key in values:
                values[key] = val
        return Config(**values)


def _coerce(raw: dict) -> dict:
    # Keep known keys, coerce to the right type, ignore the rest (typos, keys
    # from a newer version) instead of blowing up.
    known = {f.name for f in fields(Config)}
    out: dict = {}
    for key, val in raw.items():
        norm = key.replace("-", "_")
        if norm not in known:
            continue
        if norm == "low_space_gib":
            try:
                out[norm] = float(val)
            except (TypeError, ValueError):
                continue
        elif norm in ("notify", "notify_email", "audit_path"):
            out[norm] = str(val)
        else:  # the booleans
            out[norm] = bool(val)
    if "notify" in out and out["notify"] not in NOTIFY_CHOICES:
        del out["notify"]  # ignore an invalid trigger, keep the default
    return out


def parse_config_text(text: str) -> dict:
    # Bad TOML -> empty dict, and defaults win.
    try:
        raw = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return {}
    # accept either top-level keys or a [gentoo-updater] table
    section = raw.get("gentoo-updater")
    if isinstance(section, dict):
        raw = {**raw, **section}
    return _coerce(raw)


def load_config(paths: list[str] | None = None) -> Config:
    # Layer the files, later ones winning; skip any we can't read.
    if paths is None:
        paths = [SYSTEM_PATH, _user_path()]
    merged: dict = {}
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        merged.update(parse_config_text(text))
    return Config(**merged) if merged else Config()
