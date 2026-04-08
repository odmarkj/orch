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

import hashlib
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


# ── Extract env vars from shell profile ──────────────────────────────────────
#
# VS Code sources the user's shell profile (~/.zshrc) before launching
# containers, so env vars like API tokens are available to the devcontainer
# CLI's ${localEnv:...} expansion.  orch may not be launched from an
# interactive shell, so we parse `export VAR=VALUE` lines from the user's
# shell profile and inject them into the process environment automatically.

import re

_EXPORT_RE = re.compile(
    r"""^\s*export\s+"""          # export keyword
    r"""([A-Za-z_][A-Za-z0-9_]*)"""  # variable name
    r"""=(.*)$""",                # = value (rest of line)
)


def _parse_shell_exports(path: Path) -> dict[str, str]:
    """Extract ``export KEY=VALUE`` assignments from a shell profile.

    Handles single-quoted, double-quoted, and unquoted values.
    Ignores commented lines and anything that isn't a simple static export
    (e.g. ``export PATH=$HOME/bin:$PATH`` is skipped because the value
    contains a ``$`` reference that can't be resolved statically).
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        val = m.group(2).strip()
        # Strip matching quotes
        if (val.startswith('"') and val.endswith('"')) or \
           (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        # Skip values that reference other variables — we can't resolve them
        if "$" in val:
            continue
        if key:
            result[key] = val
    return result


def _load_shell_env() -> None:
    """Load exported env vars from the user's shell profile into ``os.environ``.

    Checks ``~/.zshrc`` and ``~/.bashrc`` (in that order).  Only sets vars
    that are not already present in the environment, so explicitly exported
    shell vars still take precedence.
    """
    for name in (".zshrc", ".bashrc"):
        profile = Path.home() / name
        for key, val in _parse_shell_exports(profile).items():
            if key not in os.environ:
                os.environ[key] = val


_load_shell_env()


# ── Default config ───────────────────────────────────────────────────────────

DEFAULT_IMAGE = "mcr.microsoft.com/devcontainers/base:ubuntu"
ORCH_BASE_IMAGE = "orch-base:latest"
CLAUDE_CODE_VERSION = "2.1.86"
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
    " && (command -v claude >/dev/null 2>&1"
    f" || sudo npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}"
    f" || npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION})"
)

# Unique marker to detect whether ORCH_POST_CREATE was already merged into
# an existing postCreateCommand (avoids re-appending on every rebuild).
_ORCH_POST_MARKER = "command -v claude"

# Env vars that should never be passed into containers (blocklist).
# Override via ``blocked_env`` in ~/.orch/config.toml [container] section.
DEFAULT_BLOCKED_ENV = [
    # Shell / system identity
    "HOME",
    "USER",
    "SHELL",
    "PATH",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "LOGNAME",
    "HOSTNAME",
    "PWD",
    "OLDPWD",
    "SHLVL",
    "_",
    "COMMAND_MODE",
    # macOS internals
    "SECURITYSESSIONID",
    "__CFBundleIdentifier",
    "__CF_USER_TEXT_ENCODING",
    "XPC_FLAGS",
    "XPC_SERVICE_NAME",
    "LaunchInstanceID",
    "OSLogRateLimit",
    # iTerm / terminal session
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "TERM",
    "TERM_SESSION_ID",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_FEATURES",
    "TERMINFO_DIRS",
    "COLORFGBG",
    "COLORTERM",
    "ITERM_PROFILE",
    "ITERM_SESSION_ID",
    "CF_USER_TEXT_ENCODING",
    # Tool paths that reference host filesystem
    "BUN_INSTALL",
    "WRANGLER_HOME",
    # Credentials that should not be baked into devcontainer config files.
    # They are still available at runtime via orch's dynamic env passthrough.
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


def _iterm_badge_cmd(text: str) -> str:
    """Return a shell command that sets the iTerm2 badge via escape sequence."""
    from .iterm import _iterm_badge_cmd as _badge
    return _badge(text)

# Permissions and hooks injected into every container workspace
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
    },
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python3 -c \""
                            "import sys,json,os\n"
                            "try:\n"
                            "  raw=sys.stdin.buffer.read()\n"
                            "except Exception:\n"
                            "  sys.exit(0)\n"
                            "if not raw:\n"
                            "  sys.exit(0)\n"
                            "try:\n"
                            "  d=json.loads(raw)\n"
                            "except Exception:\n"
                            "  sys.exit(0)\n"
                            "if d.get('stop_hook_active'):\n"
                            "  sys.exit(0)\n"
                            "# Only signal input needed for interactive sessions.\n"
                            "# If an auto_dispatch or pending_task flag exists, Claude\n"
                            "# was running an automated task — don't open iTerm.\n"
                            "if os.path.exists('.claude/auto_dispatch') or os.path.exists('.claude/pending_task') or os.path.exists('.claude/active_todo'):\n"
                            "  sys.exit(0)\n"
                            "msg=d.get('last_assistant_message','')\n"
                            "if not msg:\n"
                            "  t=d.get('transcript',[])\n"
                            "  msgs=[m for m in t if m.get('role')=='assistant']\n"
                            "  if msgs:\n"
                            "    parts=[p.get('text','') for p in msgs[-1].get('content',[]) if p.get('type')=='text']\n"
                            "    msg=' '.join(parts).strip()\n"
                            "txt=(msg or 'Waiting for input')[-300:]\n"
                            "os.makedirs('.claude',exist_ok=True)\n"
                            "open('.claude/waiting_for_input','w').write(txt)\n"
                            "\""
                        ),
                        "timeout": 5,
                    }
                ],
            }
        ],
        "Notification": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "python3 -c \""
                            "import sys,json,os\n"
                            "try:\n"
                            "  raw=sys.stdin.buffer.read()\n"
                            "except Exception:\n"
                            "  sys.exit(0)\n"
                            "if not raw:\n"
                            "  sys.exit(0)\n"
                            "try:\n"
                            "  d=json.loads(raw)\n"
                            "except Exception:\n"
                            "  sys.exit(0)\n"
                            "os.makedirs('.claude',exist_ok=True)\n"
                            "open('.claude/waiting_for_input','w')"
                            ".write(d.get('title','') + ': ' + d.get('message','Input needed'))\n"
                            "\""
                        ),
                        "timeout": 5,
                    }
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "rm -f .claude/waiting_for_input",
                        "timeout": 2,
                    }
                ],
            },
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 $HOME/.claude/bin/screenshot-hook.py",
                        "timeout": 5,
                    }
                ],
            }
        ],
    },
}

# Default devcontainer.json template
DEVCONTAINER_TEMPLATE = {
    "name": "orch sandbox",
    "image": DEFAULT_IMAGE,
    "features": {
        "ghcr.io/anthropics/devcontainer-features/claude-code:1": {},
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


# ── Screenshot proxy hook ────────────────────────────────────────────────────
#
# When a user drags a macOS screenshot thumbnail into iTerm, the pasted path
# points into /var/folders/.../TemporaryItems/NSIRD_screencaptureui_*/ which
# is SIP-protected — Docker's VM cannot read it, so bind mounts won't help.
#
# This UserPromptSubmit hook (running inside the container) detects the temp
# path pattern, writes a request file to .claude/ (bind-mounted and watched
# by orch on the host), then polls for a done file.  The orch host process
# reads the file with the user's permissions and uses `docker cp` to inject
# it into the container at the same path — so Claude Code can read it.

_SCREENSHOT_HOOK_SCRIPT = """\
#!/usr/bin/env python3
\"\"\"UserPromptSubmit hook: request host-side copy of macOS screenshot temp files.\"\"\"
import sys, json, re, os, time

