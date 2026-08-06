"""
orch VM management — Lima lifecycle for the single shared VM.

Replaces container.py. All projects run inside one Lima VM with virtiofs
mounts at the same host paths. No per-project containers, no Docker image
management, no credential injection.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

VM_NAME = "orch"
LIMA_YAML = Path(__file__).parent.parent / "lima" / "orch.yaml"


# ── Remote command size limit ────────────────────────────────────────────────
#
# ssh hands the *entire* remote command to the ControlMaster in a single mux
# control message. Past a few KB the client's sendmsg() returns EMSGSIZE and
# the session dies at setup — before the remote command ever runs — with
#
#     mm_send_fd: sendmsg(1): Message too long
#     mux_client_request_session: send fds failed
#
# and rc=255. Observed: a ~5.5 KB command failed every time (four attempts,
# ~2s each), the same command at ~1 KB succeeded. This is *permanent for a
# given payload*: an identical retry can never succeed.
#
# So never interpolate bulk text into a command string. Pass it over stdin
# (`input=`) or write it to a file in the VM and reference the path. The cap
# below is the backstop for anything that slips through; it is deliberately
# below the fuzzy real boundary so the failure is a clear message rather than
# rc=255. Override with ORCH_SSH_CMD_MAX_BYTES if you must.

_DEFAULT_SSH_CMD_MAX_BYTES = 4096


def ssh_cmd_max_bytes() -> int:
    """Byte cap for a remote command string (env-overridable)."""
    raw = os.environ.get("ORCH_SSH_CMD_MAX_BYTES", "")
    if raw.strip():
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_SSH_CMD_MAX_BYTES


class CommandTooLargeError(RuntimeError):
    """The assembled remote command exceeds what ssh can deliver.

    Permanent for a given payload — retrying the identical command cannot
    succeed, so callers should fail fast rather than schedule a retry.
    """


def _check_cmd_size(shell_cmd: str) -> None:
    """Refuse an oversized remote command with an actionable message."""
    size = len(shell_cmd.encode("utf-8", errors="replace"))
    cap = ssh_cmd_max_bytes()
    if size <= cap:
        return
    raise CommandTooLargeError(
        f"remote command is {size} bytes; the ssh control channel caps it near "
        f"{cap} bytes and fails at session setup (rc=255, "
        f"'mm_send_fd: sendmsg: Message too long') above that. Pass the bulk "
        f"text over stdin instead, or write it to a file the VM can read and "
        f"reference the path from the command."
    )


# stderr fingerprints of the failure above. rc=255 plus any of these means the
# command never reached the VM.
_UNDELIVERABLE_MARKERS = (
    "mm_send_fd",
    "mux_client_request_session",
    "Message too long",
)


def is_ssh_undeliverable(returncode: int | None, stderr: str | None) -> bool:
    """True when ssh failed to hand the command to the mux master.

    Distinguishes "the payload is too big to deliver" (permanent) from an
    ordinary non-zero exit of the remote command (possibly transient).
    """
    if returncode != 255 or not stderr:
        return False
    return any(m in stderr for m in _UNDELIVERABLE_MARKERS)


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
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command inside the VM via direct SSH.

    Uses the same SSH config as vm_ssh_cmd() to avoid limactl's stale
    control-socket issues.  Sources ~/.bash_env (lightweight) instead of
    a full login shell to keep execution fast.

    If *cwd* is given, the command runs in that directory.
    If *input* is given it is written to the remote command's stdin — use
    this for anything bulky rather than interpolating it into *cmd* (see
    CommandTooLargeError).

    Raises CommandTooLargeError if the assembled command is too large for
    the ssh control channel.
    """
    inner_parts = ["[ -f ~/.bash_env ] && . ~/.bash_env"]
    if cwd:
        inner_parts.append(f"cd {shlex.quote(str(cwd))}")
    inner_parts.append(cmd)
    shell_cmd = " && ".join(inner_parts)
    _check_cmd_size(shell_cmd)

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
        input=input,
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
    apps = Path(APPS_ROOT)
    for d in writable_dirs:
        # Only paths strictly inside ~/Apps get a rw bind punched through the
        # ro layer. Anything else is at best a no-op (outside ~/Apps nothing
        # was made read-only) and at worst — ~/Apps itself or an ancestor
        # like $HOME — a non-recursive self-bind that shadows the ~/Apps
        # mount, so every path under it resolves to the empty stub directory
        # behind the mountpoint and the sandboxed command sees no files.
        p = Path(os.path.normpath(d))
        if apps not in p.parents:
            continue
        qd = shlex.quote(str(p))
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
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command inside the VM with filesystem sandboxing.

    ~/Apps is read-only except for the directories in writable_dirs.

    *input* is written to the sandboxed command's stdin (it survives the
    sudo/unshare/su chain untouched), which is how bulk text — prompts,
    PR bodies — must be delivered. Interpolating it into *cmd* instead
    blows the ssh control-channel limit; see CommandTooLargeError.

    Raises CommandTooLargeError if the assembled command is too large for
    the ssh control channel.
    """
    if cwd:
        full_cmd = f"cd {shlex.quote(str(cwd))} && {cmd}"
    else:
        full_cmd = cmd

    sandboxed = sandbox_cmd(full_cmd, writable_dirs)
    shell_cmd = f"[ -f ~/.bash_env ] && . ~/.bash_env && {sandboxed}"
    _check_cmd_size(shell_cmd)

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
        input=input,
    )


def vm_health_check() -> bool:
    """Quick health check — can we execute a command in the VM?"""
    try:
        result = vm_exec("true", timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
