"""Parse `emerge --pretend -v` output into structured data. It's tolerant on
purpose: emerge's format shifts between versions, so odd lines are skipped, not
fatal."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Example lines this needs to handle:
# [ebuild   N     ] dev-libs/foo-1.2.3::gentoo  USE="bar" 1234 KiB
# [ebuild  rR    ~] sys-apps/bar-2.0  ABI_X86="32*" 0 KiB
# [binary   U  ~] cat/pkg-3-r1::overlay  ...
# [ebuild     U  ] sys-devel/gcc-14.2.0:14  ...
_LINE = re.compile(
    r"""^\[
        (?P<kind>ebuild|binary|nomerge|blocks)   # what portage will do
        \s+
        (?P<flags>[^\]]*)                          # N/R/U/D/etc + keyword marks
        \]\s+
        (?P<atom>[^\s]+)                           # category/name-version[:slot][::repo]
        (?:\s+\[(?P<oldver>[0-9][^\]]*)\])?         # emerge -v prints the installed
                                                    # version in brackets for upgrades
    """,
    re.VERBOSE,
)

# Pull category/package (drop version, slot, repo) from an atom for matching.
_ATOM_NAME = re.compile(r"^([a-z0-9+_.-]+/[a-z0-9+_-]+)-[0-9]")


@dataclass
class PkgChange:
    atom: str            # full atom as printed
    name: str            # category/package (no version)
    kind: str            # ebuild | binary
    is_new: bool
    is_upgrade: bool
    is_rebuild: bool
    needs_keyword: bool  # the '~' marker: testing keyword required
    from_binary: bool
    old_version: str = ""  # installed version being replaced (upgrades only)


@dataclass
class PretendPlan:
    changes: list[PkgChange] = field(default_factory=list)
    high_risk: set[str] = field(default_factory=set)
    download_size: str = ""
    raw: str = ""

    @property
    def total(self) -> int:
        return len(self.changes)

    @property
    def new(self) -> list[PkgChange]:
        return [c for c in self.changes if c.is_new]

    @property
    def upgrades(self) -> list[PkgChange]:
        return [c for c in self.changes if c.is_upgrade]

    @property
    def rebuilds(self) -> list[PkgChange]:
        return [c for c in self.changes if c.is_rebuild]

    @property
    def from_binary(self) -> list[PkgChange]:
        return [c for c in self.changes if c.from_binary]

    @property
    def needs_keywords(self) -> list[PkgChange]:
        return [c for c in self.changes if c.needs_keyword]


def _atom_to_name(atom: str) -> str:
    # strip repo (::x) and slot (:x) first
    core = atom.split("::", 1)[0]
    m = _ATOM_NAME.match(core)
    if m:
        return m.group(1)
    # fall back: strip trailing version-ish chunk
    return core.rsplit(":", 1)[0]


def parse_pretend(text: str, *, high_risk_atoms: tuple[str, ...] = ()) -> PretendPlan:
    plan = PretendPlan(raw=text)

    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            # grab the download total off the "Total: ... Size of downloads:" line
            if "Size of downloads" in line:
                plan.download_size = line.split("Size of downloads:", 1)[-1].strip()
            continue

        kind = m.group("kind")
        if kind in ("nomerge", "blocks"):
            continue

        flags = m.group("flags")
        atom = m.group("atom")
        name = _atom_to_name(atom)

        change = PkgChange(
            atom=atom,
            name=name,
            kind=kind,
            is_new="N" in flags,
            is_upgrade="U" in flags,
            is_rebuild="R" in flags or "r" in flags,
            needs_keyword="~" in flags,
            from_binary=(kind == "binary"),
            old_version=(m.group("oldver") or "").strip(),
        )
        plan.changes.append(change)

        for risk in high_risk_atoms:
            if name.startswith(risk) or name == risk.rstrip("/"):
                plan.high_risk.add(name)

    return plan


# Live merge progress, e.g.:
#   >>> Emerging (6 of 13) dev-lang/rust-1.83.0::gentoo
#   >>> Installing (6 of 13) dev-lang/rust-1.83.0::gentoo
# We surface these as a per-package progress line during `apply`.
_PROGRESS = re.compile(
    r"^>>>\s+(?P<action>Emerging|Installing)\s+"
    r"\((?P<n>\d+)\s+of\s+(?P<total>\d+)\)\s+"
    r"(?P<atom>\S+)"
)


@dataclass
class EmergeProgress:
    action: str   # "Emerging" or "Installing"
    n: int        # 1-based index of the current package
    total: int    # total packages in this merge
    atom: str     # cat/pkg-version (::repo stripped)

    @property
    def label(self) -> str:
        # Compact "6/13  dev-lang/rust-1.83.0" for the dashboard row.
        return f"{self.n}/{self.total}  {self.atom}"


def parse_emerge_progress(line: str) -> EmergeProgress | None:
    """Return progress for a `>>> Emerging/Installing (N of M) atom` line, or
    None for any other line. Pure and line-oriented so a streaming reader can
    feed it every line cheaply."""
    m = _PROGRESS.match(line.strip())
    if not m:
        return None
    return EmergeProgress(
        action=m.group("action"),
        n=int(m.group("n")),
        total=int(m.group("total")),
        atom=m.group("atom").split("::", 1)[0],
    )