data = json.load(sys.stdin)
prompt = data.get("prompt", "")

# Match macOS screenshot thumbnail temp paths
PATTERN = (
    r"/var/folders/.+?/TemporaryItems/"
    r"NSIRD_screencaptureui_\\w+/"
    r"[^/]+\\.(?:png|jpg|jpeg|gif|webp|tiff)"
)
raw_paths = [m.group() for m in re.finditer(PATTERN, prompt, re.IGNORECASE)]

# Unescape shell-escaped paths (iTerm pastes backslash-space for spaces)
paths = [re.sub(r'\\\\(.)', r'\\1', p) for p in raw_paths]

if not paths:
    sys.exit(0)

project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
claude_dir = os.path.join(project_dir, ".claude")
req_file = os.path.join(claude_dir, "_screenshot_copy_request")
done_file = os.path.join(claude_dir, "_screenshot_copy_done")

# Clean stale done marker
try:
    os.unlink(done_file)
except OSError:
    pass

# Signal the host
with open(req_file, "w") as f:
    f.write("\\n".join(paths))

# Poll for host to complete the copy (up to 4 seconds)
for _ in range(40):
    if os.path.exists(done_file):
        try:
            os.unlink(done_file)
        except OSError:
            pass
        try:
            os.unlink(req_file)
        except OSError:
            pass
        break
    time.sleep(0.1)
else:
    try:
        os.unlink(req_file)
    except OSError:
        pass

