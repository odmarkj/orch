"""
orch daemon — long-lived process supervised by launchd.

Owns ~/.orch/state.db (SQLite), exposes an HTTP API on localhost, runs the
bridge dispatcher and the auto-retry / cleanup janitor. Replaces the
file-based bridge protocol that lived in comm.py + app.py._handle_bridge_request.

Subcommands (used via `orch daemon …`):
  run         — run the daemon loop (foreground; what launchd invokes)
  install     — write + load the launchd plist
  uninstall   — unload + remove the plist
  status      — show launchd state + a /healthz probe
  start/stop  — kickstart / kill the launchd job
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import state
from .config import (
    bridge_max_concurrent_total,
    daemon_port,
    daemon_record_retention_days,
    daemon_worker_timeout_seconds,
    bridge_retry_backoff_seconds,
)


LAUNCHD_LABEL = "com.orch.daemon"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
LOG_DIR = Path.home() / ".orch"
LOG_FILE = LOG_DIR / "daemon.log"
PID_FILE = LOG_DIR / "daemon.pid"

log = logging.getLogger("orch.daemon")


# ── Pubsub for /bridges/stream ────────────────────────────────────────────

class _PubSub:
    """In-process broadcast queue. SSE handlers subscribe; dispatcher and
    janitor publish state-change events. Keep payloads small."""

    def __init__(self) -> None:
        self._subscribers: list[Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Queue:
        q: Queue = Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: str, payload: dict | None = None) -> None:
        msg = json.dumps({"event": event, "payload": payload or {}, "ts": state.now_iso()})
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except Exception:
                # Drop on full — slow consumer's problem, not ours.
                pass


_PUBSUB = _PubSub()


# ── Dispatcher ────────────────────────────────────────────────────────────

class _Dispatcher:
    """Polls SQLite for pending bridges, runs them on worker threads.

    Per-target serialization: at most one inflight bridge per target project
    (configurable). Total inflight capped by daemon config.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("dispatcher started")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.exception("dispatcher tick errored")
                # A poisoned connection would otherwise make every remaining
                # tick fail identically, once a second, until someone restarts
                # the daemon. Drop it; the next tick reconnects.
                if state.is_connection_fault(exc):
                    state.close_conn()
            self._stop.wait(1.0)
        log.info("dispatcher stopping; waiting for workers")
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.join(timeout=30)

    def _tick(self) -> None:
        # Reap finished workers
        with self._lock:
            for tgt in list(self._workers):
                if not self._workers[tgt].is_alive():
                    self._workers[tgt].join(timeout=0)
                    del self._workers[tgt]

        if state.count_inflight_total() >= bridge_max_concurrent_total():
            return

        for target in state.pending_targets():
            if state.count_inflight_total() >= bridge_max_concurrent_total():
                return
            with self._lock:
                if target in self._workers:
                    continue  # already running for this target
            limit = self._target_limit(target)
            if state.count_inflight_for_target(target) >= limit:
                continue

            bridge = state.claim_next_pending(target=target)
            if bridge is None:
                continue
            log.info(
                "claimed id=%s %s -> %s intent=%s",
                bridge["id"], bridge["source_project"],
                bridge["target_project"], bridge["intent"],
            )
            _PUBSUB.publish("claimed", {"id": bridge["id"], "target": target})
            t = threading.Thread(
                target=self._run_one, args=(bridge,),
                name=f"bridge-{bridge['id']}", daemon=True,
            )
            with self._lock:
                self._workers[target] = t
            t.start()

    def _target_limit(self, target: str) -> int:
        from .discovery import discover_projects
        for p in discover_projects():
            if p.name == target:
                val = p._read_orch_config_section_str("bridge", "max_concurrent_as_target")
                if val:
                    try:
                        return max(1, int(val))
                    except ValueError:
                        pass
                break
        return 1  # serial per-target by default

    def _run_one(self, bridge: dict) -> None:
        from .bridge_worker import run_bridge
        bid = bridge["id"]
        try:
            run_bridge(bridge)
        except Exception:
            log.exception("worker for %s raised", bid)
            try:
                state.mark_failed(
                    bid, error="worker raised unhandled exception",
                    error_class=state.ERROR_TRANSIENT,
                    next_retry_at=state.now_iso(),
                )
            except Exception:
                log.exception("could not record failure for %s", bid)
        finally:
            final = state.get_bridge(bid)
            log.info("worker done id=%s status=%s", bid, final["status"] if final else "?")
            _PUBSUB.publish(
                "settled",
                {"id": bid, "status": final["status"] if final else "unknown"},
            )


