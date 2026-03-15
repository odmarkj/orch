<p align="center">
  <img src="assets/orch-logo.svg" alt="orch — claude session orchestrator" width="400">
</p>

<p align="center">
  <em>One terminal, all your projects. Built for shipping — not just building.</em>
</p>

<p align="center">
  <a href="https://github.com/odmarkj/orch/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/version-0.1.0-green.svg" alt="Version: 0.1.0">
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-brightgreen.svg" alt="Python: >=3.11">
  <img src="https://img.shields.io/badge/platform-macOS-lightgrey.svg" alt="Platform: macOS">
  <a href="TODO-buymeacoffee-url"><img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-donate-yellow.svg?logo=buy-me-a-coffee&logoColor=white" alt="Buy Me A Coffee"></a>
</p>

<p align="center">
  <a href="#the-problem">The Problem</a> &bull;
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#how-it-works">How It Works</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="#cli-commands">CLI Commands</a> &bull;
  <a href="#interface-shortcuts">Interface Shortcuts</a> &bull;
  <a href="#getting-started">Getting Started</a> &bull;
  <a href="#contributing">Contributing</a>
</p>

---

## The Problem

If you're building multiple projects with Claude Code, you know the pain:

- **Context switching is manual** — you `cd` between projects, open tabs, lose track of what's running where
- **No visibility** — which Claude session is active? Which is stuck waiting for input? Which project stalled three weeks ago?
- **Permission friction** — Claude asks for approval on every file write and shell command, breaking autonomous workflows
- **No prioritization** — with ten projects in flight, it's hard to know which one to push forward today

You end up juggling terminal tabs, forgetting which projects need attention, and losing momentum to overhead instead of shipping.

### Where orch fits

orch gives you a single control plane for all your Claude Code projects. It discovers projects automatically, tracks their lifecycle, runs Claude in isolated containers with full permissions, and tells you what to work on next.

| Without orch | With orch |
|-------------|-----------|
| Manually `cd` between projects | Select from a live project list |
| Claude asks permission for everything | Containers run with `--dangerously-skip-permissions` |
| No idea which session is active or stuck | Real-time status dots and notifications |
| Context lost between sessions | Session resume built in |
| No sense of project momentum | Lifecycle tracking with stall detection |
| "What should I work on?" | AI day planner with prioritized focus |

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/odmarkj/orch.git
cd orch
pip install -e . --break-system-packages

# First-time setup (iTerm2 profile, terminal-notifier, Docker checks)
orch setup

