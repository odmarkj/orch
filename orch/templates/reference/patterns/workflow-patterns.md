This file documents Claude Code workflow patterns including agent skills, hooks, slash
commands, status line customizations, orchestration techniques, and automation best
practices. It is drawn from the awesome-claude-code repository and covers the major
categories of Claude Code extensibility. Consult it when designing new workflows,
setting up hooks, creating slash commands, or configuring agent skills for a project.

---

## Agent Skills

Skills are model-controlled configurations (files, scripts, resources) that give Claude
specialized capabilities. They live in `.claude/skills/` or similar project paths.

Key patterns from notable skill repos:

- **Skill activation via hooks**: Use hooks to detect context and auto-load relevant skills
  (pattern from Claude Code Infrastructure Showcase: https://github.com/diet103/claude-code-infrastructure-showcase)
- **Domain-specific bundles**: Group skills by domain (DevOps, security, scientific)
  rather than by tool type
- **Skill auditing**: Use meta-skills to validate and lint other skills
  (pattern from TACHES: https://github.com/glittercowboy/taches-cc-resources)

Notable skill repos:
- DevOps: https://github.com/akin-ozer/cc-devops-skills
- Security auditing: https://github.com/trailofbits/skills
- Scientific/research: https://github.com/K-Dense-AI/claude-scientific-skills
- Full-stack (65 skills): https://github.com/jeffallan/claude-skills
- Context engineering: https://github.com/NeoLabHQ/context-engineering-kit

## Hooks

Hooks activate commands at lifecycle points in Claude's agentic loop.

### Hook Types (from Claude Code docs)
- **PreToolUse**: Runs before Claude executes a tool (Bash, Read, Write, etc.)
- **PostToolUse**: Runs after tool execution completes
- **Notification**: Runs when Claude sends a notification
- **Stop**: Runs when Claude finishes a response

### Configuration (settings.json)
```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 .claude/hooks/validate-bash.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "command": "node .claude/hooks/post-write-lint.js"
          }
        ]
      }
    ]
  }
}
```

### Hook Patterns
- **Auto-approve safe commands**: AST-parse bash commands, approve read-only ops, prompt for destructive ones (Dippy: https://github.com/ldayton/Dippy)
- **TDD guard**: Block file writes that violate test-driven development principles (https://github.com/nizos/tdd-guard)
- **Quality gates**: Run TypeScript compilation + ESLint + Prettier on Write with SHA256 config caching for <5ms validation (https://github.com/bartolli/claude-code-typescript-hooks)
- **Sound notifications**: Play OS-native sounds on events (Claudio: https://github.com/ctoth/claudio)
- **Desktop notifications**: Alert on input needs or task completion (CC Notify: https://github.com/dazuiba/CCNotify)
- **Prompt injection scanning**: Scan tool I/O for injection attacks (parry: https://github.com/vaporif/parry)
- **British English conversion**: Auto-convert American spellings in comments/docstrings (Britfix: https://github.com/Talieisin/britfix)
- **Inter-agent communication**: Enable sub-agents to message each other via hooks (HCOM: https://github.com/aannoo/claude-hook-comms)

### Hook SDKs
- Python: https://github.com/GowayLee/cchooks
- PHP (Laravel-style): https://github.com/beyondcode/claude-hooks-sdk
- TypeScript: https://github.com/johnlindquist/claude-hooks
- Go (high-perf): https://github.com/Veraticus/cc-tools

## Slash Commands

NOTE: These are reference patterns from other projects. Do NOT create slash
commands unless the user explicitly asks for one. Reading this file is for
understanding what exists in the ecosystem, not for creating new commands.

Slash commands are markdown files in `.claude/commands/` that define reusable prompts.

### File Structure
```
.claude/commands/
  commit.md          -- /commit
  fix-issue.md       -- /fix-issue
  create-pr.md       -- /create-pr
  prime.md           -- /prime
  tdd.md             -- /tdd
```

### Argument Passing
Commands accept `$ARGUMENTS` placeholder:
```markdown
# fix-issue.md
Fetch GitHub issue #$ARGUMENTS using `gh issue view $ARGUMENTS`.
Analyze the issue, implement a fix, run tests, create a commit.
```

### Effective Command Categories

**Version Control**:
- `/commit` -- conventional commit with emojis (https://github.com/evmts/tevm-monorepo/blob/main/.claude/commands/commit.md)
- `/create-pr` -- full PR workflow: branch, commit, format, submit (https://github.com/toyamarinyon/giselle/blob/main/.claude/commands/create-pr.md)
- `/fix-issue` -- fetch issue, implement, test, commit (https://github.com/metabase/metabase/blob/master/.claude/commands/fix-issue.md)
- `/fix-pr` -- address unresolved PR review comments (https://github.com/metabase/metabase/blob/master/.claude/commands/fix-pr.md)

**Context Priming**:
- `/prime` -- load directory structure + key files (https://github.com/yzyydev/AI-Engineering-Structure/blob/main/.claude/commands/prime.md)
- `/context-prime` -- comprehensive project understanding (https://github.com/elizaOS/elizaos.github.io/blob/main/.claude/commands/context-prime.md)

**Testing**:
- `/tdd` -- red-green-refactor with git integration (https://github.com/zscott/pane/blob/main/.claude/commands/tdd.md)
- `/check` -- static analysis + security scanning (https://github.com/rygwdn/slack-tools/blob/main/.claude/commands/check.md)

**Meta**:
- `/create-hook` -- guided hook creation from project context (https://github.com/omril321/automated-notebooklm/blob/main/.claude/commands/create-hook.md)
- `/create-command` -- scaffold new slash commands (https://github.com/scopecraft/command/blob/main/.claude/commands/create-command.md)

## Status Lines

Status lines customize Claude Code's terminal status bar. Configuration in settings.json:

```json
{
  "statusLine": {
    "command": "python3 .claude/statusline.py"
  }
}
```

Notable implementations:
- Rust + Git + usage tracking: https://github.com/Haleclipse/CCometixLine
- Customizable formatter: https://github.com/sirmalloc/ccstatusline
- Vim-style powerline: https://github.com/Owloops/claude-powerline
- SQLite-backed persistent stats: https://github.com/hagan/claudia-statusline

## Workflow Frameworks

### Spec-Driven Development
Write specs first, then implement. Used by Claude CodePro and ContextKit.
```
1. /create-plan -> generates spec document
2. Human reviews and approves spec
3. /implement -> builds against spec
4. /review -> validates against spec
```

### Ralph Wiggum Loop
Run Claude autonomously in a loop until task completion:
- Orchestrator: https://github.com/mikeyobrien/ralph-orchestrator
- Playbook/guide: https://github.com/ClaytonFarr/ralph-playbook
- BDD variant: https://github.com/marcindulak/ralph-wiggum-bdd
- Safety: circuit breakers, rate limiting, exit detection required

### Multi-Agent Orchestration
- Claude Squad: multiple agents in separate workspaces (https://github.com/smtg-ai/claude-squad)
- Claude Swarm: interconnected agent sessions (https://github.com/parruda/claude-swarm)
- Auto-Claude: SDLC pipeline with kanban UI (https://github.com/AndyMik90/Auto-Claude)

### Session Continuity
- Session restore from logs + git history: https://github.com/ZENG3LD/claude-session-restore
- Cross-agent handoff (Claude Code <-> Codex): https://github.com/pchalasani/claude-code-tools
- Full-text session search: https://github.com/zippoxer/recall

## Config Management

- **agnix**: Lint CLAUDE.md, AGENTS.md, hooks, MCP configs (https://github.com/agent-sh/agnix)
- **claude-rules-doctor**: Detect dead `.claude/rules/` files with unmatched globs (https://github.com/nulone/claude-rules-doctor)
- **ClaudeCTX**: Switch entire Claude config with one command (https://github.com/foxj77/claudectx)
- **Rulesync**: Convert configs between Claude Code and other AI agents (https://github.com/dyoshikawa/rulesync)

## Usage Monitoring

- ccusage: CLI cost/token dashboard from local logs (https://github.com/ryoppippi/ccusage)
- ccflare / better-ccflare: Web UI dashboard (https://github.com/tombii/better-ccflare)
- Claude Code Usage Monitor: real-time terminal burn rate (https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor)
- Vibe-Log: session analysis + HTML reports (https://github.com/vibe-log/vibe-log-cli)
