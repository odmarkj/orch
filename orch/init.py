"""
orch init — bootstrap a project for Claude Code + orch.

Creates CLAUDE.md, .claude/ settings, rules, reference docs, lifecycle
tracking, and sibling project summaries. Additive only: never overwrites
existing files.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ── Helpers ──────────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    print(f"  \u2713 {msg}")

def _skip(msg: str) -> None:
    print(f"  \u00b7 {msg}")

def _err(msg: str) -> None:
    print(f"  \u2717 {msg}", file=sys.stderr)


def _copy_if_missing(src: Path, dst: Path) -> bool:
    """Copy src to dst if dst does not exist. Returns True if copied."""
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _copy_tree_additive(src_dir: Path, dst_dir: Path) -> tuple[int, int]:
    """
    Recursively copy files from src_dir to dst_dir.
    Skip any file that already exists at destination.
    Returns (copied_count, skipped_count).
    """
    copied = 0
    skipped = 0
    for src_file in sorted(src_dir.rglob("*")):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src_dir)
        dst_file = dst_dir / rel
        if dst_file.exists():
            skipped += 1
        else:
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            copied += 1
    return copied, skipped


# ── Sub-functions ────────────────────────────────────────────────────────────

def _ensure_git(target: Path) -> None:
    if (target / ".git").is_dir():
        _skip("Git repository already initialized")
        return

    subprocess.run(["git", "init"], cwd=target, capture_output=True)

    gitignore = target / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# OS\n.DS_Store\nThumbs.db\n\n"
            "# Editors\n.vscode/\n.idea/\n*.swp\n\n"
            "# Environment\n.env\n.env.*\nnode_modules/\n__pycache__/\n"
            "*.pyc\nvenv/\n.venv/\n\n"
            "# Orch state\n.orch/status\n.orch/waiting_for_input\n"
            ".orch/pending_task\n.orch/active_todo\n.orch/auto_dispatch\n"
            ".orch/sessions.json\n.orch/iterm_handle\n.orch/iterm_log_handle\n"
        )

    _ok("Git repository initialized")


def _write_claude_md(target: Path, name: str, description: str) -> None:
    claude_md = target / "CLAUDE.md"
    if claude_md.exists():
        _skip("CLAUDE.md already exists \u2014 skipped")
        return

    desc_line = f"\n{description}\n" if description else ""

    content = f"""# {name}
{desc_line}
## Memory Protocol

When you make a project decision (architecture, design tokens, conventions,
API patterns, tech stack), append a one-line summary to `.claude/decisions.log`:
`[category] key: value`

For cross-project preferences (coding style, tool prefs, workflow patterns), use:
`[global:category] key: value`

For technology preferences, use:
`[global:preferences] pref-type-name: description`

For data sources and schemas, use:
`[data] data-source-name: description`

Only log deliberate decisions, not exploratory steps.

## Orch integration

After every response, update `.orch/status` with one present-tense sentence
describing what you are currently doing. No preamble, no extra punctuation:

    echo 'Scraping attendee data from site 3 of 5' > .orch/status

Examples:
- Scraping attendee data from site 3 of 5, parsing JSON response
- Waiting for build to complete before running integration tests
- Refactoring the SMS outreach sequence, extracting send logic into service class

When you need input from the developer, write your question to
`.orch/waiting_for_input` (e.g., `echo 'your question' > .orch/waiting_for_input`).
The orchestrator will send a macOS notification and open your session
automatically. When you get the answer, delete the file with `rm .orch/waiting_for_input`.

If `.orch/pending_task` exists when you start, read it, delete it, and treat
its contents as your current task.

When working through TODOS.md:
- Mark items you start with `- [~]` (in progress)
- Mark completed items with `- [x]` (done)
- Work through items in the order they appear unless a specific task overrides

When you finish a task and have no more work to do, clear `.orch/status`:

    echo '' > .orch/status

This tells the orchestrator you are idle and ready for the next task.

## Cross-project bridge

All projects under `~/Apps/` are accessible read-only at their original paths
inside the VM. When you need changes in another orch-managed project, submit
a bridge request via the `orch bridge` CLI \u2014 it talks to the orch daemon
over HTTP, which atomically queues the request, runs a subagent on a worktree
of the target project, and reports status back you can poll.

This works from inside the Lima VM. The CLI auto-resolves the daemon at
`host.lima.internal:7777` from the VM and `127.0.0.1:7777` on the macOS host
(override via `ORCH_DAEMON_HOST` if needed).

```bash
orch bridge submit \\
  --target project-name \\
  --intent fix \\
  --summary "Brief one-line description" \\
  --context-file ./context.md \\
  --request-file ./request.md \\
  [--relevant-file path/to/file] [--relevant-file ...] \\
  [--parent-id br_...]   # only when this is a sub-request of an existing bridge
```

