"""
orch SQLite state — single source of truth for the daemon.

Bridge state today; lifecycle / plan / comm tables in future iterations
(schema designed with those in mind from day one). Do not import from this
module outside the daemon process or short-lived CLI helpers — it is the
daemon's owned data layer.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)

DB_PATH = Path.home() / ".orch" / "state.db"

SCHEMA_VERSION = 2

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bridges (
  id                TEXT PRIMARY KEY,
  source_project    TEXT NOT NULL,
  source_path       TEXT NOT NULL,
  target_project    TEXT NOT NULL,
  intent            TEXT NOT NULL,
  summary           TEXT NOT NULL,
  context           TEXT NOT NULL,
  request           TEXT NOT NULL,
  relevant_files    TEXT,
  parent_id         TEXT REFERENCES bridges(id),
  depth             INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL,
  worker_pid        INTEGER,
  worktree_path     TEXT,
  branch            TEXT,
  result            TEXT,
  pr_url            TEXT,
  error             TEXT,
  error_class       TEXT,
  retry_count       INTEGER NOT NULL DEFAULT 0,
  next_retry_at     TEXT,
  client_request_id TEXT,
  created_at        TEXT NOT NULL,
  claimed_at        TEXT,
  completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_bridges_status        ON bridges(status, created_at);
CREATE INDEX IF NOT EXISTS idx_bridges_target_status ON bridges(target_project, status);
CREATE INDEX IF NOT EXISTS idx_bridges_source_recent ON bridges(source_project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bridges_retry_due     ON bridges(status, next_retry_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bridges_client_dedupe
  ON bridges(source_project, client_request_id)
  WHERE client_request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS bridge_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  bridge_id   TEXT NOT NULL REFERENCES bridges(id),
  event       TEXT NOT NULL,
  detail      TEXT,
  ts          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_bridge ON bridge_events(bridge_id, ts);

-- Session worktrees (the `w` shortcut). Each row tracks a Claude session
-- launched into a fresh git worktree. `id` doubles as the correlation_id
-- written to /tmp/orch-{project}-{pid}.worktree so list_sessions can match
-- a live pid back to its worktree row.
CREATE TABLE IF NOT EXISTS worktrees (
  id                TEXT PRIMARY KEY,
  project_name      TEXT NOT NULL,
  project_path      TEXT NOT NULL,
  worktree_path     TEXT NOT NULL UNIQUE,
  branch            TEXT NOT NULL UNIQUE,
  base_branch       TEXT NOT NULL,
  jsonl_dir         TEXT NOT NULL,
  status            TEXT NOT NULL,
  pid               INTEGER,
  created_at        TEXT NOT NULL,
  closed_at         TEXT,
  last_pr_check_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_worktrees_status  ON worktrees(status, created_at);
CREATE INDEX IF NOT EXISTS idx_worktrees_project ON worktrees(project_name, status);
"""


# ── Status / event vocab ────────────────────────────────────────────────────

STATUS_PENDING   = "pending"
STATUS_INFLIGHT  = "inflight"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_REJECTED  = "rejected"
STATUS_CANCELLED = "cancelled"

ERROR_TRANSIENT = "transient"
ERROR_PERMANENT = "permanent"

VALID_INTENTS = ("fix", "review", "query", "inform")


# ── ID generation ───────────────────────────────────────────────────────────

def new_bridge_id() -> str:
    """Time-sortable, opaque-ish ID. `br_<base32-time><random>`."""
    ms = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"br_{ms:012x}{rand}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ── Connection management ───────────────────────────────────────────────────

_local = threading.local()

# Errors that mean "this connection is unusable", not "this query was bad".
# A cached connection can go bad while the file on disk is perfectly fine —
# a transient I/O fault or a WAL/shm reset under an idle reader leaves the
# pager reading garbage, and SQLite then reports one of these forever. Since
# _get_conn() caches per thread, a long-lived process (the daemon's dispatcher
# and janitor loops, the TUI's worker threads) would keep reusing the poisoned
# connection and fail on every subsequent call until restarted by hand — which
# is exactly how a one-second blip turns into an hour of dead `w` presses.
_CONN_FAULT_MARKERS = (
    "file is not a database",
    "disk i/o error",
    "unable to open database file",
    "database disk image is malformed",
    "attempt to write a readonly database",
)


def is_connection_fault(exc: BaseException) -> bool:
    """True if *exc* means the connection is poisoned and should be reopened.

    Distinguishes connection-level faults from genuine query errors (a bad
    constraint, a typo'd column) which reopening would not fix.
    """
    if not isinstance(exc, sqlite3.Error):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONN_FAULT_MARKERS)


