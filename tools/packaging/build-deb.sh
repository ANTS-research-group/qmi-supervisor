#!/bin/sh
# Build an installable .deb for qmi-supervisor.
#
# The application is Python plus a static frontend export, so the package is
# Architecture: all: the same file installs on armhf, arm64 and amd64. Only the
# frontend export needs Node, and only on the machine that builds the package.
#
#   ./tools/packaging/build-deb.sh                 # version from git describe
#   ./tools/packaging/build-deb.sh 1.2.3-2         # explicit version
#   REVISION=2 ./tools/packaging/build-deb.sh      # packaging-only revision
#   ./tools/packaging/build-deb.sh --print-version # print the version and exit
set -eu

project=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$project"
packaging=$project/tools/packaging/debian
output=${OUTPUT_DIR:-$project/build}
app=/opt/qmi-supervisor

fail() { echo "build-deb: $*" >&2; exit 1; }

# ---- version ---------------------------------------------------------------
derive_version() {
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "0.0.0+local$(date -u +%Y%m%d%H%M)-1"
        return
    fi
    raw=$(git describe --tags --long --dirty 2>/dev/null) || raw=
    if [ -z "$raw" ]; then
        echo "0.0.0+git$(git rev-parse --short HEAD)-1"
        return
    fi
    dirty=
    case $raw in
        *-dirty) dirty='.dirty'; raw=${raw%-dirty} ;;
    esac
    hash=${raw##*-g}
    rest=${raw%-g"$hash"}
    count=${rest##*-}
    tagged=${rest%-*}
    # A hyphen inside the tag (v1.3.0-rc1) sorts before the release: use a tilde.
    base=$(printf '%s' "${tagged#v}" | tr '-' '~')
    if [ "$count" != 0 ]; then
        base="$base+$count.g$hash"
    fi
    echo "$base$dirty-${REVISION:-1}"
}

print_version=
if [ "${1:-}" = --print-version ]; then
    print_version=1
    shift
fi
if [ $# -ge 1 ]; then
    version=$1
else
    version=$(derive_version)
fi
case $version in
    [0-9]*) ;;
    *) fail "version must start with a digit: $version" ;;
esac
if [ -n "$print_version" ]; then
    echo "$version"
    exit 0
fi

# ---- frontend export -------------------------------------------------------
# The export carries the version it was built with in out/.version. A prebuilt
# export from another version (or an export built by an older version) must be rebuilt so
# the account menu and /api/status agree on what is installed.
export_file=$project/frontend/out/index.html
export_version=$project/frontend/out/.version
needs_export=0
if [ ! -f "$export_file" ]; then
    needs_export=1
elif [ ! -f "$export_version" ]; then
    needs_export=1
elif [ "$(cat "$export_version")" != "$version" ]; then
    needs_export=1
fi
if [ "$needs_export" = 1 ]; then
    if command -v npm >/dev/null 2>&1; then
        echo "build-deb: building the static frontend export for version $version"
        export QMI_SUPERVISOR_VERSION="$version"
        npm --prefix frontend ci
        npm --prefix frontend run build
        printf '%s\n' "$version" > "$export_version"
    elif [ ! -f "$export_file" ]; then
        fail "frontend/out is missing and npm is unavailable; build the export on a machine with Node 22+ (npm --prefix frontend ci && npm --prefix frontend run build)"
    else
        fail "frontend/out was not built for $version (out/.version is missing or different) and npm is unavailable; rebuild it with QMI_SUPERVISOR_VERSION=$version npm --prefix frontend run build"
    fi
fi
[ -f "$project/frontend/out/index.html" ] || fail "the static frontend export is incomplete"

# ---- staging ---------------------------------------------------------------
stage=$(mktemp -d "${TMPDIR:-/tmp}/qmi-supervisor-deb.XXXXXX")
trap 'rm -rf "$stage"' EXIT INT TERM
mkdir -p "$stage/DEBIAN" "$stage$app" "$stage/etc/systemd/system" \
    "$stage/etc/udev/rules.d" "$stage/usr/local/sbin" "$stage/usr/share/doc/qmi-supervisor"

cp -a "$project/qmi_supervisor.py" "$stage$app/"
for name in modules tools config; do
    cp -a "$project/$name" "$stage$app/$name"
done
mkdir -p "$stage$app/frontend"
cp -a "$project/frontend/out" "$stage$app/frontend/out"
for name in README.md INSTALLATION.md CONFIGURATION.md LICENSE; do
    [ -f "$project/$name" ] && cp -a "$project/$name" "$stage$app/$name"
done
find "$stage$app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$stage$app" -name '*.pyc' -delete 2>/dev/null || true

