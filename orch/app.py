from __future__ import annotations

import json
import random
import time
import threading
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
from .iterm import notify_input_needed, notify_resumed, clear_stale_handle
from .vm import vm_ensure_running
from .agent import session_exists, kill_session


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

SESSION_ICON = "[bold blue]■[/]"
SESSION_ICON_OFF = "[dim]□[/]"


class StatusFileHandler(FileSystemEventHandler):
    """Watchdog handler — only fires for .claude/ files and TODOS.md."""

    def __init__(self, callback: Callable[[str], None]):
        self._cb = callback

    @staticmethod
    def _relevant(path: str) -> bool:
        """Only care about .claude/ contents and TODOS.md — ignore everything else."""
        return "/.claude/" in path or path.endswith("/TODOS.md")

    def on_modified(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._cb(event.src_path)

    def on_created(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._cb(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._cb(event.src_path)


class SessionJournalHandler(FileSystemEventHandler):
    """Watchdog handler for JSONL session files under ~/.claude/projects/."""

    def __init__(self, callback: Callable[[str], None]):
        self._cb = callback

    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith(".jsonl"):
            self._cb(event.src_path)


def _tail_read_jsonl(path: Path, chunk_size: int = 8192) -> list[dict]:
    """Read the last few JSON lines from a JSONL file efficiently.

    Seeks to the end minus *chunk_size* bytes, reads forward, and parses
    complete lines.  Returns entries in order (oldest first).
    """
    try:
        size = path.stat().st_size
        if size == 0:
            return []
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk_size))
            data = f.read().decode("utf-8", errors="replace")
        entries: list[dict] = []
        for line in data.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # partial first line after seek
        return entries
    except (OSError, ValueError):
        return []


# ─��� Widgets ──────────────────────────────────────────────���────────────────────

class ProjectItem(ListItem):
    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        yield Label(self._build_label(), markup=True)

    def _build_label(self) -> str:
        indicator = INDICATOR.get(self.project.status_indicator, INDICATOR["idle"])
        app = self.app
        has_session = app._has_session_cached(self.project) if isinstance(app, OrchApp) else False
        sbox = SESSION_ICON if has_session else SESSION_ICON_OFF
        auto = "[bold magenta]⚡[/]" if self.project.auto_dispatch_enabled else ""
        count = self.project.pending_count
        badge = f" [dim]{count}t[/]" if count else ""
        return f"{indicator}{sbox}{auto} {self.project.name}{badge}"

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

        # Status from .claude/status (written by JSONL journal handler or Claude)
        status = project.current_status
        indicator = project.status_indicator
        if status:
            color = {"active": "green", "waiting": "yellow"}.get(indicator, "dim")
            dot = {"active": "●", "waiting": "●"}.get(indicator, "○")
            status_line = f"[bold {color}]{dot} {status}[/]"
        else:
            status_line = "[dim]○ No status yet[/]"

        # Session existence from cache
        app = self.app
        has_session = app._has_session_cached(project) if isinstance(app, OrchApp) else False
        if has_session:
            session_line = f"\n[bold blue]■[/] {project.tmux_session}"
        else:
            session_line = "\n[dim]□ No session — press [bold]c[/dim] to start[/]"

        # Pull first non-blank line of CLAUDE.md as the project abstract
        abstract = ""
        claude_md = project.claude_md
        for line in claude_md.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                abstract = line
                break

        abstract_line = f"\n[dim]{abstract}[/]" if abstract else ""
        path_line = f"\n\n[dim]{project.path}[/]"
        error_line = ""
        if self._error:
            error_line = f"\n\n[bold red]Error:[/]\n[red]{self._error}[/]"

        self.update(f"{status_line}{session_line}{abstract_line}{path_line}{error_line}")


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
    from .iterm import _load_config, _tab_exists, _bring_tab_to_front, _run_iterm_script, _iterm_badge_cmd

    handle_file = project.claude_dir / "iterm_log_handle"

    tab_name     = f"{project.name} logs"
    badge        = _iterm_badge_cmd(project.name)

    if handle_file.exists():
        tty = handle_file.read_text().strip()
        alive = _tab_exists(tty) if tty else False
        if alive is True:
            _bring_tab_to_front(tty)
            return
        if alive is None:
            return  # Check failed — keep handle, don't open duplicate
        handle_file.unlink(missing_ok=True)

    cfg          = _load_config()
    profile      = cfg["iterm"].get("profile", "orch")
    dedicated    = cfg["iterm"].get("dedicated_window", True)
    window_title = cfg["iterm"].get("window_title", "orch sessions")
    cmd          = f"orch logs {project.name}"

    script = _build_iterm_tab_script(
        profile=profile, dedicated=dedicated, window_title=window_title,
        tab_name=tab_name, cmd=cmd, badge=project.name,
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
                             tab_name: str, cmd: str, badge: str = "") -> str:
    """Build AppleScript to open an iTerm2 tab with profile fallback."""
    from .iterm import _applescript_quote
    badge_line = f'set badge to "{badge}"' if badge else ""
    if badge:
        from .iterm import _iterm_badge_cmd
        cmd = f"{_iterm_badge_cmd(badge)} && {cmd}"
    cmd = _applescript_quote(cmd)
    if dedicated:
        return f"""
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
        max-height: 4;
        padding: 0 2;
        background: $panel;
        border-top: solid $primary;
        color: $text;
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
        Binding("c", "session_start", "Claude", show=True),
        Binding("x", "vm_shell", "VM Shell", show=True),
        Binding("d", "session_stop_press", "Stop(dd)", show=True),
        Binding("l", "open_logs", "Logs", show=True),
        Binding("p", "open_plan", "Plan", show=True),
        Binding("b", "toggle_bridge", "Bridge", show=True),
        Binding("s", "set_stage", "Stage", show=True),
        Binding("i", "ignore_project", "Ignore", show=True),
        Binding("g", "toggle_auto_dispatch", "Auto(g)", show=True),
        Binding("o", "edit_config", "Config", show=True),
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
        # Debounce: coalesce rapid file-change events per project
        self._debounce_timers: dict[str, threading.Timer] = {}
        self._debounce_lock = threading.Lock()
        self._debounce_delay: float = 0.3  # seconds
        self._wfi_last_fired: dict[str, float] = {}  # per-project cooldown for waiting_for_input
        self._wfi_cooldown: float = 3.0  # seconds — suppress duplicate iTerm opens
        # JSONL journal watcher state
        self._journal_debounce_timers: dict[str, threading.Timer] = {}
        self._journal_debounce_delay: float = 0.5  # seconds — short debounce for live status
        self._jsonl_dir_to_project: dict[str, Project] = {}
        # Session existence cache: project name -> bool (refreshed periodically)
        self._session_cache: dict[str, bool] = {}

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
            "[dim]j/k[/] navigate  [dim]Enter[/] select  [dim]t[/]ask  [dim]a[/]dd todo  [dim]e[/]xec  [dim]c[/]laude  [dim]x[/] vm shell  [dim]dd[/] stop\n"
            "[dim]l[/]ogs  [dim]p[/]lan  [dim]b[/]ridge  [dim]s[/]tage  [dim]i[/]gnore  [dim]g[/] auto  [dim]o[/] config  [dim]r[/]efresh  [dim]q[/]uit  [dim]Esc[/] cancel",
            id="help-bar",
            markup=True,
        )

    def on_mount(self) -> None:
        self.projects = discover_projects()
        self._populate_list()
        self._start_watcher()
        # Clear any stale handles left from a previous orch session
        self.run_worker(
            lambda: [clear_stale_handle(p) for p in self.projects],
            thread=True,
        )
        if self.projects:
            self.query_one("#project-list", ListView).focus()
        # Check if we should start in mobile mode
        self._check_mobile(self.size.width)
        # Refresh session state periodically (every 5 seconds)
        self.set_interval(5.0, self._refresh_session_cache)
        # Initial session cache population
        self.run_worker(self._do_refresh_session_cache, thread=True)

    def _refresh_session_cache(self) -> None:
        """Trigger a background refresh of session state via tmux capture."""
        self.run_worker(self._do_refresh_session_cache, thread=True)

    def _do_refresh_session_cache(self) -> None:
        """Query tmux sessions in the VM and update the cache.

        Detached sessions (iTerm window closed) are auto-killed since
        they're always stale in our model — the user closed the window.
        Also fires session lifecycle hooks on transitions.
        """
        from .agent import list_sessions, kill_session, fire_first_session_hook, fire_last_session_hook, _kill_session_tree
        from .vm import vm_is_running, vm_exec

        if not vm_is_running():
            if self._session_cache:
                # Fire hooks for any sessions that were active
                for p in self.projects:
                    if self._session_cache.get(p.name, False):
                        fire_last_session_hook(p)
                self._session_cache.clear()
                self.call_from_thread(self._refresh_all_labels)
            return

        try:
            active = list_sessions()
        except Exception:
            return  # VM unreachable, keep stale cache

        # Kill detached sessions — they're stale (iTerm window was closed).
        # Use _kill_session_tree to also kill the sandboxed process tree,
        # since signals don't propagate through sudo/unshare/su.
        for s in active:
            if not s["attached"]:
                _kill_session_tree(s["name"])

        # Only count attached sessions as active
        active_names = {s["project"] for s in active if s["attached"]}

        changed = False
        for p in self.projects:
            was = self._session_cache.get(p.name, False)
            now = p.name in active_names
            if was != now:
                changed = True
                # Fire lifecycle hooks on transitions
                if now and not was:
                    fire_first_session_hook(p)
                elif was and not now:
                    fire_last_session_hook(p)
            self._session_cache[p.name] = now

        if changed:
            self.call_from_thread(self._refresh_all_labels)

    def _has_session_cached(self, project) -> bool:
        """Check if a session exists (from cache)."""
        return self._session_cache.get(project.name, False)

    def _refresh_all_labels(self) -> None:
        """Refresh all project list labels and panes."""
        lv = self.query_one("#project-list", ListView)
        for item in lv.query(ProjectItem):
            item.refresh_label()
        if self.selected_project:
            self._refresh_panes()

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
        journal_handler = SessionJournalHandler(self._on_journal_changed)
        self._observer = Observer()
        watched = set()
        for p in self.projects:
            # Only watch .claude/ dir (not the entire project tree).
            # Watching whole project roots with recursive=True causes massive
            # CPU usage because Docker virtiofs propagates every container
            # file change to macOS FSEvents.
            claude_dir = p.claude_dir
            claude_dir.mkdir(parents=True, exist_ok=True)
            claude_str = str(claude_dir)
            if claude_str not in watched:
                self._observer.schedule(handler, claude_str, recursive=False)
                watched.add(claude_str)
            # Also watch TODOS.md parent (project root) non-recursively
            project_str = str(p.path)
            if project_str not in watched:
                self._observer.schedule(handler, project_str, recursive=False)
                watched.add(project_str)
            # Watch JSONL session directories for this project
            for jdir in p.jsonl_dirs:
                jdir_str = str(jdir)
                if jdir_str not in watched:
                    self._observer.schedule(journal_handler, jdir_str, recursive=False)
                    watched.add(jdir_str)
                    self._jsonl_dir_to_project[jdir_str] = p
        self._observer.start()

    def _on_file_changed(self, path: str) -> None:
        """Called from watchdog thread — debounce rapid events per project."""
        # Find which project this path belongs to for debounce grouping
        p = Path(path)
        key = path  # default: per-file debounce
        for proj in self.projects:
            try:
                if p.is_relative_to(proj.path):
                    key = str(proj.path)
                    break
            except ValueError:
                pass

        # Immediate dispatch for high-priority files (input waiting)
        if p.name == "waiting_for_input":
            # Cooldown: Stop + Notification hooks both write this file, and
            # watchdog may fire create+modify events for each write.  Without
            # a guard, rapid events can open duplicate iTerm windows.
            if p.name == "waiting_for_input" and p.exists():
                proj_key = str(p.parent.parent)  # .claude -> project root
                now = time.monotonic()
                with self._debounce_lock:
                    last = self._wfi_last_fired.get(proj_key, 0.0)
                    if now - last < self._wfi_cooldown:
                        return  # suppress duplicate
                    self._wfi_last_fired[proj_key] = now
            self.call_from_thread(self._handle_file_change, path)
            return

        with self._debounce_lock:
            existing = self._debounce_timers.get(key)
            if existing:
                existing.cancel()
            timer = threading.Timer(
                self._debounce_delay,
                lambda: self.call_from_thread(self._handle_file_change, path),
            )
            timer.daemon = True
            self._debounce_timers[key] = timer
            timer.start()

    def _handle_file_change(self, path: str) -> None:
        changed = Path(path)

        # ── waiting_for_input created: Claude needs you ──────────────────────
        if changed.name == "waiting_for_input" and changed.exists():
            project = self._project_for_path(changed)
            if project:
                question = changed.read_text().strip()
                def _handle_input(p=project, q=question):
                    notify_input_needed(p, q)
                    # Don't auto-open iTerm tabs — just notify.
                    # User can open a session manually via keybinding.

                self.run_worker(_handle_input, thread=True)
                self._refresh_project_item(project)
                if self.selected_project == project:
                    self._refresh_panes()
            return

        # ── waiting_for_input deleted: Claude resumed ─────────────────────────
        if changed.name == "waiting_for_input" and not changed.exists():
            # Clear cooldown so next stop event can fire immediately
            proj_key = str(changed.parent.parent)
            with self._debounce_lock:
                self._wfi_last_fired.pop(proj_key, None)
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

        # ── Bridge request: cross-project agent communication ─────────────────
        if changed.name == "bridge_request" and changed.exists():
            project = self._project_for_path(changed)
            if project:
                self._handle_bridge_request(project)
            return

        # ── iterm handles: no action needed ───────────────────────────────────
        if changed.name in ("iterm_handle", "iterm_log_handle"):
            return

        # ── Auto-dispatch internal files: skip refresh ───────────────────────
        if changed.name in ("active_todo", "auto_dispatch"):
            return

        # ── General file change: status, todos, etc. ──────────────────────────
        self._refresh_project_item_for_path(changed)
        if self.selected_project and changed.is_relative_to(self.selected_project.path):
            self._refresh_panes()

        # ── Auto-dispatch check on TODOS.md changes ──────────────────────────
        if changed.name == "TODOS.md":
            project = self._project_for_path(changed)
            if project:
                self._schedule_dispatch_check(project)

    # ── JSONL session journal detection ─────────────────────────────────────

    def _on_journal_changed(self, path: str) -> None:
        """Called from watchdog thread when a JSONL session file is modified.

        Debounces with a 3-second delay so we only check after Claude's turn
        has fully settled (thinking → tool_use → text → system/turn_duration).
        """
        with self._debounce_lock:
            existing = self._journal_debounce_timers.get(path)
            if existing:
                existing.cancel()
            timer = threading.Timer(
                self._journal_debounce_delay,
                lambda: self._check_journal_state(path),
            )
            timer.daemon = True
            self._journal_debounce_timers[path] = timer
            timer.start()

    def _check_journal_state(self, path: str) -> None:
        """Tail-read a JSONL session file and detect if Claude is waiting.

        Uses the waiting_for_input file as a dedup gate: only writes it if
        it doesn't already exist, so duplicate notifications are impossible.
        Also derives a live status string and writes it to .claude/status
        so the StatusPane always reflects what Claude is doing.
        """
        jsonl_path = Path(path)
        # Map JSONL directory back to project
        parent_str = str(jsonl_path.parent)
        project = self._jsonl_dir_to_project.get(parent_str)
        if not project:
            return

        entries = _tail_read_jsonl(jsonl_path)
        if not entries:
            return

        # Walk backwards to find the last meaningful entry
        # (skip file-history-snapshot and progress noise)
        last_meaningful = None
        prev_assistant_text = None
        last_tool_use = None
        for entry in reversed(entries):
            etype = entry.get("type")
            if etype in ("file-history-snapshot", "progress"):
                continue
            if last_meaningful is None:
                last_meaningful = entry
            # Track last assistant entry with tool_use for status derivation
            if etype == "assistant" and last_tool_use is None:
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "tool_use":
                            last_tool_use = c.get("name", "")
                            break
            # Also find the last assistant/text entry (may precede system entry)
            if (etype == "assistant" and prev_assistant_text is None):
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, list):
                    ctypes = [c.get("type") for c in content if isinstance(c, dict)]
                    if "text" in ctypes:
                        prev_assistant_text = entry
                elif isinstance(content, str):
                    prev_assistant_text = entry
            if last_meaningful and prev_assistant_text and last_tool_use is not None:
                break

        if not last_meaningful:
            return

        wfi = project.waiting_for_input_file
        etype = last_meaningful.get("type")
        subtype = last_meaningful.get("subtype", "")

        # ── Derive live status from journal state ───────────────────────
        status = self._derive_status(last_meaningful, last_tool_use)
        if status:
            self._write_status(project, status)

        # Turn complete: system/turn_duration follows assistant/text
        if etype == "system" and subtype == "turn_duration" and prev_assistant_text:
            if not wfi.exists():
                # Extract tail of Claude's last message for notification context
                content = prev_assistant_text.get("message", {}).get("content", [])
                text = ""
                if isinstance(content, list):
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    text = " ".join(parts).strip()[-300:]
                elif isinstance(content, str):
                    text = content.strip()[-300:]
                # Ensure .claude/ dir exists and write the gate file.
                # The existing StatusFileHandler watchdog will detect the
                # creation and fire _handle_file_change → notify_input_needed.
                wfi.parent.mkdir(parents=True, exist_ok=True)
                wfi.write_text(text or "Waiting for input")
            return

        # Activity resumed: user message or new assistant work after we notified
        if etype in ("user", "assistant") and wfi.exists():
            # Don't clear on tool_result — that's mid-turn tooling
            msg = last_meaningful.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, str):
                # Actual user prompt text — activity resumed.
                # Deletion is detected by StatusFileHandler → notify_resumed.
                try:
                    wfi.unlink()
                except FileNotFoundError:
                    pass

    # Tool name → human-readable action
    _TOOL_LABELS = {
        "Read":         "Reading files",
        "Edit":         "Editing code",
        "Write":        "Writing files",
        "Bash":         "Running commands",
        "Grep":         "Searching code",
        "Glob":         "Finding files",
        "Agent":        "Running subagent",
        "WebFetch":     "Fetching web content",
        "WebSearch":    "Searching the web",
        "NotebookEdit": "Editing notebook",
        "TodoWrite":    "Updating tasks",
        "TaskCreate":   "Creating tasks",
        "TaskUpdate":   "Updating tasks",
    }

    def _derive_status(self, last_entry: dict, last_tool: str | None) -> str:
        """Derive a human-readable status string from the latest journal entry.

        Includes the target of the action when available (file path, command).
        """
        etype = last_entry.get("type")
        subtype = last_entry.get("subtype", "")

        if etype == "system" and subtype == "turn_duration":
            return "Waiting for input"

        if etype == "system" and subtype == "stop_hook_summary":
            return "Waiting for input"

        if etype == "user":
            return "Working"

        if etype == "assistant":
            content = last_entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                # Check for tool_use — extract the target
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tool = c.get("name", "")
                        target = self._extract_tool_target(tool, c.get("input", {}))
                        label = self._TOOL_LABELS.get(tool, f"Using {tool}")
                        if target:
                            return f"{label}: {target}"
                        return label
                # Check for thinking
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "thinking":
                        return "Thinking"
                # Text-only assistant message
                return "Responding"

            if last_tool:
                return self._TOOL_LABELS.get(last_tool, f"Using {last_tool}")

            return "Working"

        return ""

    @staticmethod
    def _extract_tool_target(tool: str, input_data: dict) -> str:
        """Extract a short target description from a tool_use input."""
        if not isinstance(input_data, dict):
            return ""

        if tool in ("Read", "Edit", "Write"):
            path = input_data.get("file_path", "")
            if path:
                # Show just the filename, or last 2 path components
                parts = path.rsplit("/", 2)
                return "/".join(parts[-2:]) if len(parts) > 2 else path

        if tool == "Bash":
            cmd = input_data.get("command", "")
            if cmd:
                # First 60 chars of the command
                return cmd[:60] + ("…" if len(cmd) > 60 else "")

        if tool in ("Grep", "Glob"):
            pattern = input_data.get("pattern", "")
            if pattern:
                return f"'{pattern}'"

        if tool == "Agent":
            desc = input_data.get("description", "")
            if desc:
                return desc[:50]

        return ""

    def _write_status(self, project: Project, status: str) -> None:
        """Write status to .claude/status and refresh the UI if it changed."""
        status_file = project.status_file
        try:
            existing = status_file.read_text().strip()
        except FileNotFoundError:
            existing = ""
        if existing == status:
            return
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(status)
        # Refresh the status pane from the main thread
        self.call_from_thread(self._refresh_project_item, project)
        if self.selected_project == project:
            self.call_from_thread(self._refresh_panes)

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
            # On mobile, auto-switch to the status tab after selecting a project
            if self._mobile:
                self._apply_tab(1)

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


    def action_session_start(self) -> None:
        """Open an iTerm2 window with Claude running in the VM."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Launching Claude in VM", p)

        def _launch():
            try:
                vm_ensure_running()
                from .iterm import open_vm_session
                open_vm_session(p, with_shell=True)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Claude launched for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Launch failed: {e}", "error")

        self.run_worker(_launch, thread=True)

    def action_vm_shell(self) -> None:
        """Open an iTerm2 window with a shell in the VM at the project dir."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Opening VM shell", p)

        def _launch():
            try:
                vm_ensure_running()
                from .iterm import open_vm_shell
                open_vm_shell(p)
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Shell opened for {p.name}")
            except Exception as e:
                self.call_from_thread(self._stop_spinner_and_refresh, p,
                                      f"Shell failed: {e}", "error")

        self.run_worker(_launch, thread=True)

    def action_session_stop_press(self) -> None:
        """First d press primes, second d within 0.5s stops the session."""
        if self._input_focused: return
        if self._d_pressed:
            self._d_pressed = False
            if self._d_timer:
                self._d_timer.stop()
                self._d_timer = None
            self._do_session_stop()
        else:
            self._d_pressed = True
            self.notify("Press [bold]d[/] again to stop session", markup=True)
            self._d_timer = self.set_timer(0.8, self._reset_d_press)

    def _reset_d_press(self) -> None:
        self._d_pressed = False
        self._d_timer = None

    def _do_session_stop(self) -> None:
        """Kill the tmux session for the selected project."""
        p = self.selected_project
        if not p:
            self.notify("No project selected", severity="warning")
            return
        if not session_exists(p):
            self.notify(f"No session running for {p.name}", severity="warning")
            return

        kill_session(p)
        # Update cache immediately so the poller doesn't double-fire the hook
        self._session_cache[p.name] = False
        self.notify(f"Session stopped for {p.name}")
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
                from .iterm import open_input_tab
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

    def action_edit_config(self) -> None:
        """Open ~/.orch/config.toml in vi in an iTerm2 tab."""
        if self._input_focused: return
        if self._mobile:
            self.notify("Not available on mobile — use the bridge instead", severity="warning")
            return

        config_file = Path.home() / ".orch" / "config.toml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        if not config_file.exists():
            config_file.write_text(
                "# Orch configuration\n"
                "#\n"
                "# Settings are grouped by section (e.g. [vm], [iterm]).\n"
                "# Each setting must appear under the correct [section] header —\n"
                "# a setting placed under the wrong header will be ignored.\n\n"
                "[vm]\n"
                "# name = \"orch\"\n\n"
                "[iterm]\n"
                "# profile = \"orch\"\n"
                "# dedicated_window = true\n"
            )

        from .iterm import _load_config, _run_iterm_script

        cfg = _load_config()
        profile = cfg["iterm"].get("profile", "orch")
        dedicated = cfg["iterm"].get("dedicated_window", True)
        window_title = cfg["iterm"].get("window_title", "orch sessions")

        script = _build_iterm_tab_script(
            profile=profile, dedicated=dedicated, window_title=window_title,
            tab_name="orch config", cmd=f"vi {config_file}",
        )
        _run_iterm_script(script)
        self.notify("Config opened in iTerm2")

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
        """Fire-and-forget: run Claude headlessly in the VM with the task."""
        from .agent import run_headless

        pane = self.query_one("#status-pane", StatusPane)
        pane.start_spinner("Running task in background", project)

        def _run():
            try:
                vm_ensure_running()
                run_headless(project, task)
                self.call_from_thread(self._stop_spinner_and_refresh, project,
                                      f"Task completed for {project.name}")
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
        from .agent import run_task_in_worktree

        key = str(project.path)
        self._dispatch_timers.pop(key, None)

        if not project.auto_dispatch_enabled:
            return

        # How many slots are available?
        max_parallel = 3
        config_file = Path.home() / ".orch" / "config.toml"
        if config_file.exists():
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
                    if key.strip() == "max_parallel":
                        try:
                            max_parallel = int(val.strip().strip('"').strip("'"))
                        except ValueError:
                            pass
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
        from .agent import remove_worktree

        # Mark done
        self._mark_todo_done(project, todo_text)

        # Build notification
        pr_url = results.get("pr_url")
        branch = results.get("branch", "")
        truncated = todo_text[:40] + ("…" if len(todo_text) > 40 else "")
        test_info = ""
        if results.get("tests_passed") is True:
            attempts = results.get("test_attempts", 1)
            test_info = f" (tests passed, {attempts} attempt{'s' if attempts > 1 else ''})"
        elif results.get("tests_passed") is False:
            attempts = results.get("test_attempts", 0)
            test_info = f" (tests FAILED after {attempts} attempts)"
        if pr_url:
            self.notify(f"✓ {truncated}{test_info} → PR: {pr_url}")
        else:
            self.notify(f"✓ {truncated}{test_info} → branch: {branch}")

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

    # ── Bridge handlers ────────────────────────────────────────────────────

    def _handle_bridge_request(self, source_project: Project) -> None:
        """Handle a bridge_request file created by an agent."""
        from .comm import (
            parse_bridge_request,
            handle_bridge_request,
            MAX_BRIDGE_DEPTH,
            BridgeResponse,
            _deliver_response,
            _archive_request_file,
        )

        request = parse_bridge_request(source_project)
        if not request:
            self.notify(
                f"Bridge: invalid request from {source_project.name}",
                severity="warning",
            )
            return

        # Depth limit
        if request.depth > MAX_BRIDGE_DEPTH:
            self.notify(
                f"Bridge: {source_project.name} rejected (max depth exceeded)",
                severity="warning",
            )
            resp = BridgeResponse(
                id=request.id,
                source=request.source_project,
                target=request.target,
                intent=request.intent,
                summary=request.summary,
                status="failed",
                result="Max bridge depth exceeded.",
            )
            _deliver_response(request, resp)
            _archive_request_file(request)
            return

        truncated = request.summary[:50] + ("…" if len(request.summary) > 50 else "")
        self.notify(f"Bridge: {source_project.name} → {request.target}: {truncated}")

        def _run(req=request):
            try:
                response = handle_bridge_request(req, self.projects)
                self.call_from_thread(
                    self._on_bridge_complete, source_project, req, response,
                )
            except Exception as e:
                self.call_from_thread(
                    self._on_bridge_failed, source_project, req, e,
                )

        self.run_worker(_run, thread=True)

    def _on_bridge_complete(
        self,
        source: Project,
        request,
        response,
    ) -> None:
        status_icon = "✓" if response.status == "completed" else "✗"
        truncated = request.summary[:40] + ("…" if len(request.summary) > 40 else "")
        msg = f"Bridge {status_icon}: {truncated} → {response.status}"
        if response.pr_url:
            msg += f" PR: {response.pr_url}"
        self.notify(msg)

    def _on_bridge_failed(
        self,
        source: Project,
        request,
        error: Exception,
    ) -> None:
        from .comm import BridgeResponse, _deliver_response, _archive_request_file

        self.notify(
            f"Bridge failed: {request.summary[:40]} — {error}",
            severity="error",
        )
        # Deliver a failed response so the source agent knows
        resp = BridgeResponse(
            id=request.id,
            source=request.source_project,
            target=request.target,
            intent=request.intent,
            summary=request.summary,
            status="failed",
            result=str(error),
        )
        _deliver_response(request, resp)
        _archive_request_file(request)

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

    def action_quit(self) -> None:
        """Override quit to prompt about active sessions."""
        from .agent import list_sessions, kill_session

        sessions = list_sessions()
        if not sessions:
            self.exit()
            return

        count = len(sessions)
        names = ", ".join(s["project"] for s in sessions)
        self.notify(
            f"{count} active session(s): {names}",
            title="Kill sessions? (y = kill & quit, n = quit, Esc = cancel)",
            timeout=30,
        )
        self._pending_quit_sessions = sessions
        self.set_timer(0.1, lambda: self._install_quit_keybindings())

    def _install_quit_keybindings(self) -> None:
        """Temporarily listen for y/n/Esc after quit prompt."""
        self._quit_key_handler_active = True

    def on_key(self, event) -> None:
        if not getattr(self, "_quit_key_handler_active", False):
            return
        key = event.key
        if key == "y":
            self._quit_key_handler_active = False
            from .agent import kill_session
            for s in self._pending_quit_sessions:
                # Find matching project to call kill_session
                for p in self.projects:
                    if p.name == s["project"]:
                        kill_session(p)
                        break
            self.notify("Sessions killed.", timeout=2)
            self.set_timer(0.3, lambda: self.exit())
            event.prevent_default()
            event.stop()
        elif key == "n":
            self._quit_key_handler_active = False
            self.exit()
            event.prevent_default()
            event.stop()
        elif key == "escape":
            self._quit_key_handler_active = False
            self.notify("Quit cancelled.", timeout=2)
            event.prevent_default()
            event.stop()

    def on_unmount(self) -> None:
        # Stop dispatch timers
        for timer in self._dispatch_timers.values():
            timer.stop()
        self._dispatch_timers.clear()
        # Stop debounce timers
        with self._debounce_lock:
            for timer in self._debounce_timers.values():
                timer.cancel()
            self._debounce_timers.clear()
        if self._observer:
            self._observer.stop()
            self._observer.join()
        # Stop bridge if running
        if self._bridge_running:
            from .bridge import stop_bridge
            stop_bridge()
