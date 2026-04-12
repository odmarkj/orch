# Memory System Design

This file covers the persistence layer for agent continuity across sessions: memory layers (working through temporal knowledge graph), production framework comparison (Mem0, Zep/Graphiti, Letta, LangMem, Cognee), retrieval strategies, and consolidation patterns. Consult this when building agents that must persist knowledge across sessions, choosing between memory frameworks, implementing entity consistency, or designing memory architectures that scale.

---

## Key Insight

Tool complexity matters less than reliable retrieval. Letta's filesystem agents scored 74% on LoCoMo using basic file operations, beating Mem0's specialized tools at 68.5%.

## Framework Selection

| Framework | Architecture | Best For |
|---|---|---|
| **Mem0** | Vector store + graph, pluggable backends | Multi-tenant, fast time-to-production |
| **Zep/Graphiti** | Temporal knowledge graph, bi-temporal model | Relationship modeling + temporal reasoning |
| **Letta** | Self-editing memory, tiered storage | Full agent introspection, stateful services |
| **Cognee** | Multi-layer semantic graph, customizable ECL pipeline | Evolving memory, multi-hop reasoning |
| **LangMem** | Memory tools for LangGraph | Teams already on LangGraph |
| **File-system** | Plain files with naming conventions | Simple agents, prototyping |

**Benchmarks**: Cognee highest on HotPotQA multi-hop (EM, F1, Correctness). Zep 94.8% DMR accuracy, 90% latency reduction. Letta 74% LoCoMo (filesystem). Mem0 68.5% LoCoMo.

## Memory Layers (Escalation Path)

| Layer | Persistence | When to Use |
|---|---|---|
| **Working** | Context window only | Always -- optimize with attention positions |
| **Short-term** | Session-scoped | Intermediate tool results, conversation state |
| **Long-term** | Cross-session | User preferences, domain knowledge |
| **Entity** | Cross-session | Maintaining identity across conversations |
| **Temporal KG** | Cross-session + history | Facts that change over time, time-travel queries |

Start with simplest viable layer. Add complexity only when retrieval quality degrades.

## Retrieval Strategies

| Strategy | Use When | Limitation |
|---|---|---|
| **Semantic** (embedding similarity) | Direct factual queries | Degrades on multi-hop reasoning |
| **Entity-based** (graph traversal) | "Everything about X" queries | Requires graph structure |
| **Temporal** (validity filter) | Facts change over time | Requires validity metadata |
| **Hybrid** (semantic + keyword + graph) | Best overall accuracy | Most infrastructure |

## Escalation Path

1. **Prototype**: File-system memory. JSON with timestamps. Validates behavior before infrastructure.
2. **Scale**: Mem0 or vector store when semantic search and multi-tenant isolation needed.
3. **Complex reasoning**: Zep/Graphiti for relationship traversal and temporal validity.
4. **Full control**: Letta or Cognee for self-managed memory with deep introspection.

## Memory Consolidation

Run periodically to prevent unbounded growth. **Invalidate but do not discard** -- preserve history for temporal queries. Trigger on: memory count thresholds, degraded retrieval quality, or scheduled intervals.

## Integration with Context

- Load memories just-in-time, not preloading everything.
- Place retrieved memories at attention-favored positions (beginning/end).

## Error Recovery

1. **Empty retrieval**: Broaden search (remove filters, widen time range). If still empty, prompt user.
2. **Stale results**: Check `valid_until` timestamps. Trigger consolidation if most expired.
3. **Conflicting facts**: Prefer most recent `valid_from`. Surface conflict if low confidence.
4. **Storage failure**: Queue writes for retry. Never block response on a memory write.

## Code Examples

```python
# Mem0 integration
from mem0 import Memory
m = Memory()
m.add("User prefers dark mode and Python 3.12", user_id="alice")
results = m.search("What theme does the user prefer?", user_id="alice")

# Temporal query
graph.create_temporal_relationship(
    source_id=user_node, rel_type="LIVES_AT", target_id=address_node,
    valid_from=datetime(2024, 1, 15), valid_until=datetime(2024, 9, 1),
)
```

## Key Gotchas

- Loading all memories into prompt is expensive and degrades attention. Use just-in-time retrieval.
- Facts go stale without temporal validity tracking.
- Over-engineering early: filesystem can outperform complex tooling.
- Embedding model mismatch: writing with one model and reading with another produces poor retrieval.
- Graph schema rigidity: prefer generic relation types and flexible property bags.
- Memory about "Python" the snake vs the language -- include domain metadata for filtering.
