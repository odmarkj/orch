# Filesystem-Based Context Engineering

This file covers using the filesystem as the primary overflow layer for agent context: scratch pads for tool output offloading, plan persistence, sub-agent communication via files, dynamic skill loading, terminal/log persistence, and agent self-modification. Consult this when tool outputs bloat the context window, agents need state persistence across long trajectories, sub-agents must share information, or tasks require more context than fits in the window.

---

## Core Principle

Prefer dynamic context discovery (pulling relevant context on demand) over static inclusion. Static context consumes tokens regardless of relevance.

## Four Context Failure Modes and Filesystem Remedies

| Mode | Problem | Fix |
|---|---|---|
| Missing context | Needed info absent | Persist tool outputs and intermediate results to files |
| Under-retrieved | Retrieved content insufficient | Structure files for targeted retrieval (grep-friendly, clear headers) |
| Over-retrieved | Too much retrieved, wastes tokens | Offload bulk to files, return compact references |
| Buried context | Niche info hidden across files | Combine glob + grep (structural) with semantic search (conceptual) |

## Pattern 1: Scratch Pad (Tool Output Offloading)

Redirect large tool outputs (>2000 tokens) to files, return summary + reference:

```python
def handle_tool_output(output, threshold=2000):
    if len(output) < threshold:
        return output
    file_path = f"scratch/{tool_name}_{timestamp}.txt"
    write_file(file_path, output)
    summary = extract_summary(output, max_tokens=200)
    return f"[Output in {file_path}. Summary: {summary}]"
```

Result: ~100 tokens in context, full output accessible via grep/read.

## Pattern 2: Plan Persistence

Write plans to filesystem so agents can re-read at any point:

```yaml
# scratch/current_plan.yaml
objective: "Refactor authentication module"
status: in_progress
steps:
  - id: 1
    description: "Audit current auth endpoints"
    status: completed
  - id: 2
    description: "Design new token validation flow"
    status: in_progress
```

Re-read plan at start of each turn or after context refresh.

## Pattern 3: Sub-Agent Communication via Filesystem

Route findings through files instead of message passing (avoids "telephone game"):

```
workspace/
  agents/
    research_agent/findings.md
    code_agent/changes.md
  coordinator/synthesis.md
```

Enforce per-agent directory isolation to prevent write conflicts.

## Pattern 4: Dynamic Skill Loading

Include only skill names + descriptions in static context. Load full skill file only when task requires it:

```
Available skills (load with read_file when relevant):
- database-optimization: Query tuning and indexing strategies
- api-design: REST/GraphQL best practices
```

Converts O(n) static token cost into O(1) per task.

## Pattern 5: Terminal/Log Persistence

Persist terminal output to files automatically. Query with targeted grep:
```
grep -A 5 "error" terminals/1.txt
```

## Pattern 6: Self-Modification

Agents write learned preferences to instruction files for subsequent sessions. Guard with validation -- self-modification can accumulate incorrect instructions. Review periodically.

## File Organization

```
project/
  scratch/           # Temporary working files
    tool_outputs/    # Large tool results
    plans/           # Active plans and checklists
  memory/            # Persistent learned information
  skills/            # Loadable skill definitions
  agents/            # Sub-agent workspaces
```

## Filesystem Search

- `ls`/`list_dir`: Discover directory structure
- `glob`: Find files matching patterns (`**/*.py`)
- `grep`: Search contents, returns matching lines with context
- `read_file` with ranges: Specific sections without loading entire files

Use filesystem search for structural/exact-match queries. Semantic search for conceptual queries.

## When to Use

**Use when**: Tool outputs >2000 tokens, tasks span multiple turns, multiple agents need shared state, skills exceed system prompt size, logs need selective querying.

**Avoid when**: Tasks complete in single turns, context fits comfortably, latency is critical, model lacks filesystem tools.

## Key Gotchas

- Scratch directory unbounded growth: implement age-based or count-based retention.
- Race conditions in multi-agent file access: enforce per-agent isolation or append-only files.
- Stale file references after moves/renames: verify existence before reading cached paths.
- Overly broad globs (`**/*`) pull irrelevant files. Scope to specific directories/extensions.
- Check file size before reading to avoid dumping 100K+ tokens in one call.
- Unstructured scratch pads become unparseable. Define and enforce schema from first write.
- Hardcoded absolute paths break in different environments. Use relative paths or resolve dynamically.
