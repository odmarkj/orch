"""Regression tests for the `v` (resume) picker's session discovery.

The bug (found 2026-10-13): orch computed the Claude Code project-log
directory as `cwd.replace("/", "-")`, but Claude Code actually replaces
*every* non-alphanumeric character with `-`. Every worktree cwd contains
`.orch-worktrees` (a dot) and `wt_<id>` (an underscore), so the computed
directory never matched the one Claude actually wrote to — worktree
sessions were always invisible to the resume picker and to the live
status watcher, even though their `.jsonl` logs existed right next to the
(empty) directory orch was looking in.

Verified against real directories under `~/.claude/projects/` for a
worktree whose cwd was
`/Users/joshuaodmark/Apps/.orch-worktrees/orch/wt_01a136ab11f8cd5da950`:
Claude Code wrote to
`-Users-joshuaodmark-Apps--orch-worktrees-orch-wt-01a136ab11f8cd5da950`
(note the doubled `-orch` from the `.` and the dashed `wt-...` from the
`_`), while the old `/`-only replace computed
`-Users-joshuaodmark-Apps-.orch-worktrees-orch-wt_01a136ab11f8cd5da950` —
a directory Claude never writes to.
"""

import json

from orch.models import Project
from orch.sessions import claude_project_dirname, list_resumable_sessions
import orch.sessions as sessions_mod
import orch.state as state


def test_claude_project_dirname_dashes_dots_and_underscores():
    cwd = "/Users/joshuaodmark/Apps/.orch-worktrees/orch/wt_01a136ab11f8cd5da950"
    assert claude_project_dirname(cwd) == (
        "-Users-joshuaodmark-Apps--orch-worktrees-orch-wt-01a136ab11f8cd5da950"
    )


def test_claude_project_dirname_plain_path_unchanged_but_for_slashes():
    # No dots/underscores: old and new behavior agree here, so this must
    # keep working exactly as before.
    cwd = "/Users/joshuaodmark/Apps/orch"
    assert claude_project_dirname(cwd) == "-Users-joshuaodmark-Apps-orch"


def _write_session(jsonl_dir, session_id: str, preview_text: str) -> None:
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "type": "user",
        "message": {"content": [{"type": "text", "text": preview_text}]},
    })
    (jsonl_dir / f"{session_id}.jsonl").write_text(line + "\n")


def test_list_resumable_sessions_finds_worktree_session_with_dotted_path(
    tmp_path, monkeypatch,
):
    """End-to-end: a worktree cwd with a `.` and `_` must resolve to the
    same directory Claude Code actually wrote its log into.
    """
    projects_base = tmp_path / "claude_home" / ".claude" / "projects"
    monkeypatch.setattr(sessions_mod, "_projects_base", lambda: projects_base)

    project_path = tmp_path / "Apps" / "demo"
    project_path.mkdir(parents=True)
    project = Project(path=project_path)

    wt_path = tmp_path / "Apps" / ".orch-worktrees" / "demo" / "wt_abc123"
    wt_path.mkdir(parents=True)

    real_jsonl_dir = projects_base / claude_project_dirname(wt_path)
    _write_session(real_jsonl_dir, "sess-1", "hello from the worktree")

    monkeypatch.setattr(
        state, "list_worktrees",
        lambda project=None: [{
            "id": "wt_abc123",
            "worktree_path": str(wt_path),
            "jsonl_dir": None,  # forces the fallback through _jsonl_dir_for
            "branch": "feature",
            "base_branch": "main",
            "status": "kept-clean",
        }],
    )

    entries = list_resumable_sessions(project)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.session_id == "sess-1"
    assert entry.kind == "worktree"
    assert entry.summary == "hello from the worktree"
    assert entry.cwd == wt_path
