# orch

Claude session orchestrator. One terminal, all your projects. Built for
shipping — not just building.

---

## Install

```bash
cd ~/tools/orch
pip install -e . --break-system-packages

# First-time setup (iTerm2 profile, terminal-notifier, config file)
orch-setup
```

Then run:

```bash
orch
```

## How projects are discovered

Orch auto-scans `~/Sites/*/` and registers any folder containing a `.claude/`
directory. No manual config. Run Claude Code in a project once (which creates
`.claude/`) and it appears automatically. Press `r` to rescan at any time.

---

## TUI keybindings

| Key | Action |
|-----|--------|
| `j` / `k` or arrows | Navigate project list |
| `t` | Send a task to Claude (writes to `.claude/pending_task`) |
| `e` | Open iTerm2 tab for selected project (resumes Claude session) |
| `l` | Open iTerm2 tab tailing docker logs for project |
| `p` | Generate and display day plan in iTerm2 tab |
| `b` | Toggle mobile web bridge on/off |
| `c` | Open iTerm2 tab with Claude running in container |
| `s` | Set project stage (`stage` or `stage: note`) |
| `i` | Ignore/hide selected project from orch |
| `r` | Rescan `~/Sites` for new/removed projects |
| `q` | Quit |
| `?` | Toggle keybinding help pane |
| `Escape` | Cancel input |

---

## CLI subcommands

```bash
orch                              # Launch TUI
orch plan                         # Generate day plan (prints to terminal)
orch plan --json                  # Day plan as JSON
orch stage <project> <stage>      # Advance project lifecycle stage
orch stage <project> <stage> note # With a note in the ledger
orch logs <project>               # Tail docker logs for project
orch logs <project> -g error      # Grep filter
orch logs <project> --list        # Show discovered containers
orch logs <project> --past        # Read saved log files
orch bridge                       # Start mobile web bridge (Ctrl-C to stop)
orch container <project> up       # Start devcontainer for project
orch container <project> down     # Stop container
orch container <project> status   # Check container status
orch container <project> exec     # Exec into container running claude
orch ignore <project>             # Hide project from orch
orch ignore <project> --undo     # Un-hide project
orch setup                        # Re-run first-time setup
```

---

## Live status

Add `CLAUDE_SNIPPET.md` contents to the `CLAUDE.md` of each project.
This instructs Claude to:

- Overwrite `.claude/status` with a one-line present-tense status after every response
- Write `.claude/waiting_for_input` with a question when it needs you
- Mark TODOS.md items as in-progress (`- [~]`) and done (`- [x]`)

Orch watches these files via filesystem events — zero latency, no polling.

---

## Input needed → iTerm2 → notification flow

When Claude writes `.claude/waiting_for_input`:
1. Orch fires a macOS toast notification (requires `terminal-notifier`)
2. An iTerm2 tab opens automatically using the `orch` profile, already
   `cd`'d into the project with the Claude session resumed
3. You give input, Claude resumes and deletes the file
4. The status dot goes green — you close the tab whenever you're ready

Orch never closes iTerm2 tabs. You own that.

---

## iTerm2 profile

`orch-setup` symlinks `profiles/orch-iterm2-profile.json` into iTerm2's
DynamicProfiles directory. iTerm2 picks it up instantly — no restart.

Edit `profiles/orch-iterm2-profile.json` directly to change colors, fonts,
environment variables, or any other profile setting. Changes apply the next
time a tab opens. Key environment variables pre-set in the profile:

```json
"Environment": {
  "ORCH_SESSION":    "1",
  "CLAUDE_ORCH":     "1",
  "ANTHROPIC_API_KEY": "",
  "NODE_ENV":        "development",
  "DOCKER_BUILDKIT": "1"
}
```

Fill in `ANTHROPIC_API_KEY` here or export it in your shell profile.

---

## Project lifecycle

Each project tracks its stage in `.orch/project.toml`, committed to the repo.

Stages in order:
```
idea → building → mvp → staging → live → maintaining
```

Advance a stage:
```bash
orch stage cacao-dna mvp "Core recommendation loop working end to end"
```

Or press `s` in the TUI. The ledger is append-only — every transition is
dated and noted. Orch uses this history to detect stalled projects (current
gap > 1.5× the project's own average pace) and surface them in the day plan.

**Launch debt** — days spent in `mvp` or `staging` without shipping. Shown
in the project list and weighted heavily in the day planner. The goal is to
finish what you started before starting something new.

---

## Day planner

```bash
orch plan
```

Or press `p` in the TUI. Makes a single Claude API call with context from
every project: stage, stall score, launch debt, pending todos, git activity,
and current Claude status. Returns a prioritized plan with focus projects
(max 4), rationale, and suggested tasks pulled from your TODOS.md.

Requires `ANTHROPIC_API_KEY` in your environment or iTerm2 profile.

---

## TODOS.md format

```markdown
## Pending
- [ ] Build flavor profile recommendation engine
- [ ] Vectorize the tasting notes corpus

## In Progress
- [~] Refactor SMS outreach sequence

## Done
- [x] Set up pgvector schema
```

Pending count shows next to the project name in the TUI. Claude marks items
in-progress and done automatically as it works.

---

## Mobile access

See `MOBILE.md` for the full setup guide. Short version:

```bash
# Mac: generate SSH key for mobile
ssh-keygen -t ed25519 -C "orch-mobile" -f ~/.ssh/orch_mobile
cat ~/.ssh/orch_mobile.pub >> ~/.ssh/authorized_keys

# Enable Remote Login in System Settings → General → Sharing
```

Import `~/.ssh/orch_mobile` into Termius on your phone. Connect, run `orch`.
Full TUI works on iPad. `orch plan` and `orch stage` work well on phone.

For access outside your home network:
```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:7777  # after starting orch bridge
```

---

## Config

`orch-setup` creates `~/.orch/config.toml` with all options documented.
Key settings:

```toml
[iterm]
profile = "orch"
dedicated_window = true      # all orch tabs in one window
window_title = "orch sessions"

[notifications]
sound_input_needed = "Glass"
sound_resumed = "Pop"
notify_on_resume = true

[bridge]
port = 7777

[planner]
model = "claude-sonnet-4-20250514"
```

---

## File reference

| File | Purpose |
|------|---------|
| `~/Sites/<project>/.claude/status` | One-line live status, written by Claude |
| `~/Sites/<project>/.claude/waiting_for_input` | Claude's question; triggers notification + iTerm2 tab |
| `~/Sites/<project>/.claude/pending_task` | Task queued from orch, read by Claude |
| `~/Sites/<project>/.claude/sessions.json` | `{"active": "<session-id>"}` for `--resume` |
| `~/Sites/<project>/TODOS.md` | Project todo list |
| `~/Sites/<project>/.orch/project.toml` | Lifecycle stage + ledger (commit this) |
| `~/.orch/config.toml` | Orch configuration |
| `~/.orch/logs/<project>/` | Docker log files (1000 line rotation) |
