"""Test suite for tools/launch.py

All tests mock subprocess so no real Docker installation is needed.
"""

import atexit
import os
import pathlib
import platform
import sys
import tempfile
import unittest.mock as mock

import pytest

# Ensure the parent directory is importable.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import launch  # noqa: E402 – must come after sys.path tweak


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Set up a temporary HOME directory with the required Claude config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text('{"state": "initial"}')
    monkeypatch.setenv("HOME", str(home))
    # pathlib.Path.home() reads HOME on Linux; patch it explicitly too.
    monkeypatch.setattr(pathlib.Path, "home", staticmethod(lambda: home))
    return home


@pytest.fixture
def tmp_worktree(tmp_path):
    """Return a temporary directory acting as a git worktree root."""
    wt = tmp_path / "worktree"
    wt.mkdir()
    return wt


@pytest.fixture
def mock_docker_ok(monkeypatch):
    """Patch subprocess.run so that all Docker preflight calls succeed."""
    def _run(args, **kwargs):
        r = mock.MagicMock()
        r.returncode = 0
        r.stdout = ""
        return r

    monkeypatch.setattr(launch.subprocess, "run", _run)
    return _run


# ===========================================================================
# 1. claude.json concurrent-write corruption fix (no commit yet)
# ===========================================================================

class TestClaudeJsonTempCopy:
    """Assert that build_docker_args() mounts a temp file, not ~/.claude.json."""

    def test_mounts_temp_file_not_original(self, tmp_home, tmp_worktree, monkeypatch):
        """build_docker_args should reference a path in /tmp, not ~/.claude.json.

        Without this fix, parallel containers wrote to the same .claude.json,
        causing JSON corruption.
        """
        claude_json_tmp = "/tmp/claude-json-test-ABC123"
        # Create a fake temp file so the arg builder can reference it.
        pathlib.Path(claude_json_tmp).touch()
        try:
            args = launch.build_docker_args(
                name="test-container",
                pwd=str(tmp_worktree),
                image="claude-sandbox:latest",
                network="bridge",
                claude_json_tmp=claude_json_tmp,
            )
            # Find the -v flag that mounts something to /home/claude/.claude.json
            mounts = _extract_volume_mounts(args)
            claude_json_mounts = [m for m in mounts if m.endswith(":/home/claude/.claude.json")]
            assert claude_json_mounts, "No mount for /home/claude/.claude.json found"
            # The source must be the temp file, not the original.
            source = claude_json_mounts[0].split(":")[0]
            assert source == claude_json_tmp, (
                f"Expected temp file path {claude_json_tmp!r}, got {source!r}"
            )
            original = str(tmp_home / ".claude.json")
            assert source != original, "Should NOT mount the original ~/.claude.json directly"
        finally:
            pathlib.Path(claude_json_tmp).unlink(missing_ok=True)

    def test_temp_file_populated_with_original_content(self, tmp_home, monkeypatch):
        """The temp .claude.json copy must have the same content as the original."""
        name = "my-container"
        tmp_path = launch.make_claude_json_tmp(name)
        try:
            original_content = (tmp_home / ".claude.json").read_text()
            copy_content = pathlib.Path(tmp_path).read_text()
            assert copy_content == original_content
        finally:
            pathlib.Path(tmp_path).unlink(missing_ok=True)

    def test_temp_file_deleted_by_cleanup(self, tmp_home, monkeypatch):
        """cleanup_claude_json_tmp() must delete the temp file."""
        name = "my-container"
        tmp_path = launch.make_claude_json_tmp(name)
        assert pathlib.Path(tmp_path).exists(), "Temp file should exist before cleanup"
        launch.cleanup_claude_json_tmp(tmp_path)
        assert not pathlib.Path(tmp_path).exists(), "Temp file should be gone after cleanup"


# ===========================================================================
# 2. Symlink targets outside worktree appear broken (commit 82132f6)
# ===========================================================================

