"""
orch CLI

  orch                              Launch TUI
  orch plan                         Generate and print day plan
  orch plan --json                  Output plan as JSON
  orch logs [project]               Tail docker logs
  orch logs [project] -g error      Grep filter
  orch logs [project] --list        Show discovered containers
  orch logs [project] --past        Read saved log files
  orch bridge                       Start mobile web bridge (stays running)
  orch stage <project> <stage>      Advance project to a new stage
  orch container <project> up       Start devcontainer for project
  orch container <project> down     Stop container
  orch container <project> status   Check container status
  orch container <project> exec     Exec into container running claude
  orch ignore <project>              Hide project from orch
  orch ignore <project> --undo      Un-hide project
  orch setup                        First-time setup
"""

from __future__ import annotations

import sys
from pathlib import Path


def _find_project(name_or_cwd: str | None):
    from .discovery import discover_projects, SITES_ROOT
    from .models import Project

    projects = discover_projects()

    if name_or_cwd is None:
        cwd = Path.cwd()
        if (cwd / ".claude").is_dir():
            return Project(path=cwd)
        print(f"Not inside a project directory. Available: {', '.join(p.name for p in projects)}")
        sys.exit(1)

    for p in projects:
        if p.name == name_or_cwd:
            return p

    matches = [p for p in projects if name_or_cwd.lower() in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous: {', '.join(p.name for p in matches)}")
        sys.exit(1)

    print(f"Project '{name_or_cwd}' not found in {SITES_ROOT}")
    sys.exit(1)


def cmd_plan(argv: list[str]) -> None:
    from .discovery import discover_projects, SITES_ROOT
    from .planner import generate, format_plan
    from .lifecycle import ensure_initialized
    import json as _json

    as_json = "--json" in argv

    print("  Gathering project context…" if not as_json else "", flush=True)

    projects = discover_projects()
    # Ensure every project has a lifecycle file so the planner has data
    for p in projects:
        ensure_initialized(p)

    if not projects:
        print(f"No projects found in {SITES_ROOT}")
        sys.exit(1)

    if not as_json:
        print(f"  {len(projects)} projects found. Calling Claude…\n", flush=True)

    try:
        plan = generate(projects)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        print(_json.dumps({
            "date":                plan.date,
            "one_liner":           plan.one_liner,
            "focus_projects":      plan.focus_projects,
            "plan":                plan.plan,
            "stalled":             plan.stalled,
            "launch_debt_warning": plan.launch_debt_warning,
            "skip_today":          plan.skip_today,
        }, indent=2))
    else:
        print(format_plan(plan))


def cmd_logs(argv: list[str]) -> None:
    from .logs import (
        find_containers, container_display_name,
        list_log_files, print_past_logs, tail_project, log_dir,
    )

    project_name = None
    grep         = None
    since        = "1h"
    do_list      = False
    do_past      = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-g", "--grep") and i + 1 < len(argv):
            i += 1; grep = argv[i]
        elif arg in ("-s", "--since") and i + 1 < len(argv):
            i += 1; since = argv[i]
        elif arg == "--list":
            do_list = True
        elif arg == "--past":
            do_past = True
        elif not arg.startswith("-"):
            project_name = arg
        i += 1

    project = _find_project(project_name)

    if do_list:
        containers = find_containers(project)
        if not containers:
            print(f"No containers found for '{project.name}'")
        else:
            print(f"\n  Containers for {project.name}:\n")
            for c in containers:
                print(f"  {container_display_name(c):<50} {c.get('ID','')[:12]}  {c.get('Status','')}")
            print(f"\n  Logs → {log_dir(project)}")
        return

    if do_past:
        print_past_logs(project, grep=grep)
        return

    tail_project(project, grep=grep, since=since)


def cmd_bridge(argv: list[str]) -> None:
    import time, signal
    from .bridge import start_bridge

    try:
        port = start_bridge()
    except OSError as e:
        print(f"Error starting bridge: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  orch bridge running on http://localhost:{port}")
    print(f"  Open in browser or tunnel with:")
    print(f"    cloudflared tunnel --url http://localhost:{port}")
    print(f"  Press Ctrl-C to stop.\n")

    def _stop(sig, frame):
        from .bridge import stop_bridge
        print("\n  Bridge stopped.")
        stop_bridge()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)

    while True:
        time.sleep(1)


