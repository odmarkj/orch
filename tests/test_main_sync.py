"""Regression tests for main-sync's dirty gate and fetch observability.

Two failures motivated these, both silent:

  1. orch writes ``.orch/`` into every project it manages. In a repo whose
     ``.gitignore`` doesn't cover it, that untracked directory alone made
     ``_sync_one`` return ``dirty`` — so the project's local main never
     fast-forwarded again. Two repos on the machine were 122 and 565 commits
     behind on nothing but orch's own scratch.

  2. ``git fetch``'s return code was discarded. A repo orch can't authenticate
     to fails instantly under ``GIT_TERMINAL_PROMPT=0``, and the caller then
     branched a bridge worktree off a stale ref and answered confidently
     against old code, with nothing in the log to say so.
"""

import logging
import subprocess
from types import SimpleNamespace

import pytest

import orch.agent as agent_mod
import orch.daemon as daemon_mod
import orch.gitexclude as gitexclude
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


@pytest.fixture(autouse=True)
def _clear_exclude_memo():
    """ensure_orch_excluded memoises per process; tmp repos must not inherit."""
    gitexclude._checked.clear()
    yield
    gitexclude._checked.clear()


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
def behind_repo(tmp_path):
    """A clone whose local main sits one commit behind origin/main.

    Clean tree, main checked out — i.e. exactly the state _sync_one is meant
    to fast-forward.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )

    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", f"file://{origin}", str(seed)],
        check=True, capture_output=True, text=True,
    )
    _git(seed, "checkout", "-B", "main")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hello\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "push", "-u", "origin", "main")

    project_dir = tmp_path / "apps" / "proj"
    project_dir.parent.mkdir()
    subprocess.run(
        ["git", "clone", f"file://{origin}", str(project_dir)],
        check=True, capture_output=True, text=True,
    )
    _git(project_dir, "config", "user.email", "test@example.com")
    _git(project_dir, "config", "user.name", "Test")

    # Advance origin so the clone is genuinely behind.
    (seed / "NEW.md").write_text("upstream work\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "upstream commit")
    _git(seed, "push", "origin", "main")

    return SimpleNamespace(origin=origin, project=Project(path=project_dir))


def _make_bridge(project):
    return state.insert_bridge(state.BridgeSubmission(
        source_project="src", source_path=str(project.path),
        target_project=project.name, intent="fix",
        summary="test bridge", context="ctx", request="req",
        relevant_files=[],
    ))


def _sync(project):
    """Run one _sync_one sweep with no live root sessions."""
    return daemon_mod._MainSync()._sync_one(project, set())


# ── FIX 1: orch's own .orch/ must not read as human work ────────────────────

def test_orch_only_dirt_is_not_dirty(behind_repo):
    """A repo dirty ONLY with .orch/ still fast-forwards.

    Negative control: before the _dirt_excluding_orch filter this returned
    "dirty" and the project stayed behind forever.
    """
    project = behind_repo.project
    orch_dir = project.path / ".orch"
    orch_dir.mkdir()
    (orch_dir / "status").write_text("idle\n")

    # Precondition: git really does consider this tree dirty.
    assert _git(project.path, "status", "--porcelain").stdout.strip()

    assert _sync(project) == "fast-forwarded"
    assert (project.path / "NEW.md").exists()


def test_real_dirt_is_still_skipped(behind_repo):
    project = behind_repo.project
    (project.path / "README.md").write_text("local edit in progress\n")

    assert _sync(project) == "dirty"
    assert not (project.path / "NEW.md").exists()


def test_real_dirt_alongside_orch_dirt_is_still_skipped(behind_repo):
    project = behind_repo.project
    (project.path / ".orch").mkdir()
    (project.path / ".orch" / "status").write_text("idle\n")
    (project.path / "scratch.txt").write_text("human work\n")

    assert _sync(project) == "dirty"


def test_clean_repo_fast_forwards(behind_repo):
    assert _sync(behind_repo.project) == "fast-forwarded"


@pytest.mark.parametrize("porcelain, expected", [
    ("?? .orch/\n", []),
    ("?? .orch/status\n", []),
    (" M .orch/project.toml\n", []),
    ("?? .orchestration/\n", ["?? .orchestration/"]),
    ("?? .orch-notes\n", ["?? .orch-notes"]),
    (" M README.md\n?? .orch/\n", [" M README.md"]),
    # A rename out of .orch/ still deletes a real tracked file: keep it.
    ("R  .orch/x -> real/x\n", ["R  .orch/x -> real/x"]),
    ("R  real/x -> .orch/x\n", ["R  real/x -> .orch/x"]),
    ("R  .orch/a -> .orch/b\n", []),
])
def test_dirt_filter_classification(porcelain, expected):
    assert daemon_mod._dirt_excluding_orch(porcelain) == expected


# ── FIX 1a: .git/info/exclude ───────────────────────────────────────────────

def test_exclude_written_when_not_ignored(behind_repo):
    project = behind_repo.project
    (project.path / ".orch").mkdir()
    (project.path / ".orch" / "status").write_text("idle\n")

    assert gitexclude.ensure_orch_excluded(project.path) is True

    exclude = project.path / ".git" / "info" / "exclude"
    assert ".orch/" in exclude.read_text().splitlines()
    # The whole point: git now agrees the tree is clean.
    assert _git(project.path, "status", "--porcelain").stdout.strip() == ""


@pytest.mark.parametrize("pattern", [".orch/", ".orch", "/.orch/"])
@pytest.mark.parametrize("dir_exists", [True, False])
def test_exclude_is_a_noop_when_check_ignore_already_passes(
    behind_repo, pattern, dir_exists,
):
    """A repo whose own .gitignore covers .orch is left completely alone.

    The directory-not-yet-created case is the one that bites: a `.orch/`
    pattern is directory-only, and `git check-ignore .orch` reports "not
    ignored" for it unless the path is queried as a directory.
    """
    project = behind_repo.project
    (project.path / ".gitignore").write_text(f"{pattern}\n")
    _git(project.path, "add", ".gitignore")
    _git(project.path, "commit", "-m", "ignore orch")
    if dir_exists:
        (project.path / ".orch").mkdir()
        (project.path / ".orch" / "status").write_text("idle\n")

    exclude = project.path / ".git" / "info" / "exclude"
    before = exclude.read_text() if exclude.exists() else None

    assert gitexclude.ensure_orch_excluded(project.path) is False

    after = exclude.read_text() if exclude.exists() else None
    assert after == before


def test_exclude_is_idempotent(behind_repo):
    project = behind_repo.project

    assert gitexclude.ensure_orch_excluded(project.path) is True
    gitexclude._checked.clear()  # simulate a fresh process
    assert gitexclude.ensure_orch_excluded(project.path) is False

    exclude = (project.path / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".orch/") == 1


def test_exclude_fails_soft_outside_a_repo(tmp_path):
    assert gitexclude.ensure_orch_excluded(tmp_path) is False


def test_exclude_from_a_worktree_lands_in_the_shared_git_dir(behind_repo):
    """Worktrees share the main repo's info/exclude via --git-common-dir."""
    project = behind_repo.project
    wt = project.path.parent / "wt"
    _git(project.path, "worktree", "add", "-b", "side", str(wt))

    assert gitexclude.ensure_orch_excluded(wt) is True

    exclude = project.path / ".git" / "info" / "exclude"
    assert ".orch/" in exclude.read_text().splitlines()