def _open_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        isolation_level=None,           # autocommit; we manage txns explicitly
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _probe(conn: sqlite3.Connection) -> None:
    """Cheapest read that actually touches the database file.

    `SELECT 1` is answered from thin air and proves nothing — user_version
    lives in the header, so this opens a read transaction and reads page 1,
    which is what surfaces a poisoned pager.
    """
    conn.execute("PRAGMA user_version").fetchone()


def _get_conn() -> sqlite3.Connection:
    """Per-thread connection. SQLite connections are not thread-safe.

    Self-healing: a cached connection is probed before reuse and replaced if
    it has gone bad, so a transient fault costs one reconnect instead of
    wedging the process until someone notices and restarts it.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        if conn.in_transaction:
            # Mid-transaction the caller owns the connection; swapping it out
            # here would silently drop their uncommitted work. Let the error
            # surface so transaction() can ROLLBACK and the caller retry.
            return conn
        try:
            _probe(conn)
            return conn
        except sqlite3.Error as exc:
            log.warning("sqlite connection unusable (%s); reopening", exc)
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None

    conn = _open_conn()
    _local.conn = conn
    return conn


def close_conn() -> None:
    """Close and drop this thread's connection, if any.

    Long-lived threads (dispatcher, janitor) keep their connection for the
    process lifetime, but the HTTP server spawns one short-lived thread per
    TCP connection. Without this, each request thread leaks an unclosed
    sqlite connection (3 fds: db/-wal/-shm); under load the daemon blows past
    RLIMIT_NOFILE and every subsequent connect fails with
    ``unable to open database file``. Call this when a request thread ends.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def ping() -> None:
    """Cheap liveness probe for the data layer. Opens (or reuses) this
    thread's connection and reads from the database file. Raises sqlite3.Error
    if the database can't be reached — callers (e.g. /healthz) should treat a
    raised exception as unhealthy rather than reporting OK."""
    _probe(_get_conn())


def init_db() -> None:
    """Create schema and stamp version. Idempotent."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    conn.executescript(SCHEMA_SQL)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    current = int(row["version"]) if row else 0
    if current < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now_iso()),
        )


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = _get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as rollback_exc:
            # A poisoned connection can't even roll back. Drop it so the next
            # caller reconnects, and let the original error propagate — the
            # rollback failure is a symptom, not the cause worth reporting.
            log.warning("rollback failed (%s); dropping connection", rollback_exc)
            close_conn()
        if is_connection_fault(exc):
            close_conn()
        raise
    else:
        conn.execute("COMMIT")


# ── Records ─────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("relevant_files"):
        try:
            d["relevant_files"] = json.loads(d["relevant_files"])
        except json.JSONDecodeError:
            d["relevant_files"] = []
    else:
        d["relevant_files"] = []
    return d


# ── Bridge CRUD ─────────────────────────────────────────────────────────────

@dataclass
class BridgeSubmission:
    source_project: str
    source_path: str
    target_project: str
    intent: str
    summary: str
    context: str
    request: str
    relevant_files: list[str]
    parent_id: str | None = None
    client_request_id: str | None = None


def insert_bridge(sub: BridgeSubmission) -> dict:
    """Insert a new bridge in `pending` status, computing depth from parent.

    Honors `client_request_id` for idempotency: if a row already exists for
    (source_project, client_request_id), returns it unchanged.
    """
    if sub.intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {sub.intent!r}")

    conn = _get_conn()

    if sub.client_request_id:
        existing = conn.execute(
            "SELECT * FROM bridges WHERE source_project=? AND client_request_id=?",
            (sub.source_project, sub.client_request_id),
        ).fetchone()
        if existing is not None:
            return _row_to_dict(existing)  # type: ignore[return-value]

    depth = 0
    if sub.parent_id:
        parent = conn.execute(
            "SELECT depth FROM bridges WHERE id=?", (sub.parent_id,)
        ).fetchone()
        if parent is None:
            raise ValueError(f"parent_id not found: {sub.parent_id}")
        depth = int(parent["depth"]) + 1

    bid = new_bridge_id()
    ts = now_iso()
    relevant_json = json.dumps(sub.relevant_files) if sub.relevant_files else None

    with transaction():
        conn.execute(
            """
            INSERT INTO bridges
              (id, source_project, source_path, target_project, intent,
               summary, context, request, relevant_files, parent_id, depth,
               status, retry_count, client_request_id, created_at)
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                bid, sub.source_project, sub.source_path, sub.target_project,
                sub.intent, sub.summary, sub.context, sub.request,
                relevant_json, sub.parent_id, depth,
                STATUS_PENDING, sub.client_request_id, ts,
            ),
        )
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, detail, ts) VALUES (?, ?, ?, ?)",
            (bid, "submitted", None, ts),
        )

    return _row_to_dict(
        conn.execute("SELECT * FROM bridges WHERE id=?", (bid,)).fetchone()
    )  # type: ignore[return-value]