# Launch
orch
```

That's it. Orch auto-discovers any project in `~/Sites/` that has a `.claude/` directory. Run Claude Code in a project once and it appears automatically.

---

## How It Works

Orch operates through three core systems that work together:

### Live status monitoring

Each project's Claude session writes a one-line status to `.claude/status` after every response. Orch watches these files via filesystem events — zero polling, instant updates. Status dots in the TUI show green (active), yellow (waiting for input), or dim (idle).

When Claude needs input, it writes to `.claude/waiting_for_input`. Orch fires a macOS notification and opens an iTerm2 tab with the session already resumed. You answer, Claude continues, the file is deleted, the dot goes green.

### Container isolation

When you select a project, orch automatically starts a Docker container using either `devcontainer up` (preferred) or a raw `docker run` fallback. Inside the container, Claude runs with `--dangerously-skip-permissions` and a `settings.local.json` that allows all tools. This means fully autonomous operation — no permission prompts interrupting multi-step tasks.

Containers persist across project switches. Orch only removes them when you explicitly ask.

### Auto-dispatch with parallel worktrees

When auto-dispatch is enabled (`g` in the TUI), orch automatically picks up pending todos from `TODOS.md` and runs them — each in its own git worktree with a dedicated Claude instance. Up to 3 tasks run in parallel by default (configurable via `max_parallel`).

The full pipeline for each dispatched todo:

1. **Worktree created** — a new branch `auto/<slug>-<random>` is checked out in `../.orch-worktrees/` (automatically added to `.gitignore`)
2. **Claude works the task** — runs autonomously in the worktree with `--dangerously-skip-permissions`
3. **Code review** (optional) — a second Claude instance reviews the diff for bugs, security issues, and quality
4. **Commit & push** — changes are committed and pushed to the branch with retry backoff
5. **PR created** — a pull request is opened via `gh` CLI with the task description and review findings
6. **Cleanup** — worktree is removed, local branch is deleted (remote branch preserved on the PR), todo is marked `[x]`, next pending todo fills the slot

`.orch-worktrees` is automatically added to the project's `.gitignore` on first dispatch to keep temporary worktrees out of version control. Local branches are cleaned up after a successful push to avoid clutter — the work lives on in the remote branch and PR.

This means you can add 10 todos to a project, press `g`, and walk away. Orch will churn through them 3 at a time, each producing a PR ready for merge.

### Code review

Per-project opt-in. Add `code_review = true` to `.orch/project.toml`:

```toml
[project]
name = "my-project"
code_review = true
```

When enabled, after Claude finishes a task but before the commit and PR, a separate Claude instance reviews the diff. The review is:
- Saved to `.claude/last_review.md` in the worktree
- Included in the PR body under a "Code Review" section
- Posted as a comment on the PR

Code review is **off by default** and configured per project.

### Project lifecycle

Every project tracks its stage in `.orch/project.toml`:

```
idea → building → mvp → staging → live → maintaining
```

The ledger is append-only — every transition is dated and noted. Orch uses this history to detect stalled projects (current gap > 1.5x the project's own average pace) and calculates **launch debt** — days spent in `mvp` or `staging` without shipping. Both feed into the day planner.

---

## Architecture

```
~/Sites/
  project-a/.claude/status          ──┐
  project-b/.claude/status          ──┤
  project-c/.claude/waiting_for_input ┤
                                      │
                              [Watchdog Observer]
                                      │
                                      v
                              ┌───────────────┐
                              │   Orch TUI     │
                              │   (Textual)    │
                              │                │
                              │  Project List  │
                              │  Status Dots   │
                              │  TODO Preview  │
                              └───────┬───────┘
                                      │
               ┌──────────────┬───────┼──────────┬────────────┐
               │              │       │          │            │
               v              v       v          v            v
       ┌──────────┐  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐
       │ Container │  │  iTerm2  │ │ Bridge │ │ Auto-  │ │  Code  │
       │ Manager   │  │  Integ.  │ │ :7777  │ │Dispatch│ │ Review │
       │           │  │          │ │        │ │        │ │        │
       │devcontainer│ │ Tab mgmt │ │Mobile  │ │Worktree│ │ Claude │
       │ or docker │  │ Notifs   │ │REST API│ │Parallel│ │  Diff  │
       └──────────┘  └──────────┘ └────────┘ └───┬────┘ └────────┘
               │                                  │
               v                                  v
       ┌──────────────┐                 ┌──────────────────┐
       │   Claude      │                 │  Worktree 1..N    │
       │ (in container)│                 │  Claude per task   │
       │ --dangerously-│                 │  commit → push     │
       │ skip-perms    │                 │  → PR via gh       │
       └──────────────┘                 └──────────────────┘
