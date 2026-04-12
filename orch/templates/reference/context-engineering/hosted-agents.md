# Hosted Agent Infrastructure

This file covers building background coding agents that run in remote sandboxed environments rather than local machines. It covers sandbox infrastructure (image registry, snapshots, warm pools), agent framework selection, speed optimizations, self-spawning agents, API layer design, multiplayer support, auth patterns, and client implementations. Consult this when building background agents, designing sandboxed execution environments, implementing multiplayer sessions, or scaling agent infrastructure beyond local constraints.

---

## Core Insight

Session speed should be limited only by model provider time-to-first-token. All infrastructure setup must be completed before the user starts their session.

## Three-Layer Architecture

1. **Sandbox infrastructure** -- isolated execution per session
2. **API layer** -- state management and client coordination
3. **Client interfaces** -- user interaction across platforms (Slack, Web, Chrome, VS Code)

Keep layers cleanly separated so sandbox changes don't ripple into clients.

## Sandbox Infrastructure

### Image Registry Pattern
Pre-build environment images every 30 minutes:
- Cloned repo at known commit
- All dependencies installed
- Initial build commands completed
- Caches populated from running app/test suite once

Session startup syncs only the delta (at most 30 minutes of changes).

### Snapshot and Restore
Take filesystem snapshots at: initial image build, after agent changes, before sandbox exit. Enables instant restoration for follow-ups.

### Git Configuration
- Generate GitHub app installation tokens for repo access during clone.
- Set git `user.name` and `user.email` when committing (use prompting user's identity).

### Warm Pool
- Keep pre-warmed sandboxes ready before users start sessions.
- Expire and recreate as new images build.
- Start warming when user begins typing (predictive warm-up).

## Speed Optimizations

- **Predictive warm-up**: Start sandbox prep on keystroke, not submission. 5-30s typing interval covers most setup.
- **Parallel file reading**: Allow reads immediately even if git sync incomplete (30-min staleness rarely matters). Block edits until sync completes.
- **Maximize build-time work**: Full dependency install, DB schema setup, initial test runs -- all invisible to user.

## Self-Spawning Agents

Build tools allowing agents to spawn new sessions for:
- Research tasks across repositories
- Parallel subtask execution
- Breaking monolithic changes into smaller PRs

Three primitives: start session, read status (check-in), continue main work while sub-sessions run.

## API Layer

- **Per-session state isolation**: SQLite per session. No cross-session interference.
- **Real-time streaming**: Token streaming, tool execution status, file change notifications. WebSocket with hibernation APIs.
- **Cross-client sync**: Single state system synchronizing across all surfaces (Slack, web, Chrome extension, VS Code).

## Multiplayer Support

Design from day one (nearly free with proper sync architecture):
- Pass authorship info to each prompt
- Attribute code changes to prompting user
- Share session links for instant collaboration
- Use cases: teaching non-engineers, live QA, real-time PR review

## Authentication

- Open PRs on behalf of user (not app) to preserve audit trail.
- Sandbox-to-API flow: sandbox pushes changes -> sends event to API with branch/session ID -> API uses user's GitHub token to create PR.

## Client Implementations

| Client | Key Design Points |
|---|---|
| **Slack** | First for internal adoption (virality loop). Build classifier for repo routing. |
| **Web** | Primary power-user surface. Real-time streaming, hosted VS Code, before/after screenshots. |
| **Chrome Extension** | For non-engineering users. Extract DOM/React internals instead of raw screenshots. |

## Metrics That Matter

- Sessions resulting in merged PRs (primary)
- Time from session start to first model response
- PR approval rate and revision count
- Agent-written code percentage across repos

## Key Gotchas

- Cold starts (30-60s) feel broken. Use warm pools + predictive warm-up.
- Infrequent image rebuilds = outdated agents. Set 30-min cadence; alert on build failures.
- Long-running agents without timeouts accumulate cost. Default 4-hour limit + per-session ceiling.
- Auth tokens expire mid-session. Implement refresh logic.
- Missing git `user.name`/`user.email` causes commit failures. Always set explicitly.
- State loss on sandbox recycle. Always snapshot before termination.
- Oversubscribing warm pools wastes money. Scale based on traffic patterns.
- Results completed inside sandbox but never extracted. Build explicit extraction into teardown.
