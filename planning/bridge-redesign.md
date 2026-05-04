# Bridge System Redesign

## What's broken today

The cross-project bridge (`comm.py` + `app.py:_handle_bridge_request`) is a file-based queue with five different state locations that can desync:

- `.orch/bridge_request` — live request (single slot per project)
- `.orch/bridge_request.processing` — claim marker
- `.orch/bridge_requests/<id>.json` — archive of completed requests
- `.orch/bridge_responses/<id>.json` — archive of responses
- `.orch/_bridge_depth` — propagated recursion counter

Concrete failures observed:

1. **Single-slot filename** — `bridge_request` can hold one in-flight request at a time. When concurrent agents try to fire, the second has nowhere to put the file. We've seen agents improvise by writing directly into `bridge_requests/` (the archive dir), where nothing reads from. Five May-3 files in `~/Apps/k3s/.orch/bridge_requests/` are dead because of this.
2. **No ack** — agents write a file and have no way to confirm it was received. They re-send. Several stuck files explicitly reference each other ("a prior request never produced a response, so I'm re-sending").
3. **Dispatcher tied to TUI lifetime** — `_scan_pending_bridges` runs on a 25s timer inside the Textual app. Close the TUI, no scan, no dispatch.
4. **Virtiofs FSEvents drop** — VM-originated writes don't reliably fire macOS FSEvents. The 25s scan was the patch, but it only runs while TUI is open.
5. **Depth off-by-one** — orchestrator increments `_bridge_depth` on write *and* the agent CLAUDE.md instructions say "include value + 1." Depth grows by 2 per hop. With `MAX_BRIDGE_DEPTH=1` even the second hop in a chain is auto-rejected.
6. **No visibility** — can't answer "what happened, what's happening, what's next." The bridge.log helps post-mortem but doesn't give a queryable real-time view.
7. **`_active_bridges` set leak** — the depth-rejection path in `app.py:1947` early-returns without `discard()`-ing the project from the in-memory set, permanently locking that project's dispatcher.

## What we're replacing it with

Three layers, one daemon:

- **SQLite** at `~/.orch/state.db` — single source of truth. Owns bridge state today, lifecycle / plan / comm state in future iterations (schema designed with those tables in mind from day one).
- **HTTP API** on localhost — agent submission, status polling, TUI subscription.
- **launchd-supervised `orch-daemon`** — runs the dispatcher 100% of the time, mirroring the credbroker pattern. **Required** — TUI and CLI hard-fail with a clear error if the daemon isn't reachable, with a one-liner to start it.

CLI and TUI both become clients of the same HTTP API. Agents submit via `orch bridge submit` (a thin shim over HTTP) instead of writing files.

The existing `orch/bridge.py` (mobile dashboard HTTP server) is deleted as part of this work — the name is reclaimed for the new daemon and the mobile UI is no longer needed.

## SQLite schema

```sql
CREATE TABLE bridges (
  id              TEXT PRIMARY KEY,           -- ulid, returned to submitter as ack
  source_project  TEXT NOT NULL,
  source_path     TEXT NOT NULL,
  target_project  TEXT NOT NULL,
  intent          TEXT NOT NULL,              -- fix|review|query|inform
  summary         TEXT NOT NULL,
  context         TEXT NOT NULL,
  request         TEXT NOT NULL,
  relevant_files  TEXT,                       -- JSON array
  parent_id       TEXT REFERENCES bridges(id),-- NULL for root requests
  depth           INTEGER NOT NULL DEFAULT 0, -- computed by daemon from parent chain
  status          TEXT NOT NULL,              -- pending|inflight|completed|failed|rejected|cancelled
  worker_pid      INTEGER,
  worktree_path   TEXT,
  branch          TEXT,
  result          TEXT,
  pr_url          TEXT,
  error           TEXT,
  error_class     TEXT,                       -- transient|permanent (drives auto-retry)
  retry_count     INTEGER NOT NULL DEFAULT 0,
  next_retry_at   TEXT,                       -- when janitor should requeue (NULL = no retry)
  client_request_id TEXT,                     -- optional dedupe key from submitter
  created_at      TEXT NOT NULL,
  claimed_at      TEXT,
  completed_at    TEXT
);
CREATE UNIQUE INDEX idx_bridges_client_dedupe
  ON bridges(source_project, client_request_id)
  WHERE client_request_id IS NOT NULL;

CREATE INDEX idx_bridges_status        ON bridges(status, created_at);
CREATE INDEX idx_bridges_target_status ON bridges(target_project, status);
CREATE INDEX idx_bridges_source_recent ON bridges(source_project, created_at DESC);

CREATE TABLE bridge_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  bridge_id   TEXT NOT NULL REFERENCES bridges(id),
  event       TEXT NOT NULL,   -- submitted|claimed|prompt_sent|clarification_sent|clarification_received|pr_created|completed|failed|cancelled|retried
  detail      TEXT,            -- arbitrary JSON
  ts          TEXT NOT NULL
);
CREATE INDEX idx_events_bridge ON bridge_events(bridge_id, ts);
```

