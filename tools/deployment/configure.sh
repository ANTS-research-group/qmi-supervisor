#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 "$project_dir/tools/deployment/setup.py" configure "$@"