# Don't modify the prompt
print("{}")
"""


# ── Pre-built base image ─────────────────────────────────────────────────────


def _orch_base_image_exists() -> bool:
    """Check if the orch-base Docker image is available locally."""
    result = subprocess.run(
        ["docker", "image", "inspect", ORCH_BASE_IMAGE],
        capture_output=True,
    )
    return result.returncode == 0


def build_base_image() -> str:
    """Build the orch-base Docker image with Claude Code pre-installed.

    Returns the image tag on success.
    """
    dockerfile = (
        f"FROM {DEFAULT_IMAGE}\n"
        f"RUN apt-get update -qq && apt-get install -y -qq nodejs npm \\\n"
        f"    && npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION} \\\n"
        f"    && apt-get clean && rm -rf /var/lib/apt/lists/*\n"
    )
    result = subprocess.run(
        ["docker", "build", "-t", ORCH_BASE_IMAGE, "-f", "-", "."],
        input=dockerfile,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to build {ORCH_BASE_IMAGE}:\n{result.stderr}")
    return ORCH_BASE_IMAGE


def _effective_image() -> str:
    """Return the best available base image.

    Uses the pre-built orch-base image if it exists locally, otherwise
    falls back to the default devcontainer base image.
    """
    if _orch_base_image_exists():
        return ORCH_BASE_IMAGE
    return DEFAULT_IMAGE


# ── Config loading ───────────────────────────────────────────────────────────

def _load_container_config() -> dict:
    defaults = {
        "enabled": True,
        "image": DEFAULT_IMAGE,
        "memory": DEFAULT_MEMORY,
        "blocked_env": ",".join(DEFAULT_BLOCKED_ENV),
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


def _passthrough_env_vars(cfg: dict | None = None) -> dict[str, str]:
    """Return env vars to pass into containers.

    All vars in ``os.environ`` are passed through *except* those on the
    blocklist.  The blocklist defaults to ``DEFAULT_BLOCKED_ENV`` and can
    be overridden via ``blocked_env`` in ``~/.orch/config.toml``.
    """
    if cfg is None:
        cfg = _load_container_config()
    blocked = {
        v.strip().upper()
        for v in cfg.get("blocked_env", "").split(",")
        if v.strip()
    }
    # Prefixes that indicate macOS/system internals — block any var matching
    blocked_prefixes = ("__CF", "XPC_", "ITERM_", "Apple_")
    return {
        k: v for k, v in os.environ.items()
        if k.upper() not in blocked
        and not k.startswith(blocked_prefixes)
        and v
    }


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
    the user's Desktop and Downloads on macOS (common screenshot locations),
    plus the macOS temp directory ($TMPDIR) where screenshot thumbnails live
    before they are moved to their final destination.
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
        original = str(Path(d).expanduser())
        resolved = str(Path(d).expanduser().resolve())
        if Path(resolved).is_dir():
            # Use the resolved path as Docker source (Docker Desktop needs
            # real paths) but the *original* path as the container target so
            # it matches what iTerm pastes.  This matters on macOS where
            # /var is a symlink to /private/var — TMPDIR gives /var/folders/…
            # but resolve() produces /private/var/folders/… .
            result.append((resolved, original))
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

# Target path for the SSH agent socket inside the container.
# IMPORTANT: Must NOT be under /tmp — devcontainer features like
# docker-in-docker mount /tmp as tmpfs, which shadows bind mounts.
_CONTAINER_SSH_AUTH_SOCK = "/run/ssh-agent.sock"


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

def _prepare_container_user_settings() -> Path | None:
    """Filter host user-level settings.local.json for container use.

    The host's ~/.claude/settings.local.json is inherited via bind mount,
    but hooks referencing host-only paths (e.g. .curated-context) fail
    inside containers.  Create a filtered copy at ~/.orch/ and return its
    path so it can be shadow-mounted over the original.
    """
    host_settings = Path.home() / ".claude" / "settings.local.json"
    if not host_settings.exists():
        return None
    try:
        data = json.loads(host_settings.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    hooks = data.get("hooks", {})
    if not hooks:
        return None

    # Signatures of host-only hooks that won't work in containers
    _HOST_ONLY = (".curated-context",)

    filtered_hooks: dict = {}
    changed = False
    for event, groups in hooks.items():
        filtered_groups = []
        for group in groups:
            kept = [
                h for h in group.get("hooks", [])
                if not any(sig in h.get("command", "") for sig in _HOST_ONLY)
            ]
            if len(kept) != len(group.get("hooks", [])):
                changed = True
            if kept:
                filtered_groups.append({**group, "hooks": kept})
        if filtered_groups:
            filtered_hooks[event] = filtered_groups

    if not changed:
        return None  # nothing to filter

    filtered = {k: v for k, v in data.items() if k != "hooks"}
    if filtered_hooks:
        filtered["hooks"] = filtered_hooks

    out_dir = Path.home() / ".orch"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "container-user-settings.json"
    out.write_text(json.dumps(filtered, indent=2) + "\n")
    return out


def _mount_target(mount_str: str) -> str | None:
    """Extract the target path from a devcontainer mount string."""
    for part in mount_str.split(","):
        key, _, val = part.partition("=")
        if key.strip() == "target":
            return val
    return None


def _prepare_devcontainer_config(project: "Project") -> Path:
    """
    Prepare a merged devcontainer config for the project.

    If the project has an existing .devcontainer/devcontainer.json, orch
    reads it and merges in its requirements (Claude config mount, env vars,
    reference dirs).  The merged result is written to
    .devcontainer/devcontainer.orch.json — the user's devcontainer.json
    is NEVER modified by orch.

    If no project config exists, generates one from orch's template
    directly into devcontainer.orch.json.

    The devcontainer CLI is pointed at the .orch.json file via --config.

    Returns the path to the merged config file.
    """
    dc_dir = project.path / ".devcontainer"
    dc_json = dc_dir / "devcontainer.json"
    orch_dir = dc_dir / "orch"
    dc_orch = orch_dir / "devcontainer.json"
    dc_base = dc_dir / "devcontainer.base.json"
    cfg = _load_container_config()

    # ── Migration: undo the old base.json convention ────────────
    # Previously orch copied devcontainer.json -> devcontainer.base.json
    # and overwrote devcontainer.json with its merged output.  Restore
    # the user's original if it was backed up.
    if dc_base.exists():
        if dc_json.exists():
            # devcontainer.json is orch's merged output — replace with
            # the user's original from base.
            dc_json.write_text(dc_base.read_text())
        else:
            # No devcontainer.json at all — restore from base.
            dc_base.rename(dc_json)
        if dc_base.exists():
            dc_base.unlink()

    if dc_json.exists():
        # ── Merge mode ──────────────────────────────────────────────
        # Always read the user's devcontainer.json as the source of truth.
        config = json.loads(dc_json.read_text())

        # Upgrade to pre-built image if using the default base image
        if config.get("image") == DEFAULT_IMAGE:
            config["image"] = _effective_image()

        # Inject all host env vars (minus blocklist) into containerEnv.
        # This also resolves any ${localEnv:...} placeholders the user may
        # have in their base config — we replace them with concrete values
        # so there's no dependency on the devcontainer CLI's localEnv
        # resolution (which requires the calling process to already have
        # the var set in its environment).
        container_env = config.get("containerEnv", {})
        passthrough = _passthrough_env_vars(cfg)
        # Resolve ${localEnv:VAR} placeholders in existing values
        for key, val in list(container_env.items()):
            if isinstance(val, str) and "${localEnv:" in val:
                ref_var = val.split("${localEnv:")[1].split("}")[0]
                resolved = passthrough.get(ref_var) or os.environ.get(ref_var, "")
                if resolved:
                    container_env[key] = resolved
        # Inject remaining passthrough vars (don't overwrite explicit user config)
        for key, val in passthrough.items():
            if key not in container_env:
                container_env[key] = val
        container_env["CLAUDE_CONFIG_DIR"] = f"{CONTAINER_HOME}/.claude"
        config["containerEnv"] = container_env

        # Ensure required devcontainer features are present
        features = config.get("features", {})
        # Build a set of feature base names (without version) already present
        existing_bases = {k.rsplit(":", 1)[0] for k in features}
        for feat_key, feat_val in DEVCONTAINER_TEMPLATE["features"].items():
            feat_base = feat_key.rsplit(":", 1)[0]
            if feat_key not in features and feat_base not in existing_bases:
                features[feat_key] = feat_val
        config["features"] = features

        # Replace any existing ~/.claude mount with orch's, or add it
        claude_host = str(Path.home() / ".claude")
        claude_mount = f"source={claude_host},target={CONTAINER_HOME}/.claude,type=bind"

        existing_mounts = config.get("mounts", [])
        # Rewrite any mount sources that reference a stale home directory
        # (e.g. from a different machine) to the current home.
        current_home = str(Path.home())
        fixed_mounts = []
        for m in existing_mounts:
            if isinstance(m, str) and "source=" in m:
                src = m.split("source=")[1].split(",")[0]
                # If the source starts with /Users/ or /home/ but doesn't
                # match the current home, rewrite it.
                if (src.startswith(("/Users/", "/home/"))
                        and not src.startswith(current_home)):
                    # Extract the path relative to the old home dir
                    # e.g. /Users/odmarkj/.claude.json -> .claude.json
                    parts = src.split("/", 3)  # ['', 'Users', 'odmarkj', '.claude.json']
                    if len(parts) >= 4:
                        rel = parts[3]
                        new_src = f"{current_home}/{rel}"
                        m = m.replace(f"source={src}", f"source={new_src}")
                fixed_mounts.append(m)
            else:
                fixed_mounts.append(m)

        merged_mounts = []
        has_claude_mount = False

        for m in fixed_mounts:
            target = _mount_target(m)
            if target and target.rstrip("/").endswith("/.claude"):
                has_claude_mount = True
                merged_mounts.append(claude_mount)
            else:
                merged_mounts.append(m)

        if not has_claude_mount:
            merged_mounts.insert(0, claude_mount)

        # Shadow user-level settings.local.json to filter out host-only hooks
        # (e.g. curated-context) that fail inside containers.
        filtered_settings = _prepare_container_user_settings()
        if filtered_settings:
            settings_target = f"{CONTAINER_HOME}/.claude/settings.local.json"
            # Remove any existing shadow mount for this file
            merged_mounts = [
                m for m in merged_mounts
                if _mount_target(m) != settings_target
            ]
            merged_mounts.append(
                f"source={filtered_settings},target={settings_target},type=bind"
            )

        # Forward host SSH agent into the container
        ssh = _ssh_agent_mount()
        if ssh:
            ssh_mount, ssh_env = ssh
            # Remove any stale SSH socket mounts (e.g. old /tmp/ssh-agent.sock
            # path that gets shadowed by docker-in-docker tmpfs on /tmp)
            merged_mounts = [
                m for m in merged_mounts
                if not (_mount_target(m) or "").endswith("/ssh-agent.sock")
            ]
            merged_mounts.append(ssh_mount)
            container_env = config.get("containerEnv", {})
            container_env["SSH_AUTH_SOCK"] = ssh_env
            config["containerEnv"] = container_env

        # Mount Wrangler config so Cloudflare CLI auth persists
        wrangler_host = Path.home() / ".wrangler"
        if wrangler_host.is_dir():
            wrangler_target = f"{CONTAINER_HOME}/.wrangler"
            if not any(_mount_target(m) == wrangler_target for m in merged_mounts):
                merged_mounts.append(
                    f"source={wrangler_host},target={wrangler_target},type=bind"
                )

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

        # Mount worktree root so dispatch/bridge tasks run inside container
        worktree_root = str(project.path.parent / ".orch-worktrees")
        if not any(_mount_target(m) == worktree_root for m in merged_mounts):
            Path(worktree_root).mkdir(parents=True, exist_ok=True)
            merged_mounts.append(
                f"source={worktree_root},target={worktree_root},type=bind"
            )

        config["mounts"] = merged_mounts

        # Merge orch shell setup into postCreateCommand
        existing_post = config.get("postCreateCommand", "")
        if isinstance(existing_post, str):
            if _ORCH_POST_MARKER not in existing_post:
                parts = [p for p in (existing_post, ORCH_POST_CREATE) if p]
                config["postCreateCommand"] = " && ".join(parts)
        # If it's a list/object form, leave it alone — those are complex

        orch_dir.mkdir(exist_ok=True)
        dc_orch.write_text(json.dumps(config, indent=2) + "\n")
        return dc_orch

    # ── Generate mode: no existing config ────────────────────────
    dc_dir.mkdir(exist_ok=True)
    orch_dir.mkdir(exist_ok=True)

    config = dict(DEVCONTAINER_TEMPLATE)
    config["name"] = f"orch — {project.name}"

    # Use the pre-built orch-base image if available (Claude Code
    # is already installed, so the feature just verifies).
    config["image"] = _effective_image()

    # Add env var passthrough (all host env vars except blocklisted ones)
    container_env = dict(config.get("containerEnv", {}))
    container_env.update(_passthrough_env_vars(cfg))
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

    # Shadow user-level settings.local.json to filter out host-only hooks
    filtered_settings = _prepare_container_user_settings()
    if filtered_settings:
        config["mounts"].append(
            f"source={filtered_settings},target={CONTAINER_HOME}/.claude/settings.local.json,type=bind"
        )

    # Mount Wrangler config so Cloudflare CLI auth persists
    wrangler_host = Path.home() / ".wrangler"
    if wrangler_host.is_dir():
        config["mounts"].append(
            f"source={wrangler_host},target={CONTAINER_HOME}/.wrangler,type=bind"
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

    # Mount worktree root so dispatch/bridge tasks run inside container
    worktree_root = str(project.path.parent / ".orch-worktrees")
    Path(worktree_root).mkdir(parents=True, exist_ok=True)
    config["mounts"].append(
        f"source={worktree_root},target={worktree_root},type=bind"
    )

    dc_orch.write_text(json.dumps(config, indent=2) + "\n")
    return dc_orch


def _devcontainer_up(project: "Project") -> str:
    """Use devcontainer CLI to start container. Returns container ID."""
    config_path = _prepare_devcontainer_config(project)

    # Env vars are already in containerEnv within the devcontainer.orch.json
    # written by _prepare_devcontainer_config — no need for --remote-env.
    env_args: list[str] = []

    # If no container is tracked by orch, remove any stale devcontainer-
    # managed container so a fresh one is created with the current config
    # (mounts, env, etc. are only applied at container creation time).
    existing = _find_container_by_label(project)
    if existing:
        subprocess.run(["docker", "rm", "-f", existing], capture_output=True)

    cmd = [
        "devcontainer", "up",
        "--workspace-folder", str(project.path),
        "--config", str(config_path),
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
    # Prefer pre-built orch-base image, then user config, then default
    image = _effective_image() if _orch_base_image_exists() else cfg.get("image", DEFAULT_IMAGE)
    memory = cfg.get("memory", DEFAULT_MEMORY)
    container_name = f"{CONTAINER_PREFIX}{project.name}"

    # Remove existing stopped container with same name
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
    )

    # Build env args (all host env vars except blocklisted ones)
    env_args = []
    for key, val in _passthrough_env_vars(cfg).items():
        env_args += ["-e", f"{key}={val}"]

    claude_host = Path.home() / ".claude"
    claude_json = Path.home() / ".claude.json"

    mount_args = [
        "-v", f"{project.path}:{WORKSPACE_DIR}",
        "-v", f"{claude_host}:{CONTAINER_HOME}/.claude",
    ]
    if claude_json.exists():
        mount_args += ["-v", f"{claude_json}:{CONTAINER_HOME}/.claude.json"]

    # Mount Wrangler config so Cloudflare CLI auth persists
    wrangler_host = Path.home() / ".wrangler"
    if wrangler_host.is_dir():
        mount_args += ["-v", f"{wrangler_host}:{CONTAINER_HOME}/.wrangler"]

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

    # Mount worktree root so dispatch/bridge tasks run inside container
    worktree_root = project.path.parent / ".orch-worktrees"
    worktree_root.mkdir(parents=True, exist_ok=True)
    mount_args += ["-v", f"{worktree_root}:{worktree_root}"]

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
         f"npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}"],
        capture_output=True,
        timeout=180,
    )


# ── Permission setup ─────────────────────────────────────────────────────────

def _setup_permissions(cid: str, project: "Project | None" = None) -> None:
    """Merge orch settings into settings.local.json inside the container.

    Reads any existing settings.local.json (e.g. project-level hooks written
    by curated-context bootstrap) and deep-merges orch's required permissions
    and hooks on top, preserving entries from other sources.
    """
    workdir = _container_workdir(cid, project)
    settings_path = f"{workdir}/.claude/settings.local.json"

    # Read existing settings.local.json from the container (may not exist)
    existing: dict = {}
    result = subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"cat {settings_path} 2>/dev/null || echo '{{}}'"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            existing = json.loads(result.stdout)
        except json.JSONDecodeError:
            existing = {}

    # Deep-merge: orch permissions are authoritative, hooks are additive
    merged = existing.copy()

    # Permissions: orch's allow list is authoritative
    merged["permissions"] = SETTINGS_LOCAL["permissions"]

    # Hooks: merge per event type — prepend orch hooks, keep existing.
    # To identify stale orch hooks from prior versions (whose command text
    # may have changed), we match on signature substrings that appear in
    # every version of an orch-managed hook.  Any existing hook containing
    # one of these signatures is treated as an orch hook and replaced.
    _ORCH_SIGNATURES = ("waiting_for_input", "screenshot-hook")
    orch_hooks = SETTINGS_LOCAL.get("hooks", {})
    existing_hooks = existing.get("hooks", {})
    merged_hooks: dict = {}
    all_events = set(orch_hooks.keys()) | set(existing_hooks.keys())
    for event in all_events:
        orch_list = orch_hooks.get(event, [])
        existing_list = existing_hooks.get(event, [])
        deduped_existing = []
        for group in existing_list:
            filtered_hooks = [
                h for h in group.get("hooks", [])
                if not any(sig in h.get("command", "") for sig in _ORCH_SIGNATURES)
            ]
            if filtered_hooks:
                deduped_existing.append({**group, "hooks": filtered_hooks})
        merged_hooks[event] = orch_list + deduped_existing
    merged["hooks"] = merged_hooks

    merged_json = json.dumps(merged, indent=2)
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"mkdir -p {workdir}/.claude && "
         f"cat > {settings_path} << 'ORCHEOF'\n"
         f"{merged_json}\n"
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

# Tracks the keychain fingerprint last injected into each container.
# When the periodic refresh fires, we only overwrite the container's
# .credentials.json if the keychain has CHANGED (i.e. the user re-authed
# on the host).  This prevents clobbering tokens that the container's
# Claude Code has self-refreshed via its own OAuth refresh_token flow.
_last_injected_fingerprint: dict[str, str] = {}  # cid -> md5 hex


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


def _inject_credentials(cid: str, *, force: bool = False) -> None:
    """Extract OAuth credentials from macOS Keychain and write them into the
    container as a plaintext credentials file.

    Claude Code on Linux reads $CLAUDE_CONFIG_DIR/.credentials.json as a
    fallback when the macOS Keychain is unavailable.  We extract the token
    from the host Keychain and write it directly to that path inside the
    container.

    When *force* is False (the default for periodic refreshes), the write is
    skipped if the keychain credentials haven't changed since the last
    injection.  This prevents overwriting tokens that the container's Claude
    Code has self-refreshed via its OAuth refresh_token.

    Set *force=True* for initial injection (container startup) or when the
    user explicitly requests a credential refresh.
    """
    cred_json = _extract_host_credentials()
    if not cred_json:
        return  # No credentials to inject (not macOS, or not logged in)

    fingerprint = hashlib.md5(cred_json.encode()).hexdigest()

    if not force and _last_injected_fingerprint.get(cid) == fingerprint:
        # Keychain hasn't changed — don't overwrite container's
        # potentially self-refreshed token.
        return

    safe_cred = shlex.quote(cred_json)
    cred_path = f"{CONTAINER_HOME}/.claude/.credentials.json"
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"printf '%s' {safe_cred} > {cred_path} && chmod 600 {cred_path}"],
        capture_output=True, timeout=10,
    )
    _last_injected_fingerprint[cid] = fingerprint


# ── Plugin path compatibility ─────────────────────────────────────────────────


def _deploy_screenshot_hook(cid: str) -> None:
    """Write the screenshot UserPromptSubmit hook script into the container."""
    bin_dir = f"{CONTAINER_HOME}/.claude/bin"
    script_path = f"{bin_dir}/screenshot-hook.py"
    escaped = _SCREENSHOT_HOOK_SCRIPT.replace("'", "'\\''")
    subprocess.run(
        ["docker", "exec", "-u", CONTAINER_USER, cid, "bash", "-c",
         f"mkdir -p {bin_dir} && printf '%s' '{escaped}' > {script_path} && "
         f"chmod +x {script_path}"],
        capture_output=True,
        timeout=10,
    )


_REAUTH_SCRIPT = """\
#!/usr/bin/env bash
# orch-reauth: Re-authenticate Claude Code inside a container.
#
# Run this from the container shell tab when Claude shows a login error.
# It runs 'claude login' which will print a URL — open that in your browser
# to complete the OAuth flow. The new token is saved automatically.

