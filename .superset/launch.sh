#!/usr/bin/env bash
# DEPRECATED compat shim. The scripts moved from .superset/ to tools/.
# Update your Superset agent command to `tools/launch.sh` and delete .superset/.
set -euo pipefail
exec "$(dirname "$0")/../tools/launch.sh" "$@"
