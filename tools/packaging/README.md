# Debian package

One `.deb` per release, built from a git tag. The application is Python plus a
static frontend export, so the package is `Architecture: all`: the same file
installs on armhf (Mochabin), arm64 and amd64.

## Build

    ./tools/packaging/build-deb.sh            # version from git describe
    ./tools/packaging/build-deb.sh 1.0.0-2    # explicit version
    REVISION=2 ./tools/packaging/build-deb.sh # packaging-only revision
    ./tools/packaging/build-deb.sh --print-version # print the version and exit

The script checks out the tree already in place, so commit before building. It
builds `frontend/out` when the export is missing, was built for another version
(`frontend/out/.version` differs), or carries no version marker, and npm is
available; it stages the application, the systemd unit, the udev rule, the wrappers and, when it is
committed, the release signing keyring (`keys/release-signing.gpg`), and writes
`build/qmi-supervisor_<version>_all.deb`. When it builds
the export it sets `QMI_SUPERVISOR_VERSION=<version>`, which the frontend embeds
as `NEXT_PUBLIC_BUILD_VERSION`. A prebuilt `frontend/out` therefore has to be
rebuilt for a new version, or the account menu would keep showing the old one;
the script does that automatically when npm is present. Node is only needed on
the build machine.

The staged application carries `VERSION` (the package version, one line) and
`BUILD_INFO.json` (`{"version", "commit", "built_utc"}`) next to
`qmi_supervisor.py`, so the running supervisor reports exactly what was built.

Version derivation (`git describe --tags --long --dirty`):

| git describe | package version |
| --- | --- |
| `v1.2.3-0-gabc1234` | `1.2.3-1` |
| `v1.2.3-4-gabc1234` | `1.2.3+4.gabc1234-1` |
| `v1.2.3-0-gabc1234-dirty` | `1.2.3.dirty-1` |
| `v1.3.0-rc1-0-gabc1234` | `1.3.0~rc1-1` |
| `v1.3.0-rc1-2-gabc1234` | `1.3.0~rc1+2.gabc1234-1` |

A `-` in the tag becomes `~`, which sorts before the release:
`1.3.0~rc1-1 < 1.3.0~rc2-1 < 1.3.0-1 < 1.3.0+1.gabc1234-1`.
The same mapping is implemented in
`modules/runtime/version.py` (`describe_to_version`) for a development checkout.

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
- Installs the public keyring `/opt/qmi-supervisor/keys/release-signing.gpg`,
  which the updater uses to verify releases (when the keyring was committed at
  build time).