def cmd_stage(argv: list[str]) -> None:
    from .lifecycle import STAGES, advance_stage, STAGE_EMOJI

    if len(argv) < 2:
        print("Usage: orch stage <project> <stage> [note]")
        print(f"Stages: {' | '.join(STAGES)}")
        sys.exit(1)

    project_name = argv[0]
    stage        = argv[1].lower()
    note         = " ".join(argv[2:]) if len(argv) > 2 else ""

    project = _find_project(project_name)

    if stage not in STAGES:
        print(f"Unknown stage '{stage}'. Valid: {', '.join(STAGES)}")
        sys.exit(1)

    lc = advance_stage(project, stage, note)
    emoji = STAGE_EMOJI.get(stage, "")
    print(f"  {emoji} {project.name} → {stage}")
    if note:
        print(f"  Note: {note}")
    print(f"  Ledger entries: {len(lc.ledger)}")


def cmd_container(argv: list[str]) -> None:
    from .container import ensure_running, stop, is_running, exec_cmd

    if len(argv) < 1:
        print("Usage: orch container <project> [up|down|status|exec]")
        sys.exit(1)

    project_name = argv[0]
    action = argv[1] if len(argv) > 1 else "up"
    project = _find_project(project_name)

    if action == "up":
        try:
            cid = ensure_running(project)
            print(f"  Container running: {cid[:12]}")
            print(f"  Exec:  docker exec -it {cid[:12]} bash")
            print(f"  Claude: {exec_cmd(project)}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif action == "down":
        stop(project)
        print(f"  Container stopped for {project.name}")

    elif action == "status":
        cid = is_running(project)
        if cid:
            print(f"  {project.name}: running ({cid[:12]})")
        else:
            print(f"  {project.name}: not running")

    elif action == "exec":
        cid = is_running(project)
        if not cid:
            try:
                cid = ensure_running(project)
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)
        import os
        os.execvp("docker", [
            "docker", "exec", "-it", cid,
            "claude", "--dangerously-skip-permissions",
        ])

    else:
        print(f"Unknown action '{action}'. Use: up, down, status, exec")
        sys.exit(1)


def cmd_ignore(argv: list[str]) -> None:
    from .lifecycle import ignore_project, unignore_project

    if len(argv) < 1:
        print("Usage: orch ignore <project>       # ignore a project")
        print("       orch ignore <project> --undo # un-ignore a project")
        sys.exit(1)

    project_name = argv[0]
    undo = "--undo" in argv

    # For ignore, we need to find the project even if it's already ignored.
    # discover_projects() skips ignored ones, so we search manually.
    from .discovery import SITES_ROOT
    from .models import Project

    project_path = SITES_ROOT / project_name
    if not project_path.is_dir() or not (project_path / ".claude").is_dir():
        print(f"Project '{project_name}' not found in {SITES_ROOT}")
        sys.exit(1)

    project = Project(path=project_path)

    if undo:
        unignore_project(project)
        print(f"  {project.name} is no longer ignored")
    else:
        ignore_project(project)
        print(f"  {project.name} is now ignored")
        print(f"  To undo: orch ignore {project.name} --undo")
        print(f"  Or edit: {project_path}/.orch/project.toml → set ignored = false")


def main() -> None:
    argv = sys.argv[1:]

    if not argv:
        from .app import OrchApp
        OrchApp().run()
        return

    sub = argv[0]

    if sub in ("plan",):
        cmd_plan(argv[1:])
    elif sub in ("logs", "log"):
        cmd_logs(argv[1:])
    elif sub in ("bridge",):
        cmd_bridge(argv[1:])
    elif sub in ("stage",):
        cmd_stage(argv[1:])
    elif sub in ("container", "c"):
        cmd_container(argv[1:])
    elif sub in ("ignore",):
        cmd_ignore(argv[1:])
    elif sub in ("setup",):
        from .setup import main as setup_main
        setup_main()
    elif sub in ("-h", "--help", "help"):
        print(__doc__)
    else:
        from .app import OrchApp
        OrchApp().run()


if __name__ == "__main__":
    main()
