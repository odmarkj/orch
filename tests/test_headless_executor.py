"""Tests for the per-project headless executor and the in-VM deadline.

Two problems motivated these. `run_headless` hard-coded `claude`, so trying
Asha anywhere meant switching every project and auto-dispatch at once. And
the timeout was enforced only on the host's ssh client: killing it signals
nothing in the VM, so a timed-out `claude -p` kept running and finished the
task, editing a worktree orch had already given up on.

Invariants under test: a project runs claude unless its .orch/project.toml
opts in to another known executor; an unknown one fails before the VM is
touched; the agent is wrapped in `timeout` so the deadline is enforced
where the agent runs; and reaching that deadline surfaces as a
HeadlessTimeout (still a TimeoutExpired) with the agent actually stopped.
"""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import orch.agent as agent_mod
import orch.config as config
from orch.models import Project


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return Project(path=p)


def _write_project_toml(project, text):
    project.orch_dir.mkdir(parents=True, exist_ok=True)
    project.orch_config_file.write_text(text)


@pytest.fixture
def capture_sandboxed(monkeypatch):
    """Intercept vm_exec_sandboxed; record how the call was assembled."""
    calls = []
    reply = {"rc": 0, "stdout": "", "stderr": ""}

    def fake(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(
            returncode=reply["rc"], stdout=reply["stdout"], stderr=reply["stderr"],
        )

    monkeypatch.setattr(agent_mod, "vm_exec_sandboxed", fake)
    monkeypatch.setattr(agent_mod, "vm_ensure_running", lambda: None)
    return SimpleNamespace(calls=calls, reply=reply)


# ── Choosing the executor ───────────────────────────────────────────────────

def test_default_executor_is_claude(project, capture_sandboxed):
    """No project.toml, no [agent] section: nothing changes for anyone who
    has not opted in."""
    assert project.executor == "claude"

    agent_mod.run_headless(project, "hi", allowed_dirs=["/tmp/a"])

    cmd = capture_sandboxed.calls[0]["cmd"]
    assert "claude --dangerously-skip-permissions --add-dir /tmp/a -p" in cmd
    assert "asha" not in cmd


def test_project_opts_in_to_asha(project, capture_sandboxed):
    _write_project_toml(project, '[project]\nname = "p"\n\n[agent]\nexecutor = "asha"\n')

    agent_mod.run_headless(project, "hi", allowed_dirs=["/tmp/a dir", "/tmp/b"])

    call = capture_sandboxed.calls[0]
    # Same flags as claude — asha accepts -p and --dangerously-skip-permissions
    # as no-ops — and the prompt still travels over stdin.
    assert "asha chat --dangerously-skip-permissions" in call["cmd"]
    assert "--add-dir '/tmp/a dir'" in call["cmd"]
    assert "--add-dir /tmp/b" in call["cmd"]
    assert call["cmd"].endswith(" -p")
    assert call["input"] == "hi"


@pytest.mark.parametrize("line", [
    'executor = "asha"',
    "executor = 'asha'",
    "executor=asha",
    'executor = "ASHA"   # staged rollout, 2026-09',
])
def test_executor_value_forms(project, line):
    _write_project_toml(project, f"[agent]\n{line}\n")
    assert project.executor == "asha"


def test_executor_outside_agent_section_is_ignored(project):
    _write_project_toml(project, '[bridge]\nexecutor = "asha"\n')
    assert project.executor == "claude"


def test_unknown_executor_fails_before_touching_the_vm(project, monkeypatch):
    """A typo must not silently run the incumbent — the project would look
    opted in while running claude — and must not reach the VM at all."""
    _write_project_toml(project, '[agent]\nexecutor = "ahsa"\n')
    monkeypatch.setattr(
        agent_mod, "vm_ensure_running",
        lambda: pytest.fail("VM must not be touched for a bad executor"),
    )

    with pytest.raises(agent_mod.UnknownExecutorError) as exc:
        agent_mod.run_headless(project, "hi")

    assert "'ahsa'" in str(exc.value)
    assert "project.toml" in str(exc.value)


# ── The deadline is enforced inside the VM ──────────────────────────────────

def test_agent_is_wrapped_in_timeout_with_host_backstop(project, capture_sandboxed):
    agent_mod.run_headless(project, "hi", timeout=3600)

    call = capture_sandboxed.calls[0]
    assert call["cmd"].startswith(
        f"timeout -k {agent_mod.KILL_GRACE_SECONDS} 3600 claude "
    )
    # The host limit is only a backstop, past the in-VM SIGKILL.
    assert call["timeout"] == 3600 + agent_mod.HOST_BACKSTOP_SECONDS
    assert call["timeout"] > 3600 + agent_mod.KILL_GRACE_SECONDS


@pytest.mark.parametrize("rc", [124, 137])
def test_in_vm_deadline_raises_headless_timeout(project, capture_sandboxed, monkeypatch, rc):
    capture_sandboxed.reply.update(rc=rc, stdout="half an answer", stderr="working…")
    clock = iter([1000.0, 1000.0 + 600 + 1])
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: next(clock))

    with pytest.raises(subprocess.TimeoutExpired) as exc:
        agent_mod.run_headless(project, "hi", workdir="/wt", timeout=600)

    e = exc.value
    assert isinstance(e, agent_mod.HeadlessTimeout)
    assert e.stopped is True
    assert e.timeout == 600
    assert e.returncode == rc
    assert e.executor == "claude"
    assert e.workdir == "/wt"
    assert e.stdout == "half an answer"
    assert e.stderr == "working…"
    assert "stopped in the VM" in str(e)


