"""
orch container management.

Spins up devcontainers for projects so Claude can run with full permissions
(--dangerously-skip-permissions) in an isolated environment.

Two strategies:
  1. devcontainer CLI (preferred) — uses project's .devcontainer/ or generates one
  2. Raw docker (fallback) — docker run with explicit mounts and env vars

Container IDs are tracked in .claude/container_id per project.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Project


# ── Default config ───────────────────────────────────────────────────────────

DEFAULT_IMAGE = "mcr.microsoft.com/devcontainers/base:ubuntu"
DEFAULT_MEMORY = "12g"
CONTAINER_PREFIX = "orch-"
WORKSPACE_DIR = "/workspace"
CONTAINER_USER = "vscode"
CONTAINER_HOME = f"/home/{CONTAINER_USER}"

# Shell setup commands injected into every container
ORCH_POST_CREATE = (
    "printf 'set nocompatible\\nset backspace=indent,eol,start\\n' > ~/.vimrc"
    " && echo 'alias vim=vi' >> ~/.bashrc"
    " && echo 'export TERM=xterm-256color' >> ~/.bashrc"
)

# Env vars to always pass through from host
DEFAULT_PASSTHROUGH_ENV = [
    "ANTHROPIC_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
]

# Terminal identity env vars to forward into docker exec so that CLI tools
# (e.g. Claude Code) can detect the host terminal and activate keyboard
# protocols like Kitty/CSI-u.  These travel transparently through the PTY.
TERMINAL_ENV_VARS = ["TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM"]


def _terminal_env_flags() -> str:
    """Build ``-e VAR=val`` flags for terminal identity vars present on the host."""
    parts: list[str] = []
    for var in TERMINAL_ENV_VARS:
        val = os.environ.get(var)
        if val:
            parts.append(f"-e {var}={shlex.quote(val)}")
    return " ".join(parts)

# Permissions that allow Claude to run without prompting
SETTINGS_LOCAL = {
    "permissions": {
        "allow": [
            "Bash(*)",
            "Read(*)",
            "Write(*)",
            "Edit(*)",
            "MultiEdit(*)",
            "Glob(*)",
            "Grep(*)",
            "LS(*)",
            "WebFetch(*)",
            "WebSearch(*)",
            "TodoRead(*)",
            "TodoWrite(*)",
            "mcp__*",
        ]
    }
}

# Default devcontainer.json template
DEVCONTAINER_TEMPLATE = {
    "name": "orch sandbox",
    "image": DEFAULT_IMAGE,
    "features": {
        "ghcr.io/anthropics/devcontainer-features/claude-code:1.0": {},
        "ghcr.io/devcontainers/features/node:1": {"version": "22"},
        "ghcr.io/devcontainers/features/common-utils:2": {},
        "ghcr.io/devcontainers/features/python:1": {
            "version": "3.12",
            "installTools": True,
        },
        "ghcr.io/devcontainers/features/git:1": {},
    },
    "containerEnv": {
        "CLAUDE_CONFIG_DIR": f"{CONTAINER_HOME}/.claude",
    },
    "remoteUser": CONTAINER_USER,
    "postCreateCommand": ORCH_POST_CREATE,
    "runArgs": ["--memory=12g", "--memory-swap=12g"],
}


# ── Config loading ───────────────────────────────────────────────────────────

def _load_container_config() -> dict:
    defaults = {
        "enabled": True,
        "image": DEFAULT_IMAGE,
        "memory": DEFAULT_MEMORY,
        "passthrough_env": ",".join(DEFAULT_PASSTHROUGH_ENV),
        "prefer_devcontainer_cli": True,
    }
    config_file = Path.home() / ".orch" / "config.toml"
    if not config_file.exists():
        return defaults
    section = None
    for raw in config_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section == "container" and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            defaults[key] = val
    return defaults


# ── Host path passthrough mounts ──────────────────────────────────────────────
#
# When a user drags a file (e.g. a screenshot) into iTerm2, the terminal
# pastes the *host-side* absolute path as text.  Claude Code detects the
# image extension and tries to read the file at that path.  Inside a
# container the path doesn't exist unless we mount it.
#
# We mount common macOS directories at their *original host path* so that
# paths pasted by iTerm2 resolve identically inside the container.


def _host_passthrough_mounts() -> list[tuple[str, str]]:
    """
    Return (host_path, container_target) pairs for directories that should
    be mounted at their original host path inside the container.

    This makes drag-and-drop of files (screenshots, etc.) into iTerm2 work
    seamlessly — the host path pasted by the terminal resolves to the same
    file inside the container.

    Reads ``host_passthrough_dirs`` from [container] config.  Defaults to
    the user's Desktop and Downloads on macOS (common screenshot locations).
    """
    cfg = _load_container_config()
    raw = cfg.get("host_passthrough_dirs", "")

    if raw:
        dirs = [d.strip() for d in raw.split(",") if d.strip()]
    elif platform.system() == "Darwin":
        home = Path.home()
        dirs = [str(home / "Desktop"), str(home / "Downloads")]
    else:
        dirs = []

    result = []
    for d in dirs:
        host = Path(d).expanduser().resolve()
        if host.is_dir():
            # Mount at the same absolute path so host paths resolve as-is
            result.append((str(host), str(host)))
    return result


# ── Reference directory mounts ────────────────────────────────────────────────


def _reference_mounts() -> list[tuple[str, str]]:
    """
    Parse reference_dirs from config and return (host_path, container_target)
    tuples for each directory.  Mounted at their original host path
    so that paths are consistent between host and container.
    """
    cfg = _load_container_config()
    raw = cfg.get("reference_dirs", "")
    if not raw:
        return []

    result = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host = Path(entry).expanduser().resolve()
        result.append((str(host), str(host)))
    return result


# ── SSH agent forwarding ─────────────────────────────────────────────────────

# Target path for the SSH agent socket inside the container
_CONTAINER_SSH_AUTH_SOCK = "/tmp/ssh-agent.sock"


def _ssh_agent_mount() -> tuple[str, str] | None:
    """
    Return (mount_string, env_value) for forwarding the host SSH agent into
    the container, or None if no agent is available.

    macOS + Docker Desktop: uses the built-in /run/host-services/ssh-auth.sock
    proxy so Unix socket mounting just works.

    Linux: bind-mounts $SSH_AUTH_SOCK directly.
    """
    if platform.system() == "Darwin":
        # Docker Desktop for Mac exposes the host agent at a magic path
        return (
            f"source=/run/host-services/ssh-auth.sock,"
            f"target={_CONTAINER_SSH_AUTH_SOCK},type=bind",
            _CONTAINER_SSH_AUTH_SOCK,
        )

    sock = os.environ.get("SSH_AUTH_SOCK", "")
    if sock and Path(sock).exists():
        return (
            f"source={sock},target={_CONTAINER_SSH_AUTH_SOCK},type=bind",
            _CONTAINER_SSH_AUTH_SOCK,
        )

    return None


def _fix_ssh_socket_permissions(cid: str) -> None:
    """
    The SSH agent socket is mounted as root:root. Make it accessible to the
    container user so git/ssh operations work.
    """
    subprocess.run(
        ["docker", "exec", "-u", "root", cid,
         "chmod", "777", _CONTAINER_SSH_AUTH_SOCK],
        capture_output=True,
    )


# ── Detection helpers ────────────────────────────────────────────────────────

def _has_devcontainer_cli() -> bool:
    return shutil.which("devcontainer") is not None


def _has_docker() -> bool:
    return shutil.which("docker") is not None


# ── Container status ─────────────────────────────────────────────────────────

def is_running(project: "Project") -> str | None:
    """Return container ID if a running container exists for this project."""
    cid = project.container_id
    if not cid:
        return None

    # Verify it's actually running
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", cid],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip().lower() == "true":
        return cid

    # Container is gone or stopped — clean up stale ID
    project.container_id_file.unlink(missing_ok=True)
    return None


def clear_stale_container(project: "Project") -> None:
    """Remove stale container_id file if the container is no longer running."""
    is_running(project)  # side effect: cleans up if stale


# ── Devcontainer CLI strategy ────────────────────────────────────────────────

def _mount_target(mount_str: str) -> str | None:
    """Extract the target path from a devcontainer mount string."""
    for part in mount_str.split(","):
        key, _, val = part.partition("=")
        if key.strip() == "target":
            return val
    return None


def _prepare_devcontainer_config(project: "Project") -> Path:
    """
    Prepare a devcontainer.json for the project, ready for `devcontainer up`.

    If the project has an existing .devcontainer/devcontainer.json, orch
    reads it and merges in its requirements (Claude config mount, env vars,
    reference dirs).  The user's original config is backed up to
    devcontainer.base.json so orch can re-merge from scratch each time
    (picking up config changes like new reference_dirs).

    If no project config exists, generates one from orch's template.

    In both cases the final config is written to the project's
    .devcontainer/devcontainer.json — devcontainer CLI reliably reads
    from there regardless of --config flags.

    Returns the path to the config file.
    """
    dc_dir = project.path / ".devcontainer"
    dc_json = dc_dir / "devcontainer.json"
    dc_base = dc_dir / "devcontainer.base.json"
    cfg = _load_container_config()

    if dc_base.exists() or dc_json.exists():
        # ── Merge mode ──────────────────────────────────────────────
        # Read from the base (user's original) if available,
        # otherwise from the current devcontainer.json and save a backup.
        if dc_base.exists():
            config = json.loads(dc_base.read_text())
        else:
            config = json.loads(dc_json.read_text())
            dc_base.write_text(json.dumps(config, indent=2) + "\n")

        # Ensure CLAUDE_CONFIG_DIR is set
        container_env = config.get("containerEnv", {})
        container_env["CLAUDE_CONFIG_DIR"] = f"{CONTAINER_HOME}/.claude"
        config["containerEnv"] = container_env

        # Replace any existing ~/.claude mount with orch's, or add it
        claude_host = str(Path.home() / ".claude")
        claude_mount = f"source={claude_host},target={CONTAINER_HOME}/.claude,type=bind"

        existing_mounts = config.get("mounts", [])
        merged_mounts = []
        has_claude_mount = False

        for m in existing_mounts:
            target = _mount_target(m)
            if target and target.rstrip("/").endswith("/.claude"):
                has_claude_mount = True
                merged_mounts.append(claude_mount)
            else:
                merged_mounts.append(m)

        if not has_claude_mount:
            merged_mounts.insert(0, claude_mount)

        # Forward host SSH agent into the container
        ssh = _ssh_agent_mount()
        if ssh:
            ssh_mount, ssh_env = ssh
            if not any(_mount_target(m) == _CONTAINER_SSH_AUTH_SOCK for m in merged_mounts):
                merged_mounts.append(ssh_mount)
            container_env = config.get("containerEnv", {})
            container_env["SSH_AUTH_SOCK"] = ssh_env
            config["containerEnv"] = container_env

        # Mount reference directories (read-only)
        for host, target in _reference_mounts():
            if not any(_mount_target(m) == target for m in merged_mounts):
                merged_mounts.append(
                    f"source={host},target={target},type=bind,readonly"
                )

        # Mount host passthrough dirs at their original paths (read-only)
        # so that file paths pasted by iTerm2 drag-and-drop resolve inside
        # the container (e.g. screenshots dragged from Finder).
        for host, target in _host_passthrough_mounts():
            if not any(_mount_target(m) == target for m in merged_mounts):
                merged_mounts.append(
                    f"source={host},target={target},type=bind,readonly"
                )

        config["mounts"] = merged_mounts

        # Merge orch shell setup into postCreateCommand
        existing_post = config.get("postCreateCommand", "")
        if isinstance(existing_post, str):
            if ORCH_POST_CREATE not in existing_post:
                parts = [p for p in (existing_post, ORCH_POST_CREATE) if p]
                config["postCreateCommand"] = " && ".join(parts)
        # If it's a list/object form, leave it alone — those are complex

        dc_json.write_text(json.dumps(config, indent=2) + "\n")
        return dc_json

    # ── Generate mode: no existing config ────────────────────────
    dc_dir.mkdir(exist_ok=True)

    config = dict(DEVCONTAINER_TEMPLATE)
    config["name"] = f"orch — {project.name}"

    # Add env var passthrough
    env_vars = [v.strip() for v in cfg.get("passthrough_env", "").split(",") if v.strip()]
    container_env = dict(config.get("containerEnv", {}))
    for var in env_vars:
        host_val = os.environ.get(var, "")
        if host_val:
            container_env[var] = host_val
    config["containerEnv"] = container_env

    # Mount Claude config and auth from host
    claude_host = Path.home() / ".claude"
    claude_json_file = Path.home() / ".claude.json"
    config["mounts"] = [
        f"source={claude_host},target={CONTAINER_HOME}/.claude,type=bind",
    ]
    if claude_json_file.exists():
        config["mounts"].append(
            f"source={claude_json_file},target={CONTAINER_HOME}/.claude.json,type=bind"
        )

    # Forward host SSH agent into the container
    ssh = _ssh_agent_mount()
    if ssh:
        ssh_mount, ssh_env = ssh
        config["mounts"].append(ssh_mount)
        config["containerEnv"]["SSH_AUTH_SOCK"] = ssh_env

    # Mount reference directories (read-only)
    for host, target in _reference_mounts():
        config["mounts"].append(
            f"source={host},target={target},type=bind,readonly"
        )

    # Mount host passthrough dirs at their original paths (read-only)
    for host, target in _host_passthrough_mounts():
        config["mounts"].append(
            f"source={host},target={target},type=bind,readonly"
        )

    dc_json.write_text(json.dumps(config, indent=2) + "\n")
    return dc_json


def _devcontainer_up(project: "Project") -> str:
    """Use devcontainer CLI to start container. Returns container ID."""
    _prepare_devcontainer_config(project)

    # Pass through env vars
    cfg = _load_container_config()
    env_vars = [v.strip() for v in cfg.get("passthrough_env", "").split(",") if v.strip()]
    env_args = []
    for var in env_vars:
        val = os.environ.get(var, "")
        if val:
            env_args += ["--remote-env", f"{var}={val}"]

    # If no container is tracked by orch, remove any stale devcontainer-
    # managed container so a fresh one is created with the current config
    # (mounts, env, etc. are only applied at container creation time).
    existing = _find_container_by_label(project)
    if existing:
        subprocess.run(["docker", "rm", "-f", existing], capture_output=True)

    cmd = [
        "devcontainer", "up",
        "--workspace-folder", str(project.path),
    ] + env_args

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        # Show only the last few lines — the full stderr is a verbose build
        # log and the actual error is usually at the end.
        lines = result.stderr.strip().splitlines()
        tail = "\n".join(lines[-15:])
        raise RuntimeError(
            f"devcontainer up failed for {project.name}:\n{tail}"
        )

    # Parse the JSON output to get container ID
    try:
        data = json.loads(result.stdout)
        cid = data.get("containerId", "")
    except json.JSONDecodeError:
        # Try to find container by label
        cid = _find_container_by_label(project)

    if not cid:
        raise RuntimeError(
            f"devcontainer up succeeded but couldn't determine container ID.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    return cid


def _find_container_by_label(project: "Project") -> str:
    """Find container ID by the devcontainer.local_folder label."""
    result = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", f"label=devcontainer.local_folder={project.path}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().splitlines()[0]
    return ""


# ── Raw docker strategy ──────────────────────────────────────────────────────

def _docker_run(project: "Project") -> str:
    """Use raw docker to start container. Returns container ID."""
    cfg = _load_container_config()
    image = cfg.get("image", DEFAULT_IMAGE)
    memory = cfg.get("memory", DEFAULT_MEMORY)
    container_name = f"{CONTAINER_PREFIX}{project.name}"

    # Remove existing stopped container with same name
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
    )

    # Build env args
    env_vars = [v.strip() for v in cfg.get("passthrough_env", "").split(",") if v.strip()]
    env_args = []
    for var in env_vars:
        val = os.environ.get(var, "")
        if val:
            env_args += ["-e", f"{var}={val}"]

    claude_host = Path.home() / ".claude"
    claude_json = Path.home() / ".claude.json"

    mount_args = [
        "-v", f"{project.path}:{WORKSPACE_DIR}",
        "-v", f"{claude_host}:{CONTAINER_HOME}/.claude",
    ]
    if claude_json.exists():
        mount_args += ["-v", f"{claude_json}:{CONTAINER_HOME}/.claude.json"]

    # Forward host SSH agent for git operations
    ssh = _ssh_agent_mount()
    if ssh:
        ssh_mount, ssh_env = ssh
        # Convert devcontainer mount string to docker -v format
        src = ssh_mount.split("source=")[1].split(",")[0]
        tgt = ssh_mount.split("target=")[1].split(",")[0]
        mount_args += ["-v", f"{src}:{tgt}"]
        env_args += ["-e", f"SSH_AUTH_SOCK={ssh_env}"]

    # Mount reference directories (read-only)
    for host, target in _reference_mounts():
        mount_args += ["-v", f"{host}:{target}:ro"]

    # Mount host passthrough dirs at their original paths (read-only)
    for host, target in _host_passthrough_mounts():
        mount_args += ["-v", f"{host}:{target}:ro"]

    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        f"--memory={memory}",
        f"--memory-swap={memory}",
    ] + mount_args + [
        "-w", WORKSPACE_DIR,
        "--label", f"devcontainer.local_folder={project.path}",
        "--label", "orch.managed=true",
    ] + env_args + [
        image,
        "sleep", "infinity",  # keep container alive
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker run failed for {project.name}:\n{result.stderr}"
        )

    cid = result.stdout.strip()

    # Install claude inside the container if not present
    _install_claude_in_container(cid)

    return cid


def _install_claude_in_container(cid: str) -> None:
    """Install Claude Code CLI inside a raw docker container if needed."""
    # Check if claude is already available
    check = subprocess.run(
        ["docker", "exec", cid, "which", "claude"],
        capture_output=True,
    )
    if check.returncode == 0:
        return

    # Install via npm (node should be on the base image, but might not be)
    subprocess.run(
        ["docker", "exec", cid, "bash", "-c",
         "command -v node || (apt-get update -qq && apt-get install -y -qq nodejs npm) && "
         "npm install -g @anthropic-ai/claude-code"],
        capture_output=True,
        timeout=180,
    )


# ── Permission setup ─────────────────────────────────────────────────────────

def _setup_permissions(cid: str, project: "Project | None" = None) -> None:
    """Write settings.local.json and mark onboarding complete inside the container."""
    settings_json = json.dumps(SETTINGS_LOCAL, indent=2)
    workdir = _container_workdir(cid, project)

    # Write to the project's .claude dir inside the container (as vscode user)
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"mkdir -p {workdir}/.claude && "
         f"cat > {workdir}/.claude/settings.local.json << 'ORCHEOF'\n"
         f"{settings_json}\n"
         f"ORCHEOF"],
        capture_output=True,
        timeout=10,
    )

    # Merge onboarding flags into the user-level .claude.json so Claude
    # doesn't prompt for theme, subscription, or dangerous-mode acceptance.
    # The host ~/.claude.json is mounted at $HOME/.claude.json but Claude
    # also reads $CLAUDE_CONFIG_DIR/.claude.json — patch both.
    _mark_onboarding_complete(cid, f"{CONTAINER_HOME}/.claude.json")
    _mark_onboarding_complete(cid, f"{CONTAINER_HOME}/.claude/.claude.json")


def _mark_onboarding_complete(cid: str, config_path: str) -> None:
    """Ensure hasCompletedOnboarding and related flags are set in a Claude config file."""
    # Read existing config
    result = subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "cat", config_path],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout) if result.returncode == 0 else {}
    except json.JSONDecodeError:
        data = {}

    # Set onboarding flags
    changed = False
    for key, val in [
        ("hasCompletedOnboarding", True),
        ("theme", "dark"),
        ("hasAcknowledgedCostThreshold", True),
        ("bypassTrustPromptWorkspaces", True),
    ]:
        if data.get(key) != val:
            data[key] = val
            changed = True

    if not changed:
        return

    patched = json.dumps(data, indent=2)
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"cat > {config_path} << 'ORCHEOF'\n{patched}\nORCHEOF"],
        capture_output=True,
        timeout=10,
    )


# ── Credential injection ────────────────────────────────────────────────────


def _extract_host_credentials() -> str | None:
    """Extract Claude OAuth credentials from the macOS Keychain.

    On macOS, Claude Code stores OAuth tokens in the Keychain under the
    service name "Claude Code-credentials".  On Linux (inside containers)
    it reads from $CLAUDE_CONFIG_DIR/.credentials.json instead.

    Returns the raw JSON string, or None if unavailable.
    """
    if platform.system() != "Darwin":
        return None

    result = subprocess.run(
        ["security", "find-generic-password",
         "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None
    cred = result.stdout.strip()
    # Sanity-check: should be valid JSON containing an OAuth token
    try:
        data = json.loads(cred)
        if "claudeAiOauth" not in data:
            return None
    except (json.JSONDecodeError, TypeError):
        return None
    return cred


def _inject_credentials(cid: str) -> None:
    """Extract OAuth credentials from macOS Keychain and write them into the
    container as a plaintext credentials file.

    Claude Code on Linux reads $CLAUDE_CONFIG_DIR/.credentials.json as a
    fallback when the macOS Keychain is unavailable.  We extract the token
    from the host Keychain and write it directly to that path inside the
    container.
    """
    cred_json = _extract_host_credentials()
    if not cred_json:
        return  # No credentials to inject (not macOS, or not logged in)

    safe_cred = shlex.quote(cred_json)
    cred_path = f"{CONTAINER_HOME}/.claude/.credentials.json"
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"printf '%s' {safe_cred} > {cred_path} && chmod 600 {cred_path}"],
        capture_output=True, timeout=10,
    )


# ── Git identity ──────────────────────────────────────────────────────────────

GIT_USER_NAME = "Orch & Claude"
GIT_USER_EMAIL = "orch-claude@noreply.com"


def _setup_git_identity(cid: str, project: "Project | None" = None) -> None:
    """Configure git user.name and user.email inside the container workspace.

    Sets the identity at the repo level so commits made by Claude inside the
    container always have a consistent author regardless of host config.
    Also seeds ~/.ssh/known_hosts with GitHub's host key so git push/pull
    doesn't fail on host key verification.
    """
    workdir = _container_workdir(cid, project)
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, "-w", workdir, cid,
         "git", "config", "user.name", GIT_USER_NAME],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, "-w", workdir, cid,
         "git", "config", "user.email", GIT_USER_EMAIL],
        capture_output=True, timeout=10,
    )

    # Seed SSH known_hosts with GitHub's host key so git operations don't
    # fail on first use with "Host key verification failed"
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         "mkdir -p ~/.ssh && ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null"],
        capture_output=True, timeout=15,
    )


# ── Reference site context ────────────────────────────────────────────────────


def _inject_reference_context(cid: str) -> None:
    """Write a user-level CLAUDE.md inside the container listing available
    reference projects.

    Reference directories are mounted read-only into the container at their
    original host paths.  This function scans each reference directory for
    subdirectories (projects) and writes a ``~/.claude/CLAUDE.md`` that tells
    Claude where to look when the user asks it to reuse code from another
    project.
    """
    refs = _reference_mounts()
    if not refs:
        return

    # Scan each reference directory for project subdirectories
    projects_by_dir: dict[str, list[str]] = {}
    for host_path, container_target in refs:
        host = Path(host_path)
        if not host.is_dir():
            continue
        subdirs = sorted(
            d.name for d in host.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if subdirs:
            projects_by_dir[container_target] = subdirs

    if not projects_by_dir:
        return

    # Build the CLAUDE.md content
    lines = [
        "# Reference projects",
        "",
        "The following reference projects are mounted read-only inside this",
        "container. When the user asks you to look at, reuse, or draw inspiration",
        "from another project, check these locations.",
        "",
    ]
    for container_path, names in projects_by_dir.items():
        lines.append(f"**{container_path}/**")
        for name in names:
            lines.append(f"- `{container_path}/{name}`")
        lines.append("")

    lines.extend([
        "These are read-only. Do not attempt to modify files in reference projects.",
        "Copy code into the current workspace if you need to adapt it.",
    ])

    content = "\n".join(lines) + "\n"

    # Write to the user-level CLAUDE.md inside the container
    claude_md_path = f"{CONTAINER_HOME}/.claude/CLAUDE.md"
    escaped = content.replace("'", "'\\''")
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"printf '%s' '{escaped}' > {claude_md_path}"],
        capture_output=True, timeout=10,
    )


# ── Main lifecycle functions ─────────────────────────────────────────────────

def ensure_running(project: "Project") -> str:
    """
    Start a container for this project if not already running.
    Returns the container ID.
    """
    # Already running?
    cid = is_running(project)
    if cid:
        return cid

    if not _has_docker():
        raise RuntimeError("Docker is not installed or not running.")

    cfg = _load_container_config()
    prefer_dc = cfg.get("prefer_devcontainer_cli", True)

    # Strategy 1: devcontainer CLI
    if prefer_dc and _has_devcontainer_cli():
        cid = _devcontainer_up(project)
    else:
        # Strategy 2: raw docker
        cid = _docker_run(project)

    # Set up permissions inside the container
    _setup_permissions(cid, project)

    # Configure git identity so commits have a consistent author
    _setup_git_identity(cid, project)

    # Fix SSH agent socket permissions so the container user can access it
    _fix_ssh_socket_permissions(cid)

    # Inject host OAuth credentials so Claude doesn't prompt for login
    _inject_credentials(cid)

    # Tell Claude about available reference projects
    _inject_reference_context(cid)

    # Track the container ID
    project.claude_dir.mkdir(parents=True, exist_ok=True)
    project.container_id_file.write_text(cid)

    return cid


def stop(project: "Project") -> None:
    """Stop and remove the container for a project."""
    cid = project.container_id
    if cid:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        project.container_id_file.unlink(missing_ok=True)

    # Also remove any container devcontainer CLI may have created for this
    # workspace — it tracks containers by label, not by orch's stored ID,
    # so a stale one can survive and get reused with outdated mounts.
    result = subprocess.run(
        ["docker", "ps", "-aq",
         "--filter", f"label=devcontainer.local_folder={project.path}"],
        capture_output=True, text=True,
    )
    for stale in result.stdout.strip().splitlines():
        stale = stale.strip()
        if stale and stale != cid:
            subprocess.run(["docker", "rm", "-f", stale], capture_output=True)


def _build_claude_args(project: "Project") -> str:
    """Build claude CLI args, including --resume if a session exists."""
    args = "--dangerously-skip-permissions"
    sessions_file = project.claude_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                args += f" --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass
    return args


def _container_workdir(cid: str, project: "Project | None" = None) -> str:
    """Detect the workspace directory inside a running container."""
    # 1. Check WorkingDir from image/container config
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.WorkingDir}}", cid],
        capture_output=True, text=True,
    )
    workdir = result.stdout.strip()
    if workdir:
        return workdir

    # 2. Map host project path to container mount path
    #    Docker Desktop on macOS prefixes sources with /host_mnt, so we
    #    normalise both sides before comparing.
    if project:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .Mounts}}{{.Source}}\t{{.Destination}}\n{{end}}", cid],
            capture_output=True, text=True,
        )
        host_path = str(project.path)
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split("\t")
            if len(parts) != 2:
                continue
            src, dest = parts
            # Strip /host_mnt prefix that Docker Desktop adds on macOS
            norm_src = src.removeprefix("/host_mnt")
            if host_path.startswith(norm_src) and norm_src != "/":
                relative = host_path[len(norm_src):].lstrip("/")
                return f"{dest}/{relative}" if relative else dest

    # 3. Fallback: ask the container what exists
    for path in [WORKSPACE_DIR, "/workspaces"]:
        check = subprocess.run(
            ["docker", "exec", cid, "test", "-d", path],
            capture_output=True,
        )
        if check.returncode == 0:
            return path

    return WORKSPACE_DIR


def _ensure_credentials_injected(cid: str) -> None:
    """Re-inject credentials from the host Keychain into the container.

    Always refreshes credentials so that updated OAuth tokens (e.g. after
    a token refresh on the host) are picked up without restarting the
    container.  This is called before each Claude session.
    """
    _inject_credentials(cid)


def exec_cmd(project: "Project") -> str:
    """
    Return the docker exec command string to run claude inside the container.
    Ensures the container is running first.
    """
    cid = ensure_running(project)
    _ensure_credentials_injected(cid)
    workdir = _container_workdir(cid, project)
    args = _build_claude_args(project)
    tenv = _terminal_env_flags()
    return f"docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"


# ── iTerm2 integration ───────────────────────────────────────────────────────

def exec_claude_in_iterm(project: "Project", with_shell: bool = False) -> None:
    """
    Spin up a container (if needed) and open an iTerm2 tab running claude
    inside it with full permissions.

    If with_shell is True, also opens a second tab with a plain bash shell
    in the same window (single AppleScript call to guarantee same window).
    """
    from .iterm import _load_config, _bring_tab_to_front

    # Check existing Claude tab
    tab_name = f"{project.name} (container)"
    handle_file = project.claude_dir / "iterm_container_handle"
    claude_exists = False
    if handle_file.exists():
        tty = handle_file.read_text().strip()
        if tty and _bring_tab_to_front(tty, expected_name=tab_name):
            claude_exists = True
        else:
            handle_file.unlink(missing_ok=True)

    # Check existing shell tab
    shell_exists = False
    shell_tab_name = f"{project.name} (shell)"
    shell_handle_file = project.claude_dir / "iterm_container_shell_handle"
    if with_shell and shell_handle_file.exists():
        tty = shell_handle_file.read_text().strip()
        if tty and _bring_tab_to_front(tty, expected_name=shell_tab_name):
            shell_exists = True
        else:
            shell_handle_file.unlink(missing_ok=True)

    # Nothing to open
    if claude_exists and (not with_shell or shell_exists):
        return

    # Ensure container is running
    cid = ensure_running(project)
    _ensure_credentials_injected(cid)
    workdir = _container_workdir(cid, project)

    need_claude = not claude_exists
    need_shell = with_shell and not shell_exists

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)

    # Build commands
    if need_claude:
        args = _build_claude_args(project)
        tenv = _terminal_env_flags()
        claude_cmd = f"docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"
    if need_shell:
        shell_cmd = f"docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} bash"

    # ── Single-tab case (no shell, or shell already open) ────────────────
    if need_claude and not need_shell:
        script = _build_single_tab_script(
            profile=profile, dedicated=dedicated,
            tab_name=tab_name, cmd=claude_cmd,
            badge=project.name,
        )
        from .iterm import _run_iterm_script
        tty = _run_iterm_script(script)
        if tty:
            handle_file.write_text(tty)
        return

    if not need_claude and need_shell:
        script = _build_single_tab_script(
            profile=profile, dedicated=dedicated,
            tab_name=shell_tab_name, cmd=shell_cmd,
            badge=project.name,
        )
        from .iterm import _run_iterm_script
        tty = _run_iterm_script(script)
        if tty:
            shell_handle_file.write_text(tty)
        return

    # ── Both tabs needed — single AppleScript, one window ────────────────
    if dedicated:
        script = f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            set isNewWindow to false
            set foundOrch to false
            repeat with w in windows
                if not foundOrch then
                    repeat with aTab in tabs of w
                        if not foundOrch then
                            repeat with aSession in sessions of aTab
                                if profile name of aSession is "{profile}" then
                                    set orchWindow to w
                                    set foundOrch to true
                                    exit repeat
                                end if
                            end repeat
                        end if
                    end repeat
                end if
            end repeat
            if orchWindow is missing value then
                try
                    set orchWindow to (create window with profile "{profile}")
                on error
                    set orchWindow to (create window with default profile)
                end try
                set isNewWindow to true
            end if
            tell orchWindow
                -- Claude tab (reuses the initial session if new window)
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text "{claude_cmd}"
                    set claudeTty to tty
                end tell
                -- Shell tab (always a new tab)
                try
                    create tab with profile "{profile}"
                on error
                    create tab with default profile
                end try
                tell current session
                    set name to "{shell_tab_name}"
                    set badge to "{project.name}"
                    write text "{shell_cmd}"
                    set shellTty to tty
                end tell
                -- Switch focus back to Claude tab
                repeat with aTab in tabs
                    repeat with aSession in sessions of aTab
                        if tty of aSession is claudeTty then
                            select aTab
                        end if
                    end repeat
                end repeat
            end tell
            return claudeTty & linefeed & shellTty
        end tell
        """
    else:
        script = f"""
        tell application "iTerm2"
            activate
            set isNewWindow to false
            if (count of windows) is 0 then
                try
                    create window with profile "{profile}"
                on error
                    create window with default profile
                end try
                set isNewWindow to true
            end if
            tell current window
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text "{claude_cmd}"
                    set claudeTty to tty
                end tell
                try
                    create tab with profile "{profile}"
                on error
                    create tab with default profile
                end try
                tell current session
                    set name to "{shell_tab_name}"
                    set badge to "{project.name}"
                    write text "{shell_cmd}"
                    set shellTty to tty
                end tell
                repeat with aTab in tabs
                    repeat with aSession in sessions of aTab
                        if tty of aSession is claudeTty then
                            select aTab
                        end if
                    end repeat
                end repeat
            end tell
            return claudeTty & linefeed & shellTty
        end tell
        """

    from .iterm import _run_iterm_script
    result = _run_iterm_script(script)
    if result:
        parts = result.split("\n")
        if len(parts) >= 1 and parts[0]:
            handle_file.write_text(parts[0])
        if len(parts) >= 2 and parts[1]:
            shell_handle_file.write_text(parts[1])


