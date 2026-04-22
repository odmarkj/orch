"""
orch VM management — Lima lifecycle for the single shared VM.

Replaces container.py. All projects run inside one Lima VM with virtiofs
mounts at the same host paths. No per-project containers, no Docker image
management, no credential injection.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

VM_NAME = "orch"
LIMA_YAML = Path(__file__).parent.parent / "lima" / "orch.yaml"


def vm_status() -> str:
    """Return VM status: 'Running', 'Stopped', or 'NotCreated'."""
    result = subprocess.run(
        ["limactl", "list", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "NotCreated"

    import json
    for line in result.stdout.strip().splitlines():
        try:
            info = json.loads(line)
            if info.get("name") == VM_NAME:
                return info.get("status", "Unknown")
        except (json.JSONDecodeError, KeyError):
            continue
    return "NotCreated"


def vm_is_running() -> bool:
    """Quick check: is the VM running?"""
    return vm_status() == "Running"


def vm_create() -> None:
    """Create the orch VM from the Lima template. Idempotent."""
    if vm_status() != "NotCreated":
        return
    subprocess.run(
        ["limactl", "create", "--name", VM_NAME, str(LIMA_YAML)],
        check=True,
    )


def vm_start() -> None:
    """Start the orch VM. Creates it first if needed."""
    status = vm_status()
    if status == "Running":
        return
    if status == "NotCreated":
        vm_create()
    subprocess.run(["limactl", "start", VM_NAME], check=True)


def vm_stop() -> None:
    """Stop the orch VM."""
    if vm_status() != "Running":
        return
    subprocess.run(["limactl", "stop", VM_NAME], check=True)


def vm_delete() -> None:
    """Delete the orch VM entirely."""
    if vm_status() == "NotCreated":
        return
    subprocess.run(["limactl", "delete", "--force", VM_NAME], check=True)


def vm_exec(
    cmd: str,
    *,
    cwd: str | Path | None = None,
    timeout: int = 120,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command inside the VM via direct SSH.

    Uses the same SSH config as vm_ssh_cmd() to avoid limactl's stale
    control-socket issues.  Sources ~/.bash_env (lightweight) instead of
    a full login shell to keep execution fast.

    If *cwd* is given, the command runs in that directory.
    """
    inner_parts = ["[ -f ~/.bash_env ] && . ~/.bash_env"]
    if cwd:
        inner_parts.append(f"cd {shlex.quote(str(cwd))}")
    inner_parts.append(cmd)
    shell_cmd = " && ".join(inner_parts)

    return subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            *_MUX_OPTS,
            "-F", str(SSH_CONFIG),
            f"lima-{VM_NAME}",
            shell_cmd,
        ],
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


SSH_CONFIG = Path.home() / ".lima" / VM_NAME / "ssh.config"

# SSH multiplexing — short-lived vm_exec calls (the 15s session-cache poller
# fires several per cycle) reuse a single TCP/SSH session rather than paying
# the full handshake every time. Without this, the orch TUI generates a
# steady stream of sshd-spawns inside the VM that compete with interactive
# claude sessions for virtiofs/IO bandwidth and contribute to keystroke lag.
_CONTROL_PATH = f"/tmp/orch-ssh-{VM_NAME}-%C"
_MUX_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_CONTROL_PATH}",
    "-o", "ControlPersist=10m",
]


def vm_ssh_cmd(cwd: str | Path | None = None, extra_cmd: str = "") -> str:
    """Build an SSH command string for interactive use in iTerm2 tabs.

    Uses ssh -t with Lima's SSH config to force PTY allocation so that
    terminal resize (SIGWINCH) propagates correctly to claude.
    Skips login shell (-l) to avoid profile scripts that produce noisy
    output; sources ~/.bash_env directly for needed env vars.

    Interactive sessions intentionally do NOT use the ControlMaster mux —
    multiplexing a long-lived PTY session through a shared control channel
    can cause one tab's traffic (and SIGWINCH) to bleed into another. The
    background vm_exec callers use the mux; the user-facing iTerm tab gets
    its own dedicated connection.
    """
    # Force a dedicated connection (ControlMaster=no) so this PTY session
    # never piggybacks on the background vm_exec multiplexer — sharing would
    # let one tab's SIGWINCH or disconnect cascade into others.
    ssh_base = (
        f"ssh -t -o ControlMaster=no -o ControlPath=none "
        f"-F {shlex.quote(str(SSH_CONFIG))} lima-{VM_NAME}"
    )
    inner_parts = ["[ -f ~/.bash_env ] && . ~/.bash_env"]
    if cwd:
        inner_parts.append(f"cd {shlex.quote(str(cwd))}")
    if extra_cmd:
        inner_parts.append(extra_cmd)

    inner = " && ".join(inner_parts)
    return f"{ssh_base} {shlex.quote(inner)}"


