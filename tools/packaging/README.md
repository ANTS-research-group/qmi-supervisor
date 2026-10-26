# Debian package

One `.deb` per release, built from a git tag. The application is Python plus a
static frontend export, so the package is `Architecture: all`: the same file
installs on armhf (Mochabin), arm64 and amd64.

## Build

    ./tools/packaging/build-deb.sh            # version from git describe
    ./tools/packaging/build-deb.sh 1.0.0-2    # explicit version
    REVISION=2 ./tools/packaging/build-deb.sh # packaging-only revision

The script checks out the tree already in place, so commit before building. It
builds `frontend/out` when the export is missing and npm is available, stages
the application, the systemd unit, the udev rule and the wrappers, and writes
`build/qmi-supervisor_<version>_all.deb`. Node is only needed on the build
machine.

Version derivation (`git describe --tags --long --dirty`):

| git describe | package version |
| --- | --- |
| `v1.2.3-0-gabc1234` | `1.2.3-1` |
| `v1.2.3-4-gabc1234` | `1.2.3+4.gabc1234-1` |
| `v1.2.3-0-gabc1234-dirty` | `1.2.3.dirty-1` |
| `v1.3.0-rc1-0-gabc1234` | `1.3.0~rc1-1` (only with an explicit tag name) |

`REVISION` (or the `-N` suffix) distinguishes packaging-only rebuilds of the
same code, which is what makes `dpkg` see an upgrade.

## Install, upgrade, reinstall, remove

    sudo apt install ./qmi-supervisor_1.2.3-1_all.deb   # install or upgrade, resolves dependencies
    sudo apt --reinstall install ./qmi-supervisor_1.2.3-1_all.deb
    sudo apt install --allow-downgrades ./qmi-supervisor_1.2.2-1_all.deb
    sudo apt remove qmi-supervisor                       # keeps INI and captures
    sudo apt purge qmi-supervisor                        # deletes INI and /var/lib/qmi-supervisor

Upgrades preserve `/etc/qmi-supervisor.ini` and `/var/lib/qmi-supervisor`. The
INI is not a conffile: the package creates it on first configure and the
application owns it afterwards, so dpkg never prompts and never restores an old
copy over newer settings.

## What the package does and does not do

- Installs the application in `/opt/qmi-supervisor`, the unit in
  `/etc/systemd/system/qmi-supervisor.service`, the rule
  `/etc/udev/rules.d/85-qmi-supervisor.rules` and the CLI wrappers in
  `/usr/local/sbin`.
- `postinst` creates the configuration with the web interface enabled, a
  generated admin password printed once, idle objectives and captures under
  `/var/lib/qmi-supervisor` (the same first-run state as
  `tools/deployment/install.sh`). It reloads systemd and udev, enables the unit
  on first install and restarts it on every configure.
- `preinst`, on a first install, moves an existing hand-installed tree to
  `/opt/qmi-supervisor.preinst-<timestamp>` (with copies of the unit and
  wrappers) instead of overwriting it silently. The INI is never touched.
- It never changes ModemManager or NetworkManager policy, never installs
  packages and never sets a web password. Those remain explicit
  `qmi-supervisor-configure` decisions.
- `purge` removes the configuration and every capture under
  `/var/lib/qmi-supervisor`; copy what you need first.

## Release flow

1. Commit, then `git tag -a v1.2.3 -m "qmi-supervisor 1.2.3"` and push the tag.
2. `.github/workflows/package-deb.yml` builds the frontend and the `.deb` on the
   tag and attaches it to the GitHub release (and as a workflow artifact).
3. On each device: `scp` the `.deb` and `sudo apt install ./qmi-supervisor_1.2.3-1_all.deb`.

Without CI: build locally with the same command and keep the `.deb` next to the
tag, or publish it in a small APT repository (`dpkg-scanpackages`) so devices can
`apt upgrade`.
