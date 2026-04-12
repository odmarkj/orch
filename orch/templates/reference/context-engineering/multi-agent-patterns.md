# Multi-Agent Architecture Patterns

This file covers when and how to distribute work across multiple language model instances, each with its own context window. It covers supervisor/orchestrator, peer-to-peer/swarm, and hierarchical patterns, context isolation principles, consensus mechanisms, and failure modes. Consult this when single-agent context limits constrain task complexity, when tasks decompose into parallel subtasks, or when different subtasks need different tool sets.

---

## When to Use Multi-Agent

Use when a single agent's context window cannot hold all task-relevant information. Context isolation is the primary benefit -- each agent operates in a clean context without accumulated noise.

## Token Economics

| Architecture | Token Multiplier | Use Case |
|---|---|---|
| Single agent chat | 1x baseline | Simple queries |
| Single agent with tools | ~4x baseline | Tool-using tasks |
| Multi-agent system | ~15x baseline | Complex research/coordination |

Three factors explain 95% of performance variance: token usage (80%), tool calls (~10%), model choice (~5%).

## Three Patterns

### 1. Supervisor/Orchestrator
Central agent maintains global state, decomposes objectives, routes to specialists, synthesizes results.

**Use when**: Tasks have clear decomposition; human oversight matters.
**Trade-offs**: Strict control, easier human-in-the-loop. But supervisor context becomes bottleneck; "telephone game" problem where supervisors paraphrase sub-agent responses incorrectly.

**Fix telephone game**: Implement `forward_message` tool allowing sub-agents to pass responses directly to users without supervisor synthesis. Initially ~50% worse without this fix.

### 2. Peer-to-Peer/Swarm
No central control. Any agent transfers control to any other through explicit handoff mechanisms.

```python
def transfer_to_agent_b():
    return agent_b  # Handoff via function return
```

**Use when**: Flexible exploration needed; rigid planning counterproductive; requirements emerge dynamically.
**Trade-offs**: No single point of failure. But coordination complexity increases quadratically with agent count.

### 3. Hierarchical
Layers of abstraction: strategy (goal definition) -> planning (task decomposition) -> execution (atomic tasks).

**Use when**: Projects have clear hierarchical structure; tasks need both high-level planning and detailed execution.

## Context Isolation Mechanisms

| Mechanism | When to Use |
|---|---|
| Full context delegation | Complex tasks needing complete understanding (partially defeats isolation purpose) |
| Instruction passing | Simple, well-defined subtasks (default choice) |
| File system memory | Complex tasks with shared state (scales best) |

Default to instruction passing. Escalate to file system memory when shared state is needed.

## Consensus Mechanisms

- **Avoid simple majority voting** -- treats hallucinations as equal to reasoning.
- **Weighted voting** -- weight by confidence or expertise.
- **Debate protocols** -- adversarial critique yields higher accuracy than collaborative consensus. Guard against sycophantic convergence.

## Failure Modes

| Failure | Mitigation |
|---|---|
| Supervisor bottleneck | Constrain worker output schemas; checkpoint supervisor state. Cap 3-5 workers per supervisor. |
| Coordination overhead | Minimize communication; batch results; use async patterns. |
| Divergence | Clear objective boundaries; convergence checks; TTL limits. |
| Error propagation | Validate outputs before passing; retry with circuit breakers; add verification agent. |
| Telephone game | Use filesystem coordination instead of message-passing for state multiple agents need. |

## Key Gotchas

- Adding agents past 3-5 shows diminishing returns; coordination channels grow quadratically.
- Token cost is ~15x baseline -- budget accordingly.
- Agents in debate tend toward agreement (sycophancy), not correctness. Assign explicit adversarial roles.
- Over-decomposition: 10-step pipeline with 10 agents spends more on handoffs than work.
- Establish shared persistent storage before building multi-agent workflows.