def test_early_137_is_an_ordinary_failure_not_a_timeout(project, capture_sandboxed, monkeypatch):
    """SIGKILL well before the deadline (the OOM killer, say) is the agent
    failing, not the deadline — it must come back as a result."""
    capture_sandboxed.reply.update(rc=137)
    clock = iter([1000.0, 1005.0])
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: next(clock))

    result = agent_mod.run_headless(project, "hi", timeout=600)

    assert result.returncode == 137


def test_host_backstop_raises_unconfirmed_headless_timeout(project, monkeypatch):
    monkeypatch.setattr(agent_mod, "vm_ensure_running", lambda: None)

    def hung(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output=b"", stderr=b"x")

    monkeypatch.setattr(agent_mod, "vm_exec_sandboxed", hung)

    with pytest.raises(agent_mod.HeadlessTimeout) as exc:
        agent_mod.run_headless(project, "hi", timeout=600)

    assert exc.value.stopped is False
    assert exc.value.timeout == 600      # the agent's deadline, not the backstop
    assert "could not confirm" in str(exc.value)


# ── End to end: the wrapper really stops the agent ──────────────────────────
#
# Runs the exact command run_headless assembles through a local bash in place
# of the VM, with a fake agent on PATH. Proves the property the host-side
# timeout never had: when the deadline passes, the agent is gone.

needs_gnu_timeout = pytest.mark.skipif(
    shutil.which("timeout") is None, reason="needs coreutils timeout",
)