set -e
echo ""
echo "Starting Claude Code login..."
echo "A URL will appear below — open it in your browser to authenticate."
echo ""
claude login
echo ""
echo "Done! You can now resume your Claude session."
"""


def _deploy_reauth_helper(cid: str) -> None:
    """Write the orch-reauth script into the container's PATH."""
    script_path = "/usr/local/bin/orch-reauth"
    escaped = _REAUTH_SCRIPT.replace("'", "'\\''")
    subprocess.run(
        ["docker", "exec", "-u", "root", cid, "bash", "-c",
         f"printf '%s' '{escaped}' > {script_path} && chmod +x {script_path}"],
        capture_output=True,
        timeout=10,
    )


def _symlink_host_claude_dir(cid: str) -> None:
    """Create a symlink from the host .claude path so plugin paths resolve.

    Claude Code plugins store absolute paths (installPath, installLocation)
    in ~/.claude/plugins/*.json.  When ~/.claude is bind-mounted from the
    host, those paths reference the host home (e.g. /Users/alice/.claude)
    which doesn't exist inside the container (/home/vscode/.claude).

    We create a symlink at the host path pointing to the container path
    so that plugin marketplace validation and loading succeed.
    """
    host_claude = Path.home() / ".claude"
    if not host_claude.exists():
        return

    host_claude_str = str(host_claude)
    container_claude = f"{CONTAINER_HOME}/.claude"

    # If host path == container path, no symlink needed
    if host_claude_str == container_claude:
        return

    subprocess.run(
        ["docker", "exec", cid, "bash", "-c",
         f"mkdir -p $(dirname {shlex.quote(host_claude_str)}) && "
         f"ln -sfn {shlex.quote(container_claude)} {shlex.quote(host_claude_str)}"],
        capture_output=True,
        timeout=10,
    )


