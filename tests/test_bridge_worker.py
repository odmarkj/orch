"""Regression tests for the bridge commit/push/teardown pipeline.

The scenario that motivated these: an agent checked out a different branch
inside the bridge worktree; ``_commit_and_pr`` pushed the stale planned ref
(exit 0, shipping nothing), ``_create_pr`` found no diff, and the
force-remove teardown destroyed the only copy of the commit while the
bridge recorded ``completed`` with a clean record.

Invariant under test: a bridge whose agent ends up on a different branch
must either push that branch or fail loudly — and must never lose the
commit.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import orch.agent as agent_mod
import orch.bridge_worker as bw
import orch.state as state
from orch.models import Project


def _git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=30,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point orch.state at a throwaway SQLite DB."""
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    old = getattr(state._local, "conn", None)
    if old is not None:
        old.close()
    state._local.conn = None
    state.init_db()
    yield
    conn = getattr(state._local, "conn", None)
    if conn is not None:
        conn.close()
    state._local.conn = None


@pytest.fixture
def repo(tmp_path):
    """A project clone with a local bare 'origin' so pushes are real."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    project_dir = tmp_path / "apps" / "proj"
    project_dir.parent.mkdir()
    subprocess.run(
        ["git", "clone", str(origin), str(project_dir)],
        check=True, capture_output=True, text=True,
    )
    _git(project_dir, "checkout", "-B", "main")
    _git(project_dir, "config", "user.email", "test@example.com")
    _git(project_dir, "config", "user.name", "Test")
    (project_dir / "README.md").write_text("hello\n")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "-m", "initial")
    _git(project_dir, "push", "-u", "origin", "main")
    return SimpleNamespace(origin=origin, project=Project(path=project_dir))


@pytest.fixture
def worktree(repo):
    """A bridge worktree exactly as run_bridge would create it."""
    wt_path, branch = agent_mod.create_worktree(
        repo.project, "bridge test task", branch_prefix="bridge",
    )
    base = agent_mod.worktree_head(wt_path)
    return SimpleNamespace(path=wt_path, branch=branch, base=base)


@pytest.fixture
def local_git(monkeypatch):
    """Route _run_git's vm_exec through a local shell; skip retry sleeps."""
    def fake_vm_exec(cmd, *, cwd=None, timeout=120, capture=True):
        return subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    monkeypatch.setattr(agent_mod, "vm_exec", fake_vm_exec)
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture
def fake_pr(monkeypatch):
    """Stub PR creation; records the head branch of each call."""
    calls = []

    def _fake_create_pr(project, worktree_path, branch_name, todo_text,
                        review_text="", title_prefix="auto"):
        calls.append(branch_name)
        return f"https://github.test/pr/{branch_name}"

    monkeypatch.setattr(agent_mod, "_create_pr", _fake_create_pr)
    monkeypatch.setattr(bw, "_existing_pr_url", lambda *a, **k: None)
    return calls


def _make_bridge(target_name="proj", intent="fix"):
    return state.insert_bridge(state.BridgeSubmission(
        source_project="src", source_path="/tmp/src",
        target_project=target_name, intent=intent,
        summary="test bridge", context="ctx", request="req",
        relevant_files=[],
    ))


# ── _commit_and_pr: the branch-mismatch scenario ────────────────────────────

def test_branch_mismatch_pushes_actual_branch(repo, worktree, temp_db, local_git, fake_pr):
    """Agent switched branches → push what HEAD is, not the stale planned ref."""
    b = _make_bridge()
    _git(worktree.path, "checkout", "-b", "agent/other-branch")
    (worktree.path / "fix.py").write_text("the actual work\n")

    outcome = bw._commit_and_pr(
        repo.project, worktree.path, worktree.branch,
        "test bridge", "result", bid=b["id"], base_commit=worktree.base,
    )

    assert outcome.changed and outcome.pushed
    assert outcome.branch == "agent/other-branch"
    assert outcome.pr_url == "https://github.test/pr/agent/other-branch"
    assert fake_pr == ["agent/other-branch"]

    # The commit reached the remote on the branch the agent was actually on.
    local_head = _git(worktree.path, "rev-parse", "HEAD").stdout.strip()
    remote_head = _git(
        repo.origin, "rev-parse", "refs/heads/agent/other-branch",
    ).stdout.strip()
    assert local_head == remote_head

    # The stale planned branch was NOT pushed.
    stale = _git(repo.origin, "rev-parse", f"refs/heads/{worktree.branch}", check=False)
    assert stale.returncode != 0

    # The divergence is on the record.
    events = [e for e in state.get_events(b["id"]) if e["event"] == "branch_mismatch"]
    assert len(events) == 1
    assert events[0]["detail"]["planned"] == worktree.branch
    assert events[0]["detail"]["actual"] == "agent/other-branch"

    # And the bridge row carries the branch that was actually pushed.
    assert state.get_bridge(b["id"])["branch"] == "agent/other-branch"