The command returns immediately with a bridge id (`br_...`). Use it to check
progress:

```bash
orch bridge status br_...   # full record + event log
orch bridge list            # everything you've submitted
orch bridge cancel br_...   # cancel pending or kill an inflight worker
orch bridge retry  br_...   # resubmit a failed bridge as a new record
```

**Intents:**
- `fix` \u2014 Request code changes in the target project (creates a branch and PR)
- `review` \u2014 Ask for a review of code or approach (returns feedback text)
- `query` \u2014 Ask a question about the target project (returns answer text)
- `inform` \u2014 One-way notification, no response expected

The daemon enforces per-target serialization (one bridge per target at a time
by default), retries transient failures automatically, and will reject requests
when the target project disables bridges via its `.orch/project.toml`. After
submitting, continue your current work \u2014 you do not need to wait. Poll
`orch bridge status <id>` only if a follow-up depends on the result.

If you fire a bridge while *handling* one (i.e. the bridge subagent itself
needs help from a third project), pass `--parent-id` with the id of the bridge
that's calling you. The daemon tracks depth automatically and will reject
chains that grow too deep. You should never compute or pass a depth value
yourself.

## Reference Library (.claude-docs/)

This project includes a reference library at `.claude-docs/`. These files serve
as a compass for decision-making \u2014 they provide guidance and context, not
step-by-step instructions.

**How to use:** When starting work on a feature or module, read
`.claude-docs/index.md` to find files whose topics relate to the category of
work. Load files loosely by topic area:

- UI/UX work \u2192 read design systems, component patterns, styling guides
- Infrastructure/deployment \u2192 read infra, tooling, cloud platform guides
- API development \u2192 read API patterns, authentication, error handling guides
- Agent/AI features \u2192 read context engineering, agent skills, workflow patterns
- Code quality \u2192 read conventions, testing, review patterns

Do not keyword-match specific sections. Match the *category* of the work to the
*topic* of the file. Read the full file when the topic is relevant. These are
guidance documents that inform your decisions \u2014 a compass, not a manual.

IMPORTANT: Reference files describe patterns from other projects. Do NOT create
new files, commands, hooks, or skills based on reference content unless the user
explicitly asks you to. Use the content to inform decisions, not to generate
project scaffolding unprompted.
"""
    claude_md.write_text(content)
    _ok("CLAUDE.md created")


def _write_settings(target: Path) -> None:
    dst = target / ".claude" / "settings.local.json"
    if dst.exists():
        _skip(".claude/settings.local.json already exists \u2014 skipped")
        return

    src = TEMPLATES_DIR / "settings_local.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _ok(".claude/settings.local.json created")


def _write_rules(target: Path) -> None:
    rules_src = TEMPLATES_DIR / "rules"
    rules_dst = target / ".claude" / "rules"
    rules_dst.mkdir(parents=True, exist_ok=True)

    created = 0
    skipped = 0
    for src_file in sorted(rules_src.glob("cc-*.md")):
        dst_file = rules_dst / src_file.name
        if dst_file.exists():
            skipped += 1
        else:
            shutil.copy2(src_file, dst_file)
            created += 1

    if created:
        _ok(f".claude/rules/ \u2014 {created} skeleton rule files created")
    if skipped:
        _skip(f".claude/rules/ \u2014 {skipped} rule files already exist")


def _write_reference_docs(target: Path) -> None:
    ref_src = TEMPLATES_DIR / "reference"
    ref_dst = target / ".claude-docs"

    if not ref_src.exists():
        _skip(".claude-docs/ \u2014 no reference templates found in orch")
        return

    copied, skipped = _copy_tree_additive(ref_src, ref_dst)

    if copied:
        _ok(f".claude-docs/ \u2014 reference library ({copied} files)")
    if skipped:
        _skip(f".claude-docs/ \u2014 {skipped} reference files already exist")
    if not copied and not skipped:
        _skip(".claude-docs/ \u2014 no reference files to copy")


def _generate_sibling_summaries(target: Path, exclude_name: str) -> None:
    from .discovery import discover_projects
    from .lifecycle import load
    from .models import Project
    from .stack import detect_stack, stack_label

    projects = discover_projects()

    rows = []
    for p in projects:
        if p.name == exclude_name:
            continue

        lc = load(p)

        result = detect_stack(p.path)
        stack = stack_label(result["tags"])

        # Get purpose from description or first meaningful line of CLAUDE.md
        purpose = lc.description
        if not purpose:
            claude_md = p.path / "CLAUDE.md"
            if claude_md.is_file():
                try:
                    for line in claude_md.read_text().splitlines():
                        stripped = line.strip()
                        if stripped and not stripped.startswith("#") and not stripped.startswith("<!--"):
                            purpose = stripped[:80]
                            break
                except OSError:
                    pass
        if not purpose:
            purpose = "\u2014"

        stage = lc.stage
        rows.append(f"| {p.name} | {stage} | {stack} | {purpose} |")

    dst = target / ".claude-docs" / "sibling-projects.md"
    dst.parent.mkdir(parents=True, exist_ok=True)

    content = """# Sibling Projects (orch-managed)

