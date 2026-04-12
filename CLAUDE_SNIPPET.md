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

When you identify an issue, question, or need related to another project that
orch manages, you can request a bridge by writing `.orch/bridge_request`:

\```json
{
  "target": "project-name",
  "intent": "fix|review|query|inform",
  "summary": "Brief one-line description",
  "context": "Why you're reaching out, what you know, what you've found",
  "request": "Specific ask — what should be done or answered",
  "relevant_files": ["optional", "list of files in target project"]
}
\```

**Intents:**
- `fix` — Request code changes in the target project (creates a branch and PR)
- `review` — Ask for a review of code or approach (returns feedback text)
- `query` — Ask a question about the target project (returns answer text)
- `inform` — One-way notification, no response expected

After writing the request, continue your current work. The orchestrator will
handle routing — it spawns a subagent on a worktree of the target project inside
the VM with read-only access to your project for context.

Check `.orch/bridge_responses/` for results. Each response is a JSON file with
`status`, `result`, and optionally `pr_url`.

If `.orch/_bridge_depth` exists, include its value + 1 as `"depth"` in your
request. Do not send bridge requests if depth would exceed 2.
```

The bridge works by:
1. Claude writes `.orch/bridge_request` → orch watchdog picks it up
2. Orch creates a worktree on the target project
3. A subagent runs inside the VM with full context from both projects
4. Results are delivered back to `.orch/bridge_responses/<id>.json`
5. The worktree is cleaned up automatically
