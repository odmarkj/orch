# Context Optimization Techniques

This file covers four strategies for extending effective context capacity: KV-cache optimization, observation masking, compaction, and context partitioning. These techniques can double or triple effective capacity without larger models. Consult this when context limits constrain task complexity, optimizing for cost/latency reduction, implementing long-running agent systems, or building production systems at scale.

---

## Strategy Priority Order

Apply in this order (by impact and risk):

### 1. KV-Cache Optimization
Zero quality risk, immediate cost/latency savings. Apply first and unconditionally.

**Prompt ordering for cache hits**:
1. System prompt (most stable, never changes within session)
2. Tool definitions (stable across requests)
3. Frequently reused templates and few-shot examples
4. Conversation history (grows but shares prefix)
5. Current query and dynamic content (least stable, always last)

**Critical rules**:
- Remove timestamps, session counters, request IDs from system prompt.
- Even a single whitespace change invalidates the entire cached block downstream.
- Target: 70%+ cache hit rate = 50%+ cost reduction, 40%+ latency reduction.

### 2. Observation Masking
Tool outputs consume 80%+ of tokens in typical trajectories. Largest capacity gains.

**Masking rules**:
- **Never mask**: Current task observations, most recent turn, active reasoning chains, error outputs during debugging.
- **Mask after 3+ turns**: Verbose outputs whose key points are already extracted. Replace with: `[Obs:{ref_id} elided. Key: {summary}. Full content retrievable.]`
- **Always mask immediately**: Duplicates, boilerplate headers/footers, already-summarized outputs.

Target: 60-80% reduction in masked observations, <2% quality impact.

### 3. Compaction
Trigger at 70-80% context utilization. Summarize and reinitialize.

**Compression priority**: Tool outputs first (80%+ of tokens) -> old conversation turns -> retrieved documents. **Never** compress system prompt.

**Preserve by type**:
- Tool outputs: key findings, metrics, error codes, conclusions.
- Conversational turns: decisions, commitments, preferences, context shifts.
- Retrieved documents: claims, facts, data points relevant to active task.

Target: 50-70% token reduction, <5% quality degradation. If >70% reduction, audit for information loss.

### 4. Context Partitioning
Split work across sub-agents when estimated context exceeds 60% of window limit.

Decompose into independent subtasks, assign to sub-agents, aggregate results. Coordination has real token cost -- only partition when savings exceed overhead (typically requires 3+ subtasks).

## Budget Management

Allocate explicit budgets per category: system prompt, tool definitions, retrieved documents, message history, tool outputs, reserved buffer (5-10%).

**Trigger-based optimization signals**:
- Token utilization >80%: trigger compaction
- Repetition/missed instructions: trigger masking + compaction
- Quality score drops below baseline: audit context composition

## Decision Framework

| Context Composition | First Action | Second Action |
|---|---|---|
| Tool outputs dominate (>50%) | Observation masking | Compaction of remaining turns |
| Retrieved documents dominate | Summarization | Partitioning if docs independent |
| Message history dominates | Compaction with selective preservation | Partitioning for new subtasks |
| Multiple components | KV-cache first, then layer masking + compaction |
| Near-limit with active debugging | Mask resolved tool outputs only -- preserve errors |

## Performance Targets

| Technique | Token Reduction | Quality Impact | Latency Impact |
|---|---|---|---|
| KV-cache | 50%+ cost reduction | Zero | 40%+ reduction |
| Masking | 60-80% of observations | <2% | Near-zero overhead |
| Compaction | 50-70% overall | <5% | <10% overhead from summarization |
| Partitioning | Net savings after coordinator overhead | Varies | Parallel execution gains |

## Key Gotchas

- Whitespace/newline changes in prefix invalidate entire KV-cache downstream. Pin system prompts as immutable.
- `Current date: {today}` in system prompt = full cache miss every day. Move to user message.
- Compaction at >85% utilization degrades summarization quality. Trigger at 70-80%.
- Masking error outputs breaks debugging loops. Suspend masking for error observations until resolved.
- Partitioning overhead can exceed savings for <3 independent subtasks.
- Prompt changes between deployments cause temporary 2-5x cost spike until cache warms.
- Compacted summaries look authoritative but may carry stale information. Re-validate against current task goal.