These are all projects managed by orch. They share the same Lima VM and can
communicate via the cross-project bridge. When working on a task, check if a
sibling project has already solved a similar problem or contains reusable code,
patterns, or infrastructure decisions. All projects are accessible read-only
at their original paths under ~/Apps/.

| Project | Stage | Stack | Purpose |
|---------|-------|-------|---------|
"""
    content += "\n".join(rows) + "\n"

    # This file is always regenerated (snapshot of current state)
    dst.write_text(content)
    _ok(f".claude-docs/sibling-projects.md \u2014 {len(rows)} projects indexed")


def _write_lifecycle(target: Path, name: str, stage: str, description: str) -> None:
    toml_path = target / ".orch" / "project.toml"
    if toml_path.exists():
        _skip(".orch/project.toml already exists \u2014 skipped")
        return

    from .models import Project
    from .lifecycle import ensure_initialized, load, save

    project = Project(path=target)
    lc = ensure_initialized(project)

    if stage != "building":
        from .lifecycle import advance_stage
        advance_stage(project, stage, "Initialized via orch init")

    if description:
        lc = load(project)
        lc.description = description
        save(project, lc)

    _ok(f".orch/project.toml \u2014 stage: {stage}")


def _install_plugins(target: Path) -> None:
    # curated-context
    cc_dir = target / ".curated-context"
    if cc_dir.is_dir():
        _skip("curated-context already set up")
    else:
        print("  Installing curated-context...", flush=True)
        result = subprocess.run(
            ["npx", "curated-context", "setup"],
            cwd=target,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            _ok("curated-context installed")
        else:
            _err(f"curated-context setup failed: {result.stderr[:200]}")

    # claude-mem — check for MCP server config
    settings_path = Path.home() / ".claude" / "settings.json"
    has_claude_mem = False
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
            mcp_servers = settings.get("mcpServers", {})
            has_claude_mem = "claude-mem" in mcp_servers or any(
                "claude-mem" in k for k in mcp_servers
            )
        except (json.JSONDecodeError, OSError):
            pass

    if has_claude_mem:
        _skip("claude-mem already configured")
    else:
        print()
        print("  claude-mem is not configured in ~/.claude/settings.json")
        print("  Install it with: npx claude-mem setup")
        print()


# ── Main command ─────────────────────────────────────────────────────────────

def cmd_init(argv: list[str]) -> None:
    target = None
    name = None
    stage = "building"
    description = ""
    do_plugins = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--name",) and i + 1 < len(argv):
            i += 1
            name = argv[i]
        elif arg in ("--stage",) and i + 1 < len(argv):
            i += 1
            stage = argv[i]
        elif arg in ("--description", "--desc") and i + 1 < len(argv):
            i += 1
            description = argv[i]
        elif arg == "--plugins":
            do_plugins = True
        elif arg in ("-h", "--help"):
            print("Usage: orch init [dir] [--name NAME] [--stage STAGE] [--description DESC] [--plugins]")
            print()
            print("  Bootstrap a project for Claude Code + orch.")
            print("  Additive only — never overwrites existing files.")
            print()
            print("Options:")
            print("  dir              Target directory (default: current directory)")
            print("  --name NAME      Project name (default: directory basename)")
            print("  --stage STAGE    Initial lifecycle stage (default: building)")
            print("  --description D  One-line project description")
            print("  --plugins        Also install claude-mem and curated-context")
            return
        elif not arg.startswith("-"):
            target = arg
        i += 1

    target_path = Path(target).resolve() if target else Path.cwd()
    if not target_path.is_dir():
        target_path.mkdir(parents=True, exist_ok=True)

    project_name = name or target_path.name

    print(f"\n  orch init \u2014 bootstrapping {project_name}\n")

    _ensure_git(target_path)
    _write_claude_md(target_path, project_name, description)
    _write_settings(target_path)
    _write_rules(target_path)
    _write_reference_docs(target_path)
    _generate_sibling_summaries(target_path, project_name)
    _write_lifecycle(target_path, project_name, stage, description)

    if do_plugins:
        print()
        _install_plugins(target_path)

    print(f"\n  Project ready. Start a session with: orch \u2192 select project \u2192 c\n")
