#!/bin/sh
# Sign the checksum file of a release (run by .github/workflows/package-deb.yml).
#
#   RELEASE_SIGNING_KEY=<ASCII-armoured private key> \
#   RELEASE_SIGNING_PASSPHRASE=<passphrase> \
#   ./tools/packaging/sign-release.sh build/release/SHA256SUMS
#
# Writes <file>.asc, an ASCII-armoured detached signature, and checks it with gpgv
# against tools/packaging/keys/release-signing.gpg: the public keyring that the
# package installs as /opt/qmi-supervisor/keys/release-signing.gpg and that devices
# use to verify updates. The private key lives only in a temporary GnuPG home that
# is deleted on exit. RELEASE_KEYRING overrides the keyring path (tests only).
#
# No keyring committed and no RELEASE_SIGNING_KEY: nothing is signed (warning, exit 0);
# devices with update.require_signature = true refuse such a release.
# Keyring committed but no key, or key but no keyring, or a key that does not match
# the keyring: error, so the workflow stops before anything is published.
set -eu

project=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
keyring=${RELEASE_KEYRING:-$project/tools/packaging/keys/release-signing.gpg}
key=${RELEASE_SIGNING_KEY:-}
passphrase=${RELEASE_SIGNING_PASSPHRASE:-}

fail() { echo "sign-release: $*" >&2; exit 1; }

[ $# -eq 1 ] || fail "usage: sign-release.sh <SHA256SUMS file>"
sums=$1
[ -s "$sums" ] || fail "$sums is missing or empty"
case $keyring in /*) ;; *) keyring=$PWD/$keyring ;; esac

if [ ! -f "$keyring" ]; then
    [ -z "$key" ] || fail "RELEASE_SIGNING_KEY is set but $keyring is not committed; commit the public keyring made by tools/packaging/make-signing-key.sh first"
    echo "::warning::Release not signed: tools/packaging/keys/release-signing.gpg is not committed and RELEASE_SIGNING_KEY is not set. Devices with update.require_signature = true will refuse it."
    exit 0
fi
[ -n "$key" ] || fail "$keyring is committed but the RELEASE_SIGNING_KEY secret is empty; add it (tools/packaging/README.md, Signing key)"
command -v gpg >/dev/null 2>&1 || fail "gpg is missing"
command -v gpgv >/dev/null 2>&1 || fail "gpgv is missing"

GNUPGHOME=$(mktemp -d "${TMPDIR:-/tmp}/qmi-sign.XXXXXX")
export GNUPGHOME
trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"; rm -f "$sums.asc.tmp"' EXIT INT TERM
chmod 0700 "$GNUPGHOME"

printf '%s\n' "$key" | LC_ALL=C gpg --batch --quiet --import
printf '%s' "$passphrase" | LC_ALL=C gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 0 \
    --armor --detach-sign --output "$sums.asc.tmp" "$sums"
LC_ALL=C gpgv --keyring "$keyring" "$sums.asc.tmp" "$sums" \
    || fail "the signature does not verify with $keyring: the RELEASE_SIGNING_KEY secret and the committed keyring belong to different keys"
mv "$sums.asc.tmp" "$sums.asc"
echo "sign-release: wrote $sums.asc"
