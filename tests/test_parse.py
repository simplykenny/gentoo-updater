"""Tests for the emerge --pretend parser.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gentoo_updater.parse import parse_pretend, parse_emerge_progress

HIGH_RISK = ("sys-devel/gcc", "sys-libs/glibc", "sys-apps/systemd", "sys-kernel/")

SAMPLE = """These are the packages that would be merged, in order:

[ebuild   N     ] dev-libs/foo-1.2.3::gentoo  USE="bar" 1234 KiB
[ebuild  rR    ~] sys-apps/bar-2.0  ABI_X86="32*" 0 KiB
[binary   U     ] cat/pkg-3-r1::steam-overlay  200 KiB
[ebuild     U   ] sys-devel/gcc-14.2.0:14  2608 KiB
[ebuild   N    ~] gui-wm/hyprland-0.56.0-r1::hyproverlay  USE="X" 287 KiB
[nomerge       ] net-misc/networkmanager-1.56.0::gentoo
[ebuild   R    ] sys-kernel/gentoo-sources-7.1.4  0 KiB

Total: 6 packages, Size of downloads: 4,529 KiB
"""


def _plan():
    return parse_pretend(SAMPLE, high_risk_atoms=HIGH_RISK)


def test_total_excludes_nomerge():
    assert _plan().total == 6


def test_new_packages():
    names = [c.name for c in _plan().new]
    assert "dev-libs/foo" in names
    assert "gui-wm/hyprland" in names
    assert len(names) == 2


def test_upgrades():
    names = [c.name for c in _plan().upgrades]
    assert "sys-devel/gcc" in names
    assert "cat/pkg" in names


def test_rebuilds():
    names = [c.name for c in _plan().rebuilds]
    assert "sys-apps/bar" in names
    assert "sys-kernel/gentoo-sources" in names


def test_binary_detection():
    assert [c.name for c in _plan().from_binary] == ["cat/pkg"]


def test_keyword_detection():
    names = [c.name for c in _plan().needs_keywords]
    assert "sys-apps/bar" in names
    assert "gui-wm/hyprland" in names
    assert len(names) == 2


def test_high_risk():
    hr = _plan().high_risk
    assert "sys-devel/gcc" in hr
    assert "sys-kernel/gentoo-sources" in hr


def test_download_size():
    assert _plan().download_size == "4,529 KiB"


def test_empty_input():
    plan = parse_pretend("", high_risk_atoms=HIGH_RISK)
    assert plan.total == 0
    assert plan.high_risk == set()


def test_up_to_date_output():
    txt = "Calculating dependencies... done!\n\nTotal: 0 packages, Size of downloads: 0 KiB\n"
    plan = parse_pretend(txt, high_risk_atoms=HIGH_RISK)
    assert plan.total == 0


def test_emerge_progress_emerging():
    p = parse_emerge_progress(">>> Emerging (6 of 13) dev-lang/rust-1.83.0::gentoo")
    assert p is not None
    assert (p.action, p.n, p.total) == ("Emerging", 6, 13)
    assert p.atom == "dev-lang/rust-1.83.0"       # ::repo stripped
    assert p.label == "6/13  dev-lang/rust-1.83.0"


def test_emerge_progress_installing():
    p = parse_emerge_progress(">>> Installing (1 of 3) sys-libs/glibc-2.40-r1::gentoo")
    assert p is not None and p.action == "Installing" and p.n == 1 and p.total == 3


def test_emerge_progress_ignores_build_noise():
    for line in ("gcc -O2 -c foo.c", "", ">>> Unpacking source...",
                 "Total: 13 packages", ">>> Emerging weird line"):
        assert parse_emerge_progress(line) is None


if __name__ == "__main__":
    # allow running without pytest
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} passed")
