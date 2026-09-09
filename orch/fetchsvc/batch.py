"""Batch queue for /fetch/batch — SQLite store plus in-process workers.

The service it replaces used Postgres rows fanned out to a separate
Procrastinate worker process (``asha-worker``). That is two units, two
failure modes and a second thing that can hold the port hostage. Here the
workers are asyncio tasks inside the one serving process, so the whole fetch
service is a single unit orch can start, stop and health-check as a unit.

Durability is still real: the queue lives in a WAL-mode SQLite file under
``~/.orch/fetch``, so a restart resumes rather than loses a 10k-URL batch —
``recover()`` puts anything left mid-flight back on the queue at startup.
Per-host politeness is not this module's job; web-fetch's own host semaphores
and circuit breaker govern that regardless of how many workers pull.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    name        TEXT,
    total       INTEGER NOT NULL,
    queue       TEXT NOT NULL,
    options     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    request_id    TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    url           TEXT NOT NULL,
    status        TEXT NOT NULL,
    tier_used     TEXT,
    error_class   TEXT,
    error_message TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL,
    result        TEXT
);
CREATE INDEX IF NOT EXISTS items_batch_seq ON items (batch_id, seq);
-- Partial index: the worker's hot query is "next pending", and the pending
-- set is a vanishing fraction of the table once a big batch drains.
CREATE INDEX IF NOT EXISTS items_pending ON items (seq) WHERE status = 'pending';
"""

MAX_URLS = 10_000

# Statuses an item can hold. 'done' and 'cached' both mean success — they are
# kept apart because the single-fetch contract distinguishes them too.
TERMINAL = ("done", "cached", "blocked", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BatchStore:
    """Thread-safe SQLite queue. Connections are per-call; the lock
    serializes the read-modify-write claims, which is plenty for a
    single-process service and keeps 'database is locked' off the table."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Per-call connection, always closed.

        sqlite3.Connection's own context manager commits but does *not*
        close — using it bare leaks a file descriptor per request, which is
        exactly how orch's daemon once ran itself out of RLIMIT_NOFILE. So
        the transaction and the descriptor are both managed here.
        """
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # ── writes ──────────────────────────────────────────────────────────────

    def create(
        self,
        urls: Iterable[str],
        *,
        name: str | None,
        queue: str,
        options: dict[str, Any],
    ) -> tuple[str, list[str]]:
        batch_id = str(uuid.uuid4())
        created = _now()
        rows = []
        request_ids = []
        for seq, url in enumerate(urls):
            request_id = str(uuid.uuid4())
            request_ids.append(request_id)
            rows.append((request_id, batch_id, seq, url, "pending", created))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO batches (id, name, total, queue, options, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, name, len(rows), queue, json.dumps(options), created),
            )
            conn.executemany(
                "INSERT INTO items (request_id, batch_id, seq, url, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        return batch_id, request_ids

    def claim(self) -> dict[str, Any] | None:
        """Take the oldest pending item and mark it running, atomically."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT i.request_id, i.batch_id, i.url, b.options "
                "FROM items i JOIN batches b ON b.id = i.batch_id "
                "WHERE i.status = 'pending' ORDER BY i.created_at, i.seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE items SET status = 'running' WHERE request_id = ?",
                (row["request_id"],),
            )
        return {
            "request_id": row["request_id"],
            "batch_id": row["batch_id"],
            "url": row["url"],
            "options": json.loads(row["options"]),
        }

    def finish(self, request_id: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE items SET status = ?, tier_used = ?, error_class = ?, "
                "error_message = ?, finished_at = ?, result = ? "
                "WHERE request_id = ?",
                (
                    payload.get("status") or "failed",
                    payload.get("tier_used"),
                    payload.get("error_class"),
                    payload.get("error_message"),
                    _now(),
                    json.dumps(payload),
                    request_id,
                ),
            )

    def requeue(self, request_id: str) -> None:
        """Put a claimed item back without recording an outcome."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE items SET status = 'pending' WHERE request_id = ?",
                (request_id,),
            )

    def recover(self) -> int:
        """Requeue anything the previous process died holding."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE items SET status = 'pending' WHERE status = 'running'"
            )
            return cur.rowcount or 0

    def pending_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM items WHERE status IN ('pending','running')"
            ).fetchone()
        return int(row["n"])

    # ── reads ───────────────────────────────────────────────────────────────

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            head = conn.execute(
                "SELECT id, name, total, created_at FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if head is None:
                return None
            rows = conn.execute(
                "SELECT request_id, url, status, tier_used, error_class, "
                "error_message, finished_at FROM items WHERE batch_id = ? "
                "ORDER BY seq",
                (batch_id,),
            ).fetchall()
        counts: dict[str, int] = {}
        items = []
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            items.append({
                "request_id": row["request_id"],
                "url": row["url"],
                "status": row["status"],
                "tier_used": row["tier_used"],
                "error_class": row["error_class"],
                "error_message": row["error_message"],
                "finished_at": row["finished_at"],
            })
        return {
            "batch_id": head["id"],
            "name": head["name"],
            "total": head["total"],
            "created_at": head["created_at"],
            "counts": counts,
            "items": items,
        }

    def result(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, url, result FROM items WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["result"]:
            return json.loads(row["result"])
        # Queued but not run yet — answer in the response shape callers
        # already handle rather than 404-ing a request id we did issue.
        return {
            "request_id": request_id,
            "status": row["status"],
            "url": row["url"],
            "markdown": "",
            "chunks": [],
            "metadata": {},
            "chunk_count": 0,
            "cached": False,
            "tier_used": None,
            "cost_usd": 0.0,
            "attempts": [],
            "error_class": None,
            "error_message": None,
        }


class BatchRunner:
    """N asyncio workers draining ``store`` through ``run_one``."""

    def __init__(
        self,
        store: BatchStore,
        run_one: Callable[[str, dict[str, Any]], Any],
        *,
        concurrency: int,
        idle_sleep: float = 0.5,
    ):
        self.store = store
        self.run_one = run_one
        self.concurrency = concurrency
        self.idle_sleep = idle_sleep
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    def start(self) -> None:
        requeued = self.store.recover()
        if requeued:
            logger.info("batch: requeued %d item(s) left running by a restart",
                        requeued)
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._worker(i)) for i in range(self.concurrency)
        ]
        logger.info("batch: %d worker(s) started, %d item(s) queued",
                    self.concurrency, self.store.pending_count())

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — shutdown must not raise
                logger.debug("batch worker raised during shutdown", exc_info=True)
        self._tasks = []

    async def _worker(self, index: int) -> None:
        while not self._stopping.is_set():
            claimed = await asyncio.to_thread(self.store.claim)
            if claimed is None:
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self.idle_sleep,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            request_id = claimed["request_id"]
            try:
                payload = await self.run_one(claimed["url"], claimed["options"])
            except asyncio.CancelledError:
                # Shutting down mid-item: hand it back so recover() isn't the
                # only thing that can rescue it.
                self.store.requeue(request_id)
                raise
            except Exception as exc:  # noqa: BLE001 — one bad URL must not kill a worker
                logger.exception("batch worker %d: %s failed", index, claimed["url"])
                payload = {
                    "request_id": request_id,
                    "status": "failed",
                    "url": claimed["url"],
                    "error_class": "internal",
                    "error_message": str(exc),
                    "markdown": "",
                    "chunks": [],
                    "metadata": {},
                    "chunk_count": 0,
                    "cached": False,
                    "tier_used": None,
                    "cost_usd": 0.0,
                    "attempts": [],
                }
            payload["request_id"] = request_id
            await asyncio.to_thread(self.store.finish, request_id, payload)