def _build_single_tab_script(*, profile: str, dedicated: bool,
                              tab_name: str, cmd: str,
                              badge: str = "") -> str:
    """Build AppleScript to open a single iTerm2 tab."""
    badge_line = f'set badge to "{badge}"' if badge else ""
    if dedicated:
        return f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            set isNewWindow to false
            set foundOrch to false
            repeat with w in windows
                if not foundOrch then
                    repeat with aTab in tabs of w
                        if not foundOrch then
                            repeat with aSession in sessions of aTab
                                if profile name of aSession is "{profile}" then
                                    set orchWindow to w
                                    set foundOrch to true
                                    exit repeat
                                end if
                            end repeat
                        end if
                    end repeat
                end if
            end repeat
            if orchWindow is missing value then
                try
                    set orchWindow to (create window with profile "{profile}")
                on error
                    set orchWindow to (create window with default profile)
                end try
                set isNewWindow to true
            end if
            tell orchWindow
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    {badge_line}
                    write text "{cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """
    else:
        return f"""
        tell application "iTerm2"
            activate
            set isNewWindow to false
            if (count of windows) is 0 then
                try
                    create window with profile "{profile}"
                on error
                    create window with default profile
                end try
                set isNewWindow to true
            end if
            tell current window
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    {badge_line}
                    write text "{cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """


