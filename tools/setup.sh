#!/usr/bin/env bash
# Per-worktree setup. Superset runs this once when a worktree is created and
# may re-run it; everything here must be idempotent.
#
# Runs on the HOST, not inside the sandbox container. Use it for things that
# need to touch the host filesystem (copying env files, etc.). Anything that
# installs Python/Node deps belongs inside the container on first run.

set -euo pipefail

# 1. Copy parent worktree's .env if we don't have one yet. Superset worktrees
#    typically sit one directory below the main checkout; ../.env is a useful
#    default source.
if [ ! -f ./.env ] && [ -f ../.env ]; then
    cp ../.env ./.env
    echo "claude-sandbox setup: copied ../.env -> ./.env"
fi

# 2. Remind the user that per-project deps must be installed *inside* the
#    container. The sandbox image is generic on purpose.
if [ -f ./pyproject.toml ]; then
    echo "claude-sandbox setup: detected pyproject.toml -- run \`uv sync\` inside the container on first launch."
elif [ -f ./requirements.txt ]; then
    echo "claude-sandbox setup: detected requirements.txt -- run \`pip install -r requirements.txt\` inside the container on first launch."
fi

echo "claude-sandbox setup: ready."