def get_bridge(bid: str) -> dict | None:
    row = _get_conn().execute("SELECT * FROM bridges WHERE id=?", (bid,)).fetchone()
    return _row_to_dict(row)


def get_events(bid: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT event, detail, ts FROM bridge_events WHERE bridge_id=? ORDER BY id",
        (bid,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


def list_bridges(
    *,
    source: str | None = None,
    target: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict]:
    sql = "SELECT * FROM bridges WHERE 1=1"
    args: list[Any] = []
    if source:
        sql += " AND source_project=?"
        args.append(source)
    if target:
        sql += " AND target_project=?"
        args.append(target)
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    rows = _get_conn().execute(sql, args).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def queued_position(bid: str) -> int:
    """1-indexed position in the pending queue, or 0 if not pending."""
    conn = _get_conn()
    me = conn.execute(
        "SELECT created_at, status FROM bridges WHERE id=?", (bid,)
    ).fetchone()
    if me is None or me["status"] != STATUS_PENDING:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS pos FROM bridges
        WHERE status=? AND (created_at, id) <= (?, ?)
        """,
        (STATUS_PENDING, me["created_at"], bid),
    ).fetchone()
    return int(row["pos"]) if row else 0


def add_event(bid: str, event: str, detail: dict | None = None) -> None:
    detail_json = json.dumps(detail) if detail else None
    _get_conn().execute(
        "INSERT INTO bridge_events (bridge_id, event, detail, ts) VALUES (?, ?, ?, ?)",
        (bid, event, detail_json, now_iso()),
    )


def claim_next_pending(target: str | None = None) -> dict | None:
    """Atomically claim the oldest pending bridge whose retry-time has passed.

    If `target` is given, restricts to that target project (used for per-target
    serialization where the dispatcher checks target locks before claiming).
    """
    conn = _get_conn()
    ts = now_iso()
    sql_select = """
        SELECT id FROM bridges
        WHERE status=?
          AND (next_retry_at IS NULL OR next_retry_at <= ?)
    """
    args: list[Any] = [STATUS_PENDING, ts]
    if target:
        sql_select += " AND target_project=?"
        args.append(target)
    sql_select += " ORDER BY created_at, id LIMIT 1"

    with transaction():
        row = conn.execute(sql_select, args).fetchone()
        if row is None:
            return None
        bid = row["id"]
        conn.execute(
            "UPDATE bridges SET status=?, claimed_at=? WHERE id=? AND status=?",
            (STATUS_INFLIGHT, ts, bid, STATUS_PENDING),
        )
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, ts) VALUES (?, ?, ?)",
            (bid, "claimed", ts),
        )
    return get_bridge(bid)


def set_inflight_meta(
    bid: str, *, worker_pid: int | None = None,
    worktree_path: str | None = None, branch: str | None = None,
) -> None:
    fields = []
    args: list[Any] = []
    if worker_pid is not None:
        fields.append("worker_pid=?"); args.append(worker_pid)
    if worktree_path is not None:
        fields.append("worktree_path=?"); args.append(worktree_path)
    if branch is not None:
        fields.append("branch=?"); args.append(branch)
    if not fields:
        return
    args.append(bid)
    _get_conn().execute(
        f"UPDATE bridges SET {', '.join(fields)} WHERE id=?", args,
    )


def mark_completed(
    bid: str, *, result: str, pr_url: str | None = None, branch: str | None = None,
) -> None:
    ts = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE bridges
            SET status=?, result=?, pr_url=?, branch=?, completed_at=?, error=NULL, error_class=NULL
            WHERE id=?
            """,
            (STATUS_COMPLETED, result, pr_url, branch, ts, bid),
        )
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, detail, ts) VALUES (?, ?, ?, ?)",
            (bid, "completed", None, ts),
        )


