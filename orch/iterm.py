"""
iTerm2 integration and macOS notifications.

Design contract:
  - Orch ONLY opens iTerm2 tabs, never closes them.
  - Manual close by the user is always clean — the stale .orch/iterm_handle
    file is silently ignored on next open.
  - When Claude resumes (deletes waiting_for_input), orch updates the dot.
    That's it. No tab management.
  - All behaviour is driven by ~/.orch/config.toml so nothing requires a
    code change to customise.
  - Sessions run inside the Lima VM via limactl shell + tmux.
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from pathlib import Path

from .models import Project


# ── Orch system prompt file (injected via --append-system-prompt-file) ───────

_ORCH_PROMPT_FILE = Path.home() / ".orch" / "system-prompt.md"


def _orch_prompt_arg() -> str:
    """Return the --append-system-prompt-file flag pointing to the orch prompt.

    The file lives in ~/.orch/ which is mounted read-write inside the VM at
    the same host path, so Claude can read it from either side.
    """
    return f"--append-system-prompt-file {shlex.quote(str(_ORCH_PROMPT_FILE))}"


# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "iterm": {
        "profile":          "orch",
        "dedicated_window": True,
        "window_title":     "orch sessions",
    },
    "notifications": {
        "sound_input_needed": "Glass",
        "sound_resumed":      "Pop",
        "notify_on_resume":   True,
    },
}


def _load_config() -> dict:
    config_file = Path.home() / ".orch" / "config.toml"
    cfg = {k: dict(v) for k, v in _DEFAULTS.items()}

    if not config_file.exists():
        return cfg

    # Minimal TOML parser — avoids adding a dependency for simple key=value sections
    section = None
    for raw in config_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section not in cfg:
                cfg[section] = {}
            continue
        if "=" in line and section:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            cfg[section][key] = val

    return cfg


# ── iTerm2 badge ──────────────────────────────────────────────────────────────


def _iterm_badge_cmd(text: str) -> str:
    """Return a shell command that sets the iTerm2 badge via escape sequence.

    The badge is a faint watermark in the terminal that persists regardless
    of what the running program does to the window title.  Uses iTerm2's
    proprietary escape sequence: ``\\033]1337;SetBadgeFormat=<b64>\\007``.
    """
    encoded = base64.b64encode(text.encode()).decode()
    return f"printf '\\033]1337;SetBadgeFormat={encoded}\\007'"


# ── Notifications ─────────────────────────────────────────────────────────────


def _osascript_notify(*, title: str, subtitle: str, message: str,
                      sound: str = "") -> None:
    """Send a macOS notification via osascript (no dependencies required)."""
    parts = [
        f'display notification {_applescript_quote(message)}',
        f'with title {_applescript_quote(title)}',
        f'subtitle {_applescript_quote(subtitle)}',
    ]
    if sound:
        parts.append(f'sound name {_applescript_quote(sound)}')
    script = " ".join(parts)
    subprocess.Popen(["osascript", "-e", script])


def _applescript_quote(s: str) -> str:
    """Escape a string for use in AppleScript."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify_input_needed(project: Project, question: str) -> None:
    """Fire a macOS notification when Claude needs input."""
    cfg = _load_config()
    sound = cfg["notifications"].get("sound_input_needed", "Glass")
    _osascript_notify(
        title="orch — input needed",
        subtitle=project.name,
        message=question,
        sound=sound,
    )


def notify_resumed(project: Project) -> None:
    """Light notification when Claude resumes."""
    cfg = _load_config()
    if not cfg["notifications"].get("notify_on_resume", True):
        return
    sound = cfg["notifications"].get("sound_resumed", "Pop")
    _osascript_notify(
        title="orch",
        subtitle=project.name,
        message="Claude resumed ↩",
        sound=sound,
    )


# ── iTerm2 tab management ──────────────────────────────────────────────────────

