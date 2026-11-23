#!/bin/sh
# Create the qmi-supervisor release signing key. Run it once, on the maintainer's
# own computer: never on a device, never in CI. It uploads nothing.
#
#   ./tools/packaging/make-signing-key.sh [--home DIR] [--keyring FILE] [--uid TEXT]
#
#   --home DIR      new GnuPG home that keeps the private key
#                   (default ~/.qmi-supervisor-release-key; must be absent or empty)
#   --keyring FILE  where the public keyring is written
#                   (default tools/packaging/keys/release-signing.gpg; must be absent)
#   --uid TEXT      user ID of the key (default "qmi-supervisor release signing key")
#
# The passphrase is asked twice on the terminal, or read as one line from stdin
# when stdin is not a terminal. The script prints the fingerprint and the exact
# steps that add the private key and the passphrase as GitHub Actions secrets.
set -eu
umask 077

project=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
home=${HOME:?HOME is not set}/.qmi-supervisor-release-key
keyring=$project/tools/packaging/keys/release-signing.gpg
uid='qmi-supervisor release signing key'
repository=ANTS-research-group/qmi-supervisor

fail() { echo "make-signing-key: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case $1 in
        --home) [ $# -ge 2 ] || fail "--home needs a directory"; home=$2; shift 2 ;;
        --keyring) [ $# -ge 2 ] || fail "--keyring needs a file"; keyring=$2; shift 2 ;;
        --uid) [ $# -ge 2 ] || fail "--uid needs a value"; uid=$2; shift 2 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "unknown option: $1 (see --help)" ;;
    esac
done

command -v gpg >/dev/null 2>&1 || fail "gpg is missing; install it with: sudo apt install gnupg"
case $home in /*) ;; *) home=$PWD/$home ;; esac
case $keyring in /*) ;; *) keyring=$PWD/$keyring ;; esac
if [ -e "$home" ] && [ -n "$(ls -A "$home" 2>/dev/null)" ]; then
    fail "$home already exists and is not empty: a key was probably created before. Keep using it, or pass --home with a new directory"
fi
[ ! -e "$keyring" ] || fail "$keyring already exists; to replace or add a key follow tools/packaging/README.md (Signing key)"

if [ -t 0 ]; then
    stty -echo
    trap 'stty echo' EXIT INT TERM
    printf 'Passphrase for the new release signing key (at least 12 characters): ' >&2
    IFS= read -r passphrase
    printf '\nRepeat the passphrase: ' >&2
    IFS= read -r again
    printf '\n' >&2
    stty echo
    trap - EXIT INT TERM
    [ "$passphrase" = "$again" ] || fail "the passphrases differ"
else
    IFS= read -r passphrase || true
fi
[ ${#passphrase} -ge 12 ] || fail "the passphrase must have at least 12 characters"

mkdir -p "$home"
chmod 0700 "$home"
GNUPGHOME=$home
export GNUPGHOME
trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true' EXIT INT TERM
printf '%s' "$passphrase" | LC_ALL=C gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 0 \
    --quick-generate-key "$uid" ed25519 sign never
fingerprint=$(LC_ALL=C gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" {print $10; exit}')
[ -n "$fingerprint" ] || fail "gpg did not report the new key"

mkdir -p "$(dirname -- "$keyring")"
LC_ALL=C gpg --batch --export --export-options export-minimal "$fingerprint" > "$keyring.tmp"
mv "$keyring.tmp" "$keyring"
chmod 0644 "$keyring"

cat <<STEPS

Release signing key created. Nothing was uploaded.

  Fingerprint:     $fingerprint
  Private key:     $home
                   Back up this directory offline: it also holds the revocation
                   certificate openpgp-revocs.d/$fingerprint.rev. Never commit it.
  Public keyring:  $keyring (commit it)

Next steps:

1. In INSTALLATION.md replace the line
     Release signing key fingerprint: not published yet.
   with
     Release signing key fingerprint: \`$fingerprint\`.
   and commit both files:
     git add tools/packaging/keys/release-signing.gpg INSTALLATION.md
     git commit -m "Ship the release signing public key"

2. Add two repository secrets on GitHub, in $repository:
   Settings > Secrets and variables > Actions > New repository secret.
     RELEASE_SIGNING_PASSPHRASE  the passphrase you just typed
     RELEASE_SIGNING_KEY         the ASCII-armoured private key: write it to a private
                                 file (gpg asks for the passphrase), paste the whole file,
                                 BEGIN and END lines included, then delete the file:
       (umask 077; GNUPGHOME="$home" gpg --pinentry-mode loopback --armor --export-secret-keys $fingerprint > "\$HOME/release-signing-key.asc")
       shred -u "\$HOME/release-signing-key.asc"
   With the GitHub CLI instead (nothing is written to disk):
       GNUPGHOME="$home" gpg --pinentry-mode loopback --armor --export-secret-keys $fingerprint | gh secret set RELEASE_SIGNING_KEY --repo $repository
       gh secret set RELEASE_SIGNING_PASSPHRASE --repo $repository

3. Push main and a pre-release tag, then check the release as described in
   tools/packaging/README.md (Release flow):
       git tag -a v1.3.0-rc1 -m "qmi-supervisor 1.3.0-rc1"
       git push origin main v1.3.0-rc1
STEPS
