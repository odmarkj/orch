# Orch integration snippet

Add this block to the CLAUDE.md of every project you want orch to track:

```markdown
## Orch integration

After every response, overwrite `.orch/status` with exactly one present-tense
sentence describing what you are currently doing. No preamble, no punctuation
beyond the sentence itself, one line only.

Examples:
- Scraping attendee data from site 3 of 5, parsing JSON response
- Waiting for build to complete before running integration tests
- Refactoring the SMS outreach sequence, extracting send logic into service class

When you need input from the developer, write your question to
`.orch/waiting_for_input` (one file, plain text). The orchestrator will send a
macOS notification and open your session automatically. When you get the answer,
delete the file.

If `.orch/pending_task` exists when you start, read it, delete it, and treat
its contents as your current task.

When working through TODOS.md:
- Mark items you start with `- [~]` (in progress)
- Mark completed items with `- [x]` (done)
- Work through items in the order they appear unless a specific task overrides

When you finish a task and have no more work to do, clear `.orch/status` by
writing an empty string. This tells the orchestrator you are idle and ready for
the next task.
```

Claude will update these files automatically. Orch reads them in real time to
show live status, fire notifications, and track progress — zero LLM calls.

## Reference projects

All projects under `~/Apps/` are accessible at their original paths inside the
Lima VM. This lets Claude look at other projects when you say things like
"look at how project X does it" or "reuse the parser from Y".

Use `--allowedDirectories` to control write access — reference projects are
readable but writes are scoped to the current project.

## Cross-project bridge

Add this to the CLAUDE.md of projects that should be able to communicate:

```markdown
## Cross-project bridge

When you identify an issue, question, or need related to another orch-managed
project, submit a bridge request via the `orch bridge` CLI. It posts to the
orch daemon over HTTP, which atomically queues the request, runs a subagent on
a worktree of the target project, and reports status back.

This works from inside the Lima VM. The CLI auto-resolves the daemon at
`host.lima.internal:7777` when run from the VM, and `127.0.0.1:7777` when run
on the macOS host. Override with `ORCH_DAEMON_HOST` if needed.

\```bash
orch bridge submit \\
  --target project-name \\
  --intent fix \\
  --summary "Brief one-line description" \\
  --context-file ./context.md \\
  --request-file ./request.md \\
  [--relevant-file path/to/file] [--relevant-file ...] \\
  [--parent-id br_...]   # only when this is a sub-request of an existing bridge
\```

The command returns immediately with a bridge id (`br_...`). Use it to check
progress:

\```bash
orch bridge status br_...   # full record + event log
orch bridge list            # everything you've submitted
orch bridge cancel br_...   # cancel pending or kill an inflight worker
orch bridge retry  br_...   # resubmit a failed bridge as a new record
\```

**Intents:**
- `fix` — Request code changes in the target project (creates a branch and PR)
- `review` — Ask for a review of code or approach (returns feedback text)
- `query` — Ask a question about the target project (returns answer text)
- `inform` — One-way notification, no response expected

The daemon enforces per-target serialization (one bridge per target at a time
by default), retries transient failures automatically, and rejects requests
when the target project disables bridges via its `.orch/project.toml`. After
submitting, continue your current work — you do not need to wait. Poll
`orch bridge status <id>` only if a follow-up depends on the result.

If you fire a bridge while *handling* one (i.e. the bridge subagent itself
needs help from a third project), pass `--parent-id` with the id of the bridge
that's calling you. The daemon tracks depth automatically and rejects chains
that grow too deep. Never compute or pass a depth value yourself.

The legacy file-based protocol (`.orch/bridge_request`,
`.orch/bridge_responses/`, `.orch/_bridge_depth`) was removed — `orch bridge
submit` is the only supported path.
```