def _install_fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def local_vm(monkeypatch, tmp_path):
    """vm_exec_sandboxed → a local bash, with fake agents first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def run_locally(cmd, *, cwd=None, writable_dirs, timeout=120, capture=True, input=None):
        return subprocess.run(
            ["bash", "-c", cmd], cwd=cwd, env=env, input=input,
            capture_output=True, text=True, timeout=timeout,
        )

    monkeypatch.setattr(agent_mod, "vm_exec_sandboxed", run_locally)
    monkeypatch.setattr(agent_mod, "vm_ensure_running", lambda: None)
    monkeypatch.setattr(agent_mod, "KILL_GRACE_SECONDS", 1)
    return bin_dir


@needs_gnu_timeout
def test_timed_out_agent_is_stopped_not_orphaned(project, local_vm, tmp_path):
    marker = tmp_path / "finished"
    # A slow agent that would go on editing after orch gave up — the orphan.
    _install_fake(local_vm, "claude", (
        "cat > /dev/null\n"
        "echo 'started' >&2\n"
        "sleep 2\n"
        f"echo done > {marker}\n"
    ))

    with pytest.raises(agent_mod.HeadlessTimeout) as exc:
        agent_mod.run_headless(project, "task", timeout=1)

    assert exc.value.stopped is True
    assert exc.value.returncode == 124
    assert "started" in exc.value.stderr
    time.sleep(2)
    assert not marker.exists(), "the agent kept running after its deadline"


@needs_gnu_timeout
def test_sigterm_lets_asha_print_its_resume_line(project, local_vm):
    """SIGTERM first, SIGKILL later: an agent that handles TERM gets to say
    how to resume before it goes, and orch captures it."""
    _write_project_toml(project, '[agent]\nexecutor = "asha"\n')
    _install_fake(local_vm, "asha", (
        'trap \'echo "resume with: asha chat --resume sess-123" >&2; exit 143\' TERM\n'
        "cat > /dev/null\n"
        "sleep 5 & wait\n"
    ))

    with pytest.raises(agent_mod.HeadlessTimeout) as exc:
        agent_mod.run_headless(project, "task", timeout=1)

    assert exc.value.executor == "asha"
    assert agent_mod.resume_command("asha", exc.value.stderr) == (
        "asha chat --resume sess-123"
    )


# ── How a stopped session is resumed ────────────────────────────────────────

def test_resume_command_reads_asha_resume_line():
    stderr = (
        "budget: $5.00\n"
        "  [API  s-old  12.0s  $0.10]\n"
        "\x1b[36mresume with: asha chat --resume s-first\x1b[0m\n"
        "resume with: asha chat --resume s-last  \n"
    )
    assert agent_mod.resume_command("asha", stderr) == "asha chat --resume s-last"


def test_resume_command_for_claude_continues_in_the_worktree():
    assert agent_mod.resume_command("claude", "") == "claude --continue"


def test_resume_command_unknown_without_a_resume_line():
    """Asha killed before it could print the line: nothing to offer."""
    assert agent_mod.resume_command("asha", "budget: $5.00\n") is None
    assert agent_mod.resume_command("asha", None) is None


# ── Auto-dispatch reads its timeout from config ─────────────────────────────

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    return path


def test_dispatch_timeout_defaults_to_the_bridge_timeout(config_file):
    assert config.dispatch_worker_timeout_seconds() == 3600
    config_file.write_text("[bridge]\nworker_timeout_seconds = 2400\n")
    assert config.dispatch_worker_timeout_seconds() == 2400


def test_dispatch_timeout_can_be_set_on_its_own(config_file):
    config_file.write_text(
        "[bridge]\nworker_timeout_seconds = 2400\n\n"
        "[dispatch]\nmax_parallel = 3\nworker_timeout_seconds = 1800\n"
    )
    assert config.dispatch_worker_timeout_seconds() == 1800


def test_dispatch_uses_configured_timeout_and_keeps_timed_out_worktree(
    project, config_file, monkeypatch, tmp_path,
):
    config_file.write_text("[dispatch]\nworker_timeout_seconds = 1800\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(agent_mod, "create_worktree", lambda p, t: (wt, "auto/x-1"))
    monkeypatch.setattr(agent_mod, "worktree_head", lambda p: "abc")
    monkeypatch.setattr(agent_mod, "vm_ensure_running", lambda: None)
    monkeypatch.setattr(
        agent_mod, "remove_worktree",
        lambda *a, **k: pytest.fail("a timed-out worktree must be kept for resume"),
    )
    seen = {}

    def timed_out(project, prompt, *, workdir=None, timeout=600, **kwargs):
        seen["timeout"] = timeout
        raise agent_mod.HeadlessTimeout(
            "cmd", timeout, executor="claude", workdir=str(workdir), stopped=True,
        )

    monkeypatch.setattr(agent_mod, "run_headless", timed_out)

    with pytest.raises(agent_mod.HeadlessTimeout):
        agent_mod.run_task_in_worktree(project, "do the thing")

    assert seen["timeout"] == 1800