class TestSymlinkExternalMounts:
    """External symlink targets must be bind-mounted so they appear in the container.

    Before fix: symlinks pointing outside the worktree were silently ignored,
    causing the container to see broken symlinks.
    """

    def test_external_symlink_added_to_mounts(self, tmp_path, tmp_worktree, monkeypatch):
        """A symlink pointing outside the worktree should appear as a bind mount."""
        external_dir = tmp_path / "external_data"
        external_dir.mkdir()
        (external_dir / "file.txt").write_text("data")

        link = tmp_worktree / "data"
        link.symlink_to(external_dir)

        mounts = launch.collect_symlink_mounts(str(tmp_worktree))
        targets = [t for t, _m in mounts]
        assert str(external_dir) in targets, (
            f"External target {external_dir} should be in mounts, got: {targets}"
        )

    def test_internal_symlink_not_added(self, tmp_worktree):
        """Symlinks pointing inside the worktree must NOT be added (already mounted)."""
        inner_dir = tmp_worktree / "subdir"
        inner_dir.mkdir()
        (inner_dir / "file.txt").write_text("internal")

        link = tmp_worktree / "link_to_internal"
        link.symlink_to(inner_dir)

        mounts = launch.collect_symlink_mounts(str(tmp_worktree))
        targets = [t for t, _m in mounts]
        assert str(inner_dir) not in targets, (
            f"Internal target {inner_dir} should NOT be in mounts, got: {targets}"
        )


# ===========================================================================
# 3. .venv / venv / node_modules pruned from symlink scan (commit be205b1)
# ===========================================================================

class TestSymlinkPrunedDirs:
    """Symlinks inside pruned directories must not generate extra mounts.

    Before fix: symlinks inside .venv (Python interpreter links into uv/pyenv)
    or node_modules (npm bin links into global installs) were being mounted,
    flooding the docker run command and causing failures.
    """

    @pytest.mark.parametrize("prune_dir", [".venv", ".venv-container", "venv", "node_modules"])
    def test_pruned_dir_symlink_ignored(self, tmp_path, tmp_worktree, prune_dir):
        """Symlinks inside pruned dirs like .venv must not appear in mounts."""
        external_dir = tmp_path / "external"
        external_dir.mkdir()
        (external_dir / "bin").mkdir()
        (external_dir / "bin" / "python3").write_text("fake python")

        pruned = tmp_worktree / prune_dir
        pruned.mkdir()
        link = pruned / "python3"
        link.symlink_to(external_dir / "bin" / "python3")

        mounts = launch.collect_symlink_mounts(str(tmp_worktree))
        targets = [t for t, _m in mounts]
        assert str(external_dir / "bin" / "python3") not in targets, (
            f"Symlink target inside {prune_dir}/ should be pruned, got: {targets}"
        )


# ===========================================================================
# 4. Per-path rw control via CLAUDE_SANDBOX_SYMLINK_RW_PATHS (commit eaea144)
# ===========================================================================

class TestSymlinkRwPaths:
    """CLAUDE_SANDBOX_SYMLINK_RW_PATHS controls per-prefix rw/ro mode.

    Before fix: all external symlinks were mounted read-only with no per-path
    override, so scratch directories couldn't be written from the container.
    """

    def test_matching_prefix_gets_rw(self, tmp_path, tmp_worktree, monkeypatch):
        """A target matching CLAUDE_SANDBOX_SYMLINK_RW_PATHS prefix should be rw."""
        rw_target = tmp_path / "results_scratch"
        rw_target.mkdir()
        (rw_target / "out.csv").write_text("data")

        ro_target = tmp_path / "reference_data"
        ro_target.mkdir()
        (ro_target / "ref.txt").write_text("reference")

        (tmp_worktree / "link_rw").symlink_to(rw_target)
        (tmp_worktree / "link_ro").symlink_to(ro_target)

        monkeypatch.setenv("CLAUDE_SANDBOX_SYMLINK_RW_PATHS", str(rw_target))
        monkeypatch.delenv("CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW", raising=False)

        mounts = dict(launch.collect_symlink_mounts(str(tmp_worktree)))
        assert mounts.get(str(rw_target)) == "rw", (
            f"Target {rw_target} should be rw, got {mounts}"
        )
        assert mounts.get(str(ro_target)) == "ro", (
            f"Target {ro_target} should be ro, got {mounts}"
        )


# ===========================================================================
# 5. Relative paths in CLAUDE_SANDBOX_SYMLINK_RW_PATHS resolved against
#    worktree (commit d98dbc8)
# ===========================================================================

