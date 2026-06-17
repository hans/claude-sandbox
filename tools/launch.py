#!/usr/bin/env python3
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
#
# chmod +x launch.py  (or the install step should handle this)

import atexit
import hashlib
import os
import platform
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Container naming
# ---------------------------------------------------------------------------

def _sha1_hex(s: str) -> str:
    """Return the full SHA-1 hex digest of a UTF-8 string."""
    return hashlib.sha1(s.encode()).hexdigest()


def container_name(pwd: str) -> str:
    """Return a deterministic, Docker-safe container name for *pwd*.

    Format: claude-sandbox-<sanitized-basename>-<sha1[:8] of pwd>

    Sanitise: replace any char outside [A-Za-z0-9_.-] with '-', collapse
    consecutive '-', strip trailing '-'.
    """
    basename = os.path.basename(pwd.rstrip("/"))
    sanitized = re.sub(r"[^A-Za-z0-9_.\-]", "-", basename)
    sanitized = re.sub(r"-+", "-", sanitized)
    sanitized = sanitized.rstrip("-")
    short_hash = _sha1_hex(pwd)[:8]
    return f"claude-sandbox-{sanitized}-{short_hash}"


# ---------------------------------------------------------------------------
# Prompt detection
# ---------------------------------------------------------------------------

def detect_prompt(argv_tail: list[str]) -> str:
    """Return the initial prompt string (may be empty).

    Priority: argv tail > CLAUDE_SANDBOX_PROMPT env > stdin (if not a TTY).
    """
    if argv_tail and argv_tail[0]:
        return " ".join(argv_tail)
    env_prompt = os.environ.get("CLAUDE_SANDBOX_PROMPT", "")
    if env_prompt:
        return env_prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


# ---------------------------------------------------------------------------
# Project (workload) configuration
# ---------------------------------------------------------------------------
# Per-project tooling assumptions (which venv dir, which caches to share, which
# subtrees to skip in the symlink scan) live in an optional .claude-sandbox.toml
# at the worktree root -- NOT baked into the sandbox core. Absent the file, the
# sandbox makes no language/toolchain assumptions. This repo ships a toml with
# the Python/uv defaults; a Rust or Go checkout drops in its own (or none).


@dataclass
class ProjectConfig:
    # Extra env vars to set inside the container (KEY=VALUE).
    env: dict = field(default_factory=dict)
    # Extra bind mounts as (host_path, container_path, mode) triples.
    mounts: list = field(default_factory=list)
    # Extra directory names to prune from the symlink-escape scan.
    extra_prune: set = field(default_factory=set)