# Public keyring that verifies signed releases (made by tools/packaging/make-signing-key.sh).
keyring=$project/tools/packaging/keys/release-signing.gpg
if [ -f "$keyring" ]; then
    mkdir -p "$stage$app/keys"
    cp "$keyring" "$stage$app/keys/release-signing.gpg"
else
    echo "build-deb: warning: tools/packaging/keys/release-signing.gpg is missing; this package cannot verify signed updates" >&2
fi

# What the running application reports as its version, and how it was built.
printf '%s\n' "$version" > "$stage$app/VERSION"
commit=$(git rev-parse --short HEAD 2>/dev/null) || commit=
built_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
json_version=$(printf '%s' "$version" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{"version": "%s", "commit": "%s", "built_utc": "%s"}\n' \
    "$json_version" "$commit" "$built_utc" > "$stage$app/BUILD_INFO.json"

cp "$project/tools/deployment/qmi-supervisor.service" "$stage/etc/systemd/system/qmi-supervisor.service"
for unit in qmi-supervisor-update-check.service qmi-supervisor-update-check-manual.service qmi-supervisor-update-check.timer; do
    cp "$project/tools/update/$unit" "$stage/etc/systemd/system/$unit"
done
cp "$project/tools/deployment/qmi-supervisor-update-install@.service" "$stage/etc/systemd/system/"
cp "$project/tools/deployment/qmi-supervisor-update-rollback.service" "$stage/etc/systemd/system/"
cat > "$stage/etc/udev/rules.d/85-qmi-supervisor.rules" <<'RULE'
# QMI-Supervisor owns QMI USB network interfaces only.
SUBSYSTEM=="net", DRIVERS=="qmi_wwan", ENV{NM_UNMANAGED}="1"
RULE

cat > "$stage/usr/local/sbin/qmi-supervisor-configure" <<'WRAPPER'
#!/bin/sh
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 /opt/qmi-supervisor/tools/deployment/setup.py configure "$@"
WRAPPER
cat > "$stage/usr/local/sbin/qmi-supervisor-uninstall" <<'WRAPPER'
#!/bin/sh
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 /opt/qmi-supervisor/tools/deployment/setup.py uninstall "$@"
WRAPPER
cat > "$stage/usr/local/sbin/qmi-supervisor-update" <<'WRAPPER'
#!/bin/sh
exec /usr/bin/python3 -I -B /opt/qmi-supervisor/tools/update/updater.py "$@"
WRAPPER
chmod 0755 "$stage/usr/local/sbin/qmi-supervisor-configure" "$stage/usr/local/sbin/qmi-supervisor-uninstall" "$stage/usr/local/sbin/qmi-supervisor-update"

cp -a "$project/README.md" "$stage/usr/share/doc/qmi-supervisor/README.md"
cp -a "$project/CONFIGURATION.md" "$stage/usr/share/doc/qmi-supervisor/CONFIGURATION.md"
cp -a "$project/INSTALLATION.md" "$stage/usr/share/doc/qmi-supervisor/INSTALLATION.md"

# Normalise ownership-independent modes: readable files, no group write.
chmod -R u+rwX,go+rX,go-w "$stage"
chmod 0755 "$stage/usr/local/sbin/qmi-supervisor-configure" "$stage/usr/local/sbin/qmi-supervisor-uninstall" "$stage/usr/local/sbin/qmi-supervisor-update"

# ---- metadata --------------------------------------------------------------
installed_size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
sed -e "s|@VERSION@|$version|" -e "s|@INSTALLED_SIZE@|$installed_size|" \
    "$packaging/control.in" > "$stage/DEBIAN/control"
cp "$packaging/preinst" "$packaging/postinst" "$packaging/prerm" "$packaging/postrm" "$stage/DEBIAN/"
chmod 0755 "$stage/DEBIAN/preinst" "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm" "$stage/DEBIAN/postrm"
( cd "$stage" && find . -path ./DEBIAN -prune -o -type f -print | sed 's|^\./||' | while read -r file; do
    printf '%s  %s\n' "$(md5sum "$file" | cut -d' ' -f1)" "$file"
done ) > "$stage/DEBIAN/md5sums"
chmod 0644 "$stage/DEBIAN/md5sums"

# ---- package ---------------------------------------------------------------
mkdir -p "$output"
package=$output/qmi-supervisor_${version}_all.deb
dpkg-deb --build --root-owner-group "$stage" "$package" >/dev/null
dpkg-deb --info "$package" | sed -n '1,12p'
printf '\n%s\n' "built: $package"
printf 'sha256: %s\n' "$(sha256sum "$package" | cut -d' ' -f1)"