def mark_failed(
    bid: str, *, error: str, error_class: str,
    next_retry_at: str | None = None,
) -> None:
    ts = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE bridges
            SET status=?, error=?, error_class=?, next_retry_at=?, completed_at=?
            WHERE id=?
            """,
            (STATUS_FAILED, error, error_class, next_retry_at, ts, bid),
        )
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, detail, ts) VALUES (?, ?, ?, ?)",
            (bid, "failed", json.dumps({"error_class": error_class, "error": error[:500]}), ts),
        )


def mark_rejected(bid: str, *, reason: str) -> None:
    ts = now_iso()
    with transaction() as conn:
        conn.execute(
            """
            UPDATE bridges
            SET status=?, error=?, error_class=?, completed_at=?
            WHERE id=?
            """,
            (STATUS_REJECTED, reason, ERROR_PERMANENT, ts, bid),
        )
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, detail, ts) VALUES (?, ?, ?, ?)",
            (bid, "failed", json.dumps({"reason": reason}), ts),
        )


def cancel_bridge(bid: str) -> bool:
    """Mark a pending or inflight bridge as cancelled. Returns True if changed."""
    ts = now_iso()
    with transaction() as conn:
        cur = conn.execute(
            """
            UPDATE bridges
            SET status=?, completed_at=?
            WHERE id=? AND status IN (?, ?)
            """,
            (STATUS_CANCELLED, ts, bid, STATUS_PENDING, STATUS_INFLIGHT),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, ts) VALUES (?, ?, ?)",
            (bid, "cancelled", ts),
        )
    return True


def requeue_for_retry(bid: str) -> bool:
    """Janitor path: bump retry_count, clear next_retry_at gate, set pending."""
    ts = now_iso()
    with transaction() as conn:
        cur = conn.execute(
            """
            UPDATE bridges
            SET status=?, retry_count=retry_count+1,
                claimed_at=NULL, completed_at=NULL,
                next_retry_at=NULL,
                worker_pid=NULL, worktree_path=NULL, branch=NULL
            WHERE id=? AND status=?
            """,
            (STATUS_PENDING, bid, STATUS_FAILED),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "INSERT INTO bridge_events (bridge_id, event, ts) VALUES (?, ?, ?)",
            (bid, "retried", ts),
        )
    return True


def find_retry_eligible(*, max_retries: int) -> list[dict]:
    """Failed records whose error_class is transient and retry budget remains."""
    ts = now_iso()
    rows = _get_conn().execute(
        """
        SELECT * FROM bridges
        WHERE status=? AND error_class=?
          AND retry_count < ?
          AND (next_retry_at IS NOT NULL AND next_retry_at <= ?)
        ORDER BY next_retry_at
        """,
        (STATUS_FAILED, ERROR_TRANSIENT, max_retries, ts),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def find_stale_inflight(*, older_than_seconds: int) -> list[dict]:
    """Inflight rows whose claimed_at is older than the cutoff. Janitor uses
    this to detect dead workers (daemon crash mid-run, etc.)."""
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    rows = _get_conn().execute(
        "SELECT * FROM bridges WHERE status=? AND claimed_at < ?",
        (STATUS_INFLIGHT, cutoff_iso),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


def reset_inflight_to_failed(bid: str, *, error: str) -> None:
    """Crash recovery: mark a stuck inflight as transient-failed so the
    janitor can pick it up for retry."""
    mark_failed(bid, error=error, error_class=ERROR_TRANSIENT, next_retry_at=now_iso())


def delete_old_completed(*, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="seconds")
    delete_filter = (
        STATUS_COMPLETED, STATUS_FAILED, STATUS_REJECTED, STATUS_CANCELLED, cutoff_iso,
    )
    with transaction() as conn:
        # Foreign key cascade isn't on, so we have to detach/wipe references by
        # hand before deleting the bridges themselves.
        #
        # 1. bridge_events.bridge_id -> bridges.id
        conn.execute(
            """
            DELETE FROM bridge_events
            WHERE bridge_id IN (
              SELECT id FROM bridges
              WHERE status IN (?, ?, ?, ?) AND completed_at < ?
            )
            """,
            delete_filter,
        )
        # 2. bridges.parent_id -> bridges.id (self-reference). Any row pointing
        # at a bridge we're about to delete must be detached first, whether it
        # survives the prune or is itself in the delete set — FK cascade is off
        # and DELETE order within a statement isn't guaranteed, so a parent can
        # otherwise be removed while a child still references it.
        conn.execute(
            """
            UPDATE bridges SET parent_id = NULL
            WHERE parent_id IN (
              SELECT id FROM bridges
              WHERE status IN (?, ?, ?, ?) AND completed_at < ?
            )
            """,
            delete_filter,
        )
        cur = conn.execute(
            """
            DELETE FROM bridges
            WHERE status IN (?, ?, ?, ?) AND completed_at < ?
            """,
            delete_filter,
        )
        return cur.rowcount


def count_inflight_total() -> int:
    row = _get_conn().execute(
        "SELECT COUNT(*) AS n FROM bridges WHERE status=?", (STATUS_INFLIGHT,)
    ).fetchone()
    return int(row["n"]) if row else 0


def count_inflight_for_target(target: str) -> int:
    row = _get_conn().execute(
        "SELECT COUNT(*) AS n FROM bridges WHERE status=? AND target_project=?",
        (STATUS_INFLIGHT, target),
    ).fetchone()
    return int(row["n"]) if row else 0


# ── Worktree CRUD ───────────────────────────────────────────────────────────

# Status vocabulary for worktree rows.
WT_ACTIVE        = "active"          # session live in this worktree
WT_KEPT          = "kept"            # session closed, has commits — GC checks for merged PR
WT_KEPT_DIRTY    = "kept-dirty"      # session closed, no commits but dirty tree — GC removes after age
WT_KEPT_CLEAN    = "kept-clean"      # session closed, no commits + clean — kept for resume, GC ages out
WT_ABANDONED     = "abandoned"       # GC marked: commits but no merged PR after 30d (surfaced, not deleted)
WT_REMOVED_CLEAN = "removed-clean"   # cleanup at close: no commits + clean → removed
WT_REMOVED_MERGED = "removed-merged" # GC: matching PR merged on remote → removed
WT_REMOVED_STALE = "removed-stale"   # GC: dirty-only > age threshold → removed
WT_FAILED        = "failed"          # spawn failed; row kept for audit


def new_worktree_id() -> str:
    """Opaque, time-sortable. `wt_<base16-time><rand>`. Doubles as correlation_id."""
    ms = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"wt_{ms:012x}{rand}"


def insert_worktree(
    *,
    project_name: str,
    project_path: str,
    worktree_path: str,
    branch: str,
    base_branch: str,
    jsonl_dir: str,
    wt_id: str | None = None,
) -> str:
    """Insert a new worktree row in 'active' status. Returns the id."""
    wid = wt_id or new_worktree_id()
    _get_conn().execute(
        """
        INSERT INTO worktrees
          (id, project_name, project_path, worktree_path, branch, base_branch,
           jsonl_dir, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (wid, project_name, project_path, worktree_path, branch, base_branch,
         jsonl_dir, WT_ACTIVE, now_iso()),
    )
    return wid


