# gentoo-updater (`gup`)

A wrapper around Gentoo world updates. It runs the same commands you'd type by
hand, in the right order, and adds the checks a bare update sequence skips: a
snapshot first, risk flagging, news, and health checks after.

Every command it runs is printed to the terminal, prefixed with `$`. Nothing is
hidden.

```
─ gentoo-updater ───────────────────────────────  04:18 ─
 ✔ preflight     optional tools ok; 42.1 GiB free
 ✔ news          no unread news
 ✔ sync          all repos synced
 ✔ plan          17 package(s) pending
 ✔ glsa          no known vulnerabilities
 ✔ snapshot      created snapper#42
 ⠹ apply
 · config
 · post-update
 · verify
```

## What it does

A normal update is a sequence with decision points:

```bash
emaint sync -a
eselect news read
emerge -avuDN --with-bdeps=y @world
dispatch-conf
emerge @preserved-rebuild
emerge @module-rebuild
```

`gup` runs that, and also:

- Takes a btrfs/snapper snapshot before applying (`gup rollback` restores it).
- Flags high-risk packages in the plan (gcc, glibc, systemd, kernel, binutils, clang, llvm, rust, python).
- Lets you cherry-pick packages to update with `--select`.
- Runs `glsa-check` for known vulnerabilities.
- Warns on unread news, and flags any unread item that's about a package you're updating.
- Shows the exact `/etc/portage` lines to add when the resolver needs keyword/USE changes.
- Checks for broken linkage, pending preserved rebuilds, and post-merge `elog` messages afterward.
- Advises a reboot when the update touched the kernel, glibc, systemd, or dbus.
- Optional: JSONL audit log, desktop/email notifications, scheduled runs, and depclean.

## Install

It's pure stdlib, so it doesn't need `pip` — which is good, since Gentoo blocks
`pip install` into the system Python (PEP 668). Pick whichever fits.

**Run it from the clone** (no install):

```bash
git clone https://github.com/gabethomson/gentoo-updater
cd gentoo-updater
python -m gentoo_updater --help
```

**ebuild** — the Gentoo-native way, from the bundled overlay:

```bash
# after adding the overlay (see contrib/overlay/README.md):
echo "app-admin/gentoo-updater ~amd64" | sudo tee /etc/portage/package.accept_keywords/gentoo-updater
sudo emerge -av app-admin/gentoo-updater

# with colour and the live dashboard:
sudo USE="rich" emerge -av app-admin/gentoo-updater
```

No Manifest step: the ebuild pins the release git tag rather than a tarball.
See [`contrib/overlay/`](contrib/overlay/) for setup and the `-9999` live ebuild
if you'd rather track `main`.

The only optional dependency is `rich` (`dev-python/rich`) for colour, tables,
and the live dashboard; without it the output is plain text.

## Usage

```bash
gup                  # full update, interactive
gup -y                # unattended: assume yes, apply automatically
gup --dry-run         # print what would run, change nothing
gup --select          # pick which packages to update (checklist)

gup plan              # show the pending update only (no sync/apply)
gup verify            # post-update health checks only
gup news              # show/read pending news
gup rollback          # restore a pre-update snapshot
gup depclean          # remove orphaned packages (asks first)
gup install-schedule  # set up unattended runs (auto-detects your init)
gup install-timer     # = install-schedule --init systemd
```

**Run `gup` as your regular user, not with `sudo`.** It escalates the individual
steps that need root (sync, the snapshot, the world merge) with `sudo` itself,
so it prompts for your password when it gets there while the dashboard, news,
and plan stay in your own user context. Running `sudo gup` is refused with a
hint. If you really do need to run as root — a root shell, or the systemd unit —
pass `--no-sudo` (or set `no_sudo = true`) to skip both the guard and the
internal `sudo`.

### Flags