def exec_shell_in_iterm(project: "Project") -> None:
    """
    Open an iTerm2 tab with a plain bash shell inside the project's container.
    Ensures the container is running first.
    """
    from .iterm import _load_config, _bring_tab_to_front

    # Reuse existing shell tab if open
    tab_name = f"{project.name} (shell)"
    handle_file = project.claude_dir / "iterm_container_shell_handle"
    if handle_file.exists():
        tty = handle_file.read_text().strip()
        if tty and _bring_tab_to_front(tty, expected_name=tab_name):
            return
        handle_file.unlink(missing_ok=True)

    # Ensure container is running
    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)
    shell_cmd = f"docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} bash"

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")

    if dedicated:
        script = f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            set isNewWindow to false
            set foundOrch to false
            repeat with w in windows
                if not foundOrch then
                    repeat with aTab in tabs of w
                        if not foundOrch then
                            repeat with aSession in sessions of aTab
                                if profile name of aSession is "{profile}" then
                                    set orchWindow to w
                                    set foundOrch to true
                                    exit repeat
                                end if
                            end repeat
                        end if
                    end repeat
                end if
            end repeat
            if orchWindow is missing value then
                try
                    set orchWindow to (create window with profile "{profile}")
                on error
                    set orchWindow to (create window with default profile)
                end try
                set isNewWindow to true
            end if
            tell orchWindow
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text "{shell_cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """
    else:
        script = f"""
        tell application "iTerm2"
            activate
            set isNewWindow to false
            if (count of windows) is 0 then
                try
                    create window with profile "{profile}"
                on error
                    create window with default profile
                end try
                set isNewWindow to true
            end if
            tell current window
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text "{shell_cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """

    from .iterm import _run_iterm_script
    tty = _run_iterm_script(script)
    if tty:
        handle_file.write_text(tty)


