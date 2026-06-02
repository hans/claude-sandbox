#!/usr/bin/env bash
# Thin wrapper for backward compatibility — delegates to launch.py.
set -euo pipefail
exec python3 "$(dirname "$0")/launch.py" "$@"