def vm_ensure_running() -> None:
    """Ensure the VM is running, starting it if needed.

    Raises RuntimeError if limactl is not installed.
    """
    import shutil
    if not shutil.which("limactl"):
        raise RuntimeError(
            "Lima is not installed. Run: brew install lima\n"
            "Then: orch vm create"
        )
    vm_start()


APPS_ROOT = str(Path.home() / "Apps")


def sandbox_cmd(cmd: str, writable_dirs: list[str], *, scope: str | None = None) -> str:
    """Wrap a command so ~/Apps is read-only except for specific directories.

    Uses sudo unshare --mount to create a mount namespace where ~/Apps
    is bind-mounted read-only, then each writable_dir is re-mounted
    read-write on top. The command runs as the original user.

    If *scope* is given, the entire command runs inside a systemd scope
    (``systemd-run --scope --unit={scope}``).  Systemd tracks every
    process forked within the scope — even daemonized ones — so stopping
    the scope kills everything the session spawned.

    This wraps only the inner command — it can be used inside tmux,
    in an iTerm tab, or via vm_exec.
    """
    parts = [
        f"mount --bind {shlex.quote(APPS_ROOT)} {shlex.quote(APPS_ROOT)}",
        f"mount -o remount,bind,ro {shlex.quote(APPS_ROOT)}",
    ]
    for d in writable_dirs:
        qd = shlex.quote(d)
        parts.append(f"mount --bind {qd} {qd}")
        parts.append(f"mount -o remount,bind,rw {qd}")

    # Drop privileges WITHOUT -l (login) flag.  su -l calls setsid()
    # which creates a new session, detaching the child from the PTY's
    # foreground process group — that breaks SIGWINCH delivery so
    # terminal resize never reaches Claude.  Instead we source
    # ~/.bash_env manually to pick up env vars (CLAUDE_CONFIG_DIR, etc.).
    # su without -l still sets HOME, so ~ expands correctly.
    user = "$(logname 2>/dev/null || echo $SUDO_USER)"
    parts.append(
        f"su -s /bin/bash {user} -c "
        f"{shlex.quote(f'[ -f ~/.bash_env ] && . ~/.bash_env; export SSH_AUTH_SOCK=$ORCH_SSH_SOCK; export TERM=$ORCH_TERM; export COLORTERM=truecolor; {cmd}')}"
    )

    inner = " && ".join(parts)
    # Capture env vars before sudo drops them
    if scope:
        # sudo strips env, so pass vars through --preserve-env to
        # systemd-run, then use --setenv so unshare inherits them.
        return (
            f"ORCH_SSH_SOCK=$SSH_AUTH_SOCK ORCH_TERM=${{TERM:-xterm-256color}} "
            f"sudo --preserve-env=ORCH_SSH_SOCK,ORCH_TERM "
            f"systemd-run --scope --unit={shlex.quote(scope)} "
            f"--setenv=ORCH_SSH_SOCK=$ORCH_SSH_SOCK "
            f"--setenv=ORCH_TERM=$ORCH_TERM -- "
            f"unshare --mount /bin/bash -c {shlex.quote(inner)}"
        )
    return (
        f"ORCH_SSH_SOCK=$SSH_AUTH_SOCK ORCH_TERM=${{TERM:-xterm-256color}} "
        f"sudo --preserve-env=ORCH_SSH_SOCK,ORCH_TERM "
        f"unshare --mount /bin/bash -c {shlex.quote(inner)}"
    )


def vm_exec_sandboxed(
    cmd: str,
    *,
    cwd: str | Path | None = None,
    writable_dirs: list[str],
    timeout: int = 120,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command inside the VM with filesystem sandboxing.

    ~/Apps is read-only except for the directories in writable_dirs.
    """
    if cwd:
        full_cmd = f"cd {shlex.quote(str(cwd))} && {cmd}"
    else:
        full_cmd = cmd

    sandboxed = sandbox_cmd(full_cmd, writable_dirs)

    return subprocess.run(
        [
            "ssh", "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            *_MUX_OPTS,
            "-F", str(SSH_CONFIG),
            f"lima-{VM_NAME}",
            f"[ -f ~/.bash_env ] && . ~/.bash_env && {sandboxed}",
        ],
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def vm_health_check() -> bool:
    """Quick health check — can we execute a command in the VM?"""
    try:
        result = vm_exec("true", timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
