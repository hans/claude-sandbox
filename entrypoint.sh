#!/bin/sh
# Container entrypoint. Registers the runtime UID/GID in /etc/passwd and
# /etc/group (needed for sudo and whoami when running as a dynamic host UID),
# sources /workdir/.env so per-project env vars are visible to the agent,
# then execs the command passed by `docker run`. Defaults to `claude` via
# the Dockerfile CMD.
set -eu

if ! getent passwd "$(id -u)" >/dev/null 2>&1; then
    echo "host:x:$(id -u):$(id -g):host user:/home/claude:/bin/bash" >> /etc/passwd
fi
if ! getent group "$(id -g)" >/dev/null 2>&1; then
    echo "host:x:$(id -g):" >> /etc/group
fi

if [ -f /workdir/.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /workdir/.env
    set +a
fi

exec "$@"
