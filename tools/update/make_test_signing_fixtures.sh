#!/bin/sh
# Regenerate the committed signature fixtures of the updater tests. Needs gpg once;
# the tests only need gpgv. Private keys live in temporary GNUPGHOME directories and are deleted.
set -eu
out=${1:-tests/fixtures/update}
mkdir -p "$out"
work=$(mktemp -d "${TMPDIR:-/tmp}/qmi-update-fixtures.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM
mkdir -m 700 "$work/main" "$work/other"
gen() { GNUPGHOME=$1 gpg --batch --quiet --pinentry-mode loopback --passphrase '' --quick-gen-key "$2" ed25519 sign never; }
sign() { GNUPGHOME=$1 gpg --batch --quiet --yes --pinentry-mode loopback --passphrase '' --armor --detach-sign --output "$2" "$3"; }
gen "$work/main" 'qmi-supervisor TEST signing key (tests only) <test-signing@invalid>'
gen "$work/other" 'qmi-supervisor OTHER test key (tests only) <other-signing@invalid>'
GNUPGHOME=$work/main gpg --batch --export > "$out/test-keyring.gpg"
GNUPGHOME=$work/main gpg --batch --with-colons --list-keys | awk -F: '$1 == "fpr" {print $10; exit}' > "$out/fingerprint.txt"
printf 'qmi-supervisor test package 9.9.0-1\n' > "$out/qmi-supervisor_9.9.0-1_all.deb"
( cd "$out" && sha256sum qmi-supervisor_9.9.0-1_all.deb > SHA256SUMS )
sign "$work/main" "$out/SHA256SUMS.asc" "$out/SHA256SUMS"
sign "$work/other" "$out/SHA256SUMS.other.asc" "$out/SHA256SUMS"
sed 's/9\.9\.0-1/9.9.1-1/' "$out/SHA256SUMS" > "$out/SHA256SUMS.tampered"
echo "fixtures written to $out (fingerprint $(cat "$out/fingerprint.txt"))"