- Installs the updater `qmi-supervisor-update` in `/usr/local/sbin` and the units
  `qmi-supervisor-update-check.service`, `qmi-supervisor-update-check.timer` and the on-demand
  `qmi-supervisor-update-check-manual.service` (the web interface's "Check now") next to the service unit.
  The on-demand units `qmi-supervisor-update-install@.service` (one instance per version) and
  `qmi-supervisor-update-rollback.service` run an installation outside `qmi-supervisor.service`, so the
  restart a package performs cannot kill it. They are never enabled; the updater, the web interface and
  the timer start them with `systemctl start --no-block`.
  `postinst` enables and starts the timer on every configure; it runs the check 2 minutes after boot and then
  about every 30 minutes, contacts the release source at most every `update.check_interval_hours` and never when
  `update.mode = off`. `systemctl mask qmi-supervisor-update-check.timer` stops it for good; `disable` is undone by
  the next installation. `purge` also removes `/etc/qmi-supervisor-update-token`.
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

Tags are `vMAJOR.MINOR.PATCH` for releases and `vMAJOR.MINOR.PATCH-rcN` (N from 1) for release candidates; the workflow refuses any other tag before building.

| Tag | GitHub release | Package version | Package file on the release | Offered by the updater |
| --- | --- | --- | --- | --- |
| `v1.3.0` | Release | `1.3.0-1` | `qmi-supervisor_1.3.0-1_all.deb` | To every host; installed automatically in `auto` mode |
| `v1.3.0-rc1` | Pre-release | `1.3.0~rc1-1` | `qmi-supervisor_1.3.0.rc1-1_all.deb` | Only where pre-releases are enabled; never automatically |

1. Commit on `main` and run the checks.
2. Tag and push: `git tag -a v1.3.0 -m "qmi-supervisor 1.3.0"`, then `git push origin main v1.3.0`.
3. `.github/workflows/package-deb.yml` checks the tag name, builds the frontend and the package, checks that the package version matches the tag and writes `SHA256SUMS` (`stage-release.sh`), signs it as `SHA256SUMS.asc` and verifies that signature against `tools/packaging/keys/release-signing.gpg` (`sign-release.sh`), then attaches the three files to the release. A tag that contains `-` makes it a pre-release. The `.deb` is also kept as a workflow artifact.
4. Write the release notes on the release page; the updater shows them in System → Version and updates.
5. Hosts see the release at their next check ([Updates](../../INSTALLATION.md#updates)). A host without network access installs the `.deb` by hand after checking it.

### Release files

The updater and people installing by hand rely on this layout:

- exactly one `qmi-supervisor_<version>_all.deb`, where every `~` of the version is written as `.` (GitHub renames `~` in asset names); the package's own `Version` field keeps the `~`;
- `SHA256SUMS`: one line per package, `<sha256>  <file name>`, as `sha256sum` prints it;
- `SHA256SUMS.asc`: an ASCII-armoured detached GPG signature of `SHA256SUMS`, made with a key of `tools/packaging/keys/release-signing.gpg`;
- the package `Version` without its revision (the part after the last `-`) equals the tag without `v`, with `-` written as `~`.

Releases 1.2.0 and older have only the `.deb`.

### Signing key

The release key signs `SHA256SUMS` in CI. Its public half is committed as `tools/packaging/keys/release-signing.gpg` and installed by every package as `/opt/qmi-supervisor/keys/release-signing.gpg`. Its private half and passphrase exist only on the maintainer's computer, in an offline backup and as the GitHub Actions secrets `RELEASE_SIGNING_KEY` and `RELEASE_SIGNING_PASSPHRASE`.

Set it up once, on the maintainer's computer:

    ./tools/packaging/make-signing-key.sh

It creates an ed25519 key without expiry in `~/.qmi-supervisor-release-key` (`--home` chooses another place, for example an encrypted USB drive), writes the public keyring and prints the fingerprint and the exact next steps: publish the fingerprint in INSTALLATION.md, commit the keyring, add the two secrets and back up the key directory offline. It uploads nothing.

What `sign-release.sh` does in the workflow:

| Keyring committed | Secrets | Result |
| --- | --- | --- |
| No | Not set | Published unsigned, with a warning; hosts that require signatures refuse it |
| Yes | Set, same key | Signed, and the signature is checked with `gpgv` against the committed keyring |
| Yes | Not set, or another key | The workflow fails before publishing |
| No | Set | The workflow fails before publishing |

Rotation: create the new key with `./tools/packaging/make-signing-key.sh --home ~/.qmi-supervisor-release-key-2 --keyring /tmp/release-key-2.gpg`, append it with `cat /tmp/release-key-2.gpg >> tools/packaging/keys/release-signing.gpg`, list both fingerprints in INSTALLATION.md (``Release signing key fingerprints: `<old>`, `<new>`.``) and publish a release that is still signed with the old key. Once hosts run that release, replace both secrets with the new key; a later release, signed with the new key, removes the old one from the keyring and from INSTALLATION.md.

If the private key or the GitHub account may be compromised: delete both secrets, switch hosts in `auto` mode to `notify`, rotate the key as above and compare the fingerprint on each host.

Optional hardening: restrict who can push `v*` tags with a tag ruleset, and keep both secrets in a GitHub environment with required reviewers (add `environment: <name>` to the `deb` job) so that every signed release needs an approval.

Without CI: build with `./tools/packaging/build-deb.sh`, run `./tools/packaging/stage-release.sh build build/release`, sign with `gpg --homedir ~/.qmi-supervisor-release-key --armor --detach-sign build/release/SHA256SUMS` and upload the files of `build/release` to the release. A small APT repository (`dpkg-scanpackages`) is still possible for hosts that should use `apt upgrade`.