```

### Key design decisions

- **Filesystem events over polling** — Watchdog monitors `.claude/` directories for instant status updates with zero CPU overhead.
- **Container-first execution** — Claude runs in isolated Docker containers so `--dangerously-skip-permissions` is safe. No permission prompts, no friction.
- **No dependencies beyond the stdlib** — The TOML parser and Anthropic API client are hand-rolled. Only `textual` (TUI) and `watchdog` (file events) are external.
- **iTerm2 via AppleScript** — Tab management uses native macOS automation. Dedicated orch window, session resume by TTY handle, no stale tabs.
- **Append-only ledger** — Project lifecycle transitions are never edited, only appended. Full audit trail for stall detection and planning.

---

## CLI Commands

```bash
orch                                # Launch TUI
orch plan                           # Generate AI day plan
orch plan --json                    # Day plan as JSON
orch stage <project> <stage>        # Advance project lifecycle stage
orch stage <project> <stage> note   # With a note in the ledger
orch logs <project>                 # Tail docker logs for project
orch logs <project> -g error        # Grep filter
orch logs <project> --list          # Show discovered containers
orch logs <project> --past          # Read saved log files
orch bridge                         # Start mobile web bridge (Ctrl-C to stop)
orch container <project> up         # Start devcontainer for project
orch container <project> down       # Stop container
orch container <project> status     # Check container status
orch container <project> exec       # Exec into container running Claude
orch ignore <project>               # Hide project from orch
orch ignore <project> --undo        # Un-hide project
orch setup                          # First-time setup
```

### Day planner

`orch plan` makes a single Claude API call with context from every project: stage, stall score, launch debt, pending todos, git activity, and current Claude status. Returns a prioritized plan with focus projects (max 4), rationale, and suggested tasks pulled from your `TODOS.md`.

Requires `ANTHROPIC_API_KEY` in your environment or iTerm2 profile.

---

## Interface Shortcuts

### TUI keybindings

| Key | Action |
|-----|--------|
| `j` / `k` or arrows | Navigate project list |
| `Enter` | Select project (auto-starts container) |
| `t` | Send a task to Claude in container |
| `a` | Add a todo to TODOS.md |
| `g` | Toggle auto-dispatch (parallel worktrees) |
| `e` | Open iTerm2 tab with Claude (host) |
| `c` | Open iTerm2 tab with Claude (container) |
| `l` | Tail docker logs in iTerm2 tab |
| `p` | Generate day plan in iTerm2 tab |
| `b` | Toggle mobile web bridge on/off |
| `s` | Set project stage (`stage` or `stage: note`) |
| `i` | Ignore/hide selected project from orch |
| `r` | Rescan `~/Sites` for new/removed projects |
| `q` | Quit |
| `?` | Toggle keybinding help pane |
| `Escape` | Cancel input |

### TODOS.md format

```markdown
## Pending
- [ ] Build flavor profile recommendation engine
- [ ] Vectorize the tasting notes corpus

## In Progress
- [~] Refactor SMS outreach sequence

## Done
- [x] Set up pgvector schema
```

Pending count shows next to the project name in the TUI. Claude marks items in-progress and done automatically as it works.

---

## Getting Started

### Installation

```bash
git clone https://github.com/odmarkj/orch.git
cd orch
pip install -e . --break-system-packages
```

### First-time setup

```bash
orch setup
```

This will:
1. Symlink the iTerm2 dynamic profile
2. Install `terminal-notifier` for macOS notifications
3. Check for Docker and `devcontainer` CLI
4. Create `~/.orch/config.toml` with default settings

### Enable live status in your projects

Add the contents of `CLAUDE_SNIPPET.md` to the `CLAUDE.md` of each project you want orch to track. This instructs Claude to:

- Write a one-line status to `.claude/status` after every response
- Write questions to `.claude/waiting_for_input` when it needs you
- Mark `TODOS.md` items as in-progress (`[~]`) and done (`[x]`)

### Configuration

`orch setup` creates `~/.orch/config.toml`:

```toml
[iterm]
profile = "orch"
dedicated_window = true
window_title = "orch sessions"

[notifications]
sound_input_needed = "Glass"
sound_resumed = "Pop"
notify_on_resume = true

[container]
enabled = true
image = "mcr.microsoft.com/devcontainers/base:ubuntu"
memory = "12g"
prefer_devcontainer_cli = true

[dispatch]
# Max Claude instances running in parallel per project (each gets a worktree)
max_parallel = 3

[bridge]
port = 7777