def open_input_tab(project: Project) -> None:
    """
    Open a new iTerm2 tab for the project using the orch profile.
    - If dedicated_window is true, all orch tabs live in one named window.
    - Re-focuses an existing tab if one is already open for this project.
    - Never closes tabs — user owns that.
    """
    handle_file = project.orch_dir / "iterm_handle"
    project_name  = project.name

    # Focus existing tab if still alive — don't open duplicates
    if handle_file.exists():
        existing_tty = handle_file.read_text().strip()
        alive = _tab_exists(existing_tty) if existing_tty else False
        if alive is True:
            _bring_tab_to_front(existing_tty)
            return
        if alive is None:
            return  # Check failed (e.g. problematic iTerm session) — keep handle, don't open duplicate
        # Handle is stale (tab was closed manually) — clean it up
        handle_file.unlink(missing_ok=True)

    cfg = _load_config()
    profile       = cfg["iterm"].get("profile", "orch")
    dedicated     = cfg["iterm"].get("dedicated_window", True)
    window_title  = cfg["iterm"].get("window_title", "orch sessions")
    badge         = _iterm_badge_cmd(project_name)
    vm_cmd        = _build_vm_claude_cmd(project)
    shell_cmd     = _applescript_quote(
        f"{badge} && {vm_cmd}"
    )

    if dedicated:
        script = f"""
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
                    set name to "{project_name}"
                    set badge to "{project_name}"
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
                    set name to "{project_name}"
                    set badge to "{project_name}"
                    write text {shell_cmd}
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """

    tty = _run_iterm_script(script)
    if tty:
        handle_file.write_text(tty)


def _tab_exists(tty: str) -> bool | None:
    """
    Check whether an iTerm2 session with the given tty still exists.
    Does NOT activate or focus anything — purely a liveness check.

    Returns True/False on success, or None if the check itself failed
    (e.g. a stale/SSH session caused an AppleScript error).  Callers
    should treat None as "unknown — don't delete the handle".
    """
    script = f"""
    set found to false
    tell application "iTerm2"
        repeat with aWindow in windows
            try
                repeat with aTab in tabs of aWindow
                    repeat with aSession in sessions of aTab
                        try
                            if tty of aSession is "{tty}" then
                                set found to true
                                exit repeat
                            end if
                        on error
                            -- Session property access failed (SSH, stale, etc.) — skip
                        end try
                    end repeat
                    if found then exit repeat
                end repeat
            on error
                -- Window/tab reference went stale; skip this window
            end try
            if found then exit repeat
        end repeat
    end tell
    return found
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None  # Script failed — don't assume tab is gone
    return result.stdout.strip().lower() == "true"


def _bring_tab_to_front(tty: str, expected_name: str | None = None) -> bool:
    """
    Focus the iTerm2 tab whose session matches tty.
    Returns True if found, False if the tab no longer exists.

    Activates iTerm2 and selects the window/tab so the user sees it.
    """
    script = f"""
    set found to false
    tell application "iTerm2"
        repeat with aWindow in windows
            try
                repeat with aTab in tabs of aWindow
                    repeat with aSession in sessions of aTab
                        try
                            if tty of aSession is "{tty}" then
                                activate
                                select aWindow
                                tell aWindow to select aTab
                                set found to true
                                exit repeat
                            end if
                        on error
                            -- Session property access failed — skip
                        end try
                    end repeat
                    if found then exit repeat
                end repeat
            on error
                -- Window/tab reference went stale — skip
            end try
            if found then exit repeat
        end repeat
    end tell
    return found
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip().lower() == "true"


