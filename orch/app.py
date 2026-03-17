from __future__ import annotations

import random
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click, Resize
from textual.reactive import reactive
from textual.timer import Timer
from textual.widgets import (
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
    ensure_running, exec_claude_in_iterm, stop as container_stop,
)


# ── Status dot colours ───────────────────────────────────────────────────────

INDICATOR = {
    "active":  "[bold green]●[/]",
    "waiting": "[bold yellow]●[/]",
    "idle":    "[dim]○[/]",
}

# ── Spinner words (Claude-style) ─────────────────────────────────────────────

SPINNER_WORDS = [
    "Initializing",
    "Conjuring",
    "Percolating",
    "Synthesizing",
    "Calibrating",
    "Manifesting",
    "Bootstrapping",
    "Assembling",
    "Wrangling",
    "Contemplating",
    "Orchestrating",
    "Compiling",
    "Channeling",
    "Transmuting",
    "Fermenting",
    "Galvanizing",
    "Combusting",
    "Flummoxing",
    "Rummaging",
    "Machinating",
    "Coalescing",
    "Ruminating",
    "Perambulating",
    "Confabulating",
    "Amalgamating",
    "Deliberating",
    "Cogitating",
    "Extrapolating",
    "Triangulating",
    "Incubating",
]

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
        auto = "[bold magenta]⚡[/]" if self.project.auto_dispatch_enabled else ""
        count = self.project.pending_count
        badge = f" [dim]{count}t[/]" if count else ""
        return f"{indicator}{cbox}{auto} {self.project.name}{badge}"

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._spinner_timer: Timer | None = None
        self._spinner_label: str = ""
        self._spinner_project: Project | None = None
        self._spinner_words = list(SPINNER_WORDS)
        self._error: str | None = None  # persistent error shown until next action

    def set_error(self, message: str) -> None:
        """Set a persistent error message shown in the status pane."""
        self._error = message

    def clear_error(self) -> None:
        """Clear any persistent error."""
        self._error = None

    def start_spinner(self, label: str, project: Project | None = None) -> None:
        """Start showing a rotating activity spinner."""
        self.clear_error()
        self._spinner_label = label
        self._spinner_project = project
        random.shuffle(self._spinner_words)
        self._spinner_idx = 0
        self._update_spinner()
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(1.5, self._update_spinner)

    def stop_spinner(self) -> None:
        """Stop the activity spinner."""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._spinner_label = ""
        self._spinner_project = None

    @property
    def is_spinning(self) -> bool:
        return self._spinner_timer is not None

    def _update_spinner(self) -> None:
        word = self._spinner_words[self._spinner_idx % len(self._spinner_words)]
        self._spinner_idx += 1
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        frame = frames[self._spinner_idx % len(frames)]
        name = f" [bold]{self._spinner_project.name}[/]" if self._spinner_project else ""
        self.update(
            f"[bold yellow]{frame}[/] [italic]{word}...[/]{name}\n\n"
            f"[dim]{self._spinner_label}[/]"
        )

    def update_project(self, project: Project | None) -> None:
        # Don't overwrite an active spinner
        if self.is_spinning:
            return

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

        error_line = ""
        if self._error:
            error_line = f"\n\n[bold red]Container error:[/]\n[red]{self._error}[/]"

        self.update(f"{status_line}{container_line}{abstract_line}{path_line}{error_line}")


# ── Mobile tab bar ────────────────────────────────────────────────────────────

MOBILE_THRESHOLD = 100  # columns — below this we switch to tabbed layout

TAB_LABELS = ["projects", "status", "todos"]


class TabBar(Static):
    """Horizontal row of tappable tabs for narrow/mobile displays."""

    DEFAULT_CSS = """
    TabBar {
        layout: horizontal;
        height: 3;
        dock: top;
        background: $panel;
        border-bottom: solid $primary;
    }
    TabBar .tab {
        width: 1fr;
        content-align: center middle;
        height: 3;
        padding: 0 1;
    }
    TabBar .tab.active {
        background: $primary;
        color: $text;
        text-style: bold;
    }
    TabBar .tab:hover {
        background: $primary 30%;
    }
    """

    def __init__(self, tabs: list[str], active: int = 0):
        super().__init__()
        self._tabs = tabs
        self._active = active

    def compose(self) -> ComposeResult:
        for i, label in enumerate(self._tabs):
            cls = "tab active" if i == self._active else "tab"
            yield Static(label, classes=cls, id=f"tab-{i}")

    def set_active(self, index: int) -> None:
        self._active = index
        for i, child in enumerate(self.query(".tab")):
            if i == index:
                child.add_class("active")
            else:
                child.remove_class("active")


