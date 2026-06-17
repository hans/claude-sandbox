#!/usr/bin/env bash
# Open a bash shell inside the per-workspace Docker container.
# If the container is already running (e.g. a Claude session is active),
# exec into it. If not, start a fresh container with the same mounts
# used by launch.sh, then drop into bash.
#
# Usage: .superset/shell.sh
# Run from the workspace worktree root (Superset does this automatically).

set -euo pipefail

IMAGE="${CLAUDE_SANDBOX_IMAGE:-claude-sandbox:latest}"
NETWORK="${CLAUDE_SANDBOX_NETWORK:-bridge}"

# --- preflight --------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "shell.sh: docker not found on PATH." >&2; exit 127
fi
if ! docker info >/dev/null 2>&1; then
    echo "shell.sh: docker daemon not reachable." >&2; exit 1
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "shell.sh: image '$IMAGE' not found. Build it first:" >&2
    echo "  docker build -t claude-sandbox:latest ." >&2
    exit 1
fi

# --- container name (must match launch.sh) ----------------------------------
hash_path() {
    if command -v shasum >/dev/null 2>&1; then
        printf '%s' "$1" | shasum -a 1 | cut -c1-8
    else
        printf '%s' "$1" | sha1sum | cut -c1-8
    fi
}
WORKTREE_BASENAME="$(basename "$PWD" | tr -c 'A-Za-z0-9_.-' '-' | tr -s '-' | sed 's/-*$//')"
NAME="claude-sandbox-${WORKTREE_BASENAME}-$(hash_path "$PWD")"

# --- reattach if already running --------------------------------------------
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "shell.sh: attaching to running container $NAME" >&2
    exec docker exec -it "$NAME" /usr/local/bin/entrypoint.sh bash
fi

# --- run flags (mirrors launch.sh) ------------------------------------------
docker_args=(
    run --rm -it
    --name "$NAME"
    -v "$PWD:/workdir"
    -v "$HOME/.gitconfig:/home/claude/.gitconfig:ro"
    -w /workdir
    -e HOME=/home/claude
    -u "$(id -u):$(id -g)"
    --network "$NETWORK"
)

# Git worktree support: same .git-parent mount as launch.sh.
if [ -f "$PWD/.git" ]; then
    gitdir="$(sed -n 's/^gitdir: *//p' "$PWD/.git")"
    if [ -n "$gitdir" ]; then
        parent_git="$(dirname "$(dirname "$gitdir")")"
        [ -d "$parent_git" ] && docker_args+=(-v "$parent_git:$parent_git")
    fi
fi

# Optional SSH mount.
if [ "${CLAUDE_SANDBOX_MOUNT_SSH:-0}" = "1" ] && [ -d "$HOME/.ssh" ]; then
    docker_args+=(-v "$HOME/.ssh:/home/claude/.ssh:ro")
fi

# Symlink mounts (same logic as launch.sh).
if [ "${CLAUDE_SANDBOX_MOUNT_SYMLINKS:-1}" = "1" ]; then
    resolve_path() {
        if command -v realpath >/dev/null 2>&1; then
            realpath "$1" 2>/dev/null
        else
            python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
        fi
    }
    symlink_mount_mode() {
        local target="$1"
        [ "${CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW:-0}" = "1" ] && { echo "rw"; return; }
        if [ -n "${CLAUDE_SANDBOX_SYMLINK_RW_PATHS:-}" ]; then
            local IFS=':'
            for prefix in ${CLAUDE_SANDBOX_SYMLINK_RW_PATHS}; do
                case "$prefix" in /*) : ;; *) prefix="$(resolve_path "$PWD/$prefix")" || continue ;; esac
                [ -z "$prefix" ] && continue
                case "$target" in "$prefix"|"$prefix"/*) echo "rw"; return ;; esac
            done
        fi
        echo "ro"
    }
    pwd_real="$(resolve_path "$PWD")"; [ -z "$pwd_real" ] && pwd_real="$PWD"
    link_target_modes=()
    while IFS= read -r -d '' link; do
        target="$(resolve_path "$link")" || continue
        [ -z "$target" ] && continue
        case "$target" in "$PWD"|"$PWD"/*|"$pwd_real"|"$pwd_real"/*) continue ;; esac
        [ -e "$target" ] || continue
        link_target_modes+=("$target|$(symlink_mount_mode "$target")")
    done < <(find "$PWD" \( -path "$PWD/.git" -o -name ".venv" -o -name ".venv-container" -o -name "venv" -o -name "node_modules" \) -prune -o -type l -print0 2>/dev/null)
    if [ "${#link_target_modes[@]}" -gt 0 ]; then
        while IFS='|' read -r target mode; do
            docker_args+=(-v "$target:$target:$mode")
            echo "shell.sh: mounting symlink target $target ($mode)" >&2
        done < <(printf '%s\n' "${link_target_modes[@]}" | sort -t'|' -k1,1 -k2,2r | awk -F'|' '!seen[$1]++')
    fi
fi

exec docker "${docker_args[@]}" "$IMAGE" bash
