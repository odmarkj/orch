from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime


@dataclass
class Session:
    session_id: str
    project_path: Path
    status: str = "idle"
    status_updated: datetime | None = None

    @property
    def status_file(self) -> Path:
        return self.project_path / ".claude" / "status"

    def refresh_status(self) -> bool:
        """Read status file. Returns True if status changed."""
        try:
            text = self.status_file.read_text().strip()
            if text != self.status:
                self.status = text
                self.status_updated = datetime.now()
                return True
        except FileNotFoundError:
            pass
        return False


@dataclass
class Project:
    path: Path
    sessions: list[Session] = field(default_factory=list)
    _todos_cache: str = field(default="", repr=False)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def todos_file(self) -> Path:
        return self.path / "TODOS.md"

    @property
    def claude_dir(self) -> Path:
        return self.path / ".claude"

    @property
    def status_file(self) -> Path:
        return self.claude_dir / "status"

    @property
    def current_status(self) -> str:
        try:
            return self.status_file.read_text().strip()
        except FileNotFoundError:
            return ""

    @property
    def status_indicator(self) -> str:
        status = self.current_status
        if not status:
            return "idle"
        lower = status.lower()
        if any(w in lower for w in ["computing", "running", "fetching", "building", "parsing", "writing", "scanning"]):
            return "active"
        if any(w in lower for w in ["waiting", "blocked", "error", "failed"]):
            return "waiting"
        return "active"

    @property
    def todos_text(self) -> str:
        try:
            return self.todos_file.read_text()
        except FileNotFoundError:
            return "_No TODOS.md found._\n\nCreate one with:\n```\n## Pending\n- [ ] First task\n```"

    @property
    def pending_count(self) -> int:
        try:
            text = self.todos_file.read_text()
            return text.count("- [ ]")
        except FileNotFoundError:
            return 0

    @property
    def claude_md(self) -> str:
        try:
            return (self.path / "CLAUDE.md").read_text()
        except FileNotFoundError:
            return ""

    @property
    def container_id_file(self) -> Path:
        return self.claude_dir / "container_id"

    @property
    def container_id(self) -> str | None:
        try:
            cid = self.container_id_file.read_text().strip()
            return cid or None
        except FileNotFoundError:
            return None

    @property
    def has_devcontainer(self) -> bool:
        return (self.path / ".devcontainer" / "devcontainer.json").is_file()

    # ── Auto-dispatch properties ─────────────────────────────────────────────

    @property
    def active_todo_file(self) -> Path:
        return self.claude_dir / "active_todo"

    @property
    def active_todo(self) -> str | None:
        try:
            text = self.active_todo_file.read_text().strip()
            return text or None
        except FileNotFoundError:
            return None

    @property
    def auto_dispatch_file(self) -> Path:
        return self.claude_dir / "auto_dispatch"

    @property
    def auto_dispatch_enabled(self) -> bool:
        return self.auto_dispatch_file.exists()

    @property
    def in_progress_count(self) -> int:
        try:
            text = self.todos_file.read_text()
            return text.count("- [~]")
        except FileNotFoundError:
            return 0

    @property
    def first_pending_todo(self) -> str | None:
        try:
            for line in self.todos_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("- [ ] "):
                    return stripped[6:]
            return None
        except FileNotFoundError:
            return None