def test_detached_head_ships_to_planned_branch(repo, worktree, temp_db, local_git, fake_pr):
    """Detached HEAD (e.g. `checkout origin/x`) → commit ships via HEAD:<planned>."""
    b = _make_bridge()
    _git(worktree.path, "checkout", "--detach")
    (worktree.path / "fix.py").write_text("detached work\n")

    outcome = bw._commit_and_pr(
        repo.project, worktree.path, worktree.branch,
        "test bridge", "result", bid=b["id"], base_commit=worktree.base,
    )

    assert outcome.changed and outcome.pushed
    assert outcome.branch == worktree.branch
    local_head = _git(worktree.path, "rev-parse", "HEAD").stdout.strip()
    remote_head = _git(
        repo.origin, "rev-parse", f"refs/heads/{worktree.branch}",
    ).stdout.strip()
    assert local_head == remote_head
    events = [e for e in state.get_events(b["id"]) if e["event"] == "branch_mismatch"]
    assert len(events) == 1
    assert events[0]["detail"]["actual"] == "(detached HEAD)"


def test_no_changes_is_a_distinct_noop(repo, worktree, temp_db, local_git, fake_pr):
    b = _make_bridge()
    outcome = bw._commit_and_pr(
        repo.project, worktree.path, worktree.branch,
        "test bridge", "result", bid=b["id"], base_commit=worktree.base,
    )
    assert outcome == bw.CommitOutcome(changed=False)
    assert fake_pr == []
    stale = _git(repo.origin, "rev-parse", f"refs/heads/{worktree.branch}", check=False)
    assert stale.returncode != 0


def test_agent_self_commit_still_ships(repo, worktree, temp_db, local_git, fake_pr):
    """Clean tree but local-only commits ≠ 'nothing to do'."""
    b = _make_bridge()
    (worktree.path / "fix.py").write_text("committed by agent\n")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "agent committed on its own")

    outcome = bw._commit_and_pr(
        repo.project, worktree.path, worktree.branch,
        "test bridge", "result", bid=b["id"], base_commit=worktree.base,
    )

    assert outcome.changed and outcome.pushed
    assert outcome.branch == worktree.branch
    local_head = _git(worktree.path, "rev-parse", "HEAD").stdout.strip()
    remote_head = _git(
        repo.origin, "rev-parse", f"refs/heads/{worktree.branch}",
    ).stdout.strip()
    assert local_head == remote_head


def test_push_failure_raises_and_work_is_recoverable(repo, worktree, temp_db, local_git, fake_pr):
    b = _make_bridge()
    (worktree.path / "fix.py").write_text("work\n")
    _git(repo.project.path, "remote", "set-url", "origin", "/nonexistent/origin.git")

    with pytest.raises(bw.TransientBridgeError):
        bw._commit_and_pr(
            repo.project, worktree.path, worktree.branch,
            "test bridge", "result", bid=b["id"], base_commit=worktree.base,
        )

    # The commit exists locally and the teardown guard would keep it.
    assert agent_mod.worktree_unpushed_reason(worktree.path, worktree.base) is not None


# ── Teardown guard ──────────────────────────────────────────────────────────

