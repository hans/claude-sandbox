#!/usr/bin/env bash
# DEPRECATED compat shim. The scripts moved from .superset/ to tools/.
# Update your Superset setup command to `tools/setup.sh` and delete .superset/.
set -euo pipefail
exec "$(dirname "$0")/../tools/setup.sh" "$@"