# ── Plugin setup ─────────────────────────────────────────────────────────────


def _setup_plugins(cid: str) -> None:
    """Run Setup hooks for bind-mounted plugins inside the container.

    When ~/.claude is bind-mounted from the host, plugin caches are shared
    but may lack platform-specific artifacts (node_modules built for Linux,
    Bun binaries, etc.).  This reads each installed plugin's hooks.json and
    runs its Setup hook inside the container so the plugin can install
    whatever it needs for the container environment.
    """
    installed_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not installed_path.exists():
        return

    try:
        registry = json.loads(installed_path.read_text())
    except (json.JSONDecodeError, OSError):
        return

    for _plugin_name, installs in registry.get("plugins", {}).items():
        for install_info in installs:
            # Resolve plugin root: translate host path to container path
            host_install = install_info.get("installPath", "")
            if not host_install:
                continue
            host_claude = str(Path.home() / ".claude")
            container_install = host_install.replace(
                host_claude, f"{CONTAINER_HOME}/.claude", 1
            )

            # Read hooks.json for this plugin
            hooks_file = Path(host_install) / "hooks" / "hooks.json"
            if not hooks_file.exists():
                continue

            try:
                hooks_config = json.loads(hooks_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            setup_hooks = hooks_config.get("hooks", {}).get("Setup", [])
            for group in setup_hooks:
                for hook in group.get("hooks", []):
                    if hook.get("type") != "command":
                        continue
                    # Substitute CLAUDE_PLUGIN_ROOT with the container path
                    cmd = hook["command"].replace(
                        "${CLAUDE_PLUGIN_ROOT}", container_install
                    )
                    timeout = hook.get("timeout", 60)
                    subprocess.run(
                        ["docker", "exec", "-u", CONTAINER_USER, cid,
                         "bash", "-c", cmd],
                        capture_output=True,
                        timeout=max(timeout, 10),
                        env={**os.environ, "CLAUDE_PLUGIN_ROOT": container_install},
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
    _inject_credentials(cid, force=True)

    # Deploy the screenshot copy hook so dragged screenshots work in containers
    _deploy_screenshot_hook(cid)

    # Deploy reauth helper so users can re-login from inside the container
    _deploy_reauth_helper(cid)

    # Symlink host .claude path so plugin absolute paths resolve in container
    _symlink_host_claude_dir(cid)

    # Run plugin setup hooks so bind-mounted plugins install container deps
    _setup_plugins(cid)

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
    # 1. Map host project path to container mount path (preferred)
    #    Docker Desktop on macOS prefixes sources with /host_mnt, so we
    #    normalise both sides before comparing.
    #    Use the longest (most specific) match to avoid resolving to a
    #    parent read-only reference mount when the project has its own
    #    read-write workspace mount.
    if project:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{range .Mounts}}{{.Source}}\t{{.Destination}}\t{{.RW}}\n{{end}}", cid],
            capture_output=True, text=True,
        )
        host_path = str(project.path)
        best_match: tuple[str, int] | None = None  # (container_path, match_len)
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split("\t")
            if len(parts) < 2:
                continue
            src, dest = parts[0], parts[1]
            rw = parts[2] if len(parts) > 2 else "true"
            # Strip /host_mnt prefix that Docker Desktop adds on macOS
            norm_src = src.removeprefix("/host_mnt")
            if host_path.startswith(norm_src) and norm_src != "/":
                relative = host_path[len(norm_src):].lstrip("/")
                container_path = f"{dest}/{relative}" if relative else dest
                # Prefer read-write mounts; among same-rw mounts, prefer longest match
                match_len = len(norm_src)
                is_rw = rw.lower() == "true"
                if best_match is None:
                    best_match = (container_path, match_len, is_rw)
                else:
                    _, prev_len, prev_rw = best_match
                    # Always prefer rw over ro; within same rw class, prefer longer match
                    if (is_rw and not prev_rw) or (is_rw == prev_rw and match_len > prev_len):
                        best_match = (container_path, match_len, is_rw)
        if best_match:
            return best_match[0]

    # 2. Fallback: check common workspace paths inside the container
    #    devcontainer CLI uses /workspaces/<name>, raw docker uses /workspace
    candidates = [WORKSPACE_DIR]
    if project:
        candidates.insert(0, f"/workspaces/{project.name}")
    candidates.append("/workspaces")

    for path in candidates:
        check = subprocess.run(
            ["docker", "exec", cid, "test", "-d", path],
            capture_output=True,
        )
        if check.returncode == 0:
            return path

    return WORKSPACE_DIR