[planner]
model = "claude-sonnet-4-20250514"
```

### Per-project config

Projects can have their own settings in `.orch/project.toml`:

```toml
[project]
name = "my-project"

# Enable automatic code review on dispatched tasks (off by default)
code_review = true
```

### Mobile access

Full TUI works on iPad via SSH. `orch plan` and `orch stage` work well on phone. See [MOBILE.md](MOBILE.md) for the complete setup guide with Termius and Cloudflare Tunnel instructions.

### File reference

| File | Purpose |
|------|---------|
| `~/Sites/<project>/.claude/status` | One-line live status, written by Claude |
| `~/Sites/<project>/.claude/waiting_for_input` | Claude's question; triggers notification + iTerm2 tab |
| `~/Sites/<project>/.claude/pending_task` | Task queued from orch, read by Claude |
| `~/Sites/<project>/.claude/sessions.json` | `{"active": "<session-id>"}` for `--resume` |
| `~/Sites/<project>/.claude/auto_dispatch` | Auto-dispatch enabled flag (existence = on) |
| `~/Sites/<project>/.claude/active_todo` | Currently dispatched todo text |
| `~/Sites/<project>/.claude/last_review.md` | Most recent code review output |
| `~/Sites/<project>/TODOS.md` | Project todo list |
| `~/Sites/<project>/.orch/project.toml` | Lifecycle stage, ledger, and per-project config |
| `~/Sites/.orch-worktrees/` | Temporary worktrees for parallel dispatch |
| `~/.orch/config.toml` | Orch configuration |
| `~/.orch/logs/<project>/` | Docker log files (1000 line rotation) |

---

## System Requirements

- **Python** >= 3.11
- **macOS** (iTerm2 integration uses AppleScript)
- **Docker** (for container isolation)
- **iTerm2** (for tab management and notifications)
- **terminal-notifier** (installed by `orch setup`)

Optional:
- **devcontainer CLI** (`npm install -g @devcontainers/cli`) — preferred container strategy
- **GitHub CLI** (`brew install gh`) — enables auto-dispatch PR creation and code review comments
- **Cloudflare Tunnel** — for mobile access outside your home network

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a Pull Request

### Development

```bash
git clone https://github.com/odmarkj/orch.git
cd orch
pip install -e . --break-system-packages
orch setup
```

### Project structure

```
orch/
├── orch/                  # Python package
│   ├── __init__.py
│   ├── __main__.py        # CLI entry point and subcommand routing
│   ├── app.py             # Textual TUI application
│   ├── bridge.py          # Mobile web bridge (HTTP server + REST API)
│   ├── container.py       # Docker container lifecycle management
│   ├── discovery.py       # Auto-discovery of projects in ~/Sites
│   ├── iterm.py           # iTerm2 tab management and notifications
│   ├── lifecycle.py       # Project stages, ledger, stall detection
│   ├── logs.py            # Docker log streaming and rotation
│   ├── models.py          # Project and Session data models
│   ├── planner.py         # AI day planner (Claude API)
│   └── setup.py           # First-time setup wizard
├── profiles/
│   └── orch-iterm2-profile.json  # iTerm2 dynamic profile
├── CLAUDE_SNIPPET.md      # Status integration snippet for projects
├── MOBILE.md              # Mobile access setup guide
├── pyproject.toml         # Package configuration
└── README.md
```

---

## License

MIT

---

## Support

- **Bug reports** — [GitHub Issues](https://github.com/odmarkj/orch/issues)
- **Feature requests** — [GitHub Issues](https://github.com/odmarkj/orch/issues)

---

## Sponsors

A special thanks to our project sponsors:

<p align="center">
  <a href="https://localdataexchange.com">
    <img src="https://www.localdataexchange.com/wp-content/uploads/2023/04/1145x433-LDE-black.png" alt="Local Data Exchange" width="300">
  </a>
</p>

---

<p align="center">
  <sub>Built for the Claude Code ecosystem</sub>
</p>
