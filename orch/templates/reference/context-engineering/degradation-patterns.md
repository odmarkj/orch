# Context Degradation Patterns

This file covers the five distinct patterns of context failure -- lost-in-middle, poisoning, distraction, confusion, and clash -- along with detection signals and mitigation strategies for each. Consult this when agent performance degrades unexpectedly during long conversations, when debugging incorrect or irrelevant outputs, or when designing systems that must handle large contexts reliably.

---

## Five Degradation Patterns

### 1. Lost-in-Middle
Information in middle positions suffers 10-40% reduced recall accuracy (U-shaped attention curve).

**Detection**: Correct information exists in context but model ignores it; responses contradict provided data.

**Mitigation**: Place critical info at beginning and end. Add explicit section headers as attention anchors. When a full document must be included, prepend a summary and append conclusions.

### 2. Context Poisoning
Hallucinations, tool errors, or incorrect retrieved facts enter context and compound through self-reference.

**Detection**: Degraded output quality on previously-successful tasks; tool misalignment; persistent hallucinations despite correction.

**Mitigation**: Remove poisoned content rather than adding corrections on top. Truncate to before the poisoning point or restart with verified-only context. Validate all external inputs before they enter context.

### 3. Context Distraction
Even a single irrelevant document triggers measurable degradation (step function, not linear).

**Detection**: Attention competition between relevant and irrelevant content.

**Mitigation**: Filter aggressively before loading. Move "might need" info behind tool calls. Use namespacing and structural organization.

### 4. Context Confusion
Model applies wrong-context constraints to current task (distinct from distraction).

**Detection**: Responses address wrong aspect; tool calls appropriate for different task; outputs mix requirements from multiple sources.

**Mitigation**: Segment tasks into separate context windows. Use explicit "context reset" markers. Isolate objectives, constraints, and tool definitions per task.

### 5. Context Clash
Multiple correct-but-contradictory sources (version conflicts, perspective differences).

**Detection**: Model silently picks one conflicting fact without signaling conflict.

**Mitigation**: Establish source priority rules before conflicts arise. Mark contradictions explicitly. Filter outdated versions before they enter context.

## Four-Bucket Mitigation Framework

| Strategy | When to Use | Action |
|---|---|---|
| **Write** | Context > 70% utilization | Save context outside the window (scratchpads, files, external storage) |
| **Select** | Distraction/confusion symptoms | Pull only relevant context via retrieval, filtering, prioritization |
| **Compress** | Growing context, all relevant | Summarize, abstract, apply observation masking |
| **Isolate** | Confusion/clash, independent tasks | Split across sub-agents with isolated contexts |

## Degradation Thresholds

- Expect degradation onset at 60-70% of advertised context window.
- Only 50% of models claiming 32K+ maintain satisfactory performance at that length (RULER benchmark).
- Performance holds steady until a model-specific threshold, then drops sharply (cliff edge, not gradual).
- Models with extended thinking reduce hallucination but at higher latency/cost.

## Counterintuitive Findings

- **Shuffled context can outperform coherent context** for retrieval tasks (coherent context creates false associations).
- **Single distractors have outsized impact** -- binary effect, not proportional.
- **Low needle-question similarity accelerates degradation** -- inference across dissimilar content degrades faster.

## Architectural Patterns for Resilience

- Just-in-time context loading (retrieve only when current step needs it).
- Observation masking (replace verbose outputs with compact references after processing).
- Sub-agent architectures (each agent holds only task-relevant context).
- Trigger compaction before degradation onset threshold, not after symptoms appear.

## Key Gotchas

- Normal 5-10% quality variance is noise; sustained decline tied to context growth is signal.
- Model-specific thresholds shift 20-50% with updates. Re-benchmark quarterly.
- Needle-in-haystack 99% does not predict real-world 128K performance.
- Contradictory RAG documents poison silently -- implement contradiction detection.
- Poor prompt structure mimics degradation symptoms. Test at low context lengths first.
- Set compaction triggers at 70% of known onset, not at the onset itself.
