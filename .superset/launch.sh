#!/usr/bin/env bash
# Launch Claude Code inside a per-workspace Docker container.
#
# Superset invokes this script with $PWD = workspace worktree. When the agent
# is launched with a prompt, Superset appends the prompt as argv (per the
# "Command (With Prompt)" config slot). We also accept the prompt via
# $CLAUDE_SANDBOX_PROMPT (env) or stdin so the script is usable manually.
#
# Behavior:
#   - First invocation in a worktree: `docker run` a fresh container.
#   - Subsequent invocations while the container is still up: `docker exec`
#     into it, so closing/reopening a terminal reattaches to the same sandbox.
#   - Container is `--rm`, so when claude exits the container is removed.
#     Worktree state lives on disk via the bind mount, not in the container.

set -euo pipefail

# --- config -----------------------------------------------------------------
IMAGE="${CLAUDE_SANDBOX_IMAGE:-claude-sandbox:latest}"
NETWORK="${CLAUDE_SANDBOX_NETWORK:-bridge}"

# --- prompt detection -------------------------------------------------------
# Priority: argv tail > CLAUDE_SANDBOX_PROMPT env > stdin (if piped).
PROMPT=""
if [ "$#" -gt 0 ] && [ -n "${1:-}" ]; then
    PROMPT="$*"
elif [ -n "${CLAUDE_SANDBOX_PROMPT:-}" ]; then
    PROMPT="$CLAUDE_SANDBOX_PROMPT"
elif [ ! -t 0 ]; then
    # stdin is not a TTY -- assume it carries the prompt. Read all of it.
    PROMPT="$(cat)"
fi

# --- preflight --------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "claude-sandbox: docker not found on PATH." >&2
    echo "  Install Docker Desktop (macOS) or the docker engine (Linux)." >&2
    exit 127
fi

if ! docker info >/dev/null 2>&1; then
    echo "claude-sandbox: docker daemon not reachable." >&2
    echo "  Is Docker Desktop running?" >&2
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "claude-sandbox: image '$IMAGE' not found." >&2
    echo "  Build it from the repo root:" >&2
    echo "    docker build -t claude-sandbox:latest ." >&2
    exit 1
fi

if [ ! -d "$HOME/.claude" ] || [ ! -f "$HOME/.claude.json" ]; then
    echo "claude-sandbox: missing Claude Code host config." >&2
    echo "  Need both \$HOME/.claude/ (dir) and \$HOME/.claude.json (file)." >&2
    echo "  Run \`claude /login\` on the host first to create them." >&2
    exit 1
fi

# macOS: Claude Code stores OAuth tokens in the system keychain, not in any
# file. The Linux container can't read the keychain, so we extract the JSON
# blob and stage it at ~/.claude/.credentials.json (inside the mounted
# ~/.claude directory) so Linux Claude finds it natively.
#
# A trap syncs that file back to the keychain on every exit -- including
# Ctrl-C, set -e bailouts, and reattach exits -- so any in-container token
# refresh propagates to the keychain (and from there to host-native claude
# and to future container launches in other worktrees).
#
# The extract itself runs lazily, only when we're starting a fresh container
# (see below). On reattach the bind-mounted file is authoritative: a
# still-running container has likely refreshed the token in-place, and
# re-extracting from the (now-stale) keychain would clobber that.
MACOS_CREDS=""
sync_credentials_back() {
    [ -z "$MACOS_CREDS" ] && return 0
    [ -f "$MACOS_CREDS" ] || return 0
    local refreshed
    refreshed="$(cat "$MACOS_CREDS" 2>/dev/null)" || return 0
    [ -z "$refreshed" ] && return 0
    if security add-generic-password -s 'Claude Code-credentials' -a "$USER" -w "$refreshed" -U 2>/dev/null; then
        echo "claude-sandbox: synced refreshed credentials back to keychain." >&2
    else
        echo "claude-sandbox: warning -- couldn't update keychain after session (will need to log in next time)." >&2
    fi
}
if [ "$(uname -s)" = "Darwin" ]; then
    MACOS_CREDS="$HOME/.claude/.credentials.json"
    trap sync_credentials_back EXIT
fi

# --- container name ---------------------------------------------------------
# basename + short hash of full path -- collision-free across worktrees that
# happen to share a basename. shasum on macOS, sha1sum on Linux.
hash_path() {
    if command -v shasum >/dev/null 2>&1; then
        printf '%s' "$1" | shasum -a 1 | cut -c1-8
    else
        printf '%s' "$1" | sha1sum | cut -c1-8
    fi
}

# Sanitize: replace any char outside [A-Za-z0-9_.-] (including the trailing
# newline from basename) with `-`, then collapse runs and trim trailing `-`.
WORKTREE_BASENAME="$(basename "$PWD" | tr -c 'A-Za-z0-9_.-' '-' | tr -s '-' | sed 's/-*$//')"
NAME="claude-sandbox-${WORKTREE_BASENAME}-$(hash_path "$PWD")"