| Flag | Effect |
| --- | --- |
| `-y`, `--yes` | Assume yes to all prompts; apply automatically |
| `--non-interactive` | Never prompt; report where prompts would be (won't auto-apply unless `-y`) |
| `--dry-run` | Never run a mutating command; print what would run |
| `--plain` | No live dashboard; plain linear output |
| `-v`, `--verbose` | Stream full command output (sync, `emerge`, rebuilds) instead of the quiet dashboard |
| `--select` | Pick which pending packages to update |
| `--no-snapshot` | Skip the pre-update snapshot |
| `--no-sync` | Skip the repo sync |
| `--no-sudo` | Don't prepend `sudo` (already root) |
| `--depclean` | Also run a depclean step (pretends first, asks before removing) |
| `--notify WHEN` | Notify on completion: `never` / `failure` / `reboot` / `always` |
| `--no-audit` | Don't append a run record to the audit log |
| `--config PATH` | Use only this config file |
| `--init WHICH` | Target init for `install-schedule`: `auto`, `systemd`, `openrc`, `runit`, `cron` |
| `--schedule PERIOD` | Cadence for `install-schedule`: `daily`/`weekly`/`monthly` |

Interactive (default) prompts before applying, before the snapshot, and stops to
let you read news. `-y` runs the whole thing with no input — that's what the
scheduler uses.

## While it runs

With `rich` on a real terminal, `gup` pins the full phase checklist to the bottom
of the screen with a running clock in the header. Each row shows its state as it
goes: `·` pending, an animated spinner on the phase in progress, `✔` done, `✘`
failed, `╌` skipped, each with a one-line detail. Warnings and the plan table
scroll into history above the pinned block.

By default the long phases run **quietly**: `sync`, the world `emerge`, and the
rebuilds are captured rather than streamed, so the dashboard stays pinned the
whole run instead of being pushed off-screen by build output. During the merge
the `apply` row shows live per-package progress parsed from Portage —
`⠹ apply   6/13  dev-lang/rust-1.83.0`. The full build output still goes to the
debug log (`tail -f` it in another pane if you want to watch).

Pass `-v` / `--verbose` to stream everything to the terminal instead — Portage
takes over the screen for each streamed phase with its own coloured output, the
way it did before. `dispatch-conf` is always interactive regardless. Piped
output, cron, or no `rich` falls back to plain linear output; force that with
`--plain`.

Because the merge is captured, `gup` primes `sudo` once up front (and again
before a later phase only if the credential cache has expired — e.g. after a
multi-hour compile), so the password prompt stays clean instead of landing under
the dashboard.

Want to see it without kicking off a real update? `python contrib/ui-demo.py`
fakes a full run (add `--verbose`, `--plain`, or `--fail` to see those paths).

## Phases

1. **preflight** — check `emerge`/`emaint` exist, note optional tools, warn on low build-dir space
2. **news** — check for unread news, offer to read it
3. **sync** — `emaint sync -a`
4. **plan** — `emerge -pvuDN @world`, parsed and risk-flagged, cross-referenced against unread news; shows the `/etc/portage` fix on a masked resolution
5. **glsa** — `glsa-check` for known advisories (informational)
6. **snapshot** — btrfs/snapper restore point
7. **apply** — the real `@world` update
8. **config** — pending `._cfg` files via `dispatch-conf`
9. **post-update** — `@preserved-rebuild` + `@module-rebuild`
10. **elog** — post-merge messages from the run
11. **verify** — broken-linkage and preserved-rebuild checks

Preflight, news, sync, plan, and snapshot are fatal — they stop the run if they
fail. `glsa` and `elog` are informational. An optional **depclean** phase runs
before `elog` when enabled.

## Snapshots & rollback

Uses **snapper** if a `root` config exists, otherwise a raw `btrfs subvolume
snapshot` into `/.snapshots/` if `/` is btrfs. If neither, snapshots are skipped.

`gup rollback` lists the snapshots gup made and restores the one you pick. Under
snapper that's a real `snapper rollback` (takes effect on reboot). For raw btrfs
it won't swap subvolumes for you (that can leave you unbootable) — it prints the
manual steps instead.

## Cherry-picking with `--select`

`--select` (interactive only) turns the plan step into a checklist. Everything
starts checked; uncheck what to skip this run.

```
Select packages to update  (2/4 selected)
↑/↓ move · space toggle · a all · n none · enter confirm · q cancel

 > [x] app-editors/vim      9.0.1 → 9.1.0
   [ ] sys-libs/glibc       2.39  → 2.40
   [x] dev-lang/python      3.13  → 3.14
   [ ] www-client/firefox   128   → 130
```

Unchecked packages become `emerge --exclude` atoms. gup re-runs the pretend with
those exclusions and shows the resulting plan before applying, so a conflict
(something you kept needs something you skipped) shows up at the plan step. The
same exclusions go to the actual `emerge`.

Needs a real terminal for the arrow-key list; over a pipe it falls back to a
numbered prompt. Enable per-run with `--select` or `select = true` in the config.

## depclean

`gup depclean` (or the `--depclean` step) removes packages nothing in `@world`
needs. It pretends first, shows the count, and removes nothing until you confirm.
`emerge` still refuses anything that would break a reverse dependency. Read the
list — depclean removes things you use but never added to `@world`.

## Configuration

Set defaults in a config file instead of repeating flags. Read in order, each
overriding the last; command-line flags win:

1. `/etc/gentoo-updater.toml`
2. `~/.config/gentoo-updater/config.toml`
3. command-line flags

Keys match the flag names. Starter file: [`contrib/gentoo-updater.toml.example`](contrib/gentoo-updater.toml.example).

```toml
yes           = false
no_snapshot   = false
select        = false
verbose       = false   # stream full output instead of the quiet dashboard
depclean      = false
low_space_gib = 5.0
notify        = "reboot"   # never | failure | reboot | always
notify_email  = ""
audit         = true
```

Requires Python 3.11+ (for stdlib `tomllib`). A missing or malformed config
file falls back to the built-in defaults.

## Audit log & notifications

Each `update`/`depclean` run appends a JSON-Lines record to
`/var/log/gentoo-updater/history.jsonl` (or `~/.local/state/…` if that isn't
writable): timestamp, per-phase results, snapshot id, reboot advice, package
count. Disable with `--no-audit`.

Notifications fire on completion when configured (`--notify`): `failure` on a
failed run, `reboot` also when a reboot is advised, `always` every time. Sent via
`notify-send` and/or email (`sendmail`/`mail` to `notify_email`).

Every run also writes a **debug log** (each command, its exit code and timing,
per-phase results) to `/var/log/gentoo-updater/debug.log`, or `~/.local/state/…`
when not root. It's overwritten each run and its path is printed at the end — the
first place to look if a run hangs or misbehaves.

## Unattended updates (systemd / OpenRC / runit)

Only the scheduling differs per init. `gup install-schedule` detects your init
and installs the right thing (as root; `--dry-run` prints it). Override with
`--init`, set cadence with `--schedule`.

| Init | Installs | Activate |
| --- | --- | --- |
| systemd | `.service` + `.timer` in `/etc/systemd/system` | `systemctl enable --now gentoo-updater.timer` |
| OpenRC | `/etc/cron.daily/gentoo-updater` | run a cron daemon (e.g. cronie) |
| runit | `/etc/sv/gentoo-updater/run` (run-then-sleep loop) | `ln -s /etc/sv/gentoo-updater /var/service/` |

```bash
gup install-schedule                 # auto-detect
gup install-schedule --init runit    # or force one
gup install-timer                    # = --init systemd
```

Each just runs `gup -y --no-sudo`. Reference copies in [`contrib/`](contrib/).
OpenRC and runit have no timer of their own, which is why they use cron / a
supervised loop. Pair it with `notify = "failure"` so unattended failures reach
you.

## Safety notes

- Never auto-merges config files, even unattended — it reports pending ones.
- Never edits `/etc/portage`; it shows the lines, you apply them.
- A single-instance lock prevents two runs colliding.
- `--dry-run` runs no mutating command (snapshot creation included) and skips the
  audit write and notifications. Read-only checks still run, so it reflects real
  state. (It still writes the debug log — that's a diagnostic, not a system change.)
  Phases that would have mutated (`apply`, and anything requiring root) report
  `skipped` rather than `ok`, since nothing actually ran.
- depclean is opt-in and always asks before removing.

## Status

v0.4 — single-machine Gentoo. Quiet live dashboard with per-package merge
progress (`--verbose` for full output), package picker, pre-update snapshot with
rollback, config file, audit log, notifications, multi-init scheduling, opt-in
depclean, and a guard against running under `sudo`. Multi-distro/fleet is out of
scope for now.

## License

MIT.