# ── FIX 2a: a failed fetch must be loud, not silently stale ─────────────────

def _break_origin(project):
    """Point origin at a URL git can never reach, so fetch exits non-zero."""
    _git(project.path, "remote", "set-url", "origin",
         str(project.path.parent / "does-not-exist.git"))


def test_failed_fetch_logs_and_still_returns_a_usable_ref(behind_repo, caplog):
    project = behind_repo.project
    _break_origin(project)

    with caplog.at_level(logging.WARNING, logger="orch.agent"):
        base, ref = agent_mod._fresh_base_ref(project)

    assert (base, ref) == ("main", "origin/main")  # degraded, not fatal
    assert any(
        r.levelno >= logging.WARNING and "could not fetch origin/main" in r.message
        for r in caplog.records
    ), caplog.text


def test_failed_fetch_in_the_bridge_path_records_stale_base_ref(
    behind_repo, temp_db, caplog,
):
    project = behind_repo.project
    _break_origin(project)

    bid = _make_bridge(project)["id"]

    with caplog.at_level(logging.WARNING, logger="orch.agent"):
        agent_mod._fresh_base_ref(project, bid=bid)

    events = {e["event"] for e in state.get_events(bid)}
    assert "stale_base_ref" in events

    detail = next(
        e["detail"] for e in state.get_events(bid) if e["event"] == "stale_base_ref"
    )
    assert detail["base"] == "main"
    assert detail["ref"] == "origin/main"
    assert detail["reason"]