def test_teardown_preserves_unpushed_commits(repo, worktree, temp_db):
    b = _make_bridge()
    (worktree.path / "wip.py").write_text("wip\n")
    _git(worktree.path, "add", "-A")
    _git(worktree.path, "commit", "-m", "local only")

    bw._teardown_worktree(b["id"], repo.project, worktree.path, worktree.branch, worktree.base)

    assert worktree.path.exists()
    events = [e for e in state.get_events(b["id"]) if e["event"] == "worktree_preserved"]
    assert len(events) == 1
    assert "not on any remote branch" in events[0]["detail"]["reason"]
    assert events[0]["detail"]["worktree_path"] == str(worktree.path)


def test_teardown_preserves_uncommitted_changes(repo, worktree, temp_db):
    b = _make_bridge()
    (worktree.path / "wip.py").write_text("wip\n")

    bw._teardown_worktree(b["id"], repo.project, worktree.path, worktree.branch, worktree.base)

    assert worktree.path.exists()
    events = [e for e in state.get_events(b["id"]) if e["event"] == "worktree_preserved"]
    assert len(events) == 1
    assert "uncommitted" in events[0]["detail"]["reason"]


def test_teardown_removes_clean_worktree(repo, worktree, temp_db):
    b = _make_bridge()
    bw._teardown_worktree(b["id"], repo.project, worktree.path, worktree.branch, worktree.base)
    assert not worktree.path.exists()
    events = [e for e in state.get_events(b["id"]) if e["event"] == "worktree_preserved"]
    assert events == []


def test_teardown_ignores_orch_scratch_files(repo, worktree, temp_db):
    """.orch/ is orch's own scratch space; it must not block removal."""
    b = _make_bridge()
    (worktree.path / ".orch").mkdir()
    (worktree.path / ".orch" / "bridge_result").write_text("summary\n")

    bw._teardown_worktree(b["id"], repo.project, worktree.path, worktree.branch, worktree.base)

    assert not worktree.path.exists()


# ── Prompt ──────────────────────────────────────────────────────────────────

def test_fix_prompt_explains_post_exit_flow():
    b = {
        "source_project": "src", "source_path": "/tmp/src",
        "context": "ctx", "request": "req", "intent": "fix",
        "relevant_files": [],
    }
    prompt = bw._build_prompt(b, branch_name="bridge/foo-1234")
    assert "bridge/foo-1234" in prompt
    assert "Stay on that branch" in prompt
    assert "Do not commit or push" in prompt
    assert "becomes the PR body" in prompt


# ── run_bridge end-to-end: the exact incident, replayed ─────────────────────

def test_run_bridge_branch_mismatch_never_loses_the_commit(
    repo, temp_db, local_git, fake_pr, monkeypatch, tmp_path,
):
    b = _make_bridge(target_name=repo.project.name)

    def fake_run_headless(project, prompt, *, workdir=None, **kwargs):
        # Simulate the agent that caused the incident: it moves to another
        # branch, does the work there, and leaves a result note.
        _git(workdir, "checkout", "-b", "agent/went-elsewhere")
        (Path(workdir) / "fix.py").write_text("the actual work\n")
        (Path(workdir) / ".orch").mkdir(exist_ok=True)
        (Path(workdir) / ".orch" / "bridge_result").write_text("did the work")
        return SimpleNamespace(returncode=0, stdout="did the work", stderr="")

    monkeypatch.setattr(agent_mod, "run_headless", fake_run_headless)
    monkeypatch.setattr("orch.vm.vm_ensure_running", lambda: None)
    monkeypatch.setattr(bw, "discover_projects", lambda: [repo.project])

    bw.run_bridge(b)

    rec = state.get_bridge(b["id"])
    assert rec["status"] == "completed", rec["error"]
    assert rec["branch"] == "agent/went-elsewhere"
    assert rec["pr_url"] == "https://github.test/pr/agent/went-elsewhere"

    # The commit reached the remote — the work was never lost.
    remote = _git(repo.origin, "rev-parse", "refs/heads/agent/went-elsewhere", check=False)
    assert remote.returncode == 0

    # Everything shipped, so the worktree was safe to remove.
    assert not Path(rec["worktree_path"]).exists()

    event_names = [e["event"] for e in state.get_events(b["id"])]
    assert "branch_mismatch" in event_names
    assert "no_changes" not in event_names
