"""Regression tests for the WAL-unlink watchdog.

The incident (2026-09-28): a bridge agent in the Lima VM opened the host's
``~/.orch/state.db`` over virtiofs to read an earlier bridge's result. POSIX
locks don't cross virtiofs, so on close it believed it was the last
connection, checkpointed, and deleted ``-wal``/``-shm``. The daemon kept
committing into the unlinked WAL: the bridge that was running lost its
"completed" write, and ~2h later every request started failing with
``disk I/O error``. Nothing noticed for 21 hours.

The daemon now pins the journal files' inodes at startup; the janitor exits
the process (launchd restarts it) the moment they stop matching disk.
"""

import os
import subprocess
import sys

import pytest

import orch.bridge_worker as bw
import orch.daemon as daemon_mod
import orch.state as state


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "DB_PATH", tmp_path / "state.db")
    old = getattr(state._local, "conn", None)
    if old is not None:
        old.close()
    state._local.conn = None
    state.init_db()
    state.pin_journal_files()
    yield tmp_path / "state.db"
    conn = getattr(state._local, "conn", None)
    if conn is not None:
        conn.close()
    state._local.conn = None
    state._journal_pins.clear()


def _wal(db):
    return db.with_name(db.name + "-wal")


def test_pins_both_journal_files(temp_db):
    assert set(state._journal_pins) == {"-wal", "-shm"}
    assert state.journal_files_replaced() is None


def test_other_host_process_opening_db_is_not_a_false_positive(temp_db):
    # A well-behaved host process (the TUI, a sqlite3 shell) sees our lock and
    # leaves the WAL alone when it closes.
    subprocess.run(
        [sys.executable, "-c",
         "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]);"
         "c.execute('select count(*) from bridges').fetchone(); c.close()",
         str(temp_db)],
        check=True,
    )
    assert state.journal_files_replaced() is None


def test_deleted_wal_is_detected(temp_db):
    os.unlink(_wal(temp_db))
    assert "deleted" in state.journal_files_replaced()


def test_recreated_wal_is_detected(temp_db):
    # What the incident actually left on disk: the old WAL unlinked and a
    # fresh one created by the next opener.
    os.unlink(_wal(temp_db))
    _wal(temp_db).write_bytes(b"")
    assert "replaced" in state.journal_files_replaced()


def test_janitor_exits_before_touching_poisoned_db(temp_db, monkeypatch):
    os.unlink(_wal(temp_db))

    def _must_not_run(*a, **kw):
        raise AssertionError("janitor queried the DB after detecting poisoning")

    monkeypatch.setattr(state, "find_stale_inflight", _must_not_run)
    reasons = []
    janitor = daemon_mod._Janitor(on_poisoned=reasons.append)
    janitor._tick()

    assert len(reasons) == 1 and "deleted" in reasons[0]
    assert janitor._stop.is_set()


def test_bridge_prompt_forbids_opening_state_db():
    b = {
        "source_project": "src", "source_path": "/tmp/src",
        "context": "ctx", "request": "req", "intent": "query",
        "relevant_files": [],
    }
    prompt = bw._build_prompt(b)
    assert f"Never open `{state.DB_PATH}`" in prompt
    assert "orch bridge status <id>" in prompt
