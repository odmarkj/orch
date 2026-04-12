This file catalogs effective patterns for writing CLAUDE.md files, drawn from dozens of
real-world open-source projects listed in the awesome-claude-code repository. Consult it
when creating or improving a CLAUDE.md for any project -- it covers structural templates,
section types that work well, and concrete examples of what makes guidance effective versus
noisy. The patterns are organized by purpose: project context, build/test commands, coding
standards, workflow rules, and memory/state protocols.

---

## Structural Principles

- Keep it scannable: Claude reads CLAUDE.md on every session start; dense prose wastes tokens
- Lead with the most frequently needed info (build commands, test commands, key conventions)
- Use headers and bullet lists; avoid long paragraphs
- Separate "facts" (project structure, commands) from "rules" (coding standards, prohibitions)
- Layer files: root CLAUDE.md for repo-wide, subdirectory CLAUDE.md for package-specific

## Recommended Section Order

```
# Project Name (1 line)
## Build & Run Commands
## Test Commands
## Project Structure (brief)
## Coding Standards & Style
## Architecture / Key Patterns
## Workflow Rules (PRs, commits, deploys)
## Domain-Specific Context (if needed)
```

## Build & Test Commands

Effective CLAUDE.md files always include copy-pasteable commands. Examples:

```markdown
## Commands
- `pnpm install` -- install deps
- `pnpm dev` -- start dev server (port 3000)
- `pnpm test` -- run all tests via Vitest
- `pnpm test -- --run src/lib/foo.test.ts` -- single test file
- `pnpm lint` -- ESLint + Prettier check
- `pnpm build` -- production build
```

From Metabase (Clojure):
```markdown
## Development
- Start REPL: `clj -M:dev`
- Run specific test: `clj -X:test :kaocha.filter/focus my.ns/test-name`
- Prefer incremental REPL-driven development over full rebuilds
```

From HASH (Rust + TypeScript monorepo):
```markdown
## Rust
- `cargo fmt --all` before committing
- `cargo clippy --all-targets` must pass
- `cargo test -p <crate>` for targeted tests
```

## Project Structure Sections

Keep brief -- just enough for Claude to know where things live:

```markdown
## Structure
- `src/app/` -- Next.js app router pages
- `src/lib/` -- shared utilities and API clients
- `src/components/` -- React components (colocated tests)
- `packages/core/` -- domain logic (no framework deps)
```

## Coding Standards

The best CLAUDE.md files are specific and actionable, not generic:

```markdown
## Style
- TypeScript strict mode; no `any` unless commented with reason
- Named exports only (no default exports)
- Use `interface` for object shapes, `type` for unions/intersections
- Error handling: return Result<T, E> pattern, never throw in library code
- Imports: group stdlib, external, internal with blank lines between
```

From SteadyStart:
```markdown
## Rules
- NEVER modify files outside the project directory
- NEVER run destructive commands without confirmation
- Always explain what you are about to do before doing it
- Document every Claude Code session for team visibility
```

From pre-commit-hooks (exemplary conciseness):
```markdown
## Conventions
- All hooks must be idempotent
- Tests use table-driven patterns
- Error messages must include file path and line number
```

## Memory & State Protocols

Pattern for cross-session continuity:

```markdown
## Memory Protocol
When making a project decision, append to `.claude/decisions.log`:
  [category] key: value
Examples:
  [arch] api-style: REST with OpenAPI spec
  [data] primary-db: PostgreSQL via Prisma
  [style] css-framework: Tailwind
Only log deliberate decisions, not exploratory steps.
```

## Architecture / Domain Context

Include only what changes Claude's behavior:

```markdown
## Architecture
- Monorepo: apps/ (deployables) + packages/ (libraries)
- API layer uses tRPC; never use raw fetch in app code
- Auth: NextAuth with database sessions, not JWT
- All DB access through Prisma; never raw SQL in application code
```

From Network Chronicles (game dev):
```markdown
## AI Characters
- Each NPC has a personality prompt in data/npcs/{name}.md
- LLM calls go through src/ai/dialogue.ts, never direct API calls
- Character responses must stay in-world; no meta-references
```

## Workflow & PR Rules

```markdown
## Git Workflow
- Branch naming: `feat/`, `fix/`, `chore/` prefixes
- Commits: conventional commit format (feat:, fix:, docs:, etc.)
- PRs require: passing CI, one approval, linked issue
- Never force-push to main
```

## Anti-Patterns to Avoid

- Walls of text with no structure (Claude skims poorly formatted prose)
- Duplicating information available in config files (package.json, tsconfig)
- Generic advice ("write clean code") -- be specific or omit it
- Shouting in ALL CAPS for emphasis (use clear, direct language instead)
- Including entire API references (link to docs instead)
- Overly long files (>300 lines) -- split into subdirectory CLAUDE.md files

## Notable Reference Implementations

| Project | Language | Strength | URL |
|---------|----------|----------|-----|
| Metabase | Clojure/JS | REPL workflow, incremental dev | https://github.com/metabase/metabase/blob/master/CLAUDE.md |
| HASH | Rust/TS | Monorepo structure, PR process | https://github.com/hashintel/hash/blob/main/CLAUDE.md |
| LangGraphJS | TypeScript | Monorepo, layered architecture | https://github.com/langchain-ai/langgraphjs/blob/main/CLAUDE.md |
| SteadyStart | General | Role/permissions/communication | https://github.com/steadycursor/steadystart/blob/main/CLAUDE.md |
| pre-commit-hooks | Go | Concise, thorough, non-verbose | https://github.com/aRustyDev/pre-commit-hooks |
| Giselle | TS/Vue | pnpm + Vitest + naming conventions | https://github.com/giselles-ai/giselle/blob/main/CLAUDE.md |
| SPy | Python | Strict conventions + test decorators | https://github.com/spylang/spy/blob/main/CLAUDE.md |
| TPL | Go | Error handling + table-driven tests | https://github.com/KarpelesLab/tpl/blob/master/CLAUDE.md |
| Basic Memory | Python | MCP + knowledge structure | https://github.com/basicmachines-co/basic-memory/blob/main/CLAUDE.md |
| AWS MCP Server | Python | Security + error handling | https://github.com/alexei-led/aws-mcp-server/blob/main/CLAUDE.md |

## Layered CLAUDE.md Strategy

For monorepos or complex projects, use multiple files:

```
CLAUDE.md                  # repo-wide: build commands, git workflow, global style
packages/core/CLAUDE.md    # core library: API patterns, error handling
packages/web/CLAUDE.md     # web app: component patterns, routing conventions
.claude/rules/             # scoped rules that apply to specific glob patterns
```

Each subdirectory file should assume the root CLAUDE.md has been read and avoid
repeating its content.