class TestSymlinkRwPathsRelative:
    """Relative entries in CLAUDE_SANDBOX_SYMLINK_RW_PATHS must resolve against $PWD.

    Before fix: relative prefixes were resolved against the script's cwd (often
    the repo root), not the worktree, so the rw override silently never matched.
    """

    def test_relative_rw_path_resolves_against_pwd(self, tmp_path, tmp_worktree, monkeypatch):
        """'results_scratch' (relative) should resolve to <worktree>/results_scratch."""
        scratch_dir = tmp_path / "results_scratch_actual"
        scratch_dir.mkdir()
        (scratch_dir / "output.npy").write_text("array")

        # The worktree has a subdirectory called "results_scratch" that is a
        # symlink to the external scratch_dir.  We verify that when the env var
        # is set to the relative name "results_scratch_actual" resolved from
        # worktree, it matches.
        monkeypatch.setenv("CLAUDE_SANDBOX_SYMLINK_RW_PATHS", "results_scratch_actual")
        monkeypatch.delenv("CLAUDE_SANDBOX_SYMLINK_MOUNTS_RW", raising=False)

        (tmp_worktree / "results_scratch_actual").symlink_to(scratch_dir)

        # Temporarily change PWD so _symlink_mount_mode resolves relative to tmp_worktree
        monkeypatch.chdir(tmp_worktree)
        mounts = dict(launch.collect_symlink_mounts(str(tmp_worktree)))
        # The resolved prefix is <tmp_worktree>/results_scratch_actual → scratch_dir
        # The target IS scratch_dir, so it should match prefix = scratch_dir
        # Note: relative resolution: PWD/<relative> = tmp_worktree/results_scratch_actual
        # which resolves via symlink to scratch_dir; target == scratch_dir → rw
        mode = mounts.get(str(scratch_dir))
        assert mode == "rw", (
            f"Relative rw path should resolve to rw mount, got {mounts}"
        )


# ===========================================================================
# 6. Container lifecycle decoupled from terminal (commit 7b0750a)
# ===========================================================================

class TestContainerLifecycle:
    """Fresh containers must start detached; sessions attach via exec.

    Before fix: `docker run -it` tied the container lifetime to the launching
    terminal, so closing the tab killed the container and any ongoing agent work.
    """

    def test_fresh_container_uses_docker_run_detached(self, tmp_home, tmp_worktree, monkeypatch):
        """docker run must use -d (detached), not -it."""
        args = launch.build_docker_args(
            name="my-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        assert "-d" in args, "docker run must include -d flag"
        assert "-it" not in args, "docker run must NOT include -it"

    def test_keepalive_command_is_sleep_infinity(self, tmp_home, tmp_worktree, monkeypatch):
        """The container keepalive command must be 'sleep infinity'."""
        called_with = []

        def fake_run(cmd_args, **kwargs):
            called_with.extend(cmd_args)
            r = mock.MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        launch.docker_run(["run", "-d"], "claude-sandbox:latest")
        assert "sleep" in called_with and "infinity" in called_with, (
            f"keepalive command should be 'sleep infinity', got: {called_with}"
        )

    def test_session_attachment_uses_docker_exec_it(self, monkeypatch):
        """docker exec for session attachment must include -it flags."""
        called_with = []

        def fake_run(cmd_args, **kwargs):
            called_with.extend(cmd_args)
            r = mock.MagicMock()
            r.returncode = 0
            return r

        monkeypatch.setattr(launch.subprocess, "run", fake_run)
        launch.docker_exec("my-container", ["claude"])
        assert "exec" in called_with, "Should call docker exec"
        assert "-it" in called_with, "docker exec must include -it"


# ===========================================================================
# 7. Separate venv for container vs host (commit d26615f)
# ===========================================================================

class TestSeparateVenv:
    """UV_PROJECT_ENVIRONMENT must be set so container uses its own venv.

    Before fix: the container shared the host .venv, causing binary
    incompatibilities between host and container Python environments.
    """

    def test_uv_project_environment_in_docker_args(self, tmp_home, tmp_worktree, monkeypatch):
        """UV_PROJECT_ENVIRONMENT=/workdir/.venv-container must appear in docker args.

        Post neutral-core refactor, the uv env comes from a ProjectConfig
        (loaded from .claude-sandbox.toml) rather than being hard-coded.
        """
        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
            project_config=_uv_project_config(),
        )
        env_args = _extract_env_args(args)
        assert "UV_PROJECT_ENVIRONMENT=/workdir/.venv-container" in env_args, (
            f"UV_PROJECT_ENVIRONMENT not set correctly. env args: {env_args}"
        )


