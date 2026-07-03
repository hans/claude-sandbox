#!/usr/bin/env bash
# One-time global install for claude-sandbox.
#
# Symlinks the launcher and the per-worktree setup hook onto your PATH and
# seeds a shared config, so you never have to copy or symlink tools/ (or a
# .claude-sandbox.toml, or .superset/) into individual project repos again.
#
# After running this:
#   - Terminal:  cd into any worktree and run `claude-sandbox`.
#   - Superset:  point the agent Command and the setup hook at the ABSOLUTE
#                paths printed below (not `tools/launch.sh` / `.superset/...`),
#                then delete the per-project symlinks.
#
# Idempotent: re-running refreshes the symlinks and never clobbers an existing
# global config. Symlinks (not copies) mean `git pull` in this checkout updates
# every project at once -- the thing you actually wanted from symlinking.
#
# Knobs:
#   BINDIR   where the launcher symlinks go   (default: ~/.local/bin)
#   CONFDIR  where the shared config goes      (default: $XDG_CONFIG_HOME or ~/.config, /claude-sandbox)

set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(dirname "$here")"

BINDIR="${BINDIR:-$HOME/.local/bin}"
CONFDIR="${CONFDIR:-${XDG_CONFIG_HOME:-$HOME/.config}/claude-sandbox}"

mkdir -p "$BINDIR" "$CONFDIR"

# 1. Launcher + setup hook -> stable names on PATH. launch.py is self-contained
#    (pure stdlib) and runs via its own shebang, so a direct symlink is enough.
ln -sfn "$here/launch.py" "$BINDIR/claude-sandbox"
ln -sfn "$here/setup.sh"  "$BINDIR/claude-sandbox-setup"
echo "installed: $BINDIR/claude-sandbox        -> $here/launch.py"
echo "installed: $BINDIR/claude-sandbox-setup  -> $here/setup.sh"

# 2. Seed the shared config from this repo's default -- but never overwrite an
#    existing one (it may be hand-tuned).
config_dst="$CONFDIR/config.toml"
default_src="$repo_root/.claude-sandbox.toml"
if [ -e "$config_dst" ]; then
    echo "kept:      $config_dst (already exists, left untouched)"
elif [ -f "$default_src" ]; then
    cp "$default_src" "$config_dst"
    echo "installed: $config_dst (from $default_src)"
else
    echo "note:      no $default_src to seed a global config from -- skipping."
fi

# 3. PATH sanity check.
case ":$PATH:" in
    *":$BINDIR:"*) ;;
    *) echo; echo "WARNING: $BINDIR is not on your PATH. Add it, e.g.:"
       echo "         echo 'export PATH=\"$BINDIR:\$PATH\"' >> ~/.zshrc" ;;
esac

cat <<EOF

Done. Next steps:

  Terminal    cd into any worktree and run:  claude-sandbox
              ('claude-sandbox --shell' for a bash shell in the same container).

  Superset    Settings -> Agents, set the ABSOLUTE paths:
                Command (No Prompt)     $BINDIR/claude-sandbox
                Command (With Prompt)   $BINDIR/claude-sandbox
                Setup hook              $BINDIR/claude-sandbox-setup
              Then delete the .superset/ and tools/ symlinks from your projects.

  Config      Shared defaults live in $config_dst.
              A project that needs different settings still wins by dropping its
              own ./.claude-sandbox.toml at the worktree root.
EOF