def _ensure_credentials_injected(cid: str) -> None:
    """Re-inject credentials from the host Keychain into the container.

    Uses force=True so that opening a new session always picks up any
    host re-authentication.  This is called before each Claude session
    (explicit user action), not from periodic background refresh.
    """
    _inject_credentials(cid, force=True)


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
    badge = _iterm_badge_cmd(project.name)
    return f"{badge} && docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"


# ── iTerm2 integration ───────────────────────────────────────────────────────

def exec_claude_in_iterm(project: "Project", with_shell: bool = False) -> None:
    """
    Spin up a container (if needed) and open a NEW iTerm2 window with claude
    running inside it.  Every invocation creates a fresh window — pressing
    "c" multiple times gives multiple independent sessions.

    If with_shell is True, also opens a second tab with a plain bash shell
    in the same window.
    """
    from .iterm import _load_config, _applescript_quote, _run_iterm_script

    # Ensure container is running
    cid = ensure_running(project)
    _ensure_credentials_injected(cid)
    workdir = _container_workdir(cid, project)

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")

    tab_name = f"{project.name} (container)"
    badge = _iterm_badge_cmd(project.name)
    args = _build_claude_args(project)
    tenv = _terminal_env_flags()
    claude_cmd = _applescript_quote(
        f"{badge} && docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args}"
    )

    if with_shell:
        shell_tab_name = f"{project.name} (shell)"
        shell_cmd = _applescript_quote(
            f"{badge} && docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} bash"
        )
        # New window with Claude tab + shell tab
        script = f"""
        tell application "iTerm2"
            try
                set newWindow to (create window with profile "{profile}")
            on error
                set newWindow to (create window with default profile)
            end try
            tell newWindow
                -- Claude tab (the initial session in the new window)
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text {claude_cmd}
                    set claudeTty to tty
                end tell
                -- Shell tab
                try
                    create tab with profile "{profile}"
                on error
                    create tab with default profile
                end try
                tell current session
                    set name to "{shell_tab_name}"
                    set badge to "{project.name}"
                    write text {shell_cmd}
                end tell
                -- Switch focus back to Claude tab
                repeat with aTab in tabs
                    try
                        repeat with aSession in sessions of aTab
                            try
                                if tty of aSession is claudeTty then
                                    select aTab
                                    exit repeat
                                end if
                            on error
                            end try
                        end repeat
                    on error
                    end try
                end repeat
            end tell
        end tell
        """
    else:
        # New window with just a Claude tab
        script = f"""
        tell application "iTerm2"
            try
                set newWindow to (create window with profile "{profile}")
            on error
                set newWindow to (create window with default profile)
            end try
            tell newWindow
                tell current session
                    set name to "{tab_name}"
                    set badge to "{project.name}"
                    write text {claude_cmd}
                end tell
            end tell
        end tell
        """

    _run_iterm_script(script)


