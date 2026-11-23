#!/bin/sh
# Prepare the files of a GitHub release (run by .github/workflows/package-deb.yml).
#
#   ./tools/packaging/stage-release.sh build build/release
#
# Copies the single build/qmi-supervisor_<version>_all.deb into the release directory
# under a name GitHub keeps unchanged (GitHub renames "~" in asset names to "."), and
# writes SHA256SUMS there ("<sha256>  <asset name>" per line, as sha256sum prints it).
# In a tag build (GITHUB_REF_TYPE=tag) the package version must match the tag:
# v1.3.0 -> 1.3.0-<revision>, v1.3.0-rc1 -> 1.3.0~rc1-<revision>.
set -eu

fail() { echo "stage-release: $*" >&2; exit 1; }

[ $# -eq 2 ] || fail "usage: stage-release.sh <build directory> <release directory>"
build=$1
release=$2

set -- "$build"/qmi-supervisor_*_all.deb
[ $# -eq 1 ] && [ -f "$1" ] || fail "expected exactly one qmi-supervisor_*_all.deb in $build"
deb=$1
version=$(dpkg-deb --field "$deb" Version)
[ -n "$version" ] || fail "$deb has no Version field"

if [ "${GITHUB_REF_TYPE:-}" = tag ]; then
    upstream=$(printf '%s' "${GITHUB_REF_NAME#v}" | tr '-' '~')
    [ "${version%-*}" = "$upstream" ] || fail "package version $version does not match tag $GITHUB_REF_NAME (expected $upstream-<revision>)"
fi

name=$(basename "$deb" | tr '~' '.')
case $name in
    *[!A-Za-z0-9._+-]*) fail "unexpected character in $name" ;;
esac

rm -rf "$release"
mkdir -p "$release"
cp "$deb" "$release/$name"
( cd "$release" && sha256sum "$name" > SHA256SUMS )
cat "$release/SHA256SUMS"
