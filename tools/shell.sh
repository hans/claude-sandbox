#!/usr/bin/env bash
# Open a bash shell inside the per-workspace Docker container.
#
# Thin wrapper around launch.py --shell, so the shell gets the exact same
# mounts, network, and env as the agent (one arg-builder, no duplication).
# If a container is already running for this worktree, it reattaches; otherwise
# it starts a fresh keepalive container and execs bash into it. Note: unlike the
# old standalone shell.sh, the container persists after you exit the shell (same
# --rm keepalive the agent uses), until the agent session for this worktree ends.
#
# Usage: shell.sh   (run from the workspace worktree root)
set -euo pipefail
exec python3 "$(dirname "$0")/launch.py" --shell "$@"
