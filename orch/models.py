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
    def tmux_session(self) -> str:
        """Canonical tmux session name for this project inside the VM."""
        return f"orch-{self.name}"

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

    @property
    def pending_todos(self) -> list[str]:
        """Return all pending todo texts."""
        try:
            results = []
            for line in self.todos_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("- [ ] "):
                    results.append(stripped[6:])
            return results
        except FileNotFoundError:
            return []

    # ── JSONL session directory ─────────────────────────────────────────────

    @property
    def jsonl_dirs(self) -> list[Path]:
        """Return ~/.claude/projects/ dirs containing JSONL session files.

        With Lima VM, paths are identical to host paths, so we only need
        the host-style encoding.
        """
        base = Path.home() / ".claude" / "projects"
        host_dir = base / str(self.path).replace("/", "-").lstrip("-")
        dirs = []
        if host_dir.is_dir():
            dirs.append(host_dir)
        return dirs

    @property
    def waiting_for_input_file(self) -> Path:
        return self.claude_dir / "waiting_for_input"

    # ── Per-project config ────────────────────────────────────────────────────

    @property
    def orch_config_file(self) -> Path:
        return self.path / ".orch" / "project.toml"

    @property
    def code_review_enabled(self) -> bool:
        """Check if code review is enabled for this project (off by default)."""
        return self._read_orch_config_bool("code_review", False)

    @property
    def test_cmd(self) -> str | None:
        """Test command to run after auto-dispatch tasks. Checks .orch/project.toml first, then auto-detects."""
        explicit = self._read_orch_config_str("test_cmd")
        if explicit is not None:
            return explicit
        return self._detect_test_cmd()

    def _detect_test_cmd(self) -> str | None:
        """Auto-detect test command from project files."""
        import json

        # package.json → npm test (only if script exists and isn't the default placeholder)
        pkg = self.path / "package.json"
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text()).get("scripts", {})
                test_script = scripts.get("test", "")
                if test_script and "no test specified" not in test_script:
                    return "npm test"
            except (json.JSONDecodeError, OSError):
                pass

        # pyproject.toml → pytest
        if (self.path / "pyproject.toml").is_file():
            return "pytest -x"

        # Makefile with test target
        makefile = self.path / "Makefile"
        if makefile.is_file():
            try:
                for line in makefile.read_text().splitlines():
                    if line.startswith("test:") or line.startswith("test "):
                        return "make test"
            except OSError:
                pass

        # Cargo.toml → cargo test
        if (self.path / "Cargo.toml").is_file():
            return "cargo test"

        # go.mod → go test
        if (self.path / "go.mod").is_file():
            return "go test ./..."

        return None

    @property
    def max_fix_attempts(self) -> int:
        """Max times Claude will retry fixing failed tests (default 3)."""
        val = self._read_orch_config_str("max_fix_attempts")
        if val is not None:
            try:
                return int(val)
            except ValueError:
                pass
        return 3

    # ── Session lifecycle hooks ────────────────────────────────────────────

    @property
    def on_first_session_hook(self) -> str | None:
        """Command to run when the first session starts."""
        return self._read_orch_config_section_str("hooks", "on_first_session")

    @property
    def on_last_session_hook(self) -> str | None:
        """Command to run when the last session ends."""
        return self._read_orch_config_section_str("hooks", "on_last_session")

    def _read_orch_config_section_str(self, section: str, key: str) -> str | None:
        """Read a key from a specific [section] in .orch/project.toml."""
        try:
            in_section = False
            for line in self.orch_config_file.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("[") and stripped.endswith("]"):
                    in_section = stripped[1:-1].strip() == section
                    continue
                if in_section and stripped.startswith(key) and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        return None

    def _read_orch_config_str(self, key: str) -> str | None:
        try:
            for line in self.orch_config_file.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith(key) and "=" in stripped:
                    return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        except FileNotFoundError:
            pass
        return None

    def _read_orch_config_bool(self, key: str, default: bool = False) -> bool:
        val = self._read_orch_config_str(key)
        if val is not None:
            return val.lower() == "true"
        return default

    # ── Bridge properties ────────────────────────────────────────────────

    @property
    def bridge_request_file(self) -> Path:
        return self.claude_dir / "bridge_request"

    @property
    def bridge_responses_dir(self) -> Path:
        return self.claude_dir / "bridge_responses"

    @property
    def bridge_requests_dir(self) -> Path:
        return self.claude_dir / "bridge_requests"

    @property
    def has_pending_bridge_request(self) -> bool:
        return self.bridge_request_file.exists()

    @property
    def bridge_depth(self) -> int:
        """Current bridge depth (0 = not in a bridge context)."""
        depth_file = self.claude_dir / "_bridge_depth"
        try:
            return int(depth_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            return 0