def test_successful_fetch_records_nothing(behind_repo, temp_db, caplog):
    project = behind_repo.project
    bid = _make_bridge(project)["id"]

    with caplog.at_level(logging.WARNING, logger="orch.agent"):
        base, ref = agent_mod._fresh_base_ref(project, bid=bid)

    assert (base, ref) == ("main", "origin/main")
    assert "stale_base_ref" not in {e["event"] for e in state.get_events(bid)}
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_sync_logs_a_failed_fetch(behind_repo, caplog):
    project = behind_repo.project
    _break_origin(project)

    with caplog.at_level(logging.WARNING, logger="orch.daemon"):
        _sync(project)

    assert any(
        "fetch of origin/main failed" in r.message for r in caplog.records
    ), caplog.text


# ── FIX 2b/2c: main-sync observability ──────────────────────────────────────

def test_session_listing_failure_is_logged(monkeypatch, caplog):
    """Returning None skips EVERY project this sweep — never do it quietly."""
    import orch.vm as vm_mod

    def boom():
        raise RuntimeError("limactl exploded")

    monkeypatch.setattr(vm_mod, "vm_is_running", boom)

    with caplog.at_level(logging.WARNING, logger="orch.daemon"):
        assert daemon_mod._MainSync()._projects_with_root_sessions() is None

    assert any(
        r.levelno >= logging.WARNING and "could not list live sessions" in r.message
        for r in caplog.records
    ), caplog.text


def test_stuck_project_escalates_from_debug_to_warning(caplog):
    sync = daemon_mod._MainSync()

    with caplog.at_level(logging.DEBUG, logger="orch.daemon"):
        for _ in range(daemon_mod._SYNC_STUCK_SWEEPS - 1):
            sync._log_outcome("k3s", "session-active")
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

        sync._log_outcome("k3s", "session-active")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "stuck on session-active" in warnings[0].message


def test_stuck_warning_does_not_fire_every_sweep(caplog):
    sync = daemon_mod._MainSync()
    sweeps = daemon_mod._SYNC_STUCK_SWEEPS + daemon_mod._SYNC_STUCK_REPEAT

    with caplog.at_level(logging.DEBUG, logger="orch.daemon"):
        for _ in range(sweeps):
            sync._log_outcome("k3s", "dirty")

    # Once on crossing the threshold, once on the repeat interval — not 20.
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 2


def test_changed_outcome_resets_the_streak(caplog):
    sync = daemon_mod._MainSync()

    with caplog.at_level(logging.DEBUG, logger="orch.daemon"):
        for _ in range(daemon_mod._SYNC_STUCK_SWEEPS - 1):
            sync._log_outcome("k3s", "dirty")
        sync._log_outcome("k3s", "session-active")
        sync._log_outcome("k3s", "dirty")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_success_clears_the_streak(caplog):
    sync = daemon_mod._MainSync()

    for _ in range(daemon_mod._SYNC_STUCK_SWEEPS * 2):
        sync._log_outcome("k3s", "dirty")
    sync._log_outcome("k3s", "fast-forwarded")
    assert "k3s" not in sync._streaks

    caplog.clear()  # drop the warnings the stuck streak legitimately emitted
    with caplog.at_level(logging.DEBUG, logger="orch.daemon"):
        sync._log_outcome("k3s", "dirty")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_repos_with_no_remote_stay_quiet(caplog):
    """`no-refs` is permanent and unfixable — warning about it is just noise."""
    sync = daemon_mod._MainSync()

    with caplog.at_level(logging.DEBUG, logger="orch.daemon"):
        for _ in range(daemon_mod._SYNC_STUCK_SWEEPS * 3):
            sync._log_outcome("local-only", "no-refs")

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
