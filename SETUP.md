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

## 2. Get the image once

A prebuilt image is published on Docker Hub, and it's the launcher's
default — so you don't have to build *or* retag anything. Just pull it:

```
docker pull jrgauthier/claude-sandbox
```

Prefer to build it yourself — to pin a base image or hack on the
`Dockerfile`? From this directory:

```
docker build -t jrgauthier/claude-sandbox:latest .
```

Takes ~2 minutes on first build, seconds on subsequent rebuilds (layer
cache). You only need to do this again when the `Dockerfile` changes.

To use a different image entirely, set `CLAUDE_SANDBOX_IMAGE` (e.g. in the
agent's Environment field in step 4).

Verify (either way):

```
docker run --rm jrgauthier/claude-sandbox:latest claude --version
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
image; the default `jrgauthier/claude-sandbox:latest` works fine for most.

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

The default `jrgauthier/claude-sandbox:latest` is intentionally generic. To
bake project-specific tools into a derived image:

```
# In your project repo, e.g. ./Dockerfile.project
FROM jrgauthier/claude-sandbox:latest
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
| `claude-sandbox: image 'jrgauthier/claude-sandbox:latest' not found.` | Image not pulled/built yet                                       | `docker pull jrgauthier/claude-sandbox` (or `docker build -t jrgauthier/claude-sandbox:latest .`). |
| `Claude configuration file not found at: /home/claude/.claude.json` | Host `~/.claude.json` missing or container was started before fix | Run `claude /login` on host; `docker rm -f` any stale container; relaunch.                |
| `couldn't read 'Claude Code-credentials' from keychain`          | macOS keychain entry missing                                          | Run `claude /login` on the host.                                                          |
| `fatal: not a git repository` inside the container               | Worktree's `.git` pointer can't resolve                               | `launch.sh` should be auto-mounting the parent `.git` -- check the script is current.     |
| `permission denied` writing files                                | Host UID isn't matching the bind mount                                | `launch.sh` uses `-u $(id -u):$(id -g)` so this should just work; check you haven't overridden it. |

## 9. (Maintainers) CI and image publishing

Two GitHub Actions workflows live in `.github/workflows/`:

- **`ci.yml`** — on every push and PR: runs `pytest tools/tests` on Python
  3.9–3.12 and builds the image (no push) as a Dockerfile smoke test. No
  secrets needed.
- **`release.yml`** — on a pushed version tag (`git tag v0.2 && git push
  origin v0.2`): builds a multi-arch image (`linux/amd64,linux/arm64`) and
  pushes `jrgauthier/claude-sandbox:<tag>` and `:latest` to Docker Hub.

`release.yml` needs two repository secrets — **Settings → Secrets and
variables → Actions → New repository secret**:

| Secret               | Value                                                                  |
|----------------------|------------------------------------------------------------------------|
| `DOCKERHUB_USERNAME` | your Docker Hub account (`jrgauthier`)                                  |
| `DOCKERHUB_TOKEN`    | a Docker Hub **access token** (Docker Hub → Account → Security), not your password |

Cutting a release is then just: bump the tag, push it, and the multi-arch
image lands on Docker Hub automatically.

For deeper details on what each piece does, see `README.md`.
