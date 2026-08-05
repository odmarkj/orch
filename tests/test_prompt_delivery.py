"""Regression tests for prompt delivery to headless Claude.

The scenario that motivated these: a ~5.5 KB bridge request died in ~2s,
four times, with

    mm_send_fd: sendmsg(1): Message too long
    mux_client_request_session: send fds failed

because the whole prompt was interpolated into the shell command string
handed to ssh, and ssh sends that command to the mux master in a single
control message. The same task at 994 bytes completed fine.

Invariants under test: the remote command never carries the prompt, its
size does not grow with the prompt, and anything that *does* overflow the
control channel fails fast with an actionable message instead of rc=255.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

import orch.agent as agent_mod
import orch.vm as vm
from orch.models import Project


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return Project(path=p)


@pytest.fixture
def capture_sandboxed(monkeypatch):
    """Intercept vm_exec_sandboxed; record how the call was assembled."""
    calls = []

    def fake(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(agent_mod, "vm_exec_sandboxed", fake)
    monkeypatch.setattr(agent_mod, "vm_ensure_running", lambda: None)
    return calls


# ── The prompt travels over stdin ───────────────────────────────────────────

def test_prompt_is_not_on_the_command_line(project, capture_sandboxed):
    prompt = "please do the thing, it's got an apostrophe and a $VAR"

    agent_mod.run_headless(project, prompt)

    call = capture_sandboxed[0]
    assert call["input"] == prompt
    assert "please do the thing" not in call["cmd"]
    assert call["cmd"].endswith(" -p")


def test_command_size_is_constant_in_the_prompt(project, capture_sandboxed):
    """A 50 KB prompt must assemble the same command as a 10-byte one —
    that is the whole point: size lives in stdin, not in the argv."""
    agent_mod.run_headless(project, "tiny")
    agent_mod.run_headless(project, "x" * 50_000)

    small, large = capture_sandboxed
    assert small["cmd"] == large["cmd"]
    assert len(large["input"]) == 50_000


def test_allowed_dirs_are_still_quoted_flags(project, capture_sandboxed):
    agent_mod.run_headless(
        project, "hi", allowed_dirs=["/tmp/a dir", "/tmp/b"],
    )

    cmd = capture_sandboxed[0]["cmd"]
    assert "--add-dir '/tmp/a dir'" in cmd
    assert "--add-dir /tmp/b" in cmd
    assert cmd.endswith(" -p")


# ── The backstop: oversized commands fail fast ──────────────────────────────

def test_sandboxed_exec_refuses_an_oversized_command(monkeypatch):
    """Refusal happens before ssh is ever spawned."""
    monkeypatch.setattr(
        vm.subprocess, "run",
        lambda *a, **k: pytest.fail("ssh must not be spawned"),
    )

    with pytest.raises(vm.CommandTooLargeError) as exc:
        vm.vm_exec_sandboxed(
            "echo " + "x" * 20_000, writable_dirs=["/tmp"],
        )

    msg = str(exc.value)
    assert "bytes" in msg                 # names the size and the cap
    assert str(vm.ssh_cmd_max_bytes()) in msg
    assert "stdin" in msg and "file" in msg   # names the workaround


def test_plain_exec_refuses_an_oversized_command(monkeypatch):
    monkeypatch.setattr(
        vm.subprocess, "run",
        lambda *a, **k: pytest.fail("ssh must not be spawned"),
    )
    with pytest.raises(vm.CommandTooLargeError):
        vm.vm_exec("echo " + "x" * 20_000)


def test_cap_is_env_overridable(monkeypatch):
    monkeypatch.setenv("ORCH_SSH_CMD_MAX_BYTES", "64")
    assert vm.ssh_cmd_max_bytes() == 64
    monkeypatch.setenv("ORCH_SSH_CMD_MAX_BYTES", "not-a-number")
    assert vm.ssh_cmd_max_bytes() == vm._DEFAULT_SSH_CMD_MAX_BYTES


def test_ordinary_commands_are_unaffected(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vm.subprocess, "run", fake_run)
    vm.vm_exec("git status --porcelain", cwd="/tmp")

    assert "git status --porcelain" in seen["argv"][-1]
    assert seen["input"] is None


# ── Recognising the failure after the fact ──────────────────────────────────

@pytest.mark.parametrize("rc,stderr,expected", [
    (255, "mm_send_fd: sendmsg(1): Message too long\n"
          "mux_client_request_session: send fds failed", True),
    (255, "mux_client_request_session: send fds failed", True),
    (255, "Permission denied (publickey).", False),   # ssh failed for other reasons
    (1, "mm_send_fd: sendmsg(1): Message too long", False),  # remote ran, then failed
    (0, "", False),
    (255, None, False),
])
def test_is_ssh_undeliverable(rc, stderr, expected):
    assert vm.is_ssh_undeliverable(rc, stderr) is expected


# ── The PR body has the same problem, and the same fix ──────────────────────

def test_pr_body_goes_over_stdin(monkeypatch, project, tmp_path):
    calls = []

    def fake_vm_exec(cmd, **kwargs):
        calls.append({"cmd": cmd, **kwargs})
        return SimpleNamespace(
            returncode=0, stdout="https://github.test/pr/1", stderr="",
        )

    monkeypatch.setattr(agent_mod, "vm_exec", fake_vm_exec)

    long_review = "finding\n" * 2000
    url = agent_mod._create_pr(
        project, Path(tmp_path), "bridge/x", "do a thing", long_review,
    )

    assert url == "https://github.test/pr/1"
    call = calls[0]
    assert "--body-file -" in call["cmd"]
    assert long_review in call["input"]
    assert long_review not in call["cmd"]
    assert len(call["cmd"]) < vm.ssh_cmd_max_bytes()