def clear_stale_handle(project: Project) -> None:
    """
    Called on orch startup. If any handle file exists but the tab is gone
    (iTerm2 was quit, tab was closed), silently remove the stale handle.
    """
    handle_names = [
        "iterm_handle",
        "iterm_log_handle",
    ]
    for name in handle_names:
        handle_file = project.orch_dir / name
        if not handle_file.exists():
            continue
        tty = handle_file.read_text().strip()
        alive = _tab_exists(tty) if tty else False
        if alive is False:  # Confirmed gone — safe to remove (skip on None/error)
            handle_file.unlink(missing_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run_iterm_script(script: str) -> str:
    """
    Run an AppleScript that interacts with iTerm2. Returns stdout.
    Raises RuntimeError with stderr if it fails.
    """
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"iTerm2 AppleScript failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _build_vm_claude_cmd(project: Project) -> str:
    """Build the command to run Claude inside the Lima VM.

    Uses ssh -t for proper PTY allocation so terminal resize works.
    Claude runs directly — no sandbox, no tmux — to preserve the PTY
    foreground process group for SIGWINCH delivery.
    """
    import shlex
    from .vm import vm_ssh_cmd

    project_dir = str(project.path)
    claude_args = "--dangerously-skip-permissions"

    # Resume active session if available
    sessions_file = project.orch_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                claude_args += f" --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass

    pid_file = f"/tmp/orch-{project.name}.pid"
    inner = (
        f"export TERM=xterm-256color; "
        f"cd {shlex.quote(project_dir)} && "
        f"trap 'rm -f {shlex.quote(pid_file)}' EXIT HUP; "
        f"echo $$ > {shlex.quote(pid_file)}; "
        f"clear; claude {claude_args} {_orch_prompt_arg()}"
    )
    return vm_ssh_cmd(extra_cmd=inner)


def open_vm_session(project: Project, with_shell: bool = False) -> None:
    """Open a NEW iTerm2 window with Claude running in the VM.

    Every invocation creates a fresh window — pressing the key multiple
    times gives independent sessions (same as old container behavior).
    Claude runs inside a sandboxed mount namespace (project dir writable,
    rest of ~/Apps read-only). The shell tab is unsandboxed.
    """
    import shlex
    from .vm import sandbox_cmd, vm_ssh_cmd
    from .agent import session_exists, fire_first_session_hook

    # Fire on_first_session hook if no existing session
    if not session_exists(project):
        fire_first_session_hook(project)

    # Update stack detection if stale (cheap, local-only)
    from .agent import _maybe_update_stack_detection
    _maybe_update_stack_detection(project)

    # Clear stale status from previous sessions
    try:
        project.status_file.write_text("Starting session")
    except OSError:
        pass

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    project_dir = str(project.path)

    tab_name = f"{project.name}"
    badge = _iterm_badge_cmd(project.name)
    claude_args = "--dangerously-skip-permissions"

    # Resume session if available
    sessions_file = project.orch_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                claude_args += f" --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass

    # Run Claude directly via SSH — no tmux, no sandbox, no systemd scope.
    # Anything that calls setsid() (systemd-run --scope, su -l, etc.) breaks
    # SIGWINCH delivery by detaching from the SSH PTY's foreground group.
    # Session detection uses a PID file instead.
    pid_file = f"/tmp/orch-{project.name}.pid"
    inner_cmd = (
        f"export TERM=xterm-256color COLORTERM=truecolor; "
        f"cd {shlex.quote(project_dir)} && "
        f"trap 'rm -f {shlex.quote(pid_file)}' EXIT HUP; "
        f"echo $$ > {shlex.quote(pid_file)}; "
        f"clear; claude {claude_args} {_orch_prompt_arg()}"
    )
    vm_cmd = vm_ssh_cmd(extra_cmd=inner_cmd)
    claude_cmd = _applescript_quote(f"{badge} && {vm_cmd}")

    if with_shell:
        shell_tab_name = f"{project.name} (shell)"
        shell_inner = f"cd {shlex.quote(project_dir)} && exec bash"
        shell_vm_cmd = vm_ssh_cmd(extra_cmd=shell_inner)
        shell_cmd = _applescript_quote(f"{badge} && {shell_vm_cmd}")

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
                    write text {shell_cmd}
                end tell
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


def open_vm_shell(project: Project) -> None:
    """Open a shell inside the VM at the project directory."""
    import shlex
    from .vm import vm_ssh_cmd

    cfg = _load_config()
    profile = cfg["iterm"].get("profile", "orch")
    project_dir = shlex.quote(str(project.path))
    badge = _iterm_badge_cmd(project.name)

    inner = f"cd {project_dir} && exec bash"
    vm_cmd = vm_ssh_cmd(extra_cmd=inner)
    shell_cmd = _applescript_quote(f"{badge} && {vm_cmd}")

    script = f"""
    tell application "iTerm2"
        try
            set newWindow to (create window with profile "{profile}")
        on error
            set newWindow to (create window with default profile)
        end try
        tell newWindow
            tell current session
                set name to "{project.name} (shell)"
                set badge to "{project.name}"
                write text {shell_cmd}
            end tell
        end tell
    end tell
    """
    _run_iterm_script(script)