WAL mode for concurrent reads while the daemon writes. Three core questions become single SELECTs:

- "What's going to happen?" → `WHERE status='pending'`
- "What's happening now?" → `WHERE status='inflight'`
- "What happened?" → `bridge_events JOIN bridges`

**Depth is computed, not sent.** The daemon walks `parent_id` in SQL on submit. Agents never include a depth field.

## HTTP API

Bound to `127.0.0.1` and the Lima host-gateway interface (so VM clients reach it via `host.lima.internal`). **No authentication** — localhost is trusted, and the VM is treated as part of that trust boundary. Not bound to physical network interfaces; not reachable off-machine.

| Method | Path | Purpose |
|--------|------|---------|
| POST   | `/bridges` | submit a request, returns `{id, status, queued_position}` |
| GET    | `/bridges/<id>` | full record + event log |
| GET    | `/bridges?source=&target=&status=` | filtered list |
| POST   | `/bridges/<id>/cancel` | cancel pending or kill inflight worker |
| POST   | `/bridges/<id>/retry` | replay a failed bridge as a new record (with `parent_id` link) |
| GET    | `/bridges/stream` | SSE feed of state changes (TUI subscribes) |
| GET    | `/healthz` | daemon liveness probe |

Submit body:
```json
{
  "source_project": "k3s",
  "target": "lde-dash",
  "intent": "fix",
  "summary": "...",
  "context": "...",
  "request": "...",
  "relevant_files": ["..."],
  "parent_id": "br_01HXY..."  // optional, links to parent for depth tracking
}
```

Submit response:
```json
{
  "id": "br_01HXY...",
  "status": "pending",
  "queued_position": 3,
  "depth": 1
}
```

The synchronous `id` return is the ack — the agent now has a handle to poll. No more "did it get picked up?"

## Daemon design

`orch-daemon` is a long-lived process started by `~/Library/LaunchAgents/com.orch.daemon.plist` with `KeepAlive=true`. Mirrors `credbroker`'s plist structure.

Three threads inside the daemon:

1. **HTTP server** — handles all API endpoints. Reads/writes SQLite directly.
2. **Dispatcher** — polls SQLite for `status='pending' AND (next_retry_at IS NULL OR next_retry_at <= now)`, claims with `UPDATE ... WHERE id = (SELECT id FROM bridges WHERE ... ORDER BY created_at LIMIT 1) RETURNING *`, runs the subagent, writes the result back.
3. **Janitor** — every 60s: (a) resets stale `inflight` records (claimed > worker timeout, no PID alive) to `failed` with `error_class='transient'`; (b) requeues `failed` records whose `error_class='transient'` and `retry_count < max_retries` by setting `status='pending'` and bumping `retry_count`; (c) cleans completed records older than 30 days.

Per-target serialization: dispatcher takes a target-keyed in-memory lock. Two bridges to different targets run concurrently; two bridges to the *same* target run sequentially. Max-total-inflight is configurable.

### Auto-retry policy

Errors are classified at point of failure:

- **Transient** — VM not running, network/SSH error, git push failure, Claude API rate-limit/timeout, worker timeout. Eligible for automatic retry.
- **Permanent** — invalid request (parse/schema failure), target project not found, depth exceeded, target worktree creation failed for non-recoverable reason. Not retried; user must `orch bridge retry` explicitly.