# --- build claude argv ------------------------------------------------------
# Claude Code accepts an initial prompt as a positional argument.
# When no prompt is given, omit it so claude starts in plain interactive mode.
claude_argv=(claude)
if [ "${CLAUDE_SANDBOX_SKIP_PERMISSIONS:-1}" = "1" ]; then
    claude_argv+=(--dangerously-skip-permissions)
fi
if [ -n "$PROMPT" ]; then
    claude_argv+=("$PROMPT")
fi

# --- reattach if container already exists -----------------------------------
# Invoke entrypoint.sh explicitly so /workdir/.env is re-sourced in the exec
# path too (docker exec otherwise bypasses ENTRYPOINT).
#
# We deliberately don't `exec` here: launch.sh has to outlive the docker exec
# so the EXIT trap fires and syncs any in-container token refresh back to
# the keychain. `exec`-ing would replace this script and skip the trap.
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    docker exec -it "$NAME" /usr/local/bin/entrypoint.sh "${claude_argv[@]}"
    exit $?
fi

# Fresh container: stage the keychain blob now so Linux Claude can read it.
# (Skipped on the reattach path above -- see the comment block above MACOS_CREDS.)
if [ -n "$MACOS_CREDS" ]; then
    if blob="$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null)"; then
        umask 077
        printf '%s\n' "$blob" > "$MACOS_CREDS"
    else
        echo "claude-sandbox: warning -- couldn't read 'Claude Code-credentials' from keychain." >&2
        echo "  Run \`claude /login\` on the host (so the keychain entry exists)." >&2
        echo "  Continuing; the container may prompt you to log in instead." >&2
    fi
fi

# --- run flags --------------------------------------------------------------
# Start detached (-d) so the container lifecycle is decoupled from the
# launching terminal. All sessions (first and subsequent) attach via
# `docker exec`, so closing any tab only drops that exec session -- the
# container and every other agent inside it keep running.
docker_args=(
    run -d --rm
    --name "$NAME"
    -v "$PWD:/workdir"
    -v "$HOME/.claude:/home/claude/.claude"      # rw -- claude writes session state here
    -v "$HOME/.claude.json:/home/claude/.claude.json"  # the actual config file (lives next to .claude/, not inside)
    -v "$HOME/.gitconfig:/home/claude/.gitconfig:ro"
    -w /workdir
    -e HOME=/home/claude                          # ensure $HOME points at the user dir even
                                                  # when the runtime UID has no /etc/passwd entry
    -e UV_PROJECT_ENVIRONMENT=/workdir/.venv-container  # separate venv from host's .venv
    -u "$(id -u):$(id -g)"                        # match host UID/GID so bind-mount writes work
    --network "$NETWORK"
)

# add 8888 port mapping
docker_args+=(
    -p 8888
)

# Git worktree (and submodule) support: $PWD/.git is a *file* like
# `gitdir: /host/abs/path/to/.git/worktrees/<name>`. That absolute path is
# outside the worktree bind mount, so git in the container can't follow it.
# Mount the parent .git directory at the same absolute path so the pointer
# still resolves. Mounted rw because git commits write objects there.
if [ -f "$PWD/.git" ]; then
    gitdir="$(sed -n 's/^gitdir: *//p' "$PWD/.git")"
    if [ -n "$gitdir" ]; then
        # Worktrees: <repo>/.git/worktrees/<name>. Submodules: <super>/.git/modules/<name>.
        # Both resolve to <repo-or-super>/.git via two dirname levels.
        parent_git="$(dirname "$(dirname "$gitdir")")"
        if [ -d "$parent_git" ]; then
            docker_args+=(-v "$parent_git:$parent_git")
        fi
    fi
fi

# Optional: mount host ~/.ssh read-only when the user opts in (e.g. for git push over SSH).
if [ "${CLAUDE_SANDBOX_MOUNT_SSH:-0}" = "1" ] && [ -d "$HOME/.ssh" ]; then
    docker_args+=(-v "$HOME/.ssh:/home/claude/.ssh:ro")
fi