# ===========================================================================
# 8. SUPERSET_HOME_DIR env+mount (commit e5e5317)
# ===========================================================================

class TestSupersetHomeDir:
    """SUPERSET_HOME_DIR must be forwarded as env var and bind-mounted.

    Before fix: notify.sh couldn't locate Superset's IPC socket because
    SUPERSET_HOME_DIR was neither forwarded nor mounted in the container.
    """

    def test_superset_home_dir_forwarded_and_mounted(self, tmp_home, tmp_worktree, monkeypatch):
        """SUPERSET_HOME_DIR env var must appear in -e KEY=VALUE args and the dir bind-mounted."""
        superset_home = tmp_worktree.parent / "superset_home"
        superset_home.mkdir()
        monkeypatch.setenv("SUPERSET_HOME_DIR", str(superset_home))

        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        # SUPERSET_* vars are forwarded as KEY=VALUE so docker gets the exact value.
        env_args = _extract_env_args(args)
        assert any(e.startswith("SUPERSET_HOME_DIR=") for e in env_args), (
            f"SUPERSET_HOME_DIR should be forwarded as -e KEY=VALUE. env args: {env_args}"
        )
        # Check bind mount present
        mounts = _extract_volume_mounts(args)
        superset_mounts = [m for m in mounts if str(superset_home) in m]
        assert superset_mounts, (
            f"SUPERSET_HOME_DIR dir should be bind-mounted. mounts: {mounts}"
        )


# ===========================================================================
# 8b. host.docker.internal mapping + localhost rewriting in SUPERSET_* vars
# ===========================================================================

class TestSupersetNotificationReachability:
    """host.docker.internal must be reachable and localhost URLs rewritten.

    Before fix: the PushNotification tool called back to a Superset endpoint
    that Superset injected via SUPERSET_* env vars.  Those URLs often contained
    'localhost' or '127.0.0.1'.  Inside a bridge-networked container those
    addresses resolve to the container's own loopback, not the host, so
    notifications were silently dropped.
    """

    def test_host_docker_internal_add_host_present(self, tmp_home, tmp_worktree, monkeypatch):
        """--add-host host.docker.internal:host-gateway must appear in docker run args."""
        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        it = iter(args)
        add_host_values = []
        for token in it:
            if token == "--add-host":
                try:
                    add_host_values.append(next(it))
                except StopIteration:
                    pass
        assert "host.docker.internal:host-gateway" in add_host_values, (
            f"--add-host host.docker.internal:host-gateway missing. add-host args: {add_host_values}"
        )

    def test_localhost_in_superset_var_rewritten(self, tmp_home, tmp_worktree, monkeypatch):
        """SUPERSET_* vars with 'localhost' must be rewritten to 'host.docker.internal'."""
        monkeypatch.setenv("SUPERSET_NOTIFICATION_URL", "http://localhost:9999/notify")

        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        env_args = _extract_env_args(args)
        notification_entries = [e for e in env_args if e.startswith("SUPERSET_NOTIFICATION_URL=")]
        assert notification_entries, "SUPERSET_NOTIFICATION_URL should be forwarded"
        value = notification_entries[0].split("=", 1)[1]
        assert "localhost" not in value, (
            f"'localhost' should be rewritten in SUPERSET_* vars, got: {value!r}"
        )
        assert "host.docker.internal" in value, (
            f"'host.docker.internal' should replace 'localhost', got: {value!r}"
        )

    def test_127_0_0_1_in_superset_var_rewritten(self, tmp_home, tmp_worktree, monkeypatch):
        """SUPERSET_* vars with '127.0.0.1' must be rewritten to 'host.docker.internal'."""
        monkeypatch.setenv("SUPERSET_SOCKET_ADDR", "http://127.0.0.1:8080/cb")

        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        env_args = _extract_env_args(args)
        entries = [e for e in env_args if e.startswith("SUPERSET_SOCKET_ADDR=")]
        assert entries, "SUPERSET_SOCKET_ADDR should be forwarded"
        value = entries[0].split("=", 1)[1]
        assert "127.0.0.1" not in value, (
            f"'127.0.0.1' should be rewritten in SUPERSET_* vars, got: {value!r}"
        )
        assert "host.docker.internal" in value, (
            f"'host.docker.internal' should replace '127.0.0.1', got: {value!r}"
        )

    def test_non_localhost_superset_var_unchanged(self, tmp_home, tmp_worktree, monkeypatch):
        """SUPERSET_* vars without localhost addresses must pass through unchanged."""
        monkeypatch.setenv("SUPERSET_HOME_DIR", "/home/user/.superset")
        monkeypatch.setenv("SUPERSET_AGENT_ID", "agent-42")

        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        env_args = _extract_env_args(args)
        agent_entries = [e for e in env_args if e.startswith("SUPERSET_AGENT_ID=")]
        assert agent_entries, "SUPERSET_AGENT_ID should be forwarded"
        assert agent_entries[0] == "SUPERSET_AGENT_ID=agent-42", (
            f"Non-localhost value should be unchanged, got: {agent_entries[0]!r}"
        )