def _send_task_to_container(project: "Project", task: str) -> None:
    """
    Launch Claude in the container with a task as the initial prompt.
    Opens an iTerm2 tab running: docker exec -it <cid> claude --dangerously-skip-permissions -p "task"
    """
    from .iterm import _load_config, _run_iterm_script

    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)

    # Escape single quotes in the task for shell safety
    safe_task = task.replace("'", "'\\''")
    args = _build_claude_args(project)
    tenv = _terminal_env_flags()
    claude_cmd = f"docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args} -p '{safe_task}'"

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")
    tab_name = f"{project.name} task"

    if dedicated:
        script = f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            set isNewWindow to false
            set foundOrch to false
            repeat with w in windows
                if not foundOrch then
                    repeat with aTab in tabs of w
                        if not foundOrch then
                            repeat with aSession in sessions of aTab
                                if profile name of aSession is "{profile}" then
                                    set orchWindow to w
                                    set foundOrch to true
                                    exit repeat
                                end if
                            end repeat
                        end if
                    end repeat
                end if
            end repeat
            if orchWindow is missing value then
                try
                    set orchWindow to (create window with profile "{profile}")
                on error
                    set orchWindow to (create window with default profile)
                end try
                set isNewWindow to true
            end if
            tell orchWindow
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    write text "{claude_cmd}"
                end tell
            end tell
        end tell
        """
    else:
        script = f"""
        tell application "iTerm2"
            activate
            set isNewWindow to false
            if (count of windows) is 0 then
                try
                    create window with profile "{profile}"
                on error
                    create window with default profile
                end try
                set isNewWindow to true
            end if
            tell current window
                if not isNewWindow then
                    try
                        create tab with profile "{profile}"
                    on error
                        create tab with default profile
                    end try
                end if
                tell current session
                    set name to "{tab_name}"
                    write text "{claude_cmd}"
                end tell
            end tell
        end tell
        """

    _run_iterm_script(script)


def _run_task_headless(project: "Project", task: str) -> None:
    """
    Run Claude headlessly in the container with a task — no terminal window.
    Uses `docker exec -d` (detached) so it runs in the background.
    Claude writes status to .claude/status as it works.
    """
    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)

    safe_task = task.replace("'", "'\\''")
    args = _build_claude_args(project)

    # -d = detached (runs in background inside the container)
    subprocess.run(
        ["docker", "exec", "-d",
         "-u", CONTAINER_USER,
         "-w", workdir,
         cid, "bash", "-c",
         f"claude {args} -p '{safe_task}'"],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── Dispatch config ──────────────────────────────────────────────────────────

DEFAULT_MAX_PARALLEL = 3

def _load_dispatch_config() -> dict:
    """Load [dispatch] section from ~/.orch/config.toml."""
    defaults = {
        "max_parallel": DEFAULT_MAX_PARALLEL,
    }
    config_file = Path.home() / ".orch" / "config.toml"
    if not config_file.exists():
        return defaults
    section = None
    for raw in config_file.read_text().splitlines():
        line = raw.strip()
        if line == "[dispatch]":
            section = "dispatch"
            continue
        if line.startswith("["):
            section = None
            continue
        if section == "dispatch" and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key == "max_parallel":
                try:
                    defaults[key] = int(val)
                except ValueError:
                    pass
            else:
                if val.lower() == "true":
                    val = True
                elif val.lower() == "false":
                    val = False
                defaults[key] = val
    return defaults


# ── Worktree helpers ──────────────────────────────────────────────────────────

def _slugify(text: str, max_len: int = 30) -> str:
    """Turn a todo description into a safe branch/directory name slug."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len]


