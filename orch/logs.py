"""
orch log management.

With the Lima VM architecture, logs come from:
  - tmux session capture-pane for live agent output
  - Claude's own log files accessible via virtiofs at the same paths
  - Saved logs in ~/.orch/logs/<project>/

CLI usage (via __main__):
  orch logs                        # show recent logs for cwd project
  orch logs project-name           # show recent logs for named project
  orch logs project-name -g error  # filter output
  orch logs project-name --past    # print saved log files
  orch logs project-name --list    # show active tmux sessions
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .models import Project

LOG_MAX_LINES = 1000
ORCH_LOGS_DIR = Path.home() / ".orch" / "logs"


# ── Session discovery ────────────────────────────────────────────────────────

def find_sessions(project: Project) -> list[dict]:
    """Find active tmux sessions for this project.

    Returns list of dicts with 'ID', 'Names', 'Status' keys.
    """
    from .agent import session_exists, session_name
    from .vm import vm_is_running

    if not vm_is_running():
        return []

    name = session_name(project)
    if session_exists(project):
        return [{"ID": name, "Names": name, "Status": "running"}]
    return []


# Legacy aliases for __main__.py compatibility
find_containers = find_sessions


def session_display_name(session: dict) -> str:
    """Short human-friendly name for a session."""
    return session.get("Names", session.get("ID", "unknown"))


container_display_name = session_display_name


# ── Log directory helpers ────────────────────────────────────────────────────

def log_dir(project: Project) -> Path:
    d = ORCH_LOGS_DIR / project.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file(project: Project, session_name: str) -> Path:
    safe = session_name.replace("/", "_").replace(" ", "_")
    return log_dir(project) / f"{safe}.log"


def list_log_files(project: Project) -> list[Path]:
    d = ORCH_LOGS_DIR / project.name
    if not d.exists():
        return []
    return sorted(d.glob("*.log"))


# ── Rotation ─────────────────────────────────────────────────────────────────

def _rotate_if_needed(path: Path) -> None:
    """Keep only the last LOG_MAX_LINES lines."""
    if not path.exists():
        return
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    if len(lines) <= LOG_MAX_LINES:
        return
    backup = path.with_suffix(".log.1")
    shutil.copy2(path, backup)
    path.write_text("".join(lines[-LOG_MAX_LINES:]))


# ── Tail / streaming ────────────────────────────────────────────────────────

def tail_project(
    project: Project,
    grep: str | None = None,
    since: str = "1h",
    follow: bool = True,
) -> None:
    """Capture and display the tmux session output for a project."""
    from .agent import session_exists, session_name
    from .vm import vm_is_running, vm_exec

    if not vm_is_running():
        print("VM is not running. Start with: orch vm start")
        return

    name = session_name(project)
    if not session_exists(project):
        print(f"No active session for '{project.name}'.")
        print(f"Start one with: orch (TUI) → select project → press c")
        return

    # Capture current tmux pane contents
    result = vm_exec(
        f"tmux capture-pane -t {name} -p -S -500",
        timeout=10,
    )

    if result.returncode != 0:
        print(f"Failed to capture tmux session: {result.stderr}")
        return

    lines = result.stdout.splitlines()

    # Save to log file
    lfile = log_file(project, name)
    _rotate_if_needed(lfile)
    with lfile.open("a") as fh:
        for line in lines:
            fh.write(line + "\n")

    # Display
    print(f"  session: {name}")
    print(f"  logs → {log_dir(project)}")
    print()

    for line in lines:
        if grep and grep.lower() not in line.lower():
            continue
        print(line)


# ── Past log reader ──────────────────────────────────────────────────────────

def print_past_logs(project: Project, grep: str | None = None) -> None:
    """Print saved log files for a project."""
    files = list_log_files(project)
    if not files:
        print(f"No saved logs for '{project.name}' in {log_dir(project)}")
        return

    for f in files:
        print(f"\n{'─'*60}")
        print(f"  {f.name}  ({f.stat().st_size // 1024} KB)")
        print(f"{'─'*60}")
        lines = f.read_text(errors="replace").splitlines()
        if grep:
            lines = [l for l in lines if grep.lower() in l.lower()]
        for line in lines:
            print(line)
