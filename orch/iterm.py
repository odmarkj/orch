"""
iTerm2 integration and macOS notifications.

Design contract:
  - Orch ONLY opens iTerm2 tabs, never closes them.
  - Manual close by the user is always clean — the stale .claude/iterm_handle
    file is silently ignored on next open.
  - When Claude resumes (deletes waiting_for_input), orch updates the dot.
    That's it. No tab management.
  - All behaviour is driven by ~/.orch/config.toml so nothing requires a
    code change to customise.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .models import Project


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


# ── Notifications ─────────────────────────────────────────────────────────────

def _has_terminal_notifier() -> bool:
    return subprocess.run(
        ["which", "terminal-notifier"],
        capture_output=True,
    ).returncode == 0


def notify_input_needed(project: Project, question: str) -> None:
    """
    Fire a macOS toast notification when Claude needs input.
    Clicking it activates iTerm2 (which already has the tab open).
    Requires: brew install terminal-notifier
    """
    if not _has_terminal_notifier():
        return
    cfg = _load_config()
    sound = cfg["notifications"].get("sound_input_needed", "Glass")

    args = [
        "terminal-notifier",
        "-title",    "orch — input needed",
        "-subtitle", project.name,
        "-message",  question,
        "-activate", "com.googlecode.iterm2",
    ]
    if sound:
        args += ["-sound", sound]

    subprocess.Popen(args)


def notify_resumed(project: Project) -> None:
    """Light notification when Claude resumes."""
    cfg = _load_config()
    if not cfg["notifications"].get("notify_on_resume", True):
        return
    if not _has_terminal_notifier():
        return

    sound = cfg["notifications"].get("sound_resumed", "Pop")
    args = [
        "terminal-notifier",
        "-title",    "orch",
        "-subtitle", project.name,
        "-message",  "Claude resumed \u21a9",
    ]
    if sound:
        args += ["-sound", sound]

    subprocess.Popen(args)


# ── iTerm2 tab management ──────────────────────────────────────────────────────

def open_input_tab(project: Project) -> None:
    """
    Open a new iTerm2 tab for the project using the orch profile.
    - If dedicated_window is true, all orch tabs live in one named window.
    - Re-focuses an existing tab if one is already open for this project.
    - Never closes tabs — user owns that.
    """
    handle_file = project.claude_dir / "iterm_handle"

    # Re-focus existing tab if the handle is still alive
    if handle_file.exists():
        existing_tty = handle_file.read_text().strip()
        if existing_tty and _bring_tab_to_front(existing_tty):
            return
        # Handle is stale (tab was closed manually) — clean it up
        handle_file.unlink(missing_ok=True)

    cfg = _load_config()
    profile       = cfg["iterm"].get("profile", "orch")
    dedicated     = cfg["iterm"].get("dedicated_window", True)
    window_title  = cfg["iterm"].get("window_title", "orch sessions")
    claude_cmd    = _build_claude_cmd(project)
    project_path  = str(project.path)
    project_name  = project.name

    if dedicated:
        script = f"""
        tell application "iTerm2"
            activate
            set orchWindow to missing value
            repeat with w in windows
                if name of w contains "{window_title}" then
                    set orchWindow to w
                    exit repeat
                end if
            end repeat
            if orchWindow is missing value then
                set orchWindow to (create window with profile "{profile}")
                tell orchWindow
                    tell current session
                        set name to "{window_title}"
                    end tell
                end tell
            end if
            tell orchWindow
                create tab with profile "{profile}"
                tell current session
                    set name to "{project_name}"
                    write text "cd {project_path} && {claude_cmd}"
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
            if (count of windows) is 0 then
                create window with profile "{profile}"
            end if
            tell current window
                create tab with profile "{profile}"
                tell current session
                    set name to "{project_name}"
                    write text "cd {project_path} && {claude_cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    tty = result.stdout.strip()
    if tty:
        handle_file.write_text(tty)


def _bring_tab_to_front(tty: str) -> bool:
    """
    Focus the iTerm2 tab whose session matches tty.
    Returns True if found, False if the tab no longer exists.
    """
    script = f"""
    set found to false
    tell application "iTerm2"
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    if tty of aSession is "{tty}" then
                        activate
                        select aWindow
                        tell aWindow to select aTab
                        set found to true
                    end if
                end repeat
            end repeat
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
    Called on orch startup. If the handle file exists but the tab is gone
    (iTerm2 was quit, tab was closed), silently remove the stale handle.
    """
    handle_file = project.claude_dir / "iterm_handle"
    if not handle_file.exists():
        return
    tty = handle_file.read_text().strip()
    if tty and not _bring_tab_to_front(tty):
        handle_file.unlink(missing_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_claude_cmd(project: Project) -> str:
    """
    Build the claude CLI invocation. Resumes the active session if
    .claude/sessions.json exists and has an 'active' key.
    """
    sessions_file = project.claude_dir / "sessions.json"
    if sessions_file.exists():
        try:
            data = json.loads(sessions_file.read_text())
            session_id = data.get("active")
            if session_id:
                return f"claude --resume {session_id}"
        except (json.JSONDecodeError, KeyError):
            pass
    return "claude"