def _ensure_worktrees_gitignored(project: "Project") -> None:
    """Add .orch-worktrees to the parent directory's .gitignore if not already present."""
    gitignore = project.path / ".gitignore"
    entry = ".orch-worktrees"
    try:
        if gitignore.exists():
            content = gitignore.read_text()
            if entry in content.splitlines():
                return
            # Append with newline safety
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{entry}\n"
            gitignore.write_text(content)
        else:
            gitignore.write_text(f"{entry}\n")
    except OSError:
        pass


def create_worktree(project: "Project", todo_text: str) -> tuple[Path, str]:
    """
    Create a git worktree for the given todo.
    Returns (worktree_path, branch_name).
    """
    import random as _rand

    _ensure_worktrees_gitignored(project)

    slug = _slugify(todo_text)
    suffix = _rand.randint(1000, 9999)
    branch_name = f"auto/{slug}-{suffix}"
    worktree_dir = project.path.parent / f".orch-worktrees" / f"{project.name}-{slug}-{suffix}"

    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree with a new branch from HEAD
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_dir)],
        capture_output=True,
        text=True,
        cwd=str(project.path),
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")

    return worktree_dir, branch_name


def remove_worktree(project: "Project", worktree_path: Path, branch_name: str = "") -> None:
    """Remove a git worktree and delete the local branch if it was pushed."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        capture_output=True,
        text=True,
        cwd=str(project.path),
        timeout=30,
    )

    # Delete local branch if it exists and has been pushed to remote
    if branch_name:
        # Check if remote tracking branch exists (i.e., it was pushed)
        check = subprocess.run(
            ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
            capture_output=True, text=True, cwd=str(project.path), timeout=10,
        )
        if check.stdout.strip():
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                capture_output=True, text=True, cwd=str(project.path), timeout=10,
            )


def _run_code_review(project: "Project", worktree_path: Path, branch_name: str) -> str:
    """
    Run Claude code review on the worktree changes.
    Returns the review text.
    """
    # Get the diff of changes in the worktree
    diff_result = subprocess.run(
        ["git", "diff", "HEAD~1"],
        capture_output=True,
        text=True,
        cwd=str(worktree_path),
        timeout=30,
    )
    diff_text = diff_result.stdout.strip()
    if not diff_text:
        return ""

    # Run Claude to review the diff
    review_prompt = (
        "Review the following code changes. Be concise. "
        "Flag any bugs, security issues, or significant problems. "
        "If the changes look good, say so briefly.\n\n"
        f"```diff\n{diff_text[:8000]}\n```"
    )
    safe_prompt = review_prompt.replace("'", "'\\''")

    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", safe_prompt],
        capture_output=True,
        text=True,
        cwd=str(worktree_path),
        timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_and_push_worktree(worktree_path: Path, branch_name: str, todo_text: str) -> None:
    """Stage all changes, commit, and push the worktree branch."""
    # Stage all changes
    subprocess.run(
        ["git", "add", "-A"],
        capture_output=True, cwd=str(worktree_path), timeout=30,
    )

    # Check if there are changes to commit
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, cwd=str(worktree_path), timeout=10,
    )
    if not status.stdout.strip():
        return  # Nothing to commit

    # Commit
    safe_msg = todo_text[:72]
    subprocess.run(
        ["git", "commit", "-m", f"auto: {safe_msg}"],
        capture_output=True, text=True, cwd=str(worktree_path), timeout=30,
    )

    # Push with retries (exponential backoff)
    import time
    delays = [2, 4, 8, 16]
    for attempt in range(5):
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            capture_output=True, text=True, cwd=str(worktree_path), timeout=60,
        )
        if result.returncode == 0:
            return
        if attempt < len(delays):
            time.sleep(delays[attempt])

    # Final attempt failed — not fatal, just log


def _create_pr(worktree_path: Path, branch_name: str, todo_text: str, review_text: str = "") -> str | None:
    """
    Create a pull request for the worktree branch using gh CLI.
    Returns the PR URL or None if gh is unavailable.
    """
    import shutil
    if not shutil.which("gh"):
        return None

    body = f"## Auto-dispatched task\n\n{todo_text}\n"
    if review_text:
        body += f"\n## Code Review\n\n{review_text}\n"

    safe_title = f"auto: {todo_text[:60]}"

    result = subprocess.run(
        ["gh", "pr", "create",
         "--title", safe_title,
         "--body", body,
         "--head", branch_name],
        capture_output=True,
        text=True,
        cwd=str(worktree_path),
        timeout=30,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def run_task_in_worktree(project: "Project", todo_text: str) -> dict:
    """
    Full pipeline: worktree -> Claude task -> code review -> commit -> push -> PR.
    Returns a dict with results: {branch, worktree, pr_url, review}.
    """
    worktree_path, branch_name = create_worktree(project, todo_text)
    results = {"branch": branch_name, "worktree": str(worktree_path), "pr_url": None, "review": ""}

    try:
        # Run Claude on the task inside the worktree
        safe_task = todo_text.replace("'", "'\\''")
        task_prompt = (
            f"Work on this task: {safe_task}\n\n"
            f"When done, make sure all changes are saved. Do not commit or push."
        )
        safe_prompt = task_prompt.replace("'", "'\\''")

        subprocess.run(
            ["claude", "--dangerously-skip-permissions", "-p", safe_prompt],
            capture_output=True,
            text=True,
            cwd=str(worktree_path),
            timeout=600,
        )

        # Code review (if enabled for project)
        if project.code_review_enabled:
            # First do a temporary commit so review can diff
            subprocess.run(["git", "add", "-A"], capture_output=True, cwd=str(worktree_path), timeout=10)
            subprocess.run(
                ["git", "commit", "-m", f"wip: {todo_text[:50]}"],
                capture_output=True, text=True, cwd=str(worktree_path), timeout=10,
            )
            review = _run_code_review(project, worktree_path, branch_name)
            results["review"] = review

            # Write review as a file in the worktree for reference
            if review:
                review_file = worktree_path / ".claude" / "last_review.md"
                review_file.parent.mkdir(parents=True, exist_ok=True)
                review_file.write_text(review)
        else:
            # Just commit directly
            _commit_and_push_worktree(worktree_path, branch_name, todo_text)

        # Push (handles both cases — review already committed, or fresh commit)
        if project.code_review_enabled:
            # Amend commit with final message and push
            subprocess.run(
                ["git", "commit", "--amend", "-m", f"auto: {todo_text[:72]}"],
                capture_output=True, text=True, cwd=str(worktree_path), timeout=10,
            )
            import time
            delays = [2, 4, 8, 16]
            for attempt in range(5):
                result = subprocess.run(
                    ["git", "push", "-u", "origin", branch_name, "--force-with-lease"],
                    capture_output=True, text=True, cwd=str(worktree_path), timeout=60,
                )
                if result.returncode == 0:
                    break
                if attempt < len(delays):
                    time.sleep(delays[attempt])

        # Create PR
        pr_url = _create_pr(worktree_path, branch_name, todo_text, results.get("review", ""))
        results["pr_url"] = pr_url

    except Exception:
        # On failure, still try to clean up worktree and local branch
        try:
            remove_worktree(project, worktree_path, branch_name)
        except Exception:
            pass
        raise

    return results