# Symlinks inside the worktree may point outside $PWD (e.g. `results/`
# symlinked from a sibling worktree, or a shared dataset directory). The
# container otherwise only sees /workdir, so those links appear broken.
# For each external target we bind-mount it at the same absolute host path
# inside the container, so the link resolves identically to the host (same
# trick as the .git-parent mount above).
#
# Defaults:
#   read-only -- symlinked results are usually inputs to read, not write.
#                Set CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW=1 to mount rw.
#   enabled   -- set CLAUDE_SANDBOX_MOUNT_SYMLINKS=0 to skip the scan
#                (useful for huge worktrees where `find` is slow).
if [ "${CLAUDE_SANDBOX_MOUNT_SYMLINKS:-1}" = "1" ]; then
    # Determine mount mode for a resolved symlink target.
    # Returns "rw" if CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW=1 (all rw), or if the
    # target matches a prefix listed in CLAUDE_SANDBOX_SYMLINK_RW_PATHS
    # (colon-delimited). Returns "ro" otherwise.
    symlink_mount_mode() {
        local target="$1"
        if [ "${CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW:-0}" = "1" ]; then
            echo "rw"; return
        fi
        if [ -n "${CLAUDE_SANDBOX_SYMLINK_RW_PATHS:-results_scratch}" ]; then
            local IFS=':'
            for prefix in ${CLAUDE_SANDBOX_SYMLINK_RW_PATHS:-results_scratch}; do
                case "$prefix" in
                    /*) : ;;
                    *) prefix="$(resolve_path "$PWD/$prefix")" || continue ;;
                esac
                [ -z "$prefix" ] && continue
                case "$target" in
                    "$prefix"|"$prefix"/*) echo "rw"; return ;;
                esac
            done
        fi
        echo "ro"
    }

    # Cross-platform realpath: macOS shipped /usr/bin/realpath in 12.0; older
    # macOS lacks it. Fall back to python3 (present on both modern macOS and
    # any system that can run Claude Code).
    resolve_path() {
        if command -v realpath >/dev/null 2>&1; then
            realpath "$1" 2>/dev/null
        else
            python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null
        fi
    }

    # Resolve $PWD too so the "target inside worktree" check survives parent
    # symlinks like macOS /tmp -> /private/tmp.
    pwd_real="$(resolve_path "$PWD")"
    [ -z "$pwd_real" ] && pwd_real="$PWD"

    link_target_modes=()
    # Prune directories that are symlink-heavy but whose external targets we
    # don't want to mount: .git (worktree pointer), .venv / venv (Python
    # interpreter symlinks into uv/pyenv/system), node_modules (npm bin
    # symlinks into global installs).
    while IFS= read -r -d '' link; do
        target="$(resolve_path "$link")" || continue
        [ -z "$target" ] && continue
        # Skip targets inside the worktree -- already covered by /workdir.
        case "$target" in
            "$PWD"|"$PWD"/*|"$pwd_real"|"$pwd_real"/*) continue ;;
        esac
        # Skip if the target doesn't exist -- docker would refuse the mount.
        [ -e "$target" ] || continue
        link_target_modes+=("$target|$(symlink_mount_mode "$target")")
    done < <(find "$PWD" \( -path "$PWD/.git" -o -name ".venv" -o -name ".venv-container" -o -name "venv" -o -name "node_modules" \) -prune -o -type l -print0 2>/dev/null)

    if [ "${#link_target_modes[@]}" -gt 0 ]; then
        # Dedupe targets (multiple symlinks may point at the same path). When
        # the same target appears with both modes, prefer rw. Sort by target
        # then by mode descending (rw > ro), then keep first occurrence.
        while IFS='|' read -r target mode; do
            docker_args+=(-v "$target:$target:$mode")
            echo "claude-sandbox: mounting symlink target $target ($mode)" >&2
        done < <(printf '%s\n' "${link_target_modes[@]}" | sort -t'|' -k1,1 -k2,2r | awk -F'|' '!seen[$1]++')
    fi
fi

# Forward ANTHROPIC_* env vars from the host (API keys, base URLs, model overrides).
while IFS= read -r var; do
    [ -n "$var" ] && docker_args+=(-e "$var")
done < <(env | awk -F= '/^ANTHROPIC_/ {print $1}')

# Forward all Superset-injected env vars (workspace identity, home dir, etc.)
# so hooks like notify.sh can locate Superset's IPC socket/scripts.
while IFS= read -r var; do
    [ -n "$var" ] && docker_args+=(-e "$var")
done < <(env | awk -F= '/^SUPERSET_/ {print $1}')

# Mount SUPERSET_HOME_DIR at the same path so notify.sh and any IPC sockets
# it references resolve correctly inside the container.
if [ -n "${SUPERSET_HOME_DIR:-}" ] && [ -d "$SUPERSET_HOME_DIR" ]; then
    docker_args+=(-v "$SUPERSET_HOME_DIR:$SUPERSET_HOME_DIR")
fi

# --- go ---------------------------------------------------------------------
# Start the detached keepalive container (no-op if it somehow already exists).
docker "${docker_args[@]}" "$IMAGE" sleep infinity >/dev/null

# Attach an interactive claude session. Closing this terminal only drops the
# exec -- the container (and any other exec'd agents) keep running.
# Sync-back to the macOS keychain runs from the EXIT trap installed above.
# To stop the sandbox when finished: docker stop "$NAME"
docker exec -it "$NAME" /usr/local/bin/entrypoint.sh "${claude_argv[@]}"
