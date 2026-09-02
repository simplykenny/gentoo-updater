# Copyright 2026 Gentoo Authors
# Distributed under the terms of the MIT license

EAPI=8

DISTUTILS_USE_PEP517=setuptools
PYTHON_COMPAT=( python3_{11..14} )

inherit distutils-r1 git-r3

DESCRIPTION="Wraps emerge world updates with snapshots, checks, and rollback"
HOMEPAGE="https://github.com/gabethomson/gentoo-updater"

# Pinned to the release tag. Fetching the tag over git rather than the GitHub
# tarball keeps this reproducible without a Manifest: there's no distfile to
# checksum, so no `ebuild ... manifest` step and nothing to regenerate when
# GitHub re-rolls its archive tarballs. Users just emerge it.
EGIT_REPO_URI="https://github.com/gabethomson/gentoo-updater.git"
EGIT_COMMIT="v${PV}"

LICENSE="MIT"
SLOT="0"
KEYWORDS="~amd64"
IUSE="rich"

# Everything the tool needs at runtime is stdlib; rich is optional and only
# enables colour, tables, and the animated status line.
RDEPEND="rich? ( dev-python/rich[${PYTHON_USEDEP}] )"

pkg_postinst() {
	elog "Run 'gup --help' to get started, or 'gup --dry-run' for a safe look."
	if ! use rich; then
		elog
		elog "Built without USE=rich: output is plain text. Enable the rich USE"
		elog "flag for colour, tables, and the animated status line."
	fi
}
