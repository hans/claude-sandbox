#!/usr/bin/env bash
# DEPRECATED compat shim. The scripts moved from .superset/ to tools/.
# Update your Superset shell command to `tools/shell.sh` and delete .superset/.
set -euo pipefail
exec "$(dirname "$0")/../tools/shell.sh" "$@"
