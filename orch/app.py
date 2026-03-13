from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Markdown,
    Static,
    Input,
)
from textual.worker import Worker, get_current_worker
from textual import work, on
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .models import Project
from .discovery import discover_projects
from .iterm import notify_input_needed, notify_resumed, clear_stale_handle, open_input_tab
from .container import (
    clear_stale_container, is_running as container_is_running,
    ensure_running, exec_claude_in_iterm,
)


# ── Status dot colours ───────────────────────────────────────────────────────

INDICATOR = {
    "active":  "[bold green]●[/]",
    "waiting": "[bold yellow]●[/]",
    "idle":    "[dim]○[/]",
}

CONTAINER_ICON = "[bold blue]■[/]"
CONTAINER_ICON_OFF = "[dim]□[/]"


class StatusFileHandler(FileSystemEventHandler):
    """Watchdog handler — calls back into the TUI on any .claude/ or TODOS.md change."""

    def __init__(self, callback: Callable[[str], None]):
        self._cb = callback

    def on_modified(self, event):
        if not event.is_directory:
            self._cb(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._cb(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._cb(event.src_path)


# ── Widgets ───────────────────────────────────────────────────────────────────

class ProjectItem(ListItem):
    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        yield Label(self._build_label(), markup=True)

    def _build_label(self) -> str:
        indicator = INDICATOR.get(self.project.status_indicator, INDICATOR["idle"])
        cbox = CONTAINER_ICON if container_is_running(self.project) else CONTAINER_ICON_OFF
        count = self.project.pending_count
        badge = f" [dim]{count}t[/]" if count else ""
        return f"{indicator}{cbox} {self.project.name}{badge}"

    def refresh_label(self) -> None:
        self.query_one(Label).update(self._build_label())


class StatusPane(Static):
    """Center pane: shows current status sentence + container + CLAUDE.md abstract."""

    DEFAULT_CSS = """
    StatusPane {
        padding: 1 2;
        height: 100%;
    }
    """

    def update_project(self, project: Project | None) -> None:
        if project is None:
            self.update("[dim]No project selected[/]")
            return

        status = project.current_status
        status_line = (
            f"[bold]{status}[/]"
            if status
            else "[dim italic]No status yet — Claude hasn't written to .claude/status[/]"
        )

        # Container status
        cid = container_is_running(project)
        if cid:
            container_line = f"\n[bold blue]■[/] Container running ({cid[:12]})"
        else:
            container_line = "\n[dim]□ No container[/]"

        # Pull first non-blank line of CLAUDE.md as the project abstract
        abstract = ""
        claude_md = project.claude_md
        for line in claude_md.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                abstract = line
                break

        abstract_line = (
            f"\n[dim]{abstract}[/]"
            if abstract
            else ""
        )

        path_line = f"\n\n[dim]{project.path}[/]"

        self.update(f"{status_line}{container_line}{abstract_line}{path_line}")


# ── Log tab helper ────────────────────────────────────────────────────────────

def _open_log_tab(project: Project) -> None:
    """
    Open an iTerm2 tab running `orch logs <project>`.
    Uses the orch profile so it's visually distinct.
    Reuses an existing log tab for this project if one is open.
    """
    from .iterm import _load_config, _bring_tab_to_front

    handle_file = project.claude_dir / "iterm_log_handle"

    if handle_file.exists():
        tty = handle_file.read_text().strip()
        if tty and _bring_tab_to_front(tty):
            return
        handle_file.unlink(missing_ok=True)

    cfg          = _load_config()
    profile      = cfg["iterm"].get("profile", "orch")
    dedicated    = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")
    tab_name     = f"{project.name} logs"
    cmd          = f"orch logs {project.name}"

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
            end if
            tell orchWindow
                create tab with profile "{profile}"
                tell current session
                    set name to "{tab_name}"
                    write text "{cmd}"
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
                    set name to "{tab_name}"
                    write text "{cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """

    import subprocess
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    tty = result.stdout.strip()
    if tty:
        handle_file.write_text(tty)


# ── Plan tab helper ──────────────────────────────────────────────────────────

def _open_plan_tab() -> None:
    """Open an iTerm2 tab running `orch plan`."""
    from .iterm import _load_config

    cfg          = _load_config()
    profile      = cfg["iterm"].get("profile", "orch")
    dedicated    = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")

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
            end if
            tell orchWindow
                create tab with profile "{profile}"
                tell current session
                    set name to "day plan"
                    write text "orch plan"
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
                    set name to "day plan"
                    write text "orch plan"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """

    import subprocess
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True)


# ── Main App ──────────────────────────────────────────────────────────────────

class OrchApp(App):
    """Orch — Claude session orchestrator."""

    TITLE = "orch"
    CSS = """
    Screen {
        layout: vertical;
    }

    #main-row {
        height: 1fr;
        layout: horizontal;
    }

    #project-panel {
        width: 30;
        min-width: 24;
        border-right: solid $panel;
        height: 100%;
    }

    #project-panel-title {
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        border-bottom: solid $panel;
    }

    #project-list {
        height: 1fr;
        overflow-y: auto;
    }

    #center-panel {
        width: 1fr;
        height: 100%;
        border-right: solid $panel;
    }

    #center-title {
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        border-bottom: solid $panel;
    }

    #status-pane {
        height: 1fr;
        padding: 1 2;
    }

    #input-row {
        height: 3;
        padding: 0 1;
        border-top: solid $panel;
        layout: horizontal;
        align: left middle;
    }

    #task-input {
        width: 1fr;
    }

    #right-panel {
        width: 40%;
        height: 100%;
    }

    #right-title {
        padding: 0 1;
        color: $text-muted;
        text-style: bold;
        border-bottom: solid $panel;
    }

    #todos-view {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }

    #no-projects {
        padding: 2;
        color: $text-muted;
    }

    #help-bar {
        height: auto;
        max-height: 14;
        padding: 1 2;
        background: $panel;
        border-top: solid $primary;
        color: $text;
    }

    #help-bar.hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("t", "focus_input", "Task", show=True),
        Binding("e", "exec_shell", "Shell", show=True),
        Binding("c", "container_up", "Container", show=True),
        Binding("l", "open_logs", "Logs", show=True),
        Binding("p", "open_plan", "Plan", show=True),
        Binding("b", "toggle_bridge", "Bridge", show=True),
        Binding("s", "set_stage", "Stage", show=True),
        Binding("i", "ignore_project", "Ignore", show=True),
        Binding("question_mark", "toggle_help", "?", show=True),
        Binding("escape", "blur_input", "Cancel", show=False),
    ]

    selected_project: reactive[Project | None] = reactive(None)

    def __init__(self):
        super().__init__()
        self.projects: list[Project] = []
        self._observer: Observer | None = None
        self._bridge_running = False
        self._input_mode: str = "task"  # "task" or "stage"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main-row"):
            with Vertical(id="project-panel"):
                yield Static("projects", id="project-panel-title")
                yield ListView(id="project-list")
            with Vertical(id="center-panel"):
                yield Static("status", id="center-title")
                yield StatusPane("", id="status-pane")
                with Horizontal(id="input-row"):
                    yield Input(placeholder="Send task to Claude… (t)", id="task-input")
            with Vertical(id="right-panel"):
                yield Static("todos", id="right-title")
                yield Markdown("", id="todos-view")
        yield Static(
            "[bold]Keybindings[/]\n"
            "\n"
            "  [bold cyan]j/k[/] [dim]or[/] [bold cyan]arrows[/]  Navigate projects\n"
            "  [bold cyan]Enter[/]          Select project (auto-starts container)\n"
            "  [bold cyan]t[/]              Send task to Claude in container\n"
            "  [bold cyan]e[/]              Open iTerm2 tab with Claude (host)\n"
            "  [bold cyan]c[/]              Open iTerm2 tab with Claude (container)\n"
            "  [bold cyan]l[/]              Tail docker logs in iTerm2 tab\n"
            "  [bold cyan]p[/]              Generate day plan in iTerm2 tab\n"
            "  [bold cyan]b[/]              Toggle mobile web bridge on/off\n"
            "  [bold cyan]s[/]              Set project stage (type: stage or stage: note)\n"
            "  [bold cyan]i[/]              Ignore/hide selected project from orch\n"
            "  [bold cyan]r[/]              Rescan ~/Sites for projects\n"
            "  [bold cyan]q[/]              Quit\n"
            "  [bold cyan]?[/]              Toggle this help\n"
            "  [bold cyan]Esc[/]            Cancel input",
            id="help-bar",
            markup=True,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.projects = discover_projects()
        self._populate_list()
        self._start_watcher()
        # Clear any stale handles left from a previous orch session
        self.run_worker(
            lambda: [
                (clear_stale_handle(p), clear_stale_container(p))
                for p in self.projects
            ],
            thread=True,
        )
        if self.projects:
            self.query_one("#project-list", ListView).focus()

    def _populate_list(self) -> None:
        lv = self.query_one("#project-list", ListView)
        lv.clear()
        if not self.projects:
            lv.mount(ListItem(Label("[dim]No projects found in ~/Sites[/]", markup=True), id="no-projects"))
            return
        for project in self.projects:
            lv.mount(ProjectItem(project))

    def _start_watcher(self) -> None:
        if not self.projects:
            return
        handler = StatusFileHandler(self._on_file_changed)
        self._observer = Observer()
        watched = set()
        for p in self.projects:
            target = str(p.path)
            if target not in watched:
                self._observer.schedule(handler, target, recursive=True)
                watched.add(target)
        self._observer.start()

    def _on_file_changed(self, path: str) -> None:
        """Called from watchdog thread — post a message to the main thread."""
        self.call_from_thread(self._handle_file_change, path)

    def _handle_file_change(self, path: str) -> None:
        changed = Path(path)

        # ── waiting_for_input created: Claude needs you ──────────────────────
        if changed.name == "waiting_for_input" and changed.exists():
            project = self._project_for_path(changed)
            if project:
                question = changed.read_text().strip()
                def _handle_input(p=project, q=question):
                    notify_input_needed(p, q)
                    if container_is_running(p):
                        exec_claude_in_iterm(p)
                    else:
                        open_input_tab(p)

                self.run_worker(_handle_input, thread=True)
                self._refresh_project_item(project)
                if self.selected_project == project:
                    self._refresh_panes()
            return

        # ── waiting_for_input deleted: Claude resumed ─────────────────────────
        if changed.name == "waiting_for_input" and not changed.exists():
            project = self._project_for_path(changed)
            if project:
                self.run_worker(
                    lambda p=project: notify_resumed(p),
                    thread=True,
                )
                self._refresh_project_item(project)
                if self.selected_project == project:
                    self._refresh_panes()
            return

        # ── container_id changed: container started/stopped ───────────────────
        if changed.name == "container_id":
            self._refresh_project_item_for_path(changed)
            if self.selected_project and changed.is_relative_to(self.selected_project.path):
                self._refresh_panes()
            return

        # ── iterm handles: no action needed ───────────────────────────────────
        if changed.name in ("iterm_handle", "iterm_container_handle", "iterm_log_handle"):
            return

        # ── General file change: status, todos, etc. ──────────────────────────
        self._refresh_project_item_for_path(changed)
        if self.selected_project and changed.is_relative_to(self.selected_project.path):
            self._refresh_panes()

    def _project_for_path(self, path: Path) -> Project | None:
        """Find the project that owns this path."""
        for p in self.projects:
            try:
                if path.is_relative_to(p.path):
                    return p
            except ValueError:
                pass
        return None

    def _refresh_project_item(self, project: Project) -> None:
        lv = self.query_one("#project-list", ListView)
        for item in lv.query(ProjectItem):
            if item.project.path == project.path:
                item.refresh_label()
                break

    def _refresh_project_item_for_path(self, path: Path) -> None:
        lv = self.query_one("#project-list", ListView)
        for item in lv.query(ProjectItem):
            try:
                if path.is_relative_to(item.project.path):
                    item.refresh_label()
            except ValueError:
                pass

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, ProjectItem):
            self.selected_project = event.item.project
            self._refresh_panes()
            # Auto-start container in background if not already running
            self._ensure_container(event.item.project)

    def _ensure_container(self, project: Project) -> None:
        """Start container in background if not already running."""
        if container_is_running(project):
            return

        def _start():
            try:
                cid = ensure_running(project)
                self.call_from_thread(
                    self.notify,
                    f"Container ready for {project.name} ({cid[:12]})",
                )
                # Refresh the project item to show container icon
                self.call_from_thread(self._refresh_project_item, project)
                if self.selected_project and self.selected_project.path == project.path:
                    self.call_from_thread(self._refresh_panes)
            except Exception as e:
                self.call_from_thread(
                    self.notify,
                    f"Container failed for {project.name}: {e}",
                    severity="error",
                )

        self.run_worker(_start, thread=True)

    def _refresh_panes(self) -> None:
        p = self.selected_project
        if p is None:
            return

        # Update center title
        self.query_one("#center-title", Static).update(p.name)

        # Update status pane
        self.query_one("#status-pane", StatusPane).update_project(p)

        # Update right title
        count = p.pending_count
        badge = f" ({count} pending)" if count else ""
        self.query_one("#right-title", Static).update(f"todos{badge}")

        # Update todos markdown
        self.query_one("#todos-view", Markdown).update(p.todos_text)

    # ── Actions ──────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        """Rescan ~/Sites for new/removed projects."""
        self.projects = discover_projects()
        self._populate_list()
        if self.selected_project:
            self._refresh_panes()
        self.notify("Projects refreshed")

    def action_focus_input(self) -> None:
        self._input_mode = "task"
        inp = self.query_one("#task-input", Input)
        inp.placeholder = "Send task to Claude… (Enter to send, Esc to cancel)"
        inp.focus()

    def action_blur_input(self) -> None:
        inp = self.query_one("#task-input", Input)
        inp.value = ""
        inp.placeholder = "Send task to Claude… (t)"
        self._input_mode = "task"
        self.query_one("#project-list", ListView).focus()

    def action_ignore_project(self) -> None:
        """Ignore the selected project so it no longer appears in orch."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        from .lifecycle import ignore_project
        ignore_project(p)
        self.notify(f"{p.name} ignored — undo with: orch ignore {p.name} --undo")
        self.action_refresh()

    def action_toggle_help(self) -> None:
        bar = self.query_one("#help-bar")
        bar.toggle_class("hidden")

    def action_container_up(self) -> None:
        """Open an iTerm2 tab with Claude running inside the container."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        self.notify(f"Starting container for {p.name}...")
        self.run_worker(lambda: exec_claude_in_iterm(p), thread=True)

    def action_exec_shell(self) -> None:
        """Open an iTerm2 tab with Claude on the host (no container)."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        self.run_worker(lambda: open_input_tab(p), thread=True)
        self.notify(f"Opening iTerm2 tab for {p.name}")

    def action_open_logs(self) -> None:
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        self.run_worker(lambda: _open_log_tab(p), thread=True)
        self.notify(f"Tailing logs for {p.name}")

    def action_open_plan(self) -> None:
        """Open day plan in an iTerm2 tab."""
        self.run_worker(_open_plan_tab, thread=True)
        self.notify("Generating day plan...")

    def action_toggle_bridge(self) -> None:
        """Toggle the mobile web bridge on/off."""
        from .bridge import start_bridge, stop_bridge, bridge_running
        if bridge_running():
            stop_bridge()
            self._bridge_running = False
            self.notify("Bridge stopped")
        else:
            try:
                port = start_bridge()
                self._bridge_running = True
                self.notify(f"Bridge running on http://localhost:{port}")
            except OSError as e:
                self.notify(f"Bridge failed: {e}", severity="error")

    def action_set_stage(self) -> None:
        """Prompt for a new stage via the input bar."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        self._input_mode = "stage"
        inp = self.query_one("#task-input", Input)
        inp.placeholder = "Stage (e.g. mvp or mvp: core loop working) — Enter to set, Esc to cancel"
        inp.focus()

    @on(Input.Submitted, "#task-input")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if not value or not self.selected_project:
            self.action_blur_input()
            return

        if self._input_mode == "stage":
            self._handle_stage_input(self.selected_project, value)
        else:
            self._send_task(self.selected_project, value)

        event.input.value = ""
        self._input_mode = "task"
        event.input.placeholder = "Send task to Claude… (t)"
        self.query_one("#project-list", ListView).focus()

    def _handle_stage_input(self, project: Project, value: str) -> None:
        """Parse 'stage' or 'stage: note' and advance the project."""
        from .lifecycle import STAGES, advance_stage, STAGE_EMOJI
        if ":" in value:
            stage, _, note = value.partition(":")
            stage = stage.strip().lower()
            note = note.strip()
        else:
            stage = value.strip().lower()
            note = ""

        if stage not in STAGES:
            self.notify(f"Unknown stage '{stage}'. Use: {', '.join(STAGES)}", severity="warning")
            return

        advance_stage(project, stage, note)
        emoji = STAGE_EMOJI.get(stage, "")
        self.notify(f"{emoji} {project.name} → {stage}")
        self._refresh_project_item(project)
        self._refresh_panes()

    def _send_task(self, project: Project, task: str) -> None:
        """
        Write task to .claude/pending_task AND launch Claude in the container
        with the task as the initial prompt so it actually gets executed.
        """
        pending = project.claude_dir / "pending_task"
        pending.write_text(task)

        # Launch Claude in container with the task as the prompt
        cid = container_is_running(project)
        if cid:
            from .container import _send_task_to_container
            self.run_worker(
                lambda: _send_task_to_container(project, task),
                thread=True,
            )
            self.notify(f"Task sent to {project.name} (container)")
        else:
            self.notify(f"Task queued for {project.name} (no container running)")

    def on_unmount(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
        # Stop bridge if running
        if self._bridge_running:
            from .bridge import stop_bridge
            stop_bridge()
