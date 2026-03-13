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

# Env vars to always pass through from host
DEFAULT_PASSTHROUGH_ENV = [
    "ANTHROPIC_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_ACCOUNT_ID",
]

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
        if line == "[container]":
            section = "container"
            continue
        if line.startswith("["):
            section = None
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

def _ensure_devcontainer_json(project: "Project") -> Path:
    """
    Ensure a .devcontainer/devcontainer.json exists for the project.
    If the project already has one, use it. Otherwise, generate one from template.
    Returns the path to the devcontainer.json.
    """
    dc_dir = project.path / ".devcontainer"
    dc_json = dc_dir / "devcontainer.json"

    if dc_json.exists():
        return dc_json

    # Generate from template
    dc_dir.mkdir(exist_ok=True)
    config = dict(DEVCONTAINER_TEMPLATE)
    config["name"] = f"orch — {project.name}"

    # Add env var passthrough
    cfg = _load_container_config()
    env_vars = [v.strip() for v in cfg.get("passthrough_env", "").split(",") if v.strip()]
    container_env = dict(config.get("containerEnv", {}))
    for var in env_vars:
        host_val = os.environ.get(var, "")
        if host_val:
            container_env[var] = host_val
    config["containerEnv"] = container_env

    # Mount Claude config and auth from host so container skips login/setup
    claude_host = Path.home() / ".claude"
    claude_json = Path.home() / ".claude.json"
    config["mounts"] = [
        f"source={claude_host},target={CONTAINER_HOME}/.claude,type=bind",
    ]
    if claude_json.exists():
        config["mounts"].append(
            f"source={claude_json},target={CONTAINER_HOME}/.claude.json,type=bind"
        )

    dc_json.write_text(json.dumps(config, indent=2) + "\n")
    return dc_json


def _devcontainer_up(project: "Project") -> str:
    """Use devcontainer CLI to start container. Returns container ID."""
    _ensure_devcontainer_json(project)

    # Pass through env vars
    cfg = _load_container_config()
    env_vars = [v.strip() for v in cfg.get("passthrough_env", "").split(",") if v.strip()]
    env_args = []
    for var in env_vars:
        val = os.environ.get(var, "")
        if val:
            env_args += ["--remote-env", f"{var}={val}"]

    cmd = [
        "devcontainer", "up",
        "--workspace-folder", str(project.path),
    ] + env_args

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    if result.returncode != 0:
        raise RuntimeError(
            f"devcontainer up failed for {project.name}:\n{result.stderr}"
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

    # Track the container ID
    project.claude_dir.mkdir(parents=True, exist_ok=True)
    project.container_id_file.write_text(cid)

    return cid


def stop(project: "Project") -> None:
    """Stop and remove the container for a project."""
    cid = project.container_id
    if not cid:
        return

    subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    project.container_id_file.unlink(missing_ok=True)


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


def exec_cmd(project: "Project") -> str:
    """
    Return the docker exec command string to run claude inside the container.
    Ensures the container is running first.
    """
    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)
    args = _build_claude_args(project)
    return f"docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"


# ── iTerm2 integration ───────────────────────────────────────────────────────

def exec_claude_in_iterm(project: "Project") -> None:
    """
    Spin up a container (if needed) and open an iTerm2 tab running claude
    inside it with full permissions.
    """
    from .iterm import _load_config, _bring_tab_to_front

    # Reuse existing tab if open
    handle_file = project.claude_dir / "iterm_container_handle"
    if handle_file.exists():
        tty = handle_file.read_text().strip()
        if tty and _bring_tab_to_front(tty):
            return
        handle_file.unlink(missing_ok=True)

    # Ensure container is running
    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)
    args = _build_claude_args(project)
    claude_cmd = f"docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")
    tab_name = f"{project.name} (container)"

    if dedicated:
        script = f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            set isNewWindow to false
            repeat with w in windows
                if name of w contains "{window_title}" then
                    set orchWindow to w
                    exit repeat
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
                    write text "{claude_cmd}"
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
    claude_cmd = f"docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} claude {args} -p '{safe_task}'"

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
            repeat with w in windows
                if name of w contains "{window_title}" then
                    set orchWindow to w
                    exit repeat
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