def get_worktree(wid: str) -> dict | None:
    row = _get_conn().execute(
        "SELECT * FROM worktrees WHERE id=?", (wid,)
    ).fetchone()
    return dict(row) if row else None


def list_worktrees(*, status: str | None = None, project: str | None = None) -> list[dict]:
    sql = "SELECT * FROM worktrees WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if project:
        sql += " AND project_name=?"
        args.append(project)
    sql += " ORDER BY created_at DESC"
    rows = _get_conn().execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_active_worktrees() -> list[dict]:
    """All worktrees whose session is supposedly still live."""
    return list_worktrees(status=WT_ACTIVE)


def update_worktree_pid(wid: str, pid: int | None) -> None:
    _get_conn().execute("UPDATE worktrees SET pid=? WHERE id=?", (pid, wid))


def update_worktree_status(wid: str, status: str) -> None:
    _get_conn().execute(
        "UPDATE worktrees SET status=? WHERE id=?", (status, wid),
    )


def mark_worktree_closed(wid: str, status: str) -> None:
    _get_conn().execute(
        "UPDATE worktrees SET status=?, closed_at=? WHERE id=?",
        (status, now_iso(), wid),
    )


def set_worktree_pr_check(wid: str) -> None:
    _get_conn().execute(
        "UPDATE worktrees SET last_pr_check_at=? WHERE id=?", (now_iso(), wid),
    )


def delete_worktree_row(wid: str) -> None:
    _get_conn().execute("DELETE FROM worktrees WHERE id=?", (wid,))


def pending_targets() -> list[str]:
    """Distinct targets with at least one ready-to-run pending bridge."""
    ts = now_iso()
    rows = _get_conn().execute(
        """
        SELECT DISTINCT target_project FROM bridges
        WHERE status=? AND (next_retry_at IS NULL OR next_retry_at <= ?)
        ORDER BY target_project
        """,
        (STATUS_PENDING, ts),
    ).fetchall()
    return [r["target_project"] for r in rows]
