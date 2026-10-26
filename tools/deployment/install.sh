#!/bin/sh
# Bootstrap requires only the shell and apt; never install packages during a dry run.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
preview=false
skip_packages=false
doctor=false
for option in "$@"; do
    case "$option" in --purge) echo '--purge is only valid with tools/deployment/uninstall.sh.' >&2; exit 2 ;; esac
    case "$option" in --dry-run|--doctor|--help|-h) preview=true ;; esac
    case "$option" in --skip-packages) skip_packages=true ;; --doctor) doctor=true ;; esac
done
if [ ! -r /etc/os-release ]; then
    echo 'Cannot identify the operating system (/etc/os-release missing).' >&2
    exit 1
fi
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in
    *' debian '*|*' ubuntu '*) ;;
    *) echo 'This installer supports Debian/Ubuntu with systemd.' >&2; exit 1 ;;
esac
if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    if "$skip_packages" || "$doctor"; then
        echo 'Python >= 3.10 is missing; no packages were installed.' >&2
        exit 1
    fi
    case "${ID:-}:${VERSION_ID:-}" in
        ubuntu:2[2-9].*|ubuntu:[3-9][0-9].*|debian:1[2-9]*|debian:[2-9][0-9]*) ;;
        *) echo 'Python >= 3.10 is required; use Debian >= 12 or Ubuntu >= 22.04.' >&2; exit 1 ;;
    esac
    if "$preview"; then
        echo 'Python >= 3.10 is missing. Bootstrap plan (no changes made):'
        echo '  apt-get update'
        echo '  apt-get install -y --no-remove python3'
        echo 'Then the installer validates configuration, installs QMI dependencies and deploys the service.'
        exit 0
    fi
    if [ "$(id -u)" -ne 0 ]; then echo 'Run with sudo.' >&2; exit 1; fi
    command -v apt-get >/dev/null
    command -v dpkg-query >/dev/null
    command -v systemctl >/dev/null
    if [ ! -d /run/systemd/system ]; then echo 'systemd must be running on the target.' >&2; exit 1; fi
    bootstrap_receipt_dir=/var/lib/qmi-supervisor-install
    if [ -L "$bootstrap_receipt_dir" ]; then
        echo 'Refusing a symlinked Python bootstrap receipt directory.' >&2
        exit 1
    fi
    mkdir -p -m 700 -- "$bootstrap_receipt_dir"
    chmod 700 -- "$bootstrap_receipt_dir"
    for bootstrap_inventory in before after; do
        if [ -e "$bootstrap_receipt_dir/bootstrap-packages.$bootstrap_inventory" ] || [ -L "$bootstrap_receipt_dir/bootstrap-packages.$bootstrap_inventory" ]; then
            echo 'An unconsumed Python bootstrap inventory exists; retain it for recovery before retrying.' >&2
            exit 1
        fi
    done
    bootstrap_snapshot() {
        bootstrap_temporary=$(mktemp "$bootstrap_receipt_dir/.bootstrap-XXXXXX")
        if dpkg-query -W '-f=${binary:Package}\t${Status}\n' > "$bootstrap_temporary"; then
            chmod 600 -- "$bootstrap_temporary"
            mv -- "$bootstrap_temporary" "$bootstrap_receipt_dir/bootstrap-packages.$1"
        else
            rm -f -- "$bootstrap_temporary"
            return 1
        fi
    }
    bootstrap_snapshot before
    bootstrap_result=0
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove python3 || bootstrap_result=$?
    # Record even a partially failed APT transaction without relying on Python.
    bootstrap_snapshot after
    if [ "$bootstrap_result" -ne 0 ]; then exit "$bootstrap_result"; fi
fi
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 "$project_dir/tools/deployment/setup.py" install "$@"