# ── Janitor ───────────────────────────────────────────────────────────────

class _Janitor:
    """Runs every 60s: stale-inflight reset, transient-failure requeue,
    record retention."""

    INTERVAL_SECONDS = 60

    def __init__(self) -> None:
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("janitor started")
        # Run once immediately on start to reap a previous crash.
        try:
            self._tick()
        except Exception:
            log.exception("janitor initial tick errored")
        while not self._stop.wait(self.INTERVAL_SECONDS):
            try:
                self._tick()
            except Exception as exc:
                log.exception("janitor tick errored")
                if state.is_connection_fault(exc):
                    state.close_conn()

    def _tick(self) -> None:
        # 1. Recover stale-inflight (claimed but worker is dead/timed-out).
        timeout_s = daemon_worker_timeout_seconds()
        for stale in state.find_stale_inflight(older_than_seconds=timeout_s):
            pid = stale.get("worker_pid")
            if pid and _pid_alive(pid):
                continue  # legitimately running, just slow
            log.warning("stale inflight id=%s; resetting to retry", stale["id"])
            state.reset_inflight_to_failed(
                stale["id"], error="worker timeout / orphaned",
            )
            _PUBSUB.publish("stale_recovered", {"id": stale["id"]})

        # 2. Requeue transient failures whose next_retry_at has passed.
        from .config import bridge_max_retries
        for failed in state.find_retry_eligible(max_retries=bridge_max_retries()):
            if state.requeue_for_retry(failed["id"]):
                log.info(
                    "requeued id=%s (retry %d/%d)",
                    failed["id"], failed["retry_count"] + 1, bridge_max_retries(),
                )
                _PUBSUB.publish("requeued", {"id": failed["id"]})

        # 3. Retention
        deleted = state.delete_old_completed(retention_days=daemon_record_retention_days())
        if deleted:
            log.info("janitor pruned %d old records", deleted)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