Retry schedule: exponential backoff with jitter — 30s, 2m, 8m, then give up (configurable). On retry, a `bridge_event` row records `retried` with the prior error.

### Configuration

Daemon-wide defaults live in `~/.orch/config.toml`:

```toml
[daemon]
port = 7777
worker_timeout_seconds = 900
record_retention_days = 30

[bridge]
max_concurrent_total = 3
max_retries = 3
retry_backoff_seconds = [30, 120, 480]
```

Per-project overrides live in `<project>/.orch/config.toml`:

```toml
[bridge]
max_concurrent_as_target = 1   # bridges TO this project run serially (default)
max_retries = 5                # this project tolerates more retries
disabled = false               # set true to refuse all bridges to this project
```

Daemon reads project config at submission time and at dispatcher claim time. No restart needed for config changes.

## Agent interface

CLI (the simple path, recommended in CLAUDE.md):

```
orch bridge submit \
  --target lde-dash \
  --intent fix \
  --summary "..." \
  --context-file ./context.md \
  --request-file ./request.md \
  [--parent-id br_01HXY...] \
  [--relevant-file path/to/file]

orch bridge status <id>
orch bridge list [--source X] [--target Y] [--status pending|inflight|completed|failed]
orch bridge cancel <id>
orch bridge retry <id>
```

Under the hood: HTTP POST/GET to the daemon. The CLI auto-detects `source_project` from the working directory.

No file-drop fallback. The CLI (and the HTTP API behind it) is the only submission path — eliminating the file-soup is the whole point of the redesign.

## TUI changes

- Bridge pane lists pending / inflight / recent (last 24h), all live via SSE.
- `b` to drill into a record — full event log with timestamps.
- `c` to cancel a pending or inflight bridge.
- `r` to retry a failed one.
- Status line shows daemon connection health.

If the daemon is unreachable, the TUI shows a blocking dialog:

```
orch-daemon is not running.
Start it with:  orch daemon start
```

No read-only fallback. The daemon is the only legal source of state.

## Migration

One-shot importer: `orch bridge migrate-from-files`

- Scan all projects for `.orch/bridge_request*`, `.orch/bridge_requests/`, `.orch/bridge_responses/`.
- For each request file with a matching response → insert as `status='completed'`, populate result/pr_url.
- For each request file without a response → insert as `status='failed'`, error=`"unprocessed_pre_redesign"`. Agent can retry via CLI.
- Move all `.orch/bridge*` files to `.orch/_archive/bridge-files-pre-redesign/` for forensic reference, then delete after one week.

CLAUDE.md template (`init.py`) updated to drop the file-based instructions and document `orch bridge submit`. `orch init --upgrade` for existing projects.

## Reliability

- **launchd `KeepAlive=true`** — daemon auto-restarts on crash.
- **SQLite WAL** — readers don't block writers.
- **Crash recovery** — on daemon start, any `inflight` records past their worker timeout reset to `failed` with `error_class='transient'`, becoming auto-retry candidates.
- **Worker timeouts** — hard SIGKILL the headless Claude after the configured max, mark failed/transient.
- **Idempotent submit** — optional `client_request_id` in submit body, unique per `source_project`. Resubmit returns the original record. Lets agents safely retry network errors during submission.
- **Health endpoint** — `GET /healthz` for TUI / external supervision. Daemon being down is a hard error everywhere; nothing silently degrades.

## Implementation order

1. SQLite schema + module (`orch/state.py`) — pure data layer, unit-testable.
2. Daemon scaffolding + launchd plist + `orch daemon {start,stop,status}` CLI.
3. HTTP API on the daemon.
4. Dispatcher logic ported from `comm.handle_bridge_request` into the daemon, talking to SQLite. Auto-retry janitor.
5. `orch bridge` CLI subcommands.
6. CLAUDE.md template updates (`orch init` template).
7. TUI rewiring to the SSE feed; daemon-required dialog.
8. Migration tool + execute against current `.orch/bridge*` state.
9. Delete old file-based code paths from `comm.py` and `app.py`. Delete `orch/bridge.py` (mobile UI).

Each step leaves the system in a working state — the old file-based path keeps running until step 9.
