"""HTTP serving layer for the local fetch service.

Serves the contract every Claude session in the VM is told to use:

    POST /fetch                  single URL, synchronous
    POST /fetch/batch            1..10000 URLs, queued
    GET  /fetch/batch/<id>       batch progress
    GET  /fetch/<request_id>     one queued request's result
    GET  /health                 liveness ({"status": "ok"})
    GET  /status                 which tiers are live (orch addition)

The response body is web-fetch's ``FetchResult`` rendered to JSON, so the
documented keys — ``status, tier_used, cached, cost_usd, markdown, chunks,
metadata`` — carry exactly what they did before. ``body`` (raw bytes for
binary payloads) is dropped on the way out: it is an in-process affordance,
not something to push down a socket.

Implementation notes:

* stdlib ``ThreadingHTTPServer`` in front of a single background asyncio
  loop. web-fetch's API is async and its connection pools, breaker and host
  semaphores all want to live on one loop; the alternative (uvicorn/FastAPI)
  would add a web framework to orch for four endpoints.
* Batch work runs as asyncio tasks on that same loop, so the whole service
  is one process and one systemd unit — no separate worker to fall over
  independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse, urlunparse
from uuid import UUID, uuid4

from . import (
    BATCH_DB,
    STATE_DIR,
    base_url,
    batch_concurrency,
    bind,
    web_fetch_dir,
)
from . import diagnostics
from .batch import MAX_URLS, BatchRunner, BatchStore


logger = logging.getLogger("orch.fetchsvc")

MAX_BODY_BYTES = 8 * 1024 * 1024

# Per-fetch knobs a caller may set, with the defaults the previous service
# used.
_FETCH_OPTIONS: dict[str, tuple[type, Any]] = {
    "render": (bool, False),
    "mode": (str, "main"),
    "force_refresh": (bool, False),
    "use_cache": (bool, True),
    "cache_ttl_seconds": (int, 86_400),
    "include_html": (bool, False),
    "extract_links": (bool, True),
    "keep_images": (bool, False),
    "max_chunk_chars": (int, 30_000),
    "overlap_lines": (int, 5),
    "timeout": (float, 60.0),
    "session_id": (str, None),
    "warm": (bool, False),
    "tier": (int, None),
}

_VALID_QUEUES = ("fetch_default", "fetch_bulk")


class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ── request parsing ─────────────────────────────────────────────────────────

def normalize_url(raw: Any) -> str:
    """Validate and lightly normalize a caller-supplied URL.

    Mirrors what pydantic's ``HttpUrl`` did for the previous service —
    lowercase scheme/host and an explicit ``/`` path — so the ``url`` echoed
    back is byte-identical to what callers have been seeing.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise HttpError(422, "url is required and must be a string")
    parsed = urlparse(raw.strip())
    if parsed.scheme.lower() not in ("http", "https"):
        raise HttpError(422, f"url must be http(s): {raw!r}")
    if not parsed.netloc:
        raise HttpError(422, f"url has no host: {raw!r}")
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def unknown_fields(
    body: dict[str, Any], *, extra_allowed: tuple[str, ...] = (),
) -> list[str]:
    """Body keys this service does not act on.

    Reported, never fatal. The previous service was a pydantic model with the
    default ``extra="ignore"``, so unknown keys were accepted silently; making
    them a 422 would break any caller that has been passing one. They still
    get a warning in the log and a note in ``metadata``, because a silently
    dropped option is the same class of bug as a silently skipped tier.
    """
    known = set(_FETCH_OPTIONS) | set(extra_allowed) | {"url", "urls"}
    return sorted(set(body) - known)