# ── HTTP API ──────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "orch-daemon/1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        log.debug("http " + format, *args)

    def finish(self) -> None:
        # ThreadingHTTPServer runs each TCP connection in its own thread, and
        # state._get_conn() caches a sqlite connection in thread-local storage.
        # That connection never gets reused after this thread dies, so close it
        # here to avoid leaking fds (db/-wal/-shm) until the daemon hits
        # RLIMIT_NOFILE and sqlite starts failing to open the database.
        try:
            super().finish()
        finally:
            state.close_conn()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            raise ValueError("invalid JSON body")

    # ── Routes ─────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/healthz":
            # A health check that ignores the DB is worse than none: the daemon
            # can be listening yet unable to serve any real request (e.g. fd
            # exhaustion -> "unable to open database file"). Probe the DB so an
            # unhealthy daemon reports unhealthy.
            try:
                state.ping()
            except Exception as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "ts": state.now_iso()},
                    status=503,
                )
                return
            self._send_json({"ok": True, "ts": state.now_iso()})
            return

        if path == "/bridges":
            bridges = state.list_bridges(
                source=qs.get("source", [None])[0],
                target=qs.get("target", [None])[0],
                status=qs.get("status", [None])[0],
                limit=int(qs.get("limit", ["200"])[0]),
            )
            self._send_json(bridges)
            return

        if path.startswith("/bridges/"):
            rest = path[len("/bridges/"):]
            if rest == "stream":
                self._sse_stream()
                return
            if "/" not in rest:
                bid = rest
                bridge = state.get_bridge(bid)
                if not bridge:
                    self._send_text("not found", 404)
                    return
                bridge["events"] = state.get_events(bid)
                bridge["queued_position"] = state.queued_position(bid)
                self._send_json(bridge)
                return

        self._send_text("not found", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/bridges":
            try:
                body = self._read_json()
            except ValueError as e:
                self._send_text(str(e), 400)
                return
            try:
                self._submit(body)
            except ValueError as e:
                self._send_text(str(e), 400)
            return

        if path.startswith("/bridges/") and path.endswith("/cancel"):
            bid = path[len("/bridges/"):-len("/cancel")]
            ok = state.cancel_bridge(bid)
            if not ok:
                self._send_text("not cancellable (already settled or not found)", 409)
                return
            _PUBSUB.publish("cancelled", {"id": bid})
            self._send_json({"id": bid, "status": state.STATUS_CANCELLED})
            return

        if path.startswith("/bridges/") and path.endswith("/retry"):
            bid = path[len("/bridges/"):-len("/retry")]
            self._retry(bid)
            return

        self._send_text("not found", 404)

    def _submit(self, body: dict) -> None:
        required = ("source_project", "source_path", "target", "intent",
                    "summary", "context", "request")
        for k in required:
            if not str(body.get(k, "")).strip():
                self._send_text(f"missing field: {k}", 400)
                return
        if body["intent"] not in state.VALID_INTENTS:
            self._send_text(f"invalid intent: {body['intent']!r}", 400)
            return

        # A source_path at or above the projects root (e.g. $HOME, which has
        # .claude/ and .orch/ and used to fool the CLI's auto-detection) gets
        # bind-mounted over itself by the worker's sandbox, shadowing the
        # ~/Apps mount and making the freshly created worktree invisible.
        # Reject it here so the submitter gets an actionable error instead of
        # three doomed retries.
        from .discovery import SITES_ROOT
        src = Path(os.path.normpath(body["source_path"]))
        if src == SITES_ROOT or src in SITES_ROOT.parents:
            self._send_text(
                f"source_path {src} is not a project directory (it is at or "
                f"above the projects root {SITES_ROOT}); run from inside a "
                "project or pass --source and --source-path",
                400,
            )
            return

        sub = state.BridgeSubmission(
            source_project=body["source_project"],
            source_path=body["source_path"],
            target_project=body["target"],
            intent=body["intent"],
            summary=body["summary"],
            context=body["context"],
            request=body["request"],
            relevant_files=list(body.get("relevant_files") or []),
            parent_id=body.get("parent_id") or None,
            client_request_id=body.get("client_request_id") or None,
        )
        try:
            bridge = state.insert_bridge(sub)
        except ValueError as e:
            self._send_text(str(e), 400)
            return
        bridge["queued_position"] = state.queued_position(bridge["id"])
        _PUBSUB.publish("submitted", {"id": bridge["id"], "target": bridge["target_project"]})
        self._send_json(bridge, status=201)

    def _retry(self, bid: str) -> None:
        bridge = state.get_bridge(bid)
        if not bridge:
            self._send_text("not found", 404)
            return
        # Insert a NEW bridge linked to the original via parent_id, leaving the
        # original record intact for audit. This gives a clean retry chain.
        sub = state.BridgeSubmission(
            source_project=bridge["source_project"],
            source_path=bridge["source_path"],
            target_project=bridge["target_project"],
            intent=bridge["intent"],
            summary=bridge["summary"],
            context=bridge["context"],
            request=bridge["request"],
            relevant_files=list(bridge.get("relevant_files") or []),
            parent_id=bridge["id"],
        )
        new_bridge = state.insert_bridge(sub)
        state.add_event(new_bridge["id"], "retried", {"of": bid})
        _PUBSUB.publish("submitted", {"id": new_bridge["id"], "of": bid})
        self._send_json(new_bridge, status=201)

    def _sse_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = _PUBSUB.subscribe()
        try:
            # Initial snapshot lets clients render without a separate fetch.
            snapshot = json.dumps({
                "event": "snapshot",
                "payload": {
                    "pending":  state.list_bridges(status=state.STATUS_PENDING),
                    "inflight": state.list_bridges(status=state.STATUS_INFLIGHT),
                    "recent":   state.list_bridges(limit=20),
                },
                "ts": state.now_iso(),
            })
            self.wfile.write(f"data: {snapshot}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                try:
                    msg = q.get(timeout=15)
                except Empty:
                    # Keep-alive ping so the client connection doesn't time out.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            _PUBSUB.unsubscribe(q)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Run loop ──────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(LOG_FILE)]
    # When run from a terminal, also log to stderr.
    if sys.stderr and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _write_pidfile() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _clear_pidfile() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _bind_server(port: int) -> _Server:
    """Bind to all loopback interfaces. Lima's vzNAT exposes the daemon to
    the VM via host.lima.internal; macOS does not bridge this to physical
    networks, so 0.0.0.0 here is functionally local-only.
    """
    return _Server(("0.0.0.0", port), _Handler)


def run_daemon(verbose: bool = False) -> int:
    _setup_logging(verbose)
    _write_pidfile()

    state.init_db()

    # Recover stale-inflight from a previous crash before accepting new work.
    for stale in state.find_stale_inflight(older_than_seconds=0):
        log.warning("startup: resetting stale inflight id=%s", stale["id"])
        state.reset_inflight_to_failed(stale["id"], error="daemon restarted")

    port = daemon_port()
    server = _bind_server(port)
    log.info("orch daemon listening on port %d (pid %d)", port, os.getpid())

    dispatcher = _Dispatcher()
    janitor = _Janitor()

    threads = [
        threading.Thread(target=server.serve_forever, name="http", daemon=True),
        threading.Thread(target=dispatcher.run, name="dispatcher", daemon=True),
        threading.Thread(target=janitor.run, name="janitor", daemon=True),
    ]
    for t in threads:
        t.start()

    stop_evt = threading.Event()

    def _shutdown(*_: Any) -> None:
        log.info("shutdown signal received")
        stop_evt.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        stop_evt.wait()
    finally:
        log.info("stopping…")
        try:
            server.shutdown()
        except Exception:
            pass
        dispatcher.stop()
        janitor.stop()
        for t in threads:
            t.join(timeout=10)
        _clear_pidfile()
        log.info("stopped")
    return 0


# ── launchd integration ────────────────────────────────────────────────────

def install_launchd() -> int:
    if platform.system() != "Darwin":
        print("orch daemon only runs on macOS", file=sys.stderr)
        return 2
    orch_bin = shutil.which("orch")
    if not orch_bin:
        print("orch not found on PATH; install with `pip install -e .`", file=sys.stderr)
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [orch_bin, "daemon", "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(LOG_FILE),
        "StandardErrorPath": str(LOG_FILE),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": os.environ.get(
                "PATH",
                "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            ),
        },
    }
    with open(LAUNCHD_PLIST, "wb") as f:
        plistlib.dump(plist, f)

    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr}", file=sys.stderr)
        return 1

    print(f"installed: {LAUNCHD_PLIST}")
    print(f"log:       {LOG_FILE}")
    return 0


