import json
from pathlib import Path

import pytest

from orch import claude_paths, sessions
from orch.models import Project

WT = "/Users/me/Apps/.orch-worktrees/asha-v3/wt_01a07ebb533f52eaff39"


@pytest.fixture
def base(tmp_path, monkeypatch):
    b = tmp_path / "projects"
    b.mkdir()
    monkeypatch.setenv("ORCH_CLAUDE_PROJECTS_DIR", str(b))
    return b


def _transcript(d: Path, sid: str, cwd: str, text: str = "hello there") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text(json.dumps({
        "type": "user", "cwd": cwd,
        "message": {"role": "user", "content": text},
    }) + "\n")
    return p


def test_encode_replaces_every_non_alphanumeric():
    assert claude_paths.encode_cwd(WT) == (
        "-Users-me-Apps--orch-worktrees-asha-v3-wt-01a07ebb533f52eaff39"
    )


def test_projects_base_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCH_CLAUDE_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert claude_paths.projects_base() == tmp_path / "cfg" / "projects"
    monkeypatch.setenv("ORCH_CLAUDE_PROJECTS_DIR", str(tmp_path / "x"))
    assert claude_paths.projects_base() == tmp_path / "x"


def test_resolves_encoded_dir(base):
    real = base / claude_paths.encode_cwd(WT)
    _transcript(real, "s1", WT)
    assert claude_paths.jsonl_dir_for(WT) == real


def test_empty_encoded_dir_falls_back_to_cwd_index(base):
    # An empty dir at the encoded name (e.g. orch's own pre-create) must not
    # mask transcripts Claude wrote under a different name.
    (base / claude_paths.encode_cwd(WT)).mkdir()
    elsewhere = base / "some-future-encoding"
    _transcript(elsewhere, "s1", WT)
    assert claude_paths.jsonl_dir_for(WT) == elsewhere


def test_unknown_cwd_returns_encoded_dir(base):
    assert claude_paths.jsonl_dir_for(WT) == base / claude_paths.encode_cwd(WT)


def test_worktree_sessions_listed_despite_stale_row(base, tmp_path, monkeypatch):
    root = tmp_path / "Apps" / "asha-v3"
    wt = tmp_path / "Apps" / ".orch-worktrees" / "asha-v3" / "wt_01abc"
    root.mkdir(parents=True)
    wt.mkdir(parents=True)
    _transcript(base / claude_paths.encode_cwd(root), "root-sid", str(root))
    _transcript(base / claude_paths.encode_cwd(wt), "wt-sid", str(wt))
    # Row as written before the fix: jsonl_dir is the /-only encoding.
    stale = base / str(wt).replace("/", "-")
    stale.mkdir()
    monkeypatch.setattr(sessions.state, "list_worktrees", lambda project: [{
        "id": "wt_01abc", "worktree_path": str(wt), "jsonl_dir": str(stale),
        "branch": "b", "base_branch": "main", "status": "active",
    }])

    entries = sessions.list_resumable_sessions(Project(path=root))

    by_id = {e.session_id: e for e in entries}
    assert set(by_id) == {"root-sid", "wt-sid"}
    assert by_id["wt-sid"].kind == "worktree"
    assert by_id["wt-sid"].cwd == wt
    assert by_id["wt-sid"].wt_id == "wt_01abc"
