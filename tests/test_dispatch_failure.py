"""Auto-dispatch must not rerun a todo whose agent timed out.

`_on_dispatch_failed` puts a failed todo back to `- [ ]` and schedules a
dispatch check, which claims it again ten seconds later — so every failure
is retried for as long as dispatch stays on. For a timeout that means the
same work, the same deadline, again and again. A timed-out todo is parked as
`- [!]` instead: not pending, not in progress, so neither re-dispatched nor
holding a slot.
"""

from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

import orch.agent as agent_mod
from orch.app import OrchApp
from orch.models import Project


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "TODOS.md").write_text(
        "## Pending\n- [~] slow task\n- [ ] next task\n"
    )
    return Project(path=p)


def _fake_app():
    notes = []
    app = SimpleNamespace(
        notify=lambda msg, **kw: notes.append((msg, kw)),
        _schedule_dispatch_check=lambda p: None,
    )
    return app, notes


def test_timed_out_todo_is_parked_not_requeued(project):
    app, notes = _fake_app()
    err = agent_mod.HeadlessTimeout(
        "cmd", 3600, stderr=b"", executor="claude",
        workdir="/wt/slow", stopped=True, returncode=124,
    )

    OrchApp._on_dispatch_failed(app, project, "slow task", err)

    todos = project.todos_file.read_text()
    assert "- [!] slow task" in todos
    assert project.pending_todos == ["next task"]
    assert project.in_progress_count == 0
    assert "cd /wt/slow && claude --continue" in notes[0][0]


def test_other_failures_still_go_back_to_pending(project):
    app, notes = _fake_app()

    OrchApp._on_dispatch_failed(
        app, project, "slow task", RuntimeError("git worktree add failed"),
    )

    assert project.pending_todos == ["slow task", "next task"]
    assert "[!]" not in project.todos_file.read_text()
