#!/bin/sh
# Local tests only: write SHA256SUMS for every .deb in DIR and sign it with the key in $GNUPGHOME.
set -eu
dir=${1:?usage: sign_local_release.sh RELEASE_DIR}
: "${GNUPGHOME:?set GNUPGHOME to the local test key directory}"
cd "$dir"
sha256sum -- *.deb > SHA256SUMS
gpg --batch --yes --pinentry-mode loopback --passphrase '' --armor --detach-sign --output SHA256SUMS.asc SHA256SUMS
