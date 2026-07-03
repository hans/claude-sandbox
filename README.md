# claude-sandbox

[![Docker Hub](https://img.shields.io/docker/v/jrgauthier/claude-sandbox?logo=docker&logoColor=white&label=Docker%20Hub&sort=semver&arch=arm64)](https://hub.docker.com/r/jrgauthier/claude-sandbox)
[![Image size](https://img.shields.io/docker/image-size/jrgauthier/claude-sandbox/latest?logo=docker&label=image%20size&arch=arm64)](https://hub.docker.com/r/jrgauthier/claude-sandbox)
[![Pulls](https://img.shields.io/docker/pulls/jrgauthier/claude-sandbox?logo=docker&label=pulls)](https://hub.docker.com/r/jrgauthier/claude-sandbox)

A per-workspace Docker sandbox for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).

Point any launcher at `tools/launch.sh` and every workspace you open gets its own throwaway container with the worktree bind-mounted at `/workdir`. The agent runs its bash tool inside the container; your files, git history, and diffs stay on the host. The container is just a blast radius.

It's built to be portable along three axes, so the core stays small:

- **Launcher** — [Superset](https://superset.sh) is the original driver, but the core is launcher-agnostic. A bare terminal, a VS Code task, or CI works too; launcher-specific glue lives in opt-in **host profiles** (see below).
- **Project type** — no language/toolchain is baked in. Per-project tooling (which venv, which caches) goes in an optional `.claude-sandbox.toml` (see [Per-project configuration](#per-project-configuration)).
- **Agent** — this one is fixed: it runs Claude Code.

Deliberately *not* a Container Use (`cu`) setup: the host worktree is the source of truth, the container is the cell.

## A 90-second demo

Assuming you've got Docker and you've logged into Claude on the host once (`claude /login`):

```bash
# 1. Get the image. Pull the prebuilt one (no build needed):
docker pull jrgauthier/claude-sandbox
docker tag jrgauthier/claude-sandbox claude-sandbox:latest
#     ...or build your own: `docker build -t claude-sandbox:latest .`

# 2a. Bare terminal: from any worktree, just run the launcher.
cd /path/to/worktree && /path/to/claude-sandbox/tools/launch.sh
#     ...or `tools/launch.sh --shell` to drop into a bash shell in the
#     same container instead of the agent.

# 2b. Superset: Settings → Agents → New agent, set:
#      Command (No Prompt)    tools/launch.sh
#      Command (With Prompt)  tools/launch.sh
#      Environment            CLAUDE_SANDBOX_NETWORK=bridge

# 3. Open the workspace. Claude starts inside a fresh container.
#    Inside the container you'll see:
#      /workdir            ← the worktree, read-write
#      ~/.claude{,.json}   ← your host login, bind-mounted in
#      <symlink targets>   ← any path your worktree symlinks to (see below)

# 4. Close the terminal. The container is still alive until claude exits.
#    Relaunch → launch.sh detects the running container and `docker exec`s
#    back into it. Same session, same shell history.

# 5. Quit claude. The container is `--rm`, so it disappears. Nothing on
#    the host changes except whatever you actually edited in /workdir.
```

That's the whole loop. The interesting bits are how it handles two things that normally break containerized agents: **symlinks that escape the worktree**, and **ports / network reachability**.

## Symlinks that escape the worktree

The thing that makes Snakemake, MNE, big-data ML, and "shared results" projects work in real life is symlinks. Your worktree has a `results/` that's actually a link to `/scratch/shared/foo`, or a dataset under `/opt/data/...`. Mount only the worktree into the container and every one of those links becomes a dangling pointer. Cool, useless sandbox.

`launch.sh` fixes this with a small trick: before `docker run`, it walks the worktree, follows every symlink, and for each target *outside* `$PWD` adds a bind mount at the **same absolute host path** inside the container. The link resolves identically inside and out. `.git` is the canonical case (worktrees keep their gitdir at `<repo>/.git/worktrees/<name>`), but anything works.

Knobs:

| Variable                            | Effect                                                                                                          |
|-------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| `CLAUDE_SANDBOX_MOUNT_SYMLINKS=0`   | Skip the scan entirely. Useful when the tree is huge and you don't actually have escape symlinks.               |
| `CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW=1`| Mount **all** escape targets read-write. Default is ro — symlinked results are usually inputs you read.         |
| `CLAUDE_SANDBOX_SYMLINK_RW_PATHS`   | Colon-list of path prefixes that should be rw; everything else stays ro. E.g. `results_scratch:/data/shared`.   |

Details that bite people:

- Mounts are added at `docker run` time. **Add a new escape symlink mid-session and the agent won't see it** — exit and relaunch.
- Broken symlinks are skipped (docker would refuse the mount anyway). Duplicate targets are deduped.
- Each mount is logged to stderr (`mounting symlink target ...`) so it's visible what's being exposed.
- **Docker Desktop on macOS** restricts bind mounts to a configured allowlist (`/Users`, `/tmp`, `/private`, `/var/folders`, `/Volumes` by default). If a target lives outside it (`/opt/data/...`), `docker run` fails with "mounts denied". Add the prefix under Settings → Resources → File sharing.
- Anything you can reach via a symlink is exposed to the agent. Treat it like any bind mount — only link in directories you'd hand to the sandbox.

## Ports & networking

Three modes via `CLAUDE_SANDBOX_NETWORK`:

| Mode     | What it gets you                                                                                       | When to pick it                                              |
|----------|--------------------------------------------------------------------------------------------------------|--------------------------------------------------------------|
| `bridge` | NAT'd Docker network. Outbound works. **`localhost` on the host is unreachable.**                      | Default. Most agent work doesn't need host services.         |
| `host`   | Container shares the host's network stack. `localhost:5432` on the host = `localhost:5432` in the box. | You're testing against a local API, DB, or dev server.       |
| `none`   | No network at all.                                                                                     | Hermetic runs. Useful for paranoid one-shots.                |

A common gotcha: the agent spins up a dev server on port `3000` inside the container under `bridge` and wonders why the browser can't see it. With `bridge`, ports aren't published unless you explicitly forward them — `launch.sh` doesn't, by design. Either switch to `host`, or `docker exec` an `ssh -L` from outside, or just look at the rendered HTML the agent emits. (One side note about Superset notifications calling back into the host: those use `host.docker.internal` rather than `localhost`, which is why they work under `bridge`.)

## Prerequisites

- macOS or Linux host with Docker (Desktop on macOS, Engine on Linux).
- Python 3.11+ on the host (for `tools/launch.py`; 3.9/3.10 work too if `tomli` is installed and you use a `.claude-sandbox.toml`).
- A launcher — [Superset](https://superset.sh), or just a terminal.
- Claude Code installed and logged in on the host: `claude /login`. That creates `~/.claude` which the container mounts.

## Get the image

A prebuilt image is published on Docker Hub, so you don't have to build it yourself. Pull it and tag it as the default name the launcher expects:

```
docker pull jrgauthier/claude-sandbox
docker tag jrgauthier/claude-sandbox claude-sandbox:latest
```

(Or skip the retag and point the launcher at it directly: `export CLAUDE_SANDBOX_IMAGE=jrgauthier/claude-sandbox`.)

Prefer to build it yourself — to pin a base image, audit the layers, or hack on the `Dockerfile`:

```
docker build -t claude-sandbox:latest .
```

Rebuild only when the `Dockerfile` changes. Per-project tooling (extra Python deps, system libs) goes in a downstream image — see below.

## Launchers

`launch.sh` reads its initial prompt from argv, then `CLAUDE_SANDBOX_PROMPT`, then stdin — so anything that can run a command in the worktree can drive it. The launcher-specific glue (extra env forwarding, callback-URL rewriting, extra mounts) lives in **host profiles**, selected by `CLAUDE_SANDBOX_HOST` or auto-detected from the environment:

| Profile    | Selected when                                          | Adds                                                                                              |
|------------|--------------------------------------------------------|---------------------------------------------------------------------------------------------------|
| `generic`  | default                                                | nothing launcher-specific (just the always-on `ANTHROPIC_*` forwarding + `host.docker.internal`)  |
| `superset` | any `SUPERSET_*` env var is present, or set explicitly | forwards `SUPERSET_*` (rewriting `localhost`/`127.0.0.1` → `host.docker.internal`), mounts `SUPERSET_HOME_DIR` |

Set `CLAUDE_SANDBOX_HOST=generic` to force the generic profile even under Superset. Adding a new launcher is one entry in the `_HOST_PROFILES` registry in `tools/launch.py`.

**Install once, globally.** `./tools/install.sh` symlinks the launcher and the
per-worktree setup hook onto your PATH and seeds a shared config, so you never
copy `tools/`, symlink `.superset/`, or duplicate a `.claude-sandbox.toml` into
individual repos:

```
~/.local/bin/claude-sandbox         -> tools/launch.py
~/.local/bin/claude-sandbox-setup   -> tools/setup.sh
~/.config/claude-sandbox/config.toml   (shared defaults; per-project ./.claude-sandbox.toml still wins)
```

Symlinks into this checkout mean `git pull` updates every project at once.
`BINDIR=` / `CONFDIR=` override the locations.

**Bare terminal:** `cd` into any worktree and run `claude-sandbox "your prompt"` (`claude-sandbox --shell` for a bash shell).

**Superset** — **Settings → Agents → New agent** (or duplicate the built-in `claude` preset):

| Field                  | Value                                       |
|------------------------|---------------------------------------------|
| Command (No Prompt)    | `~/.local/bin/claude-sandbox`               |
| Command (With Prompt)  | `~/.local/bin/claude-sandbox`               |
| Setup hook             | `~/.local/bin/claude-sandbox-setup`         |
| Prompt Command Suffix  | *(empty)*                                   |
| Task Prompt Template   | *(default is fine)*                         |
| Environment            | `CLAUDE_SANDBOX_NETWORK=bridge` *(example)* |

Use the **absolute** paths (expand `~` if Superset won't). Because they resolve identically in every workspace, nothing has to live in the repo — delete any `.superset/` and `tools/` symlinks once you've switched. Superset appends the prompt as argv to "Command (With Prompt)" after the suffix, which is what the launcher expects. The Environment field is where you set `CLAUDE_SANDBOX_*` variables; one per line. You can also `export` them in the shell that starts Superset for a host-wide default. The `superset` host profile is auto-selected because Superset injects `SUPERSET_*` vars.

> Superset itself doesn't define a `SUPERSET_PROMPT` env var; the env-var and stdin paths in `launch.sh` are fallbacks for invoking the script manually from a terminal, not Superset-driven transports.

## Per-project extension

The global `claude-sandbox` image stays generic. Two ways to add per-project tools:

**One-off:** `docker exec` into the running container and install ad-hoc. Lives in the writable layer; gone when claude exits (we run `--rm`). Good for trying a tool out.

**Persistent:** drop a `Dockerfile.project` in your project:

```dockerfile
FROM claude-sandbox:latest
RUN pip install --break-system-packages mne nibabel
```

Build it, then point `launch.sh` at it via env var:

```
docker build -t claude-sandbox-myproj -f Dockerfile.project .
export CLAUDE_SANDBOX_IMAGE=claude-sandbox-myproj
```

(or set `CLAUDE_SANDBOX_IMAGE` in the Superset agent's Environment field.)

## Per-project configuration

The sandbox core makes **no language/toolchain assumptions**. Per-worktree tooling — which venv to use, which caches to share across containers, which extra subtrees to skip in the symlink scan — goes in an optional `.claude-sandbox.toml` at the worktree root:

```toml
# Top-level keys must come before any [table] (TOML rule), so prune goes first.
prune = ["target", "dist"]                       # extra dir names skipped by the symlink scan

[env]                                            # extra env vars inside the container
UV_PROJECT_ENVIRONMENT = "/workdir/.venv-container"
UV_CACHE_DIR = "/home/claude/.cache/uv"

[[mounts]]                                       # extra bind mounts (host → container)
host = "~/.cache/uv"                             # ~ and $VARS are expanded
container = "/home/claude/.cache/uv"
mode = "rw"
```

This repo ships the Python/uv config above (a container-private venv plus a shared uv cache). With no config found anywhere, no `UV_*` vars or cache mounts are emitted. (`.git`, `.venv`, `venv`, `.venv-container`, and `node_modules` are always pruned from the symlink scan regardless.)

Note the interaction with the global default below: once `install.sh` has seeded a Python/uv `~/.config/claude-sandbox/config.toml`, a worktree that omits `./.claude-sandbox.toml` **inherits** it. A non-Python checkout that wants the neutral core back opts out explicitly by dropping its own `./.claude-sandbox.toml` (its own settings, or an empty file for none) — the per-project file always wins.

**Where the config comes from** — the launcher resolves in two steps, so common settings don't have to be duplicated per worktree:

1. **`./.claude-sandbox.toml`** at the worktree root — an explicit per-project config. Always wins.
2. **`~/.config/claude-sandbox/config.toml`** — the user-global default (seeded by `tools/install.sh`). Used only when there's no per-project file. Relocate it with `$XDG_CONFIG_HOME`, or point at a specific file with `CLAUDE_SANDBOX_CONFIG=/path/to.toml`.

So your Python/uv defaults live in one place and apply to every worktree; a project that genuinely differs just drops its own `./.claude-sandbox.toml`. There's no walk-up or merging — exactly one file is used, and the per-project one is never overridden by the global default.

If the chosen file is present but unparseable, `launch.sh` exits with an error rather than silently falling back — a config that says "don't use uv" must never be quietly ignored.

## Environment variables

There are two places variables are consumed, and they need different placement.

### Host-side (read by `launch.sh` before Docker runs)

Set in **Superset → Settings → Agents → your agent → Environment**, one per line, or `export` before starting Superset.

| Variable                            | Purpose                                                                                                                | Default                  |
|-------------------------------------|------------------------------------------------------------------------------------------------------------------------|--------------------------|
| `CLAUDE_SANDBOX_IMAGE`              | Docker image to run                                                                                                    | `claude-sandbox:latest`  |
| `CLAUDE_SANDBOX_HOST`               | Host (launcher) profile: `generic` / `superset`. Auto-detected if unset (any `SUPERSET_*` var → `superset`)             | auto                     |
| `CLAUDE_SANDBOX_NETWORK`            | `--network` value: `bridge` / `host` / `none` / custom network name                                                    | `bridge`                 |
| `CLAUDE_SANDBOX_MOUNT_SSH`          | Set to `1` to mount `~/.ssh` read-only (for git push over SSH)                                                         | unset (off)              |
| `CLAUDE_SANDBOX_MOUNT_SYMLINKS`     | Set to `0` to skip the symlink-escape scan                                                                             | `1` (on)                 |
| `CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW`  | Set to `1` to mount **all** symlink targets read-write                                                                 | `0` (read-only)          |
| `CLAUDE_SANDBOX_SYMLINK_RW_PATHS`   | Colon-delimited path prefixes to mount rw; everything else stays ro. E.g. `results_scratch:/data/shared`               | unset                    |
| `CLAUDE_SANDBOX_PROMPT`             | Manual override for the agent's initial prompt                                                                         | unset                    |
| `ANTHROPIC_*`                       | Any var matching this prefix is forwarded into the container                                                           | inherited from host      |

### Container-side (visible to the agent inside Docker)

Put project-specific secrets and config in a `.env` file at the worktree root. The container's `entrypoint.sh` sources it on every launch, including reattaches:

```
ANTHROPIC_API_KEY=sk-ant-...
SOME_PROJECT_API_KEY=...
```

`setup.sh` copies `../.env` → `./.env` when a new worktree is created (if the parent has one), so one `.env` in the main checkout propagates automatically. `ANTHROPIC_*` host vars are also forwarded by `launch.sh`, so pick whichever fits your secrets workflow.

## How git worktrees are handled

Superset workspaces are git worktrees. In a worktree, `.git` is a *file* containing `gitdir: /abs/path/to/main-repo/.git/worktrees/<name>` — an absolute host path that points *outside* the worktree directory. Bind-mount only the worktree and git inside the container can't resolve it; every git command fails with "not a git repository".

`launch.sh` reads `$PWD/.git`, walks up to the common `.git` dir (`<repo>/.git`), and bind-mounts it at the same absolute path inside the container so the pointer resolves. Read-write, because commits need to write objects and refs. Same mechanism that makes submodules work.

If your project uses a non-standard layout (custom `GIT_DIR`, etc.), you may need to extend `launch.sh`.

## Troubleshooting

**`permission denied` writing into the worktree or `~/.claude`.** `launch.sh` runs the container as `-u $(id -u):$(id -g)`, which works on macOS (host UID 501) and Linux out of the box. If you've hardcoded `-u 1000:1000` for some reason and the host UID isn't 1000, bind-mount writes fail. Revert to the default UID flag.

**`credentials not found` / Claude asks you to log in inside the container.** The container mounts both `~/.claude` (directory) and the sibling `~/.claude.json` (file). If either is missing, run `claude /login` on the host first.

On macOS, the OAuth tokens themselves live in the system keychain, not in any file. `launch.sh` extracts the `Claude Code-credentials` keychain entry on every launch and stages it at `~/.claude/.credentials.json`, which Linux Claude reads natively. **Side effect:** when Claude inside the container refreshes its access token, the host keychain's refresh token may be invalidated, and you'll need to re-`claude /login` on the host next time. Refresh tokens last weeks, so it's infrequent.

**`Claude configuration file not found at: /home/claude/.claude.json`** after the host config was edited. Bind-mounted single files are pinned to the inode at mount time. Some atomic-write tools (and Claude Code's own backup flow) replace the file rather than truncating it, which breaks the mount. Exit the container and relaunch.

**Container name collision** (`Conflict. The container name "/claude-sandbox-..." is already in use`). Rare — the name includes a hash of the worktree path. An old container is still running:

```
docker rm -f claude-sandbox-<basename>-<hash>
```

**Closing the laptop killed my session.** `launch.sh` uses `--rm`, so when the `claude` process inside the container exits (or its TTY drops), the container is removed. Reattach works *while* the container is still up; it's not a way to survive arbitrary disconnections. If you need sleep-survives sessions, run a long-lived `tmux` inside the container.

## What this deliberately doesn't do

- **No Container Use.** No nested branch namespace, no auto-commits to a separate remote. Git history lives on the host worktree.
- **No multi-agent orchestration.** One agent = one container. Superset runs multiple workspaces in parallel; each gets its own container via this script.
- **No GPU passthrough.** Local sandbox is CPU-only by design.
- **No published ports.** If you need to reach a service the agent runs, use `CLAUDE_SANDBOX_NETWORK=host` or forward yourself.
- **No Windows support.** macOS + Linux only.
