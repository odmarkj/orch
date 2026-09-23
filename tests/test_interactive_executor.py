"""Tests for `[agent] executor` in the iTerm2 sessions orch opens.

`run_headless` honoured the setting but the four iTerm2 launch sites
hard-coded `claude`, so a project opted in to Asha still opened claude in the
terminal — the path that has to earn trust before bridges do.

Invariants under test: a project without `[agent] executor` launches exactly
the command it did before, with no probe and unchanged labels; an opted-in
project launches `asha shell` with the same arguments and says so in its tab;
an unknown executor, or one missing from the VM, fails before any tab opens
or any side effect happens.
"""

import subprocess
from types import SimpleNamespace

import pytest

import orch.agent as agent_mod
import orch.iterm as iterm
from orch.models import Project


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return Project(path=p)


def _opt_in(project, executor):
    project.orch_dir.mkdir(parents=True, exist_ok=True)
    project.orch_config_file.write_text(f'[agent]\nexecutor = "{executor}"\n')


@pytest.fixture
def probe(monkeypatch):
    """Intercept the VM probe; record what was run."""
    calls = []
    reply = {"rc": 0, "stdout": "asha 3.0.0\n", "stderr": ""}

    def fake(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=reply["rc"], stdout=reply["stdout"], stderr=reply["stderr"],
        )

    monkeypatch.setattr(agent_mod, "vm_exec", fake)
    return SimpleNamespace(calls=calls, reply=reply)


@pytest.fixture
def launched(monkeypatch):
    """Stub everything around the AppleScript and capture the script."""
    scripts = []
    monkeypatch.setattr(iterm, "_run_iterm_script", lambda s: scripts.append(s) or "")
    monkeypatch.setattr(agent_mod, "session_exists", lambda p: True)
    monkeypatch.setattr(agent_mod, "_maybe_update_stack_detection", lambda p: None)
    return scripts


# ── interactive_program ────────────────────────────────────────────────────

def test_default_is_claude_without_probing(project, probe):
    assert agent_mod.interactive_program(project) == "claude"
    assert probe.calls == []


def test_asha_is_probed_then_runs_asha_shell(project, probe):
    _opt_in(project, "asha")
    assert agent_mod.interactive_program(project) == "asha shell"
    assert probe.calls == ["asha --version"]


def test_unknown_executor_fails_without_touching_the_vm(project, probe):
    _opt_in(project, "ahsa")
    with pytest.raises(agent_mod.UnknownExecutorError) as exc:
        agent_mod.interactive_program(project)
    assert "'ahsa'" in str(exc.value)
    assert probe.calls == []


def test_missing_asha_is_reported_legibly(project, probe):
    _opt_in(project, "asha")
    probe.reply.update(rc=127, stdout="", stderr="bash: line 1: asha: command not found\n")
    with pytest.raises(agent_mod.ExecutorUnavailableError) as exc:
        agent_mod.interactive_program(project)
    msg = str(exc.value)
    assert "asha: command not found" in msg
    assert "~/.local/bin/asha" in msg


def test_probe_timeout_is_reported(project, monkeypatch):
    _opt_in(project, "asha")

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(agent_mod, "vm_exec", slow)
    with pytest.raises(agent_mod.ExecutorUnavailableError):
        agent_mod.interactive_program(project)


# ── The launch sites ───────────────────────────────────────────────────────

def test_default_session_launch_is_unchanged(project, probe, launched):
    iterm.open_vm_session(project, with_shell=True)

    script = launched[0]
    assert "clear; claude --dangerously-skip-permissions --append-system-prompt-file" in script
    assert "asha" not in script
    assert f'set name to "{project.name}"' in script
    assert f'set badge to "{project.name}"' in script
    assert "·" not in script


def test_asha_session_launch(project, probe, launched):
    _opt_in(project, "asha")
    project.orch_dir.joinpath("sessions.json").write_text('{"active": "abc-123"}')

    iterm.open_vm_session(project, with_shell=True)

    script = launched[0]
    assert (
        "clear; asha shell --dangerously-skip-permissions --resume abc-123 "
        "--append-system-prompt-file"
    ) in script
    assert "clear; claude " not in script
    # The agent tab names the executor; the shell tab does not.
    assert f'set name to "{project.name} · asha"' in script
    assert f'set badge to "{project.name} · asha"' in script
    assert f'set name to "{project.name} (shell)"' in script


def test_worktree_and_resume_launches_use_asha(project, probe, launched, tmp_path):
    _opt_in(project, "asha")

    iterm.open_vm_session_in_worktree(
        project, tmp_path / "wt", "wt_1", branch="b", base_branch="main",
    )
    iterm.open_vm_resume_session(project, cwd=tmp_path, session_id="s-1")

    assert "clear; asha shell --dangerously-skip-permissions --append-system-prompt-file" in launched[0]
    assert "clear; asha shell --dangerously-skip-permissions --resume s-1 " in launched[1]


def test_build_vm_cmd_uses_asha(project, probe):
    _opt_in(project, "asha")
    assert "asha shell --dangerously-skip-permissions" in iterm._build_vm_claude_cmd(project)


def test_resolved_program_is_not_probed_again(project, probe, launched, tmp_path):
    _opt_in(project, "asha")
    iterm.open_vm_session_in_worktree(
        project, tmp_path / "wt", "wt_1", branch="b", base_branch="main",
        program="asha shell",
    )
    assert probe.calls == []


def test_bad_executor_opens_nothing_and_changes_nothing(project, probe, launched, monkeypatch):
    _opt_in(project, "asha")
    probe.reply.update(rc=127, stdout="", stderr="asha: command not found")
    monkeypatch.setattr(
        agent_mod, "fire_first_session_hook",
        lambda p: pytest.fail("no side effects for an unusable executor"),
    )
    monkeypatch.setattr(agent_mod, "session_exists", lambda p: False)

    with pytest.raises(agent_mod.ExecutorUnavailableError):
        iterm.open_vm_session(project, with_shell=True)

    assert launched == []
    assert not project.status_file.exists()
