"""
orch bridge — CLI client for the daemon's bridge HTTP API.

Thin shim: every command is one HTTP call. Source project is auto-detected
from the working directory (matching by Project.path under SITES_ROOT).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import daemon_port


def _daemon_host() -> str:
    """Pick the right hostname to reach the daemon.

    On the macOS host the daemon listens on loopback, so 127.0.0.1 is correct.
    Inside the Lima VM, loopback is the VM itself; the host daemon is reachable
    via the Lima-injected host.lima.internal entry. Detect by resolving that
    name — if it exists, we're in the VM (or any environment that has aliased
    it intentionally).
    """
    explicit = os.environ.get("ORCH_DAEMON_HOST")
    if explicit:
        return explicit
    try:
        socket.gethostbyname("host.lima.internal")
        return "host.lima.internal"
    except OSError:
        return "127.0.0.1"


def _api_base() -> str:
    return f"http://{_daemon_host()}:{daemon_port()}"


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    url = _api_base() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "null")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        return e.code, payload
    except urllib.error.URLError:
        from .daemon import daemon_required
        msg = daemon_required() or "daemon unreachable"
        print(msg, file=sys.stderr)
        sys.exit(2)


def _detect_source_project() -> tuple[str, str] | None:
    """Walk up from cwd looking for a project root (presence of .claude/ + .orch/)."""
    cwd = Path.cwd().resolve()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / ".claude").is_dir() and (candidate / ".orch").is_dir():
            return candidate.name, str(candidate)
    return None


def _read_optional_file(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text()
    except OSError as e:
        print(f"failed to read {path}: {e}", file=sys.stderr)
        sys.exit(1)


# ── Commands ───────────────────────────────────────────────────────────────

def cmd_submit(args: argparse.Namespace) -> int:
    source = args.source
    source_path = args.source_path
    if not source or not source_path:
        detected = _detect_source_project()
        if detected is None:
            print(
                "could not auto-detect source project from cwd; "
                "pass --source and --source-path", file=sys.stderr,
            )
            return 1
        source = source or detected[0]
        source_path = source_path or detected[1]

    context = args.context or _read_optional_file(args.context_file)
    request_text = args.request or _read_optional_file(args.request_file)
    if not context.strip():
        print("missing --context or --context-file", file=sys.stderr)
        return 1
    if not request_text.strip():
        print("missing --request or --request-file", file=sys.stderr)
        return 1

    body = {
        "source_project": source,
        "source_path": source_path,
        "target": args.target,
        "intent": args.intent,
        "summary": args.summary,
        "context": context,
        "request": request_text,
        "relevant_files": args.relevant_file or [],
    }
    if args.parent_id:
        body["parent_id"] = args.parent_id
    if args.client_request_id:
        body["client_request_id"] = args.client_request_id

    status, payload = _http("POST", "/bridges", body)
    if status >= 400:
        print(f"submit failed [{status}]: {payload}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"submitted: {payload['id']}")
        print(f"  status: {payload['status']}  depth: {payload.get('depth', 0)}  "
              f"queued_position: {payload.get('queued_position', '?')}")
    return 0


def _render_event(e: dict, *, verbose: bool) -> None:
    """Pretty-print one bridge event. Headless-output events get a summary
    one-liner plus, on failure or --verbose, the captured stdout/stderr."""
    ts = e["ts"]
    name = e["event"]
    detail = e.get("detail") or {}
    if name == "headless_output" and isinstance(detail, dict):
        rc = detail.get("returncode")
        phase = detail.get("phase", "?")
        timed_out = detail.get("timed_out")
        stdout = detail.get("stdout") or ""
        stderr = detail.get("stderr") or ""
        size = f"stdout={len(stdout)}B stderr={len(stderr)}B"
        flag = " TIMEOUT" if timed_out else (f" rc={rc}" if rc not in (0, None) else "")
        print(f"  {ts}  {name} [{phase}{flag} {size}]")
        failed = bool(timed_out) or (rc not in (0, None))
        if failed or verbose:
            if stderr.strip():
                print("    stderr:")
                for line in stderr.rstrip().splitlines():
                    print(f"      {line}")
            if stdout.strip() and (verbose or not stderr.strip()):
                print("    stdout:")
                for line in stdout.rstrip().splitlines():
                    print(f"      {line}")
        return
    print(f"  {ts}  {name}{(' ' + json.dumps(detail)) if detail else ''}")


def cmd_status(args: argparse.Namespace) -> int:
    status, payload = _http("GET", f"/bridges/{args.id}")
    if status == 404:
        print(f"bridge not found: {args.id}", file=sys.stderr)
        return 1
    if status >= 400:
        print(f"status failed [{status}]: {payload}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"id:       {payload['id']}")
    print(f"status:   {payload['status']}")
    print(f"flow:     {payload['source_project']} → {payload['target_project']}  ({payload['intent']})")
    print(f"summary:  {payload['summary']}")
    print(f"depth:    {payload['depth']}   retry_count: {payload['retry_count']}")
    if payload.get("error"):
        print(f"error:    [{payload.get('error_class') or '?'}] {payload['error']}")
    if payload.get("pr_url"):
        print(f"pr:       {payload['pr_url']}")
    if payload.get("branch"):
        print(f"branch:   {payload['branch']}")
    print(f"created:  {payload['created_at']}")
    if payload.get("claimed_at"):
        print(f"claimed:  {payload['claimed_at']}")
    if payload.get("completed_at"):
        print(f"settled:  {payload['completed_at']}")
    if payload.get("events"):
        print()
        print("events:")
        for e in payload["events"]:
            _render_event(e, verbose=args.verbose)
    if payload.get("result"):
        print()
        print("result:")
        print(payload["result"])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    qs = []
    if args.source:
        qs.append(f"source={args.source}")
    if args.target:
        qs.append(f"target={args.target}")
    if args.status:
        qs.append(f"status={args.status}")
    qs.append(f"limit={args.limit}")
    path = "/bridges?" + "&".join(qs)
    status, payload = _http("GET", path)
    if status >= 400:
        print(f"list failed [{status}]: {payload}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if not payload:
        print("(no bridges)")
        return 0
    print(f"{'id':<26} {'status':<10} {'src → tgt':<40} {'intent':<7} summary")
    for b in payload:
        flow = f"{b['source_project']} → {b['target_project']}"
        summary = b['summary'] if len(b['summary']) <= 40 else b['summary'][:37] + "…"
        print(f"{b['id']:<26} {b['status']:<10} {flow:<40} {b['intent']:<7} {summary}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    status, payload = _http("POST", f"/bridges/{args.id}/cancel")
    if status >= 400:
        print(f"cancel failed [{status}]: {payload}", file=sys.stderr)
        return 1
    print(f"cancelled: {args.id}")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    status, payload = _http("POST", f"/bridges/{args.id}/retry")
    if status >= 400:
        print(f"retry failed [{status}]: {payload}", file=sys.stderr)
        return 1
    print(f"retried as: {payload['id']}")
    return 0


# ── Argument parser ────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "submit", help="submit a cross-project bridge request",
        description=(
            "Submit a cross-project bridge request. Keep --context and "
            "--request to roughly a page each: all projects share the VM "
            "filesystem, so long detail (logs, diffs, full reports) belongs "
            "in a file that the request references by path (e.g. "
            "/tmp/report.md), not inline in the request itself."
        ),
    )
    s.add_argument("--target", required=True, help="target project name")
    s.add_argument("--intent", required=True, choices=("fix", "review", "query", "inform"))
    s.add_argument("--summary", required=True, help="one-line description")
    s.add_argument("--context", help="context text, ~a page (or use --context-file)")
    s.add_argument("--context-file", help="path to a file containing context")
    s.add_argument("--request", help="request text, ~a page (or use --request-file)")
    s.add_argument("--request-file", help="path to a file containing the request")
    s.add_argument("--relevant-file", action="append", default=[],
                   help="file in target project to highlight (repeatable)")
    s.add_argument("--parent-id", help="parent bridge id (for chained requests)")
    s.add_argument("--client-request-id", help="optional dedupe key for safe resubmit")
    s.add_argument("--source", help="override source project name (auto-detected from cwd)")
    s.add_argument("--source-path", help="override source project path")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("status", help="show full record + event log")
    s.add_argument("id", help="bridge id")
    s.add_argument("--json", action="store_true")
    s.add_argument(
        "--verbose", "-v", action="store_true",
        help="print full headless stdout/stderr for every turn, not just failures",
    )
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("list", help="list bridges, optionally filtered")
    s.add_argument("--source")
    s.add_argument("--target")
    s.add_argument("--status",
                   choices=("pending", "inflight", "completed", "failed",
                            "rejected", "cancelled"))
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("cancel", help="cancel a pending or inflight bridge")
    s.add_argument("id")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("retry", help="resubmit a failed bridge as a new record")
    s.add_argument("id")
    s.set_defaults(func=cmd_retry)

    from . import bridge_migrate
    bridge_migrate.add_subparser(sub)

    return p


def main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
