from pathlib import Path
from .models import Project


SITES_ROOT = Path.home() / "Sites"


def _is_ignored(project_path: Path) -> bool:
    """Check if .orch/project.toml has ignored = true."""
    toml_path = project_path / ".orch" / "project.toml"
    if not toml_path.exists():
        return False
    try:
        for line in toml_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("ignored") and "=" in stripped:
                val = stripped.partition("=")[2].strip().strip('"').strip("'").lower()
                if val == "true":
                    return True
    except OSError:
        pass
    return False


def discover_projects(root: Path = SITES_ROOT) -> list[Project]:
    """
    Auto-discover projects from ~/Sites/* where a .claude/ directory exists.
    Skips projects with ignored = true in .orch/project.toml.
    Sorted alphabetically. No manual registration needed.
    """
    if not root.exists():
        return []

    projects = []
    for candidate in sorted(root.iterdir()):
        if candidate.is_dir() and (candidate / ".claude").is_dir():
            if not _is_ignored(candidate):
                projects.append(Project(path=candidate))

    return projects


def get_watch_paths(projects: list[Project]) -> list[Path]:
    """All paths the file watcher should monitor."""
    paths = []
    for p in projects:
        paths.append(p.claude_dir)
        if p.todos_file.exists():
            paths.append(p.todos_file)
    return paths