# ── Log tab helper ────────────────────────────────────────────────────────────

def _open_log_tab(project: Project) -> None:
    """
    Open an iTerm2 tab running `orch logs <project>`.
    Reuses an existing log tab for this project if one is open.
    """
    from .iterm import _load_config, _bring_tab_to_front, _run_iterm_script

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

    script = _build_iterm_tab_script(
        profile=profile, dedicated=dedicated, window_title=window_title,
        tab_name=tab_name, cmd=cmd,
    )

    tty = _run_iterm_script(script)
    if tty:
        handle_file.write_text(tty)


# ── Plan tab helper ──────────────────────────────────────────────────────────

def _open_plan_tab() -> None:
    """Open an iTerm2 tab running `orch plan`."""
    from .iterm import _load_config, _run_iterm_script

    cfg          = _load_config()
    profile      = cfg["iterm"].get("profile", "orch")
    dedicated    = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")

    script = _build_iterm_tab_script(
        profile=profile, dedicated=dedicated, window_title=window_title,
        tab_name="day plan", cmd="orch plan",
    )

    _run_iterm_script(script)


def _build_iterm_tab_script(*, profile: str, dedicated: bool, window_title: str,
                             tab_name: str, cmd: str) -> str:
    """Build AppleScript to open an iTerm2 tab with profile fallback."""
    if dedicated:
        return f"""
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
                    write text "{cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """
    else:
        return f"""
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
                    write text "{cmd}"
                    set thetty to tty
                end tell
            end tell
            return thetty
        end tell
        """


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
        height: auto;
        min-height: 3;
        max-height: 5;
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

    /* ── Tab bar (hidden by default, shown in mobile mode) ── */
    TabBar {
        display: none;
    }

    /* ── Mobile mode: panels shown/hidden via classes ── */
    .mobile #project-panel {
        width: 1fr;
        min-width: 0;
        border-right: none;
    }
    .mobile #center-panel {
        width: 1fr;
        border-right: none;
    }
    .mobile #right-panel {
        width: 1fr;
    }
    .mobile .panel-hidden {
        display: none;
    }
    .mobile TabBar {
        display: block;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("t", "focus_input_task", "Task", show=True),
        Binding("a", "focus_input_todo", "Add Todo", show=True),
        Binding("e", "exec_shell", "Shell", show=True),
        Binding("c", "container_up", "Container", show=True),
        Binding("x", "container_shell", "Shell(ctr)", show=True),
        Binding("d", "container_down_press", "Down(dd)", show=True),
        Binding("l", "open_logs", "Logs", show=True),
        Binding("p", "open_plan", "Plan", show=True),
        Binding("b", "toggle_bridge", "Bridge", show=True),
        Binding("s", "set_stage", "Stage", show=True),
        Binding("i", "ignore_project", "Ignore", show=True),
        Binding("g", "toggle_auto_dispatch", "Auto(g)", show=True),
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
        self._d_pressed: bool = False
        self._d_timer: Timer | None = None
        self._mobile: bool = False
        self._active_tab: int = 0  # 0=projects, 1=status, 2=todos
        self._dispatch_timers: dict[str, Timer] = {}  # project path -> pending dispatch timer

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield TabBar(TAB_LABELS, active=0)
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
            "[dim]t[/]ask  [dim]a[/]dd todo  [dim]e[/]xec  [dim]c[/]ontainer  [dim]x[/] shell(ctr)  [dim]dd[/] down  "
            "[dim]l[/]ogs  [dim]p[/]lan  [dim]b[/]ridge  [dim]s[/]tage  "
            "[dim]i[/]gnore  [dim]g[/] auto  [dim]r[/]efresh  [dim]q[/]uit  [dim]?[/] toggle help\n"
            "\n"
            "  [bold cyan]j/k[/] [dim]or[/] [bold cyan]arrows[/]  Navigate projects\n"
            "  [bold cyan]Enter[/]          Select project (auto-starts container)\n"
            "  [bold cyan]t[/]              Send task to Claude (fire-and-forget)\n"
            "  [bold cyan]a[/]              Add todo to TODOS.md\n"
            "  [bold cyan]e[/]              Open iTerm2 tab with Claude (host) [dim]desktop only[/]\n"
            "  [bold cyan]c[/]              Open iTerm2 tab with Claude (container) [dim]desktop only[/]\n"
            "  [bold cyan]x[/]              Open iTerm2 tab with bash shell (container) [dim]desktop only[/]\n"
            "  [bold cyan]dd[/]             Stop and remove container (double-press)\n"
            "  [bold cyan]l[/]              Tail docker logs in iTerm2 tab [dim]desktop only[/]\n"
            "  [bold cyan]p[/]              Generate day plan in iTerm2 tab [dim]desktop only[/]\n"
            "  [bold cyan]b[/]              Toggle mobile web bridge on/off\n"
            "  [bold cyan]s[/]              Set project stage (type: stage or stage: note)\n"
            "  [bold cyan]i[/]              Ignore/hide selected project from orch\n"
            "  [bold cyan]g[/]              Toggle auto-dispatch of pending todos (⚡)\n"
            "  [bold cyan]r[/]              Rescan ~/Sites for projects\n"
            "  [bold cyan]q[/]              Quit\n"
            "  [bold cyan]?[/]              Toggle this help\n"
            "  [bold cyan]Esc[/]            Cancel input\n"
            "\n"
            "  [bold magenta]Mobile:[/] Tap tabs at top to switch panels. "
            "Width < 100 cols enables tabbed mode.",
            id="help-bar",
            markup=True,
        )

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
        # Check if we should start in mobile mode
        self._check_mobile(self.size.width)

    # ── Mobile / tabbed layout ───────────────────────────────────────────────

    def on_resize(self, event: Resize) -> None:
        """Switch layout when terminal width crosses the mobile threshold."""
        self._check_mobile(event.size.width)

    def _check_mobile(self, width: int) -> None:
        mobile = width < MOBILE_THRESHOLD
        if mobile == self._mobile:
            return
        self._mobile = mobile
        if mobile:
            self._enter_mobile()
        else:
            self._exit_mobile()

    def _enter_mobile(self) -> None:
        """Switch to tabbed single-panel layout."""
        self.screen.add_class("mobile")
        self._active_tab = 0
        self._apply_tab(0)
        # Hide help bar in mobile — too much screen estate
        self.query_one("#help-bar").add_class("hidden")

    def _exit_mobile(self) -> None:
        """Restore the three-pane side-by-side layout."""
        self.screen.remove_class("mobile")
        for panel_id in ("project-panel", "center-panel", "right-panel"):
            self.query_one(f"#{panel_id}").remove_class("panel-hidden")

    def _apply_tab(self, index: int) -> None:
        """Show only the panel for the given tab index."""
        self._active_tab = index
        panels = ["project-panel", "center-panel", "right-panel"]
        for i, pid in enumerate(panels):
            panel = self.query_one(f"#{pid}")
            if i == index:
                panel.remove_class("panel-hidden")
            else:
                panel.add_class("panel-hidden")
        self.query_one(TabBar).set_active(index)

    @on(Click, "TabBar .tab")
    def _on_tab_click(self, event: Click) -> None:
        """Handle tap/click on a tab in mobile mode."""
        if not self._mobile:
            return
        widget = event.widget
        if widget.id and widget.id.startswith("tab-"):
            index = int(widget.id.split("-")[1])
            self._apply_tab(index)

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
            # Container just started — check if there are pending todos to dispatch
            project = self._project_for_path(changed)
            if project and changed.exists():
                self._schedule_dispatch_check(project)
            return

        # ── iterm handles: no action needed ───────────────────────────────────
        if changed.name in ("iterm_handle", "iterm_container_handle", "iterm_container_shell_handle", "iterm_log_handle"):
            return

        # ── Auto-dispatch files: skip refresh loop ────────────────────────────
        if changed.name in ("active_todo", "auto_dispatch"):
            return

        # ── General file change: status, todos, etc. ──────────────────────────
        self._refresh_project_item_for_path(changed)
        if self.selected_project and changed.is_relative_to(self.selected_project.path):
            self._refresh_panes()

        # ── Auto-dispatch check on status or TODOS.md changes ─────────────────
        if changed.name in ("status", "TODOS.md"):
            project = self._project_for_path(changed)
            if project:
                self._schedule_dispatch_check(project)

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
            # On mobile, auto-switch to the status tab after selecting a project
            if self._mobile:
                self._apply_tab(1)

    def _ensure_container(self, project: Project) -> None:
        """Start container in background if not already running."""
        if container_is_running(project):
            return

        # Start the spinner
        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Starting container — pulling image, installing tools", project)

        def _start():
            try:
                cid = ensure_running(project)
                self.call_from_thread(self._stop_spinner_and_refresh, project,
                                      f"Container ready for {project.name} ({cid[:12]})")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, project,
                                      f"Container failed for {project.name}: {e}",
                                      "error")

        self.run_worker(_start, thread=True)

    def _stop_spinner_and_refresh(self, project: Project, message: str,
                                   severity: str = "information") -> None:
        """Stop the spinner, show a notification, and refresh the UI."""
        pane = self.query_one("#status-pane", StatusPane)
        pane.stop_spinner()
        if severity == "error":
            pane.set_error(message)
        self.notify(message, severity=severity)
        self._refresh_project_item(project)
        if self.selected_project and self.selected_project.path == project.path:
            self._refresh_panes()

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

    @property
    def _input_focused(self) -> bool:
        """True when the task input box has focus — suppress keybindings."""
        return isinstance(self.focused, Input)

    def action_refresh(self) -> None:
        """Rescan ~/Sites for new/removed projects."""
        if self._input_focused: return
        self.projects = discover_projects()
        self._populate_list()
        if self.selected_project:
            self._refresh_panes()
        self.notify("Projects refreshed")

    def action_focus_input_task(self) -> None:
        if self._input_focused: return
        self._input_mode = "task"
        inp = self.query_one("#task-input", Input)
        inp.placeholder = "Send task to Claude… (Enter to send, Esc to cancel)"
        inp.focus()

    def action_focus_input_todo(self) -> None:
        if self._input_focused: return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        self._input_mode = "todo"
        inp = self.query_one("#task-input", Input)
        inp.placeholder = "Add todo… (Enter to add, Esc to cancel)"
        inp.focus()

    def action_blur_input(self) -> None:
        inp = self.query_one("#task-input", Input)
        inp.value = ""
        inp.placeholder = "Send task to Claude… (t)"
        self._input_mode = "task"
        self.query_one("#project-list", ListView).focus()

    def action_ignore_project(self) -> None:
        """Ignore the selected project so it no longer appears in orch."""
        if self._input_focused: return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        from .lifecycle import ignore_project
        ignore_project(p)
        self.notify(f"{p.name} ignored — undo with: orch ignore {p.name} --undo")
        self.action_refresh()

    def action_toggle_auto_dispatch(self) -> None:
        """Toggle auto-dispatch of pending todos for the selected project."""
        if self._input_focused: return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        p.claude_dir.mkdir(parents=True, exist_ok=True)
        if p.auto_dispatch_enabled:
            p.auto_dispatch_file.unlink(missing_ok=True)
            # Cancel any pending dispatch timer
            key = str(p.path)
            if key in self._dispatch_timers:
                self._dispatch_timers[key].stop()
                del self._dispatch_timers[key]
            self.notify(f"Auto-dispatch OFF for {p.name}")
        else:
            p.auto_dispatch_file.write_text("1")
            self.notify(f"⚡ Auto-dispatch ON for {p.name}")
            self._schedule_dispatch_check(p)
        self._refresh_project_item(p)

    def action_toggle_help(self) -> None:
        if self._input_focused: return
        bar = self.query_one("#help-bar")
        bar.toggle_class("hidden")

    def action_container_up(self) -> None:
        """Open an iTerm2 tab with Claude running inside the container."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Launching Claude in container", p)

        def _launch():
            try:
                exec_claude_in_iterm(p)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Claude launched for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Launch failed: {e}", "error")

        self.run_worker(_launch, thread=True)

    def action_container_shell(self) -> None:
        """Open an iTerm2 tab with a bash shell inside the container."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Opening shell in container", p)

        def _launch():
            try:
                from .container import exec_shell_in_iterm
                exec_shell_in_iterm(p)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Shell opened for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Shell failed: {e}", "error")

        self.run_worker(_launch, thread=True)

    def action_container_down_press(self) -> None:
        """First d press primes, second d within 0.5s stops the container."""
        if self._input_focused: return
        if self._d_pressed:
            # Second press — execute
            self._d_pressed = False
            if self._d_timer:
                self._d_timer.stop()
                self._d_timer = None
            self._do_container_down()
        else:
            # First press — prime and start timeout
            self._d_pressed = True
            self.notify("Press [bold]d[/] again to stop container", markup=True)
            self._d_timer = self.set_timer(0.8, self._reset_d_press)

    def _reset_d_press(self) -> None:
        self._d_pressed = False
        self._d_timer = None

    def _do_container_down(self) -> None:
        """Stop and remove the container for the selected project."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        if not container_is_running(p):
            self.notify(f"No container running for {p.name}", severity="warning")
            return

        container_stop(p)
        self.notify(f"Container stopped for {p.name}")
        self._refresh_project_item(p)
        self._refresh_panes()

    def action_exec_shell(self) -> None:
        """Open an iTerm2 tab with Claude on the host (no container)."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Opening iTerm2 tab", p)

        def _open():
            try:
                open_input_tab(p)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"iTerm2 tab opened for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Failed: {e}", "error")

        self.run_worker(_open, thread=True)

    def action_open_logs(self) -> None:
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Opening log stream", p)

        def _open():
            try:
                _open_log_tab(p)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Tailing logs for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Logs failed: {e}", "error")

        self.run_worker(_open, thread=True)

    def action_open_plan(self) -> None:
        """Open day plan in an iTerm2 tab."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Generating day plan — calling Claude API")

        def _plan():
            try:
                _open_plan_tab()
                self.call_from_thread(self._finish_plan)
            except Exception as e:
                self.call_from_thread(self._finish_plan, str(e))

        self.run_worker(_plan, thread=True)

    def _finish_plan(self, error: str | None = None) -> None:
        pane = self.query_one("#status-pane", StatusPane)
        pane.stop_spinner()
        if error:
            self.notify(f"Plan failed: {error}", severity="error")
        else:
            self.notify("Day plan opened in iTerm2")
        if self.selected_project:
            self._refresh_panes()

    def action_toggle_bridge(self) -> None:
        """Toggle the mobile web bridge on/off."""
        if self._input_focused: return
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
        if self._input_focused: return
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
        elif self._input_mode == "todo":
            self._add_todo(self.selected_project, value)
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
        Fire-and-forget: run Claude headlessly in the container with the task.
        No iTerm tab — Claude runs in the background. Status dot updates as it works.
        """
        cid = container_is_running(project)
        if not cid:
            # Write to pending_task for pickup when a session starts
            project.claude_dir.mkdir(parents=True, exist_ok=True)
            (project.claude_dir / "pending_task").write_text(task)
            self.notify(f"Task queued for {project.name} (no container — will run on next session)")
            return

        from .container import _run_task_headless
        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Running task in background", project)

        def _run():
            try:
                _run_task_headless(project, task)
                self.call_from_thread(self._stop_spinner_and_refresh, project,
                                      f"Task running for {project.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, project,
                                      f"Task failed: {e}", "error")

        self.run_worker(_run, thread=True)

    def _add_todo(self, project: Project, text: str) -> None:
        """Append a todo item to the project's TODOS.md."""
        todos_file = project.path / "TODOS.md"
        if not todos_file.exists():
            todos_file.write_text("## Pending\n")

        content = todos_file.read_text()

        # Insert under ## Pending section, or append at end
        new_item = f"- [ ] {text}\n"
        if "## Pending" in content:
            content = content.replace("## Pending\n", f"## Pending\n{new_item}", 1)
        else:
            content = f"## Pending\n{new_item}\n" + content

        todos_file.write_text(content)
        self.notify(f"Todo added to {project.name}")
        self._refresh_panes()

    # ── Auto-dispatch logic (parallel worktree-based) ──────────────────────

    def _schedule_dispatch_check(self, project: Project) -> None:
        """Debounced: schedule a dispatch check 10 seconds from now."""
        if not project.auto_dispatch_enabled:
            return
        key = str(project.path)
        if key in self._dispatch_timers:
            self._dispatch_timers[key].stop()
        self._dispatch_timers[key] = self.set_timer(
            10.0,
            lambda p=project: self._maybe_auto_dispatch(p),
        )

    def _maybe_auto_dispatch(self, project: Project) -> None:
        """
        Dispatch pending todos in parallel using worktrees.
        Up to max_parallel tasks run concurrently (default 3).
        """
        from .container import _load_dispatch_config, run_task_in_worktree

        key = str(project.path)
        self._dispatch_timers.pop(key, None)

        if not project.auto_dispatch_enabled:
            return

        # How many slots are available?
        cfg = _load_dispatch_config()
        max_parallel = cfg.get("max_parallel", 3)
        active = project.in_progress_count
        slots = max(0, max_parallel - active)

        if slots == 0:
            return

        # Grab up to `slots` pending todos
        pending = project.pending_todos[:slots]
        if not pending:
            return

        # Claim and dispatch each todo in parallel
        for todo_text in pending:
            if not self._claim_todo(project, todo_text):
                continue

            def _run(tt=todo_text):
                try:
                    results = run_task_in_worktree(project, tt)
                    self.call_from_thread(
                        self._on_dispatch_complete, project, tt, results
                    )
                except Exception as e:
                    self.call_from_thread(
                        self._on_dispatch_failed, project, tt, e
                    )

            self.run_worker(_run, thread=True)
            truncated = todo_text[:50] + ("…" if len(todo_text) > 50 else "")
            self.notify(f"⚡ Dispatched (worktree): {truncated}")

        self._refresh_project_item(project)
        if self.selected_project and self.selected_project.path == project.path:
            self._refresh_panes()

    def _claim_todo(self, project: Project, todo_text: str) -> bool:
        """Mark first matching '- [ ] {todo_text}' as '- [~]' in TODOS.md."""
        try:
            content = project.todos_file.read_text()
        except FileNotFoundError:
            return False

        target = f"- [ ] {todo_text}"
        if target not in content:
            return False

        new_content = content.replace(target, f"- [~] {todo_text}", 1)
        project.todos_file.write_text(new_content)

        project.claude_dir.mkdir(parents=True, exist_ok=True)
        project.active_todo_file.write_text(todo_text)
        return True

    def _mark_todo_done(self, project: Project, todo_text: str) -> None:
        """Mark a todo as done in TODOS.md."""
        try:
            content = project.todos_file.read_text()
            content = content.replace(f"- [~] {todo_text}", f"- [x] {todo_text}", 1)
            project.todos_file.write_text(content)
        except FileNotFoundError:
            pass

    def _on_dispatch_complete(
        self, project: Project, todo_text: str, results: dict
    ) -> None:
        from .container import remove_worktree
        from pathlib import Path

        # Mark done
        self._mark_todo_done(project, todo_text)

        # Build notification
        pr_url = results.get("pr_url")
        branch = results.get("branch", "")
        truncated = todo_text[:40] + ("…" if len(todo_text) > 40 else "")
        if pr_url:
            self.notify(f"✓ {truncated} → PR: {pr_url}")
        else:
            self.notify(f"✓ {truncated} → branch: {branch}")

        # If there's a code review, write it as a comment on the PR
        review = results.get("review", "")
        if review and pr_url:
            self._post_review_comment(pr_url, review)

        # Clean up worktree and local branch
        wt_path = results.get("worktree")
        if wt_path:
            try:
                remove_worktree(project, Path(wt_path), branch)
            except Exception:
                pass

        # Clear active_todo if this was the last in-progress
        if project.in_progress_count == 0:
            project.active_todo_file.unlink(missing_ok=True)

        self._refresh_project_item(project)
        if self.selected_project and self.selected_project.path == project.path:
            self._refresh_panes()

        # Check if more todos to dispatch
        self._schedule_dispatch_check(project)

    def _on_dispatch_failed(
        self, project: Project, todo_text: str, error: Exception
    ) -> None:
        self.notify(
            f"Auto-dispatch failed for {project.name}: {error}",
            severity="error",
        )
        # Unclaim: revert - [~] back to - [ ]
        try:
            content = project.todos_file.read_text()
            content = content.replace(
                f"- [~] {todo_text}", f"- [ ] {todo_text}", 1
            )
            project.todos_file.write_text(content)
        except FileNotFoundError:
            pass
        project.active_todo_file.unlink(missing_ok=True)

        # Still try to dispatch remaining todos
        self._schedule_dispatch_check(project)

    def _post_review_comment(self, pr_url: str, review: str) -> None:
        """Post a code review comment on the PR using gh CLI."""
        import subprocess, shutil
        if not shutil.which("gh"):
            return
        try:
            subprocess.run(
                ["gh", "pr", "comment", pr_url, "--body", review],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            pass

    def on_unmount(self) -> None:
        # Stop dispatch timers
        for timer in self._dispatch_timers.values():
            timer.stop()
        self._dispatch_timers.clear()
        if self._observer:
            self._observer.stop()
            self._observer.join()
        # Stop bridge if running
        if self._bridge_running:
            from .bridge import stop_bridge
            stop_bridge()
