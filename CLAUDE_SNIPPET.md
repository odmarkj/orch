# Orch integration snippet

Add this block to the CLAUDE.md of every project you want orch to track:

```markdown
## Orch integration

After every response, overwrite `.claude/status` with exactly one present-tense
sentence describing what you are currently doing. No preamble, no punctuation
beyond the sentence itself, one line only.

Examples:
- Scraping attendee data from site 3 of 5, parsing JSON response
- Waiting for build to complete before running integration tests
- Refactoring the SMS outreach sequence, extracting send logic into service class

When you need input from the developer, write your question to
`.claude/waiting_for_input` (one file, plain text). The orchestrator will send a
macOS notification and open your session automatically. When you get the answer,
delete the file.

If `.claude/pending_task` exists when you start, read it, delete it, and treat
its contents as your current task.

When working through TODOS.md:
- Mark items you start with `- [~]` (in progress)
- Mark completed items with `- [x]` (done)
- Work through items in the order they appear unless a specific task overrides
```

Claude will update these files automatically. Orch reads them in real time to
show live status, fire notifications, and track progress — zero LLM calls.