def _build_single_tab_script(*, profile: str, dedicated: bool,
                              tab_name: str, cmd: str,
                              badge: str = "") -> str:
    """Build AppleScript to open a single iTerm2 tab.

    ``cmd`` must already be AppleScript-quoted (via ``_applescript_quote``).
    """
    badge_line = f'set badge to "{badge}"' if badge else ""
    if dedicated:
        return f"""
        tell application "iTerm2"
            set orchWindow to missing value
            set isNewWindow to false
            set foundOrch to false
            repeat with w in windows
                if not foundOrch then
                    try
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
                    on error
                        -- Window/tab reference went stale; skip it
                    end try
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
                    write text {cmd}
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """
    else:
        return f"""
        tell application "iTerm2"
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
                    write text {cmd}
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
    from .iterm import _load_config, _tab_exists, _bring_tab_to_front

    # Reuse existing shell tab if open — focus it instead of opening a duplicate
    tab_name = f"{project.name} (shell)"
    handle_file = project.claude_dir / "iterm_container_shell_handle"
    if handle_file.exists():
        tty = handle_file.read_text().strip()
        alive = _tab_exists(tty) if tty else False
        if alive is True:
            _bring_tab_to_front(tty)
            return
        if alive is None:
            return  # Check failed — keep handle, don't open duplicate
        handle_file.unlink(missing_ok=True)

    # Ensure container is running
    cid = ensure_running(project)
    workdir = _container_workdir(cid, project)
    from .iterm import _applescript_quote
    badge = _iterm_badge_cmd(project.name)
    shell_cmd = _applescript_quote(
        f"{badge} && docker exec -it -u {CONTAINER_USER} -w {workdir} {cid} bash"
    )

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")

    if dedicated:
        script = f"""
        tell application "iTerm2"
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
                    write text {shell_cmd}
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """
    else:
        script = f"""
        tell application "iTerm2"
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
                    write text {shell_cmd}
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
    from .iterm import _applescript_quote
    badge = _iterm_badge_cmd(project.name)
    claude_cmd = _applescript_quote(
        f"{badge} && docker exec -it {tenv} -u {CONTAINER_USER} -w {workdir} {cid} claude {args} -p '{safe_task}'"
    )

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    dedicated = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")
    tab_name = f"{project.name} task"

    if dedicated:
        script = f"""
        tell application "iTerm2"
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
                    write text {claude_cmd}
                end tell
            end tell
        end tell
        """
    else:
        script = f"""
        tell application "iTerm2"
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
                    write text {claude_cmd}
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


def worktree_container_path(project: "Project", worktree_path: Path) -> str:
    """Map a host worktree path to its container mount path.

    We mount the worktree root at the same host path inside the container,
    so the path is identical.
    """
    return str(worktree_path)


def run_claude_in_container(
    project: "Project",
    prompt: str,
    workdir: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    """Run Claude inside the project's container with a prompt.

    Ensures the container is running first.  Uses ``docker exec`` (blocking,
    not detached) so the caller can capture output.  Used by both the
    auto-dispatch pipeline and the bridge.
    """
    cid = ensure_running(project)
    if workdir is None:
        workdir = _container_workdir(cid, project)

    safe_prompt = prompt.replace("'", "'\\''")

    return subprocess.run(
        [
            "docker", "exec",
            "-u", CONTAINER_USER,
            "-w", workdir,
            cid, "bash", "-c",
            f"claude --dangerously-skip-permissions -p '{safe_prompt}'",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_git_in_container(
    project: "Project",
    worktree_path: Path,
    git_args: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """Run a git command inside the container at *worktree_path*."""
    cid = ensure_running(project)
    container_wt = worktree_container_path(project, worktree_path)
    cmd_str = " ".join(shlex.quote(a) for a in ["git"] + git_args)
    return subprocess.run(
        [
            "docker", "exec",
            "-u", CONTAINER_USER,
            "-w", container_wt,
            cid, "bash", "-c", cmd_str,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


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


def create_worktree(
    project: "Project", todo_text: str, *, branch_prefix: str = "auto",
) -> tuple[Path, str]:
    """Create a git worktree for the given task.

    *branch_prefix* controls the branch namespace (``auto/`` for dispatch,
    ``bridge/`` for bridge requests).

    Returns (worktree_path, branch_name).
    """
    import random as _rand

    _ensure_worktrees_gitignored(project)

    slug = _slugify(todo_text)
    suffix = _rand.randint(1000, 9999)
    branch_name = f"{branch_prefix}/{slug}-{suffix}"
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
    Run Claude code review on the worktree changes inside the container.
    Returns the review text.
    """
    # Get the diff of changes in the worktree
    diff_result = _run_git_in_container(
        project, worktree_path, ["diff", "HEAD~1"], timeout=30,
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

    container_wt = worktree_container_path(project, worktree_path)
    result = run_claude_in_container(
        project, review_prompt, workdir=container_wt, timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _commit_and_push_worktree(
    project: "Project", worktree_path: Path, branch_name: str, todo_text: str,
) -> None:
    """Stage all changes, commit, and push the worktree branch inside the container."""
    # Stage all changes
    _run_git_in_container(project, worktree_path, ["add", "-A"], timeout=30)

    # Check if there are changes to commit
    status = _run_git_in_container(
        project, worktree_path, ["status", "--porcelain"], timeout=10,
    )
    if not status.stdout.strip():
        return  # Nothing to commit

    # Commit
    safe_msg = todo_text[:72]
    _run_git_in_container(
        project, worktree_path, ["commit", "-m", f"auto: {safe_msg}"], timeout=30,
    )

    # Push with retries (exponential backoff)
    import time
    delays = [2, 4, 8, 16]
    for attempt in range(5):
        result = _run_git_in_container(
            project, worktree_path,
            ["push", "-u", "origin", branch_name],
            timeout=60,
        )
        if result.returncode == 0:
            return
        if attempt < len(delays):
            time.sleep(delays[attempt])

    # Final attempt failed — not fatal, just log


def _create_pr(
    project: "Project",
    worktree_path: Path,
    branch_name: str,
    todo_text: str,
    review_text: str = "",
    title_prefix: str = "auto",
) -> str | None:
    """Create a pull request for the worktree branch using gh inside the container.

    Returns the PR URL or None on failure.
    """
    body = f"## Auto-dispatched task\n\n{todo_text}\n"
    if review_text:
        body += f"\n## Code Review\n\n{review_text}\n"

    safe_title = f"{title_prefix}: {todo_text[:60]}"
    safe_body = body.replace("'", "'\\''")
    safe_title_sh = safe_title.replace("'", "'\\''")

    cid = ensure_running(project)
    container_wt = worktree_container_path(project, worktree_path)
    gh_cmd = (
        f"gh pr create --title '{safe_title_sh}' "
        f"--body '{safe_body}' --head '{branch_name}'"
    )
    result = subprocess.run(
        [
            "docker", "exec",
            "-u", CONTAINER_USER,
            "-w", container_wt,
            cid, "bash", "-c", gh_cmd,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _run_tests(
    project: "Project", worktree_path: Path, test_cmd: str, timeout: int = 300,
) -> tuple[bool, str]:
    """Run the project's test command inside the container.

    Returns (passed, output) where output includes both stdout and stderr.
    """
    cid = ensure_running(project)
    container_wt = worktree_container_path(project, worktree_path)

    result = subprocess.run(
        [
            "docker", "exec",
            "-u", CONTAINER_USER,
            "-w", container_wt,
            cid, "bash", "-c", test_cmd,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        output += "\n" + result.stderr
    # Cap output to avoid blowing up the prompt
    output = output.strip()
    if len(output) > 8000:
        output = output[-8000:]
    return result.returncode == 0, output


def run_task_in_worktree(project: "Project", todo_text: str) -> dict:
    """Full pipeline: worktree -> container Claude -> test-fix -> review -> commit -> push -> PR.

    All Claude invocations, tests, and git operations run inside the
    project's container via ``docker exec``.

    Returns a dict: {branch, worktree, pr_url, review, test_attempts, tests_passed}.
    """
    worktree_path, branch_name = create_worktree(project, todo_text)
    container_wt = worktree_container_path(project, worktree_path)
    results = {
        "branch": branch_name,
        "worktree": str(worktree_path),
        "pr_url": None,
        "review": "",
        "test_attempts": 0,
        "tests_passed": None,
    }

    test_cmd = project.test_cmd
    max_fix = project.max_fix_attempts if test_cmd else 0

    try:
        # Ensure the container is up before any docker exec calls
        ensure_running(project)

        # ── Initial Claude run ──────────────────────────────────────────
        task_prompt = (
            f"Work on this task: {todo_text}\n\n"
            f"When done, make sure all changes are saved. Do not commit or push."
        )
        run_claude_in_container(project, task_prompt, workdir=container_wt, timeout=600)

        # ── Test-fix loop ───────────────────────────────────────────────
        if test_cmd:
            for attempt in range(1, max_fix + 2):  # attempt 1 = first test run
                results["test_attempts"] = attempt
                passed, test_output = _run_tests(project, worktree_path, test_cmd)

                if passed:
                    results["tests_passed"] = True
                    break

                # Last attempt failed — record and move on
                if attempt > max_fix:
                    results["tests_passed"] = False
                    break

                # Send failure back to Claude for fixing
                fix_prompt = (
                    f"The tests failed (attempt {attempt}/{max_fix}). "
                    f"Fix the failing tests and make sure all changes are saved. "
                    f"Do not commit or push.\n\n"
                    f"Test command: {test_cmd}\n\n"
                    f"Test output:\n```\n{test_output}\n```"
                )
                run_claude_in_container(project, fix_prompt, workdir=container_wt, timeout=600)

        # ── Code review (if enabled) ────────────────────────────────────
        if project.code_review_enabled:
            # First do a temporary commit so review can diff
            _run_git_in_container(project, worktree_path, ["add", "-A"], timeout=10)
            _run_git_in_container(
                project, worktree_path,
                ["commit", "-m", f"wip: {todo_text[:50]}"],
                timeout=10,
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
            _commit_and_push_worktree(project, worktree_path, branch_name, todo_text)

        # Push (handles both cases — review already committed, or fresh commit)
        if project.code_review_enabled:
            # Amend commit with final message and push
            _run_git_in_container(
                project, worktree_path,
                ["commit", "--amend", "-m", f"auto: {todo_text[:72]}"],
                timeout=10,
            )
            import time
            delays = [2, 4, 8, 16]
            for attempt in range(5):
                result = _run_git_in_container(
                    project, worktree_path,
                    ["push", "-u", "origin", branch_name, "--force-with-lease"],
                    timeout=60,
                )
                if result.returncode == 0:
                    break
                if attempt < len(delays):
                    time.sleep(delays[attempt])

        # Create PR
        pr_url = _create_pr(
            project, worktree_path, branch_name, todo_text, results.get("review", ""),
        )
        results["pr_url"] = pr_url

    except Exception:
        # On failure, still try to clean up worktree and local branch
        try:
            remove_worktree(project, worktree_path, branch_name)
        except Exception:
            pass
        raise

    return results