def parse_options(body: dict[str, Any], *, extra_allowed: tuple[str, ...] = ()) -> dict[str, Any]:
    """Coerce the per-fetch options out of a request body.

    Unknown keys are ignored (see ``unknown_fields``); a *known* key with an
    unusable value is a 422, which is what the previous service did too.
    """
    out: dict[str, Any] = {}
    for name, (kind, default) in _FETCH_OPTIONS.items():
        if name not in body or body[name] is None:
            out[name] = default
            continue
        value = body[name]
        if kind is bool:
            if not isinstance(value, bool):
                raise HttpError(422, f"{name} must be a boolean")
        elif kind is int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise HttpError(422, f"{name} must be an integer")
        elif kind is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HttpError(422, f"{name} must be a number")
            value = float(value)
        elif kind is str and not isinstance(value, str):
            raise HttpError(422, f"{name} must be a string")
        out[name] = value

    if out["mode"] not in ("main", "full"):
        raise HttpError(422, "mode must be 'main' or 'full'")
    if out["timeout"] <= 0:
        raise HttpError(422, "timeout must be positive")
    return out


# ── JSON rendering ──────────────────────────────────────────────────────────

def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        # Never on the wire. Binary payloads are described in `markdown` and
        # flagged by `binary`/`content_type`; the bytes are for in-process
        # consumers only.
        return None
    if is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    return str(value)


def render_result(result: Any) -> dict[str, Any]:
    payload = to_jsonable(asdict(result) if is_dataclass(result) else result)
    payload.pop("body", None)
    return payload


# ── the service ─────────────────────────────────────────────────────────────

