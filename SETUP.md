# Setup: wiring claude-sandbox into Superset

A first-time walkthrough. After this you'll be able to open a Superset
workspace and have Claude Code run inside a Docker container with the
worktree bind-mounted, instead of running on the host.

## 1. Prereqs check

```
docker version            # Docker Desktop or engine must be running
claude --version          # Claude Code must be installed on the host
ls ~/.claude.json         # host login state must exist (run `claude /login` if not)
security find-generic-password -s "Claude Code-credentials" -w >/dev/null \
    && echo OK            # macOS only: keychain entry must exist
```

If any of these fails, fix it before continuing.

## 2. Build the image once

From this directory (`superset-docker-sandbox/`):

```
docker build -t claude-sandbox:latest .
```

Takes ~2 minutes on first build, seconds on subsequent rebuilds (layer
cache). You only need to do this again when the `Dockerfile` changes.

Verify:

```
docker run --rm claude-sandbox:latest claude --version
```

Should print the Claude Code version.

## 3. Put the launch scripts where Superset can find them

`launch.sh` is intentionally a relative-path script (`tools/launch.sh`)
so each repo carries its own copy and you can customize per project. Two
ways to deploy it.

**Per repo (recommended).** Copy the `tools/` directory into each
repo you want to sandbox:

```
cp -r tools /path/to/your/repo/
cd /path/to/your/repo/
git add tools/
git commit -m "Add Superset sandbox launcher"
```

The launcher reads `CLAUDE_SANDBOX_IMAGE` so projects can override the
image; the default `claude-sandbox:latest` works fine for most.

> The core is toolchain-neutral: it emits no `UV_*` env or cache mounts
> unless the worktree has a `.claude-sandbox.toml`. If your project uses
> Python/uv, copy this repo's `.claude-sandbox.toml` too (or write your own) —
> otherwise you lose the container-private venv. See README → *Per-project
> configuration*.

**Global (one script for everything).** If you'd rather not copy files into
each repo, drop `launch.sh` somewhere on your PATH and point Superset at
the absolute path:

```
install -m 755 tools/launch.sh ~/bin/claude-sandbox-launch
```

Then use `~/bin/claude-sandbox-launch` instead of `tools/launch.sh` in
the Superset config below.

## 4. Configure a Superset agent

Open Superset and go to **Settings &rarr; Agents**.

### 4a. Either duplicate the built-in `claude` preset and edit it...

That's the fastest path if you mainly want to keep all the built-in
behavior and just wrap the launcher.

### 4b. ...or create a new agent from scratch

Click **New agent** (or whatever the current UI calls it). Fill the fields
like this:

| Field                  | Value                                    |
|------------------------|------------------------------------------|
| Label                  | `Claude (sandbox)` (or anything memorable) |
| Enabled                | ON                                       |
| Command (No Prompt)    | `tools/launch.sh`                        |
| Command (With Prompt)  | `tools/launch.sh`                        |
| Prompt Command Suffix  | *(leave empty)*                          |
| Task Prompt Template   | *(leave default)*                        |
| Model Override         | *(leave empty unless you want one)*      |

If you went with the global-script approach in step 3, replace
`tools/launch.sh` with `/Users/you/bin/claude-sandbox-launch`.

Save the agent.

## 5. Launch it in a workspace

1. Open or create a workspace pointing at one of the repos where you
   deployed `tools/launch.sh`.
2. Pick your new "Claude (sandbox)" agent from the agent picker.
3. Type a prompt and hit launch.

What should happen:

- Superset starts a fresh terminal pane.
- You see `claude-sandbox` doing preflight checks (no output if everything
  is fine).
- Claude Code starts up and processes your prompt.

To verify it's actually running in a container, in another terminal:

```
docker ps --filter "name=claude-sandbox-"
```

You should see one container per active workspace.

## 6. Reattaching after a disconnect

If you close the Superset tab while Claude is still running, the container
keeps running in the background. Launching the same agent in the same
workspace again calls `docker exec` into the existing container so you
reattach to the same sandbox.

If the Claude process itself exits (you `/quit` or it crashes), the
container is removed (`--rm`) and the next launch starts a fresh one.
Worktree state is on disk, so nothing is lost.

## 7. (Optional) Per-project extension

The default `claude-sandbox:latest` is intentionally generic. To bake
project-specific tools into a derived image:

```
# In your project repo, e.g. ./Dockerfile.project
FROM claude-sandbox:latest
RUN pip install --break-system-packages mne nibabel
```

Build it:

```
docker build -t claude-sandbox-myproj -f Dockerfile.project .
```

Tell launch.sh to use it -- easiest is to set the env var in Superset's
agent **Environment** field:

```
CLAUDE_SANDBOX_IMAGE=claude-sandbox-myproj
```

Or export it before starting Superset.

## 8. Troubleshooting first-launch issues

| Symptom                                                          | Cause                                                                 | Fix                                                                                       |
|------------------------------------------------------------------|-----------------------------------------------------------------------|-------------------------------------------------------------------------------------------|
| `docker: command not found`                                      | Docker not on PATH                                                    | Open Docker Desktop / install Docker engine.                                              |
| `claude-sandbox: image 'claude-sandbox:latest' not found.`       | Image not built yet                                                   | `docker build -t claude-sandbox:latest .` from this dir.                                  |
| `Claude configuration file not found at: /home/claude/.claude.json` | Host `~/.claude.json` missing or container was started before fix | Run `claude /login` on host; `docker rm -f` any stale container; relaunch.                |
| `couldn't read 'Claude Code-credentials' from keychain`          | macOS keychain entry missing                                          | Run `claude /login` on the host.                                                          |
| `fatal: not a git repository` inside the container               | Worktree's `.git` pointer can't resolve                               | `launch.sh` should be auto-mounting the parent `.git` -- check the script is current.     |
| `permission denied` writing files                                | Host UID isn't matching the bind mount                                | `launch.sh` uses `-u $(id -u):$(id -g)` so this should just work; check you haven't overridden it. |

For deeper details on what each piece does, see `README.md`.