def uninstall_launchd() -> int:
    if not LAUNCHD_PLIST.exists():
        print("no plist installed")
        return 0
    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    LAUNCHD_PLIST.unlink()
    print(f"removed: {LAUNCHD_PLIST}")
    return 0


def _healthz_probe(timeout: float = 1.0) -> bool:
    try:
        port = daemon_port()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=timeout,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def status_launchd() -> int:
    healthy = _healthz_probe()
    if not LAUNCHD_PLIST.exists():
        if healthy:
            print(f"daemon: running (no launchd plist; foreground process)")
            return 0
        print(f"daemon: not running (no plist at {LAUNCHD_PLIST})")
        return 1

    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"daemon: plist installed but not loaded ({LAUNCHD_PLIST})")
        return 1
    print(result.stdout.rstrip())
    print(f"plist:   {LAUNCHD_PLIST}")
    print(f"log:     {LOG_FILE}")
    print(f"healthz: {'OK' if healthy else 'FAIL'}")
    return 0 if healthy else 1


def kickstart_launchd() -> int:
    if not LAUNCHD_PLIST.exists():
        return install_launchd()
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Fall back to load if kickstart fails (e.g., not yet loaded)
        subprocess.run(
            ["launchctl", "load", str(LAUNCHD_PLIST)],
            capture_output=True,
        )
    print("daemon: kickstarted")
    return 0


def stop_launchd() -> int:
    if not LAUNCHD_PLIST.exists():
        print("no plist installed")
        return 0
    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    print("daemon: stopped")
    return 0


# ── CLI dispatch ───────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch-daemon")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").add_argument("--verbose", "-v", action="store_true")
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    sub.add_parser("start")
    sub.add_parser("stop")
    args = parser.parse_args(argv)

    if args.cmd == "run":
        return run_daemon(verbose=getattr(args, "verbose", False))
    if args.cmd == "install":
        return install_launchd()
    if args.cmd == "uninstall":
        return uninstall_launchd()
    if args.cmd == "status":
        return status_launchd()
    if args.cmd == "start":
        return kickstart_launchd()
    if args.cmd == "stop":
        return stop_launchd()
    parser.error(f"unknown command: {args.cmd}")
    return 2


def daemon_required(*, timeout: float = 1.0) -> str | None:
    """Probe /healthz; return an error message if the daemon is unreachable.

    Used by CLI and TUI to fail fast with a clear message. The TUI passes a
    longer timeout so a momentarily busy daemon isn't mistaken for a dead one.
    """
    if _healthz_probe(timeout=timeout):
        return None
    return (
        "orch-daemon is not running.\n"
        "  Start it with:  orch daemon start\n"
        "  Or install:     orch daemon install"
    )


if __name__ == "__main__":
    sys.exit(main())