class FetchService:
    """Thin adapter over ``web_fetch``: build a request, run it, shape the
    response, and make a configuration gap say so."""

    def __init__(self) -> None:
        self.store = BatchStore(BATCH_DB)
        self.started_at = datetime.now(timezone.utc)
        self.runner: BatchRunner | None = None

    # -- diagnostics ---------------------------------------------------------

    def tier_configuration(self) -> dict[str, Any]:
        try:
            return diagnostics.tier_configuration()
        except Exception as exc:  # noqa: BLE001 — never fail a fetch on this
            logger.warning("tier inventory unavailable: %s", exc)
            return {"configured": [], "unconfigured": []}

    def self_check(self) -> dict[str, Any]:
        """Everything a "why is this not working" question needs, in one
        place. Logged at startup and served at GET /status.

        The whole point: a tier that is skipped for configuration reasons is
        *named*, at startup, before anyone has to reverse-engineer an empty
        ``attempts`` list.
        """
        from web_fetch.config import get_config

        cfg = get_config()
        config_info = self.tier_configuration()
        checks: dict[str, Any] = {
            "service": "orch-fetch",
            "implementation": str(web_fetch_dir()),
            "listen": base_url(),
            "started_at": self.started_at.isoformat(),
            "stateless": bool(cfg.stateless),
            "database_configured": bool(cfg.database_url),
            "redis_configured": bool(cfg.redis_url),
            "tiers_live": [t["tier"] for t in config_info.get("configured") or []],
            "tiers_skipped": diagnostics.skipped(config_info, render=False),
            "batch_pending": self.store.pending_count(),
        }
        checks["database_reachable"] = self._probe_database(cfg)
        return checks

    @staticmethod
    def _probe_database(cfg: Any) -> bool | None:
        if cfg.stateless or not cfg.database_url:
            return None
        try:
            from web_fetch.db import connection

            with connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("postgres unreachable: %s", exc)
            return False

    # -- fetching ------------------------------------------------------------

    async def fetch(self, url: str, options: dict[str, Any]) -> dict[str, Any]:
        from web_fetch.models import FetchRequest
        from web_fetch.service import fetch_one

        render = bool(options.get("render"))
        config_info = self.tier_configuration()

        # Per-request visibility. DEBUG on the happy path (one line per fetch
        # is noise once it works), but the pre-flight refusal below is a
        # warning, because it is always actionable.
        skipped = diagnostics.skipped(config_info, render=render)
        if skipped:
            logger.debug("fetch %s: skipping %s", url,
                         ", ".join(f"{s['tier']}[{s['reason']}]" for s in skipped))

        refusal = diagnostics.preflight(config_info, render=render)
        if refusal is not None:
            logger.warning("fetch %s refused: %s", url, refusal)
            return diagnostics.not_configured_result(
                request_id=str(uuid4()),
                url=url,
                cache_key="",
                reason=refusal,
                config_info=config_info,
                render=render,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        req = FetchRequest(url=url, **options)
        result = await fetch_one(req)
        payload = render_result(result)
        return diagnostics.annotate(payload, config_info, render=render)

    # -- batches -------------------------------------------------------------

    def submit_batch(
        self, urls: list[str], *, name: str | None, queue: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        batch_id, request_ids = self.store.create(
            urls, name=name, queue=queue, options=options,
        )
        logger.info("batch %s queued: %d url(s) on %s", batch_id, len(urls), queue)
        return {
            "batch_id": batch_id,
            "total": len(request_ids),
            "request_ids": request_ids,
        }


# ── event loop plumbing ─────────────────────────────────────────────────────

class LoopThread:
    """A single asyncio loop on its own thread. Handler threads hand it
    coroutines; web-fetch's pools and semaphores stay on one loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="orch-fetch-loop", daemon=True,
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=10)

    def submit(self, coro, timeout: float):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


# ── HTTP handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "orch-fetch/1.0"
    protocol_version = "HTTP/1.1"

    service: FetchService
    loop: LoopThread

    # -- helpers -------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        # Any bail-out before the body is consumed leaves unread bytes in the
        # socket, which a keep-alive connection would then parse as the next
        # request line. Close instead of desynchronizing the stream.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise HttpError(422, "invalid Content-Length")
        if length <= 0:
            raise HttpError(422, "request body is required")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise HttpError(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpError(422, f"invalid JSON body: {exc}")
        if not isinstance(body, dict):
            raise HttpError(422, "request body must be a JSON object")
        return body

    def _dispatch(self, handler) -> None:
        try:
            status, payload = handler()
        except HttpError as exc:
            status, payload = exc.status, {"detail": exc.detail}
        except Exception as exc:  # noqa: BLE001 — a 500 beats a dropped socket
            logger.exception("unhandled error on %s", self.path)
            status, payload = 500, {"detail": f"internal error: {exc}"}
        self._send(status, payload)

    # -- routes --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            # Byte-for-byte what the previous service returned. Anything
            # richer lives at /status so a health probe stays a health probe.
            self._dispatch(lambda: (200, {"status": "ok"}))
        elif path == "/status":
            self._dispatch(lambda: (200, self.service.self_check()))
        elif path.startswith("/fetch/batch/"):
            self._dispatch(lambda: self._get_batch(path[len("/fetch/batch/"):]))
        elif path.startswith("/fetch/"):
            self._dispatch(lambda: self._get_request(path[len("/fetch/"):]))
        else:
            self._send(404, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/fetch":
            self._dispatch(self._post_fetch)
        elif path == "/fetch/batch":
            self._dispatch(self._post_batch)
        else:
            self.close_connection = True
            self._send(404, {"detail": "not found"})

    def _post_fetch(self) -> tuple[int, Any]:
        body = self._read_json()
        url = normalize_url(body.get("url"))
        options = parse_options(body)
        ignored = unknown_fields(body)
        if ignored:
            logger.warning("fetch %s: ignoring unknown field(s): %s",
                           url, ", ".join(ignored))
        # The socket must outlive the fetch: web-fetch escalates through
        # several tiers, each with its own `timeout`, so the wall-clock budget
        # is a multiple of it.
        budget = options["timeout"] * 6 + 30
        try:
            payload = self.loop.submit(
                self.service.fetch(url, options), timeout=budget,
            )
        except FutureTimeout:
            raise HttpError(504, f"fetch exceeded the {budget:.0f}s budget")
        if ignored:
            payload.setdefault("metadata", {})["ignored_fields"] = ignored
        return 200, payload

    def _post_batch(self) -> tuple[int, Any]:
        body = self._read_json()
        raw_urls = body.get("urls")
        if not isinstance(raw_urls, list) or not raw_urls:
            raise HttpError(422, "urls must be a non-empty list")
        if len(raw_urls) > MAX_URLS:
            raise HttpError(422, f"urls exceeds the {MAX_URLS} limit")
        urls = [normalize_url(u) for u in raw_urls]

        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise HttpError(422, "name must be a string")
        queue = body.get("queue") or "fetch_default"
        if queue not in _VALID_QUEUES:
            raise HttpError(422, f"queue must be one of {', '.join(_VALID_QUEUES)}")

        ignored = unknown_fields(body, extra_allowed=("name", "queue"))
        if ignored:
            logger.warning("batch: ignoring unknown field(s): %s",
                           ", ".join(ignored))
        options = parse_options(body, extra_allowed=("name", "queue"))
        return 200, self.service.submit_batch(
            urls, name=name, queue=queue, options=options,
        )

    def _get_batch(self, batch_id: str) -> tuple[int, Any]:
        _require_uuid(batch_id, "batch_id")
        batch = self.service.store.batch(batch_id)
        if batch is None:
            raise HttpError(404, "batch not found")
        return 200, batch

    def _get_request(self, request_id: str) -> tuple[int, Any]:
        _require_uuid(request_id, "request_id")
        result = self.service.store.result(request_id)
        if result is None:
            raise HttpError(404, "request not found")
        return 200, result


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except ValueError:
        raise HttpError(400, f"invalid {label} {value!r}")


# ── entry point ─────────────────────────────────────────────────────────────

def _configure_logging() -> None:
    level = os.environ.get("ORCH_FETCH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _log_self_check(service: FetchService) -> None:
    """The 30-second diagnosis. Startup says, in the journal, exactly which
    tiers are live and what each dead one is waiting for — so "every fetch
    comes back blocked" is answered by `journalctl --user -u orch-fetch`
    instead of by reading the escalation loop."""
    checks = service.self_check()
    logger.info("serving %s from %s", checks["listen"], checks["implementation"])
    logger.info(diagnostics.summary_line(service.tier_configuration()))
    if not checks["tiers_live"]:
        logger.error(
            "NO TIER IS CONFIGURED — every fetch will fail with "
            "error_class=not_configured until credentials are set"
        )
    elif checks["tiers_live"] == ["direct"]:
        logger.warning(
            "only the direct tier is live: any site that needs a proxy or a "
            "browser will fail with error_class=not_configured, not 'blocked'"
        )
    if checks["database_configured"] and checks["database_reachable"] is False:
        logger.error("postgres is configured but unreachable — cache and "
                     "per-host learning are disabled for this run")
    if not checks["database_configured"] and not checks["stateless"]:
        logger.warning("WEB_FETCH_DATABASE_URL is unset — running without "
                       "cache or per-host learning")


def serve(argv: list[str] | None = None) -> int:
    _configure_logging()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(web_fetch_dir() / "src"))
    try:
        import web_fetch  # noqa: F401
    except ImportError as exc:
        logger.error("cannot import web_fetch from %s: %s", web_fetch_dir(), exc)
        logger.error("run `orch fetch install` to (re)provision the service")
        return 1

    service = FetchService()
    _log_self_check(service)

    loop = LoopThread()
    loop.start()

    runner = BatchRunner(
        service.store,
        service.fetch,
        concurrency=batch_concurrency(),
    )
    service.runner = runner
    asyncio.run_coroutine_threadsafe(_start_runner(runner), loop.loop).result(30)

    host, port = bind()
    Handler.service = service
    Handler.loop = loop
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    def shutdown(signum, _frame):
        logger.info("signal %s — shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("listening on %s:%d", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        try:
            asyncio.run_coroutine_threadsafe(runner.stop(), loop.loop).result(15)
        except Exception:  # noqa: BLE001
            logger.debug("batch runner shutdown raised", exc_info=True)
        loop.stop()
        logger.info("stopped")
    return 0


async def _start_runner(runner: BatchRunner) -> None:
    runner.start()
