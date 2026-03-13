"""
orch log management.

Design:
  - Discovers docker containers by devcontainer.local_folder label — no config.
  - Streams docker logs to ~/.orch/logs/<project>/<container-name>.log
  - Rotates at LOG_MAX_LINES (1000). Old lines rotate to .log.1, then gone.
  - Never touches stdout of running containers, purely docker logs passthrough.
  - All public functions work from a project path or name only.

CLI usage (via __main__):
  orch logs                        # tail all containers for cwd project
  orch logs cacao-dna              # tail all containers for named project
  orch logs cacao-dna --grep error # filter output
  orch logs cacao-dna --list       # show discovered containers, no tail
  orch logs cacao-dna --past       # print saved log files, no tail
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Iterator

from .models import Project

LOG_MAX_LINES = 1000
ORCH_LOGS_DIR = Path.home() / ".orch" / "logs"


# ── Container discovery ───────────────────────────────────────────────────────

def find_containers(project: Project) -> list[dict]:
    """
    Find all running (or recently stopped) containers associated with this
    project path via the devcontainer.local_folder label.

    Returns list of dicts with keys: id, name, status, labels.
    """
    project_path = str(project.path)

    result = subprocess.run(
        [
            "docker", "ps", "-a",
            "--format", "{{json .}}",
            "--filter", f"label=devcontainer.local_folder={project_path}",
        ],
        capture_output=True,
        text=True,
    )

    containers = []
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            if line.strip():
                try:
                    containers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Fallback: match by container name containing the project folder name.
    # Devcontainers typically name containers like "project-name_devcontainer-app-1"
    if not containers:
        result2 = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
        )
        folder_name = project.name.lower().replace("-", "").replace("_", "")
        if result2.returncode == 0:
            for line in result2.stdout.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                    cname = c.get("Names", "").lower().replace("-", "").replace("_", "")
                    if folder_name in cname:
                        containers.append(c)
                except json.JSONDecodeError:
                    pass

    return containers


def container_display_name(container: dict) -> str:
    """Short human-friendly name for a container."""
    name = container.get("Names", container.get("ID", "unknown"))
    return name.lstrip("/")


# ── Log directory helpers ─────────────────────────────────────────────────────

def log_dir(project: Project) -> Path:
    d = ORCH_LOGS_DIR / project.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file(project: Project, container_name: str) -> Path:
    safe = container_name.replace("/", "_").replace(" ", "_")
    return log_dir(project) / f"{safe}.log"


def list_log_files(project: Project) -> list[Path]:
    d = ORCH_LOGS_DIR / project.name
    if not d.exists():
        return []
    return sorted(d.glob("*.log"))


# ── Rotation ──────────────────────────────────────────────────────────────────

def _rotate_if_needed(path: Path) -> None:
    """
    If path has more than LOG_MAX_LINES lines, keep only the last LOG_MAX_LINES.
    Writes a .log.1 backup of the full file first.
    """
    if not path.exists():
        return
    lines = path.read_text(errors="replace").splitlines(keepends=True)
    if len(lines) <= LOG_MAX_LINES:
        return
    backup = path.with_suffix(".log.1")
    shutil.copy2(path, backup)
    path.write_text("".join(lines[-LOG_MAX_LINES:]))


# ── Streaming ─────────────────────────────────────────────────────────────────

def stream_container_logs(
    container_id: str,
    since: str = "24h",
    follow: bool = True,
) -> Iterator[str]:
    """
    Yield lines from docker logs for a single container.
    since: docker --since value e.g. "24h", "1h", "2023-01-01T00:00:00"
    """
    cmd = ["docker", "logs", "--timestamps"]
    if follow:
        cmd.append("--follow")
    if since:
        cmd += ["--since", since]
    cmd.append(container_id)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # docker logs sends to stderr
        text=True,
        bufsize=1,
    )

    try:
        for line in proc.stdout:
            yield line.rstrip("\n")
    except (KeyboardInterrupt, GeneratorExit):
        proc.terminate()
    finally:
        proc.wait()


def _stream_to_file_and_stdout(
    project: Project,
    container: dict,
    grep: str | None,
    since: str,
    prefix: bool,
    follow: bool,
) -> None:
    """
    Stream one container's logs to both stdout (with optional grep) and its
    log file. Runs rotation check on the log file before streaming.
    """
    cname = container_display_name(container)
    cid   = container.get("ID", cname)
    lfile = log_file(project, cname)

    _rotate_if_needed(lfile)

    with lfile.open("a", buffering=1) as fh:
        for line in stream_container_logs(cid, since=since, follow=follow):
            fh.write(line + "\n")

            if grep and grep.lower() not in line.lower():
                continue

            out = f"[{cname}] {line}" if prefix else line
            print(out, flush=True)


def tail_project(
    project: Project,
    grep: str | None = None,
    since: str = "1h",
    follow: bool = True,
) -> None:
    """
    Tail all containers for a project simultaneously.
    Streams to ~/.orch/logs/<project>/<container>.log and stdout.
    Ctrl-C exits cleanly.
    """
    containers = find_containers(project)
    if not containers:
        print(f"No containers found for project '{project.name}'.")
        print("Checked devcontainer.local_folder label and name matching.")
        _suggest_manual(project)
        return

    multi = len(containers) > 1
    threads = []
    for c in containers:
        t = threading.Thread(
            target=_stream_to_file_and_stdout,
            args=(project, c, grep, since, multi, follow),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Print header
    names = ", ".join(container_display_name(c) for c in containers)
    print(f"  tailing {len(containers)} container(s): {names}")
    if grep:
        print(f"  grep: '{grep}'")
    print(f"  logs → {log_dir(project)}")
    print()

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n  stopped.")


def _suggest_manual(project: Project) -> None:
    print()
    print("  To manually register a container, run inside the project:")
    print(f"    docker ps  # find your container name or ID")
    print(f"    # Then tail it with:")
    print(f"    docker logs --follow --since 1h <name-or-id>")
    print()
    print("  Or label it for auto-discovery:")
    print(f"    docker run --label devcontainer.local_folder={project.path} ...")


# ── Past log reader ───────────────────────────────────────────────────────────

def print_past_logs(project: Project, grep: str | None = None) -> None:
    """Print saved log files for a project (no tail, no docker)."""
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