def load_project_config(pwd: str) -> ProjectConfig:
    """Load .claude-sandbox.toml from *pwd*; return an empty config if absent.

    If the file exists but can't be parsed (or no TOML parser is available on
    this Python), fail loud rather than silently ignoring it -- a present
    config that says "don't use uv" must never be silently overridden by a
    default that does.
    """
    path = pathlib.Path(pwd) / ".claude-sandbox.toml"
    if not path.is_file():
        return ProjectConfig()

    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            print(
                f"claude-sandbox: found {path} but no TOML parser is available.",
                file=sys.stderr,
            )
            print(
                "  Run launch.py with Python 3.11+ or `pip install tomli`.",
                file=sys.stderr,
            )
            sys.exit(1)

    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001 -- surface any parse error loudly
        print(f"claude-sandbox: failed to parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    home = str(pathlib.Path.home())

    def _expand(p: str) -> str:
        return os.path.expanduser(os.path.expandvars(p)) if p else p

    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    mounts = []
    for m in data.get("mounts") or []:
        host = _expand(str(m.get("host", "")))
        container = str(m.get("container", "")) or host
        mode = str(m.get("mode", "rw"))
        if host:
            mounts.append((host, container, mode))
    extra_prune = {str(n) for n in (data.get("prune") or [])}
    return ProjectConfig(env=env, mounts=mounts, extra_prune=extra_prune)


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def preflight_checks(image: str) -> None:
    """Run all preflight checks; print an error and sys.exit on failure."""
    # docker binary present?
    if shutil.which("docker") is None:
        print("claude-sandbox: docker not found on PATH.", file=sys.stderr)
        print("  Install Docker Desktop (macOS) or the docker engine (Linux).", file=sys.stderr)
        sys.exit(127)

    # daemon reachable?
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print("claude-sandbox: docker daemon not reachable.", file=sys.stderr)
        print("  Is Docker Desktop running?", file=sys.stderr)
        sys.exit(1)

    # image present?
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        print(f"claude-sandbox: image '{image}' not found.", file=sys.stderr)
        print("  Build it from the repo root:", file=sys.stderr)
        print("    docker build -t claude-sandbox:latest .", file=sys.stderr)
        sys.exit(1)

    # ~/.claude/ dir and ~/.claude.json file present?
    home = pathlib.Path.home()
    if not (home / ".claude").is_dir() or not (home / ".claude.json").is_file():
        print("claude-sandbox: missing Claude Code host config.", file=sys.stderr)
        print("  Need both $HOME/.claude/ (dir) and $HOME/.claude.json (file).", file=sys.stderr)
        print("  Run `claude /login` on the host first to create them.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# macOS keychain credential helpers
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return platform.system() == "Darwin"


def extract_macos_credentials(creds_path: pathlib.Path) -> None:
    """Extract 'Claude Code-credentials' from the macOS keychain to *creds_path*.

    Only called on fresh-container path.  Skips silently on non-macOS.
    """
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        blob = result.stdout.rstrip("\n")
        old_umask = os.umask(0o077)
        try:
            creds_path.write_text(blob + "\n")
        finally:
            os.umask(old_umask)
    else:
        print("claude-sandbox: warning -- couldn't read 'Claude Code-credentials' from keychain.", file=sys.stderr)
        print("  Run `claude /login` on the host (so the keychain entry exists).", file=sys.stderr)
        print("  Continuing; the container may prompt you to log in instead.", file=sys.stderr)


def sync_credentials_back(macos_creds: str) -> None:
    """Sync *macos_creds* file back into the macOS keychain.

    Called via atexit; no-ops when macos_creds is empty or the file is absent.
    """
    if not macos_creds:
        return
    creds_path = pathlib.Path(macos_creds)
    if not creds_path.is_file():
        return
    refreshed = creds_path.read_text(errors="replace").strip()
    if not refreshed:
        return
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-s", "Claude Code-credentials",
            "-a", os.environ.get("USER", ""),
            "-w", refreshed,
            "-U",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        print("claude-sandbox: synced refreshed credentials back to keychain.", file=sys.stderr)
    else:
        print(
            "claude-sandbox: warning -- couldn't update keychain after session "
            "(will need to log in next time).",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Per-container temp copy of .claude.json
# ---------------------------------------------------------------------------

def make_claude_json_tmp(name: str) -> str:
    """Copy ~/.claude.json to a named temp file; return its path.

    Using a per-container copy prevents concurrent-write corruption when
    multiple sandboxes run in parallel.  The file holds only UI state
    (release notes seen, caches, watermarks).
    """
    src = pathlib.Path.home() / ".claude.json"
    fd, tmp_path = tempfile.mkstemp(prefix=f"claude-json-{name}-", dir="/tmp")
    os.close(fd)
    shutil.copy2(str(src), tmp_path)
    os.chmod(tmp_path, 0o600)
    return tmp_path


def cleanup_claude_json_tmp(tmp_path: str) -> None:
    """Remove the temp .claude.json copy; called via atexit."""
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Symlink mount helpers
# ---------------------------------------------------------------------------

def _resolve_path(p: str) -> str:
    """Return the real (canonical) path of *p*, or *p* on failure."""
    try:
        return str(pathlib.Path(p).resolve(strict=False))
    except Exception:
        return p


def _symlink_mount_mode(target: str, pwd: str) -> str:
    """Return 'rw' or 'ro' for the given resolved symlink target.

    Logic mirrors the Bash symlink_mount_mode() function:
      - CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW=1 → always rw
      - CLAUDE_SANDBOX_SYMLINK_RW_PATHS (colon-delimited prefixes) → rw if target matches
      - default → ro
    """
    if os.environ.get("CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW", "0") == "1":
        return "rw"
    rw_paths_raw = os.environ.get("CLAUDE_SANDBOX_SYMLINK_RW_PATHS", "results_scratch")
    if rw_paths_raw:
        for prefix in rw_paths_raw.split(":"):
            if not prefix:
                continue
            if not os.path.isabs(prefix):
                prefix = _resolve_path(os.path.join(pwd, prefix))
            if not prefix:
                continue
            if target == prefix or target.startswith(prefix + "/"):
                return "rw"
    return "ro"


def collect_symlink_mounts(pwd: str, extra_prune=None) -> list[tuple[str, str]]:
    """Return deduplicated (target, mode) pairs for external symlinks in *pwd*.

    Skips: .git plus common virtualenv/dep subtrees (.venv, .venv-container,
    venv, node_modules) and any names in *extra_prune* (from project config).
    Skips: targets inside the worktree.
    Skips: targets that don't exist on disk.
    Deduplication: if the same target appears with rw and ro, prefer rw.
    """
    _PRUNE_NAMES = {".git", ".venv", ".venv-container", "venv", "node_modules"}
    if extra_prune:
        _PRUNE_NAMES = _PRUNE_NAMES | set(extra_prune)
    pwd_real = _resolve_path(pwd)

    # Gather {target -> mode}; when both rw and ro appear, keep rw.
    target_modes: dict[str, str] = {}

    def _walk(dirpath: str) -> None:
        try:
            entries = list(os.scandir(dirpath))
        except PermissionError:
            return
        for entry in entries:
            name = entry.name
            # Prune special directories (check both the entry name and the
            # special case for .git which may also be a file at root level).
            if entry.is_dir(follow_symlinks=False) and name in _PRUNE_NAMES:
                continue
            if entry.is_symlink():
                target = _resolve_path(entry.path)
                if not target:
                    continue
                # Skip targets inside the worktree (covered by /workdir mount).
                if (
                    target == pwd
                    or target.startswith(pwd + "/")
                    or target == pwd_real
                    or target.startswith(pwd_real + "/")
                ):
                    continue
                # Skip non-existent targets (docker would refuse the mount).
                if not os.path.exists(target):
                    continue
                mode = _symlink_mount_mode(target, pwd)
                existing = target_modes.get(target)
                if existing is None or (existing == "ro" and mode == "rw"):
                    target_modes[target] = mode
            elif entry.is_dir(follow_symlinks=False):
                _walk(entry.path)

    _walk(pwd)

    # Sort by target then mode descending (rw > ro), return deduplicated list.
    return sorted(target_modes.items(), key=lambda kv: kv[0])


# ---------------------------------------------------------------------------
# Docker args builder
# ---------------------------------------------------------------------------

def build_docker_args(
    *,
    name: str,
    pwd: str,
    image: str,
    network: str,
    claude_json_tmp: str,
    project_config: "ProjectConfig | None" = None,
) -> list[str]:
    """Return the full argument list for `docker run` (excluding the image/cmd).

    This is a pure function (aside from reading env vars and the filesystem)
    so it can be unit-tested without invoking Docker. *project_config* defaults
    to auto-detection from the worktree's .claude-sandbox.toml.
    """
    if project_config is None:
        project_config = load_project_config(pwd)

    home = str(pathlib.Path.home())
    uid_gid = f"{os.getuid()}:{os.getgid()}"

    args = [
        "run", "-d", "--rm",
        "--name", name,
        "-v", f"{pwd}:/workdir",
        "-v", f"{home}/.claude:/home/claude/.claude",
        "-v", f"{claude_json_tmp}:/home/claude/.claude.json",
        "-v", f"{home}/.gitconfig:/home/claude/.gitconfig:ro",
        "-w", "/workdir",
        "-e", "HOME=/home/claude",
        "-u", uid_gid,
        "--network", network,
    ]

    # Project (workload) env vars and mounts from .claude-sandbox.toml.
    for key in sorted(project_config.env):
        args += ["-e", f"{key}={project_config.env[key]}"]
    for host_path, container_path, mode in project_config.mounts:
        args += ["-v", f"{host_path}:{container_path}:{mode}"]

    # Port 8888 mapping
    args += ["-p", "8888"]

    # Git worktree / submodule support: if $PWD/.git is a file, read the
    # gitdir pointer and bind-mount the parent .git directory.
    git_file = pathlib.Path(pwd) / ".git"
    if git_file.is_file():
        content = git_file.read_text(errors="replace")
        for line in content.splitlines():
            if line.startswith("gitdir:"):
                gitdir = line[len("gitdir:"):].strip()
                if gitdir:
                    parent_git = str(pathlib.Path(gitdir).parent.parent)
                    if pathlib.Path(parent_git).is_dir():
                        args += ["-v", f"{parent_git}:{parent_git}"]
                break

    # Optional SSH mount
    ssh_dir = pathlib.Path(home) / ".ssh"
    if os.environ.get("CLAUDE_SANDBOX_MOUNT_SSH", "0") == "1" and ssh_dir.is_dir():
        args += ["-v", f"{ssh_dir}:/home/claude/.ssh:ro"]

    # Symlink mounts
    if os.environ.get("CLAUDE_SANDBOX_MOUNT_SYMLINKS", "1") == "1":
        mounts = collect_symlink_mounts(pwd, extra_prune=project_config.extra_prune)
        for target, mode in mounts:
            args += ["-v", f"{target}:{target}:{mode}"]
            print(f"claude-sandbox: mounting symlink target {target} ({mode})", file=sys.stderr)

    # Forward ANTHROPIC_* env vars
    for key in sorted(os.environ):
        if key.startswith("ANTHROPIC_"):
            args += ["-e", key]

    # Make the host reachable as host.docker.internal from inside the container.
    # Docker Desktop (macOS/Windows) provides this automatically; on Linux we
    # need the explicit --add-host mapping.  Bridge-networked containers resolve
    # "localhost" to their own loopback, so Superset notification endpoints that
    # use localhost/127.0.0.1 would silently fail without this.
    args += ["--add-host", "host.docker.internal:host-gateway"]

    # Forward SUPERSET_* env vars, rewriting localhost/127.0.0.1 references to
    # host.docker.internal so that notification callbacks (e.g. PushNotification)
    # reach the host process rather than the container's own loopback.
    _localhost_re = re.compile(r'\b(localhost|127\.0\.0\.1)\b')
    for key in sorted(os.environ):
        if key.startswith("SUPERSET_"):
            value = os.environ[key]
            rewritten = _localhost_re.sub("host.docker.internal", value)
            args += ["-e", f"{key}={rewritten}"]

    # Mount SUPERSET_HOME_DIR if set and exists
    superset_home = os.environ.get("SUPERSET_HOME_DIR", "")
    if superset_home and pathlib.Path(superset_home).is_dir():
        args += ["-v", f"{superset_home}:{superset_home}"]

    return args


# ---------------------------------------------------------------------------
# Docker interaction helpers (thin wrappers; easy to monkeypatch in tests)
# ---------------------------------------------------------------------------

def container_is_running(name: str) -> bool:
    """Return True if a container with *name* is currently running."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    running = result.stdout.splitlines()
    return name in running


def docker_run(args: list[str], image: str) -> None:
    """Start the detached keepalive container (stdout discarded)."""
    subprocess.run(
        ["docker"] + args + [image, "sleep", "infinity"],
        stdout=subprocess.DEVNULL,
        check=True,
    )


def docker_exec(name: str, claude_argv: list[str]) -> int:
    """Attach an interactive claude session; return the exit code."""
    result = subprocess.run(
        ["docker", "exec", "-it", name, "/usr/local/bin/entrypoint.sh"] + claude_argv,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    image = os.environ.get("CLAUDE_SANDBOX_IMAGE", "claude-sandbox:latest")
    network = os.environ.get("CLAUDE_SANDBOX_NETWORK", "bridge")
    pwd = os.getcwd()

    # --- prompt detection ---------------------------------------------------
    prompt = detect_prompt(sys.argv[1:])

    # --- preflight ----------------------------------------------------------
    preflight_checks(image)

    # --- macOS keychain setup -----------------------------------------------
    # Register credential sync-back via atexit so it runs on every exit path
    # (normal exit, sys.exit, Ctrl-C / KeyboardInterrupt, and after reattach).
    macos_creds = ""
    if _is_macos():
        macos_creds = str(pathlib.Path.home() / ".claude" / ".credentials.json")
        atexit.register(sync_credentials_back, macos_creds)

    # --- container name -----------------------------------------------------
    name = container_name(pwd)

    # --- per-container temp copy of .claude.json ----------------------------
    claude_json_tmp = make_claude_json_tmp(name)
    atexit.register(cleanup_claude_json_tmp, claude_json_tmp)

    # --- build claude argv --------------------------------------------------
    claude_argv = ["claude"]
    if os.environ.get("CLAUDE_SANDBOX_SKIP_PERMISSIONS", "1") == "1":
        claude_argv.append("--dangerously-skip-permissions")
    if prompt:
        claude_argv.append(prompt)

    # --- reattach if container already running ------------------------------
    if container_is_running(name):
        rc = docker_exec(name, claude_argv)
        sys.exit(rc)

    # Fresh container: extract macOS keychain credentials now.
    if macos_creds:
        extract_macos_credentials(pathlib.Path(macos_creds))

    # --- build docker run args and launch -----------------------------------
    docker_args = build_docker_args(
        name=name,
        pwd=pwd,
        image=image,
        network=network,
        claude_json_tmp=claude_json_tmp,
    )

    docker_run(docker_args, image)

    # Attach an interactive claude session.
    rc = docker_exec(name, claude_argv)
    sys.exit(rc)


if __name__ == "__main__":
    main()
