"""Tests for the D102 dev-channels dialog auto-confirm in orch/iterm.py.

`asha shell` always shows a "WARNING: Loading development channels" dialog
before its TUI starts, and it has no persisted "don't ask again" setting —
unlike the two dialogs asha-v3's D184 fixed. D102 says the launcher that
starts these sessions unattended should send the confirming keystroke
itself; this is that fix.

Invariants under test: nothing is sent for a bare `claude` launch or when no
tty was captured; a background watcher is armed for an `asha shell` launch;
the watcher sends exactly one Return once the dialog's marker text is seen,
never before; and it fails safe (sends nothing) on timeout or on any error
talking to iTerm2, rather than risk a blind keystroke.
"""

from types import SimpleNamespace

import pytest

import orch.iterm as iterm


# ── _watch_and_confirm_dev_channels_dialog ─────────────────────────────────

def test_noop_for_claude_executor(monkeypatch):
    monkeypatch.setattr(
        iterm.threading, "Thread",
        lambda *a, **kw: pytest.fail("claude never shows this dialog"),
    )
    iterm._watch_and_confirm_dev_channels_dialog("claude", "/dev/ttys001")


def test_noop_without_a_captured_tty(monkeypatch):
    monkeypatch.setattr(
        iterm.threading, "Thread",
        lambda *a, **kw: pytest.fail("nothing to watch without a tty"),
    )
    iterm._watch_and_confirm_dev_channels_dialog("asha shell", None)
    iterm._watch_and_confirm_dev_channels_dialog("asha shell", "")


def test_asha_shell_arms_a_background_watcher(monkeypatch):
    spawned = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            spawned["target"] = target
            spawned["args"] = args
            spawned["daemon"] = daemon

        def start(self):
            spawned["started"] = True

    monkeypatch.setattr(iterm.threading, "Thread", FakeThread)
    iterm._watch_and_confirm_dev_channels_dialog("asha shell", "/dev/ttys001")

    assert spawned["target"] is iterm._confirm_dev_channels_dialog
    assert spawned["args"] == ("/dev/ttys001",)
    assert spawned["daemon"] is True
    assert spawned["started"] is True


# ── _confirm_dev_channels_dialog (the poll loop) ───────────────────────────

def _no_sleep(monkeypatch):
    monkeypatch.setattr(iterm.time, "sleep", lambda s: None)


def test_sends_return_once_marker_appears(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(iterm.time, "monotonic", _counter())

    contents = iter(["", "", "WARNING: Loading development channels for..."])
    monkeypatch.setattr(iterm, "_session_contents", lambda tty: next(contents))
    sent = []
    monkeypatch.setattr(iterm, "_send_return", lambda tty: sent.append(tty))

    iterm._confirm_dev_channels_dialog("/dev/ttys001")

    assert sent == ["/dev/ttys001"]


def test_never_sends_before_the_marker_is_seen(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(iterm.time, "monotonic", _counter())

    # Three blank polls, then the window closed (contents went missing: None).
    contents = iter(["", "", None])
    monkeypatch.setattr(iterm, "_session_contents", lambda tty: next(contents, None))
    sent = []
    monkeypatch.setattr(iterm, "_send_return", lambda tty: sent.append(tty))
    monkeypatch.setattr(iterm, "_DEV_CHANNELS_POLL_TIMEOUT", 3.5)  # lets all 3 polls run

    iterm._confirm_dev_channels_dialog("/dev/ttys001")

    assert sent == []


def test_fails_safe_on_timeout_with_no_marker_ever_seen(monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(iterm.time, "monotonic", _counter())
    monkeypatch.setattr(iterm, "_session_contents", lambda tty: "")
    sent = []
    monkeypatch.setattr(iterm, "_send_return", lambda tty: sent.append(tty))
    monkeypatch.setattr(iterm, "_DEV_CHANNELS_POLL_TIMEOUT", 3.0)

    iterm._confirm_dev_channels_dialog("/dev/ttys001")

    assert sent == []


def test_fails_safe_rather_than_raise_on_an_applescript_error(monkeypatch):
    monkeypatch.setattr(iterm.time, "monotonic", lambda: 0.0)

    def boom(tty):
        raise RuntimeError("osascript blew up")

    monkeypatch.setattr(iterm, "_session_contents", boom)
    sent = []
    monkeypatch.setattr(iterm, "_send_return", lambda tty: sent.append(tty))

    iterm._confirm_dev_channels_dialog("/dev/ttys001")  # must not raise

    assert sent == []


def _counter():
    """A fake time.monotonic() that advances by 1.0 on every call."""
    state = {"t": 0.0}

    def tick():
        state["t"] += 1.0
        return state["t"]

    return tick


# ── _session_contents / _send_return (the raw AppleScript calls) ──────────

def test_session_contents_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(
        iterm.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout="WARNING: Loading development channels\n", stderr="",
        ),
    )
    assert "Loading development channels" in iterm._session_contents("/dev/ttys001")


def test_session_contents_is_none_when_the_script_fails(monkeypatch):
    monkeypatch.setattr(
        iterm.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="not scriptable"),
    )
    assert iterm._session_contents("/dev/ttys001") is None


def test_send_return_reports_whether_the_session_was_found(monkeypatch):
    monkeypatch.setattr(
        iterm.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="true\n", stderr=""),
    )
    assert iterm._send_return("/dev/ttys001") is True

    monkeypatch.setattr(
        iterm.subprocess, "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="false\n", stderr=""),
    )
    assert iterm._send_return("/dev/ttys001") is False
