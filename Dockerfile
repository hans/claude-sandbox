# claude-sandbox: container image used by Superset to run Claude Code agents
# with the workspace worktree bind-mounted at /workdir.
#
# Build:  docker build -t claude-sandbox:latest .
# Run:    handled by .superset/launch.sh (see README.md)

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PATH="/home/claude/.local/bin:$PATH"

# --- core toolchain ---------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        git openssh-client \
        build-essential pkg-config \
        less vim-tiny \
        ripgrep jq fd-find \
        sudo \
        python3 python3-pip python3-venv \
    && ln -s "$(command -v fdfind)" /usr/local/bin/fd \
    && echo 'ALL ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers \
    && rm -rf /var/lib/apt/lists/*

# --- uv (Python package/env manager) ----------------------------------------
# The official installer defaults to ~/.local/bin; pin it to /usr/local/bin so
# it's on PATH for every user, and tell it not to touch shell profiles.
RUN curl -fsSL https://astral.sh/uv/install.sh \
    | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

# --- Node 22 + Claude Code --------------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

# --- (optional) scientific Python stack -------------------------------------
# Uncomment for neuroscience / data work. Adds ~1.5 GB to the image.
# RUN pip3 install --no-cache-dir --break-system-packages \
#         numpy scipy pandas matplotlib scikit-learn

# --- non-root user ----------------------------------------------------------
# We create a `claude` user at UID 1000 as the nominal owner of /home/claude,
# but at runtime launch.sh passes `-u $(id -u):$(id -g)` so the container runs
# as the host user. To make that work, /home/claude must be writable by any
# UID -- hence the 0777 mode below. Bind-mounted subdirs (.claude, .gitconfig,
# .ssh) carry host UIDs and permissions, which is what we want.
# ubuntu:24.04 ships a stock `ubuntu` user at UID 1000; remove it so we can
# claim that UID for `claude`.
RUN userdel -r ubuntu 2>/dev/null || true \
    && useradd --create-home --uid 1000 --shell /bin/bash claude \
    && chmod 0777 /home/claude

# Trust the bind-mounted worktree even when its UID doesn't match the
# runtime UID (git refuses by default since CVE-2022-24765).
RUN git config --system --add safe.directory /workdir

WORKDIR /workdir

# Entrypoint sources /workdir/.env (if present) and execs the command.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["claude"]
