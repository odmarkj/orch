"""Resumable Claude session discovery for the orch TUI `v` (Resume) picker.

A *session* is one ``.jsonl`` conversation log that Claude Code writes under::

    ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-uuid>.jsonl

Claude keys ``--resume`` on the working directory, so to resume a session we
relaunch ``claude --resume <session_id>`` from that session's original cwd —
which means the directory must still exist on disk.

We surface two kinds of session for a project:

  * ``root``     — cwd is the project root (the ``c`` shortcut). Every ``c``
                   press lands here, so the root dir accumulates them.
  * ``worktree`` — cwd is a ``w`` worktree, tracked in the ``worktrees`` table.
                   The table supplies the branch, base branch, and status, and
                   lets us skip worktrees whose directory was removed (their
                   history isn't resumable because the cwd is gone).

This module is pure host-filesystem reads (the TUI runs on the macOS host where
``Path.home()`` is the real ``~/.claude`` that the VM shares via virtiofs), plus
a read of the worktrees table. It is safe to call from the TUI process, which
already owns a state connection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import state

if TYPE_CHECKING:
    from .models import Project


# Bound the work: how many newest sessions to surface per source, and how many
# lines to scan in a single .jsonl before giving up on a human-readable preview.
_MAX_PER_SOURCE = 50
_PREVIEW_SCAN_LINES = 300
_PREVIEW_MAX_CHARS = 100


@dataclass
class SessionEntry:
    """One resumable Claude conversation."""

    session_id: str
    cwd: Path                 # original working dir — `claude --resume` runs here
    kind: str                 # "root" | "worktree"
    summary: str              # one-line human preview (first user message)
    mtime: float              # last-modified epoch seconds (for sort + age)
    jsonl_path: Path
    # Worktree-only metadata (None for root sessions):
    wt_id: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    wt_status: str | None = None


def _projects_base() -> Path:
    return Path.home() / ".claude" / "projects"


def _dash(path: Path | str) -> str:
    """Encode a cwd the way Claude Code names its project dir: / → -."""
    return str(path).replace("/", "-")


def _jsonl_dir_for(cwd: Path | str) -> Path:
    return _projects_base() / _dash(cwd)


def _first_user_preview(jsonl_path: Path) -> str:
    """Cheap one-line preview: the first human message in the conversation.

    Reads line-by-line and stops at the first user text block (usually within
    the first few lines), or after _PREVIEW_SCAN_LINES. Returns "" if none.
    """
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= _PREVIEW_SCAN_LINES:
                    break
                line = line.strip()
                if not line or '"user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            if text.strip():
                                break
                text = text.strip()
                if not text:
                    continue
                # Collapse to the first non-empty line, trim slash-command noise.
                first_line = next(
                    (ln.strip() for ln in text.splitlines() if ln.strip()), ""
                )
                if len(first_line) > _PREVIEW_MAX_CHARS:
                    first_line = first_line[: _PREVIEW_MAX_CHARS - 1] + "…"
                return first_line
    except OSError:
        pass
    return ""


def _scan_dir(jsonl_dir: Path) -> list[tuple[str, Path, float, str]]:
    """Return (session_id, jsonl_path, mtime, preview) for each log in a dir.

    Empty/aborted logs (no extractable preview AND tiny on disk) are skipped —
    they carry no resumable conversation worth showing.
    """
    if not jsonl_dir.is_dir():
        return []
    rows: list[tuple[str, Path, float, str]] = []
    files = [p for p in jsonl_dir.glob("*.jsonl") if p.is_file()]
    # Newest first, and cap so a busy project doesn't blow the scan budget.
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:_MAX_PER_SOURCE]:
        try:
            st = path.stat()
        except OSError:
            continue
        preview = _first_user_preview(path)
        if not preview and st.st_size < 1024:
            continue
        rows.append((path.stem, path, st.st_mtime, preview or "(no preview)"))
    return rows


def list_resumable_sessions(project: "Project") -> list[SessionEntry]:
    """All resumable sessions for a project — root and worktree — newest first.

    Sessions are de-duplicated by id (a session id belongs to exactly one cwd).
    Worktree sessions whose directory no longer exists are omitted.
    """
    entries: list[SessionEntry] = []
    seen: set[str] = set()

    # ── Root (`c`) sessions ────────────────────────────────────────────────
    root_dir = _jsonl_dir_for(project.path)
    for sid, path, mtime, preview in _scan_dir(root_dir):
        if sid in seen:
            continue
        seen.add(sid)
        entries.append(SessionEntry(
            session_id=sid, cwd=project.path, kind="root",
            summary=preview, mtime=mtime, jsonl_path=path,
        ))

    # ── Worktree (`w`) sessions ────────────────────────────────────────────
    for row in state.list_worktrees(project=project.name):
        wt_path = row.get("worktree_path")
        if not wt_path or not Path(wt_path).is_dir():
            continue  # dir removed → cwd gone → not resumable
        jsonl_dir = Path(row.get("jsonl_dir") or _jsonl_dir_for(wt_path))
        for sid, path, mtime, preview in _scan_dir(jsonl_dir):
            if sid in seen:
                continue
            seen.add(sid)
            entries.append(SessionEntry(
                session_id=sid, cwd=Path(wt_path), kind="worktree",
                summary=preview, mtime=mtime, jsonl_path=path,
                wt_id=row.get("id"), branch=row.get("branch"),
                base_branch=row.get("base_branch"), wt_status=row.get("status"),
            ))

    entries.sort(key=lambda e: e.mtime, reverse=True)
    return entries