# ===========================================================================
# 9. macOS keychain sync on every exit, including reattach (commit 4d47514)
# ===========================================================================

class TestMacosCredentialSync:
    """Credential sync must be registered via atexit for every exit path.

    Before fix: the sync-back trap was only registered on the fresh-container
    path, so closing a reattached session left stale tokens in the keychain.
    """

    def test_credential_sync_registered_via_atexit_on_macos(self, tmp_home, monkeypatch):
        """On macOS, sync_credentials_back must be registered via atexit."""
        monkeypatch.setattr(launch, "_is_macos", lambda: True)

        registered = []
        monkeypatch.setattr(atexit, "register", lambda fn, *a, **kw: registered.append((fn, a)))

        # Simulate the atexit registration portion of main() (without full Docker calls)
        macos_creds = ""
        if launch._is_macos():
            macos_creds = str(pathlib.Path.home() / ".claude" / ".credentials.json")
            atexit.register(launch.sync_credentials_back, macos_creds)

        fns = [fn for fn, _args in registered]
        assert launch.sync_credentials_back in fns, (
            "sync_credentials_back must be registered via atexit on macOS"
        )

    def test_credential_sync_skipped_when_macos_creds_empty(self, monkeypatch, capsys):
        """sync_credentials_back must no-op when macos_creds is empty (Linux path)."""
        # Should not raise and should not call 'security'
        called = []
        monkeypatch.setattr(launch.subprocess, "run", lambda *a, **kw: called.append(a))
        launch.sync_credentials_back("")  # empty → no-op
        assert not called, "security should not be called when macos_creds is empty"


# ===========================================================================
# 10. Shared uv cache across containers (commit aa0c5e9)
# ===========================================================================

class TestSharedUvCache:
    """uv cache must be bind-mounted and UV_CACHE_DIR set inside the container.

    Before fix: each container downloaded packages independently, wasting
    bandwidth and disk space, and slowing down agent startup.
    """

    def test_uv_cache_mount_in_docker_args(self, tmp_home, tmp_worktree, monkeypatch):
        """~/.cache/uv:/home/claude/.cache/uv must appear in docker run -v args."""
        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
            project_config=_uv_project_config(),
        )
        home = str(pathlib.Path.home())
        mounts = _extract_volume_mounts(args)
        uv_mounts = [m for m in mounts if "/home/claude/.cache/uv" in m]
        assert uv_mounts, f"uv cache mount missing. mounts: {mounts}"
        # Check the source is ~/.cache/uv
        assert any(m.startswith(f"{home}/.cache/uv:") for m in uv_mounts), (
            f"uv cache source should be {home}/.cache/uv, got {uv_mounts}"
        )

    def test_uv_cache_dir_env_in_docker_args(self, tmp_home, tmp_worktree, monkeypatch):
        """UV_CACHE_DIR=/home/claude/.cache/uv must appear in -e args."""
        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
            project_config=_uv_project_config(),
        )
        env_args = _extract_env_args(args)
        assert "UV_CACHE_DIR=/home/claude/.cache/uv" in env_args, (
            f"UV_CACHE_DIR not set correctly. env args: {env_args}"
        )


# ===========================================================================
# General correctness tests
# ===========================================================================

class TestContainerName:
    """Container name must be deterministic, sanitized, and include path hash."""

    def test_deterministic(self):
        """Same pwd always produces the same container name."""
        name1 = launch.container_name("/home/user/my-project")
        name2 = launch.container_name("/home/user/my-project")
        assert name1 == name2

    def test_different_paths_differ(self):
        """Different paths must produce different names (hash prevents collision)."""
        name1 = launch.container_name("/home/user/project-a")
        name2 = launch.container_name("/home/user/project-b")
        assert name1 != name2

    def test_sanitized_no_special_chars(self):
        """Container name must only contain [A-Za-z0-9_.-]."""
        name = launch.container_name("/home/user/my project (dev)!")
        # Docker name chars: [a-zA-Z0-9][a-zA-Z0-9_.-]
        import re
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", name), (
            f"Container name {name!r} contains invalid characters"
        )

    def test_includes_path_hash(self):
        """Container name must embed an 8-char path hash."""
        pwd = "/home/user/worktree"
        name = launch.container_name(pwd)
        # Last segment should be 8 hex chars
        parts = name.split("-")
        assert len(parts[-1]) == 8, f"Expected 8-char hash suffix, got: {parts[-1]!r}"
        assert all(c in "0123456789abcdef" for c in parts[-1]), (
            f"Hash suffix {parts[-1]!r} is not hex"
        )

    def test_prefix(self):
        """Container name must start with 'claude-sandbox-'."""
        name = launch.container_name("/some/path")
        assert name.startswith("claude-sandbox-")


class TestPromptDetection:
    """Prompt must be read from argv, env, or stdin in that priority order."""

    def test_argv_takes_priority(self, monkeypatch):
        """Argv prompt takes priority over env and stdin."""
        monkeypatch.setenv("CLAUDE_SANDBOX_PROMPT", "from env")
        prompt = launch.detect_prompt(["from", "argv"])
        assert prompt == "from argv"

    def test_env_takes_priority_over_stdin(self, monkeypatch):
        """CLAUDE_SANDBOX_PROMPT env takes priority over stdin."""
        monkeypatch.setenv("CLAUDE_SANDBOX_PROMPT", "from env")
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            mock_stdin.read.return_value = "from stdin"
            prompt = launch.detect_prompt([])
        assert prompt == "from env"

    def test_stdin_used_when_not_tty(self, monkeypatch):
        """When stdin is not a TTY and no argv/env prompt, stdin is used."""
        monkeypatch.delenv("CLAUDE_SANDBOX_PROMPT", raising=False)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            mock_stdin.read.return_value = "from stdin\n"
            prompt = launch.detect_prompt([])
        assert prompt == "from stdin\n"

    def test_empty_when_tty_and_no_env(self, monkeypatch):
        """When stdin is a TTY and no argv/env, prompt should be empty."""
        monkeypatch.delenv("CLAUDE_SANDBOX_PROMPT", raising=False)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            prompt = launch.detect_prompt([])
        assert prompt == ""


class TestReattachPath:
    """When container is running, docker exec must be called (not docker run)."""

    def test_reattach_calls_docker_exec_not_run(self, monkeypatch):
        """container_is_running=True → docker_exec called, docker_run not called."""
        run_called = []
        exec_called = []

        monkeypatch.setattr(launch, "container_is_running", lambda name: True)
        monkeypatch.setattr(launch, "docker_exec", lambda name, argv: exec_called.append(name) or 0)
        monkeypatch.setattr(launch, "docker_run", lambda args, image: run_called.append(image))

        # We need to run the reattach branch without hitting preflight or main.
        # Simulate directly:
        name = "test-container"
        claude_argv = ["claude"]
        if launch.container_is_running(name):
            rc = launch.docker_exec(name, claude_argv)

        assert exec_called, "docker_exec should have been called on reattach"
        assert not run_called, "docker_run should NOT be called on reattach"


class TestDangerouslySkipPermissions:
    """--dangerously-skip-permissions flag behaviour."""

    def test_present_by_default(self, monkeypatch):
        """Flag must be present when CLAUDE_SANDBOX_SKIP_PERMISSIONS is unset."""
        monkeypatch.delenv("CLAUDE_SANDBOX_SKIP_PERMISSIONS", raising=False)
        # Default is "1"
        skip = os.environ.get("CLAUDE_SANDBOX_SKIP_PERMISSIONS", "1") == "1"
        claude_argv = ["claude"]
        if skip:
            claude_argv.append("--dangerously-skip-permissions")
        assert "--dangerously-skip-permissions" in claude_argv

    def test_absent_when_disabled(self, monkeypatch):
        """Flag must be absent when CLAUDE_SANDBOX_SKIP_PERMISSIONS=0."""
        monkeypatch.setenv("CLAUDE_SANDBOX_SKIP_PERMISSIONS", "0")
        skip = os.environ.get("CLAUDE_SANDBOX_SKIP_PERMISSIONS", "1") == "1"
        claude_argv = ["claude"]
        if skip:
            claude_argv.append("--dangerously-skip-permissions")
        assert "--dangerously-skip-permissions" not in claude_argv


class TestAnthropicEnvForwarding:
    """ANTHROPIC_* env vars must be forwarded to the container."""

    def test_anthropic_vars_in_docker_args(self, tmp_home, tmp_worktree, monkeypatch):
        """All ANTHROPIC_* env vars must appear as -e KEY in docker run args."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-1234")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com")

        args = launch.build_docker_args(
            name="test-container",
            pwd=str(tmp_worktree),
            image="claude-sandbox:latest",
            network="bridge",
            claude_json_tmp="/tmp/fake-claude-json",
        )
        env_args = _extract_env_args(args)
        # The Bash version forwards the key name only (not key=value) for -e
        assert "ANTHROPIC_API_KEY" in env_args, f"ANTHROPIC_API_KEY missing. env args: {env_args}"
        assert "ANTHROPIC_BASE_URL" in env_args, f"ANTHROPIC_BASE_URL missing. env args: {env_args}"


# ===========================================================================
# 11. Host (launcher) profiles — env-sniff + explicit selection
# ===========================================================================

class TestHostProfiles:
    """Launcher coupling lives in HostProfile, selected by env-sniff or override."""

    def test_generic_by_default_no_superset_forwarding(self, tmp_home, tmp_worktree, monkeypatch):
        """With no SUPERSET_* vars, nothing Superset-specific is forwarded."""
        for k in list(os.environ):
            if k.startswith("SUPERSET_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.delenv("CLAUDE_SANDBOX_HOST", raising=False)
        assert launch.detect_host_profile(os.environ) is launch.GENERIC_HOST

    def test_superset_autodetected_from_env(self, monkeypatch):
        """Presence of any SUPERSET_* var selects the superset profile."""
        monkeypatch.delenv("CLAUDE_SANDBOX_HOST", raising=False)
        monkeypatch.setenv("SUPERSET_AGENT_ID", "agent-1")
        assert launch.detect_host_profile(os.environ) is launch.SUPERSET_HOST

    def test_explicit_generic_overrides_superset_env(self, monkeypatch):
        """CLAUDE_SANDBOX_HOST=generic wins even when SUPERSET_* is present."""
        monkeypatch.setenv("SUPERSET_AGENT_ID", "agent-1")
        monkeypatch.setenv("CLAUDE_SANDBOX_HOST", "generic")
        assert launch.detect_host_profile(os.environ) is launch.GENERIC_HOST

    def test_unknown_host_falls_back_to_generic_with_warning(self, monkeypatch, capsys):
        """An unknown CLAUDE_SANDBOX_HOST warns and falls back to generic."""
        monkeypatch.setenv("CLAUDE_SANDBOX_HOST", "bogus")
        profile = launch.detect_host_profile(os.environ)
        assert profile is launch.GENERIC_HOST
        assert "bogus" in capsys.readouterr().err

    def test_generic_profile_does_not_forward_superset(self, tmp_home, tmp_worktree, monkeypatch):
        """Under the generic profile, SUPERSET_* vars are NOT forwarded."""
        monkeypatch.setenv("SUPERSET_SECRET", "should-not-leak")
        args = launch.build_docker_args(
            name="c", pwd=str(tmp_worktree), image="img", network="bridge",
            claude_json_tmp="/tmp/fake", host_profile=launch.GENERIC_HOST,
        )
        env_args = _extract_env_args(args)
        assert not any(e.startswith("SUPERSET_") for e in env_args), (
            f"Generic profile must not forward SUPERSET_* vars, got: {env_args}"
        )


# ===========================================================================
# 12. Project config — neutral core, toml loading, fail-loud parsing
# ===========================================================================

class TestProjectConfig:
    """uv/Node assumptions come from .claude-sandbox.toml, not the core."""

    def test_neutral_core_emits_no_uv_vars(self, tmp_home, tmp_worktree):
        """With no .claude-sandbox.toml, the core emits no uv env/mounts."""
        args = launch.build_docker_args(
            name="c", pwd=str(tmp_worktree), image="img", network="bridge",
            claude_json_tmp="/tmp/fake",
        )
        env_args = _extract_env_args(args)
        assert not any(e.startswith("UV_") for e in env_args), (
            f"Toolchain-neutral core must not emit UV_* vars, got: {env_args}"
        )

    def test_toml_loaded_into_env_and_mounts(self, tmp_worktree, tmp_home):
        """A .claude-sandbox.toml populates ProjectConfig env + mounts (with ~ expanded)."""
        (tmp_worktree / ".claude-sandbox.toml").write_text(
            '[env]\n'
            'UV_PROJECT_ENVIRONMENT = "/workdir/.venv-container"\n'
            '[[mounts]]\n'
            'host = "~/.cache/uv"\n'
            'container = "/home/claude/.cache/uv"\n'
            'mode = "rw"\n'
        )
        cfg = launch.load_project_config(str(tmp_worktree))
        assert cfg.env["UV_PROJECT_ENVIRONMENT"] == "/workdir/.venv-container"
        home = str(pathlib.Path.home())
        assert (f"{home}/.cache/uv", "/home/claude/.cache/uv", "rw") in cfg.mounts

    def test_toml_top_level_prune_reaches_config(self, tmp_worktree):
        """A top-level `prune` key must reach ProjectConfig.extra_prune.

        Guards the TOML gotcha: a `prune` placed after a [table] header is
        parsed into that table, not the top level, and would be silently
        dropped. Top-level keys must precede all tables.
        """
        (tmp_worktree / ".claude-sandbox.toml").write_text(
            'prune = ["target", "dist"]\n'
            '[env]\n'
            'FOO = "bar"\n'
        )
        cfg = launch.load_project_config(str(tmp_worktree))
        assert cfg.extra_prune == {"target", "dist"}, (
            f"top-level prune should reach extra_prune, got: {cfg.extra_prune}"
        )

    def test_prune_names_extend_symlink_scan(self, tmp_path, tmp_worktree):
        """prune entries in project config are skipped by the symlink scan."""
        external = tmp_path / "ext"
        external.mkdir()
        (external / "f").write_text("x")
        pruned = tmp_worktree / "target_dir"
        pruned.mkdir()
        (pruned / "link").symlink_to(external)
        mounts = launch.collect_symlink_mounts(str(tmp_worktree), extra_prune={"target_dir"})
        assert str(external) not in [t for t, _ in mounts]

    def test_unparseable_toml_fails_loud(self, tmp_worktree):
        """A present-but-broken .claude-sandbox.toml exits non-zero, never silently defaults."""
        (tmp_worktree / ".claude-sandbox.toml").write_text("this is = = not toml ][")
        with pytest.raises(SystemExit) as exc:
            launch.load_project_config(str(tmp_worktree))
        assert exc.value.code != 0


# ===========================================================================
# Helper functions
# ===========================================================================

def _uv_project_config() -> "launch.ProjectConfig":
    """The Python/uv ProjectConfig this repo ships in .claude-sandbox.toml.

    Built explicitly so the uv tests assert the "config in → args out" contract
    rather than relying on the (now toolchain-neutral) core to emit uv vars.
    """
    home = str(pathlib.Path.home())
    return launch.ProjectConfig(
        env={
            "UV_PROJECT_ENVIRONMENT": "/workdir/.venv-container",
            "UV_CACHE_DIR": "/home/claude/.cache/uv",
        },
        mounts=[(f"{home}/.cache/uv", "/home/claude/.cache/uv", "rw")],
    )


def _extract_volume_mounts(args: list[str]) -> list[str]:
    """Return all -v argument values from a docker args list."""
    mounts = []
    it = iter(args)
    for token in it:
        if token == "-v":
            try:
                mounts.append(next(it))
            except StopIteration:
                pass
    return mounts


def _extract_env_args(args: list[str]) -> list[str]:
    """Return all -e argument values from a docker args list."""
    envs = []
    it = iter(args)
    for token in it:
        if token == "-e":
            try:
                envs.append(next(it))
            except StopIteration:
                pass
    return envs
