# Context Compression Strategies

This file covers how to compress context in long-running agent sessions while preserving task-critical information. It covers three production-ready compression approaches (anchored iterative, opaque, regenerative), the artifact trail problem, structured summary design, trigger strategies, and probe-based evaluation. Consult this when agent sessions exceed context limits, when agents "forget" files they modified, or when designing conversation summarization.

---

## Key Insight: Optimize for Tokens-Per-Task, Not Tokens-Per-Request

Measure total tokens from task start to completion. When compression drops file paths or decision rationale, the agent re-explores and re-derives -- wasting far more tokens than compression saved. Track re-fetching frequency as primary quality signal.

## Three Compression Approaches

### 1. Anchored Iterative Summarization
Best for long sessions where file tracking matters. Maintain structured summaries with explicit sections. On compression, summarize only newly-truncated span and merge with existing summary (never regenerate from scratch).

### 2. Opaque Compression
For short sessions with low re-fetching cost. Produces compressed representations optimized for reconstruction (99%+ compression ratios). Sacrifices interpretability -- cannot verify what was preserved without probe evaluation.

### 3. Regenerative Full Summary
For sessions with clear phase boundaries where readability is critical. Generates detailed structured summaries each trigger. Weakness: cumulative detail loss across repeated cycles.

## Comparison

| Method | Compression Ratio | Quality Score | Best For |
|---|---|---|---|
| Anchored Iterative | 98.6% | 3.70 | Long sessions, file tracking |
| Regenerative | 98.7% | 3.44 | Readable summaries, phase boundaries |
| Opaque | 99.3% | 3.35 | Max token savings, short sessions |

## The Artifact Trail Problem

Artifact trail integrity scores only 2.2-2.5/5.0 across all methods. Preserve explicitly in every compression cycle:
- Files created (full paths)
- Files modified and what changed (include function names)
- Files read but not changed
- Specific identifiers: function names, variable names, error messages, error codes

Implement a separate artifact index rather than relying on the summarizer.

## Structured Summary Template

```markdown
## Session Intent
[What the user is trying to accomplish]

## Files Modified
- auth.controller.ts: Fixed JWT token generation
- config/redis.ts: Updated connection pooling

## Decisions Made
- Using Redis connection pool instead of per-request connections

## Current State
- 14 tests passing, 2 failing

## Next Steps
1. Fix remaining test failures
2. Run full test suite
```

Adapt sections to domain (debugging agent needs "Root Cause" and "Error Messages").

## Compression Triggers

| Strategy | Trigger Point | Trade-off |
|---|---|---|
| Fixed threshold | 70-80% context utilization | Simple but may compress too early |
| Sliding window | Keep last N turns + summary | Predictable context size |
| Importance-based | Compress low-relevance first | Complex but preserves signal |
| Task-boundary | Compress at task completions | Clean summaries, unpredictable timing |

Default: sliding window with structured summaries for coding agents.

## Probe-Based Evaluation

Traditional metrics (ROUGE, embedding similarity) fail to capture functional quality. Use probes:

| Probe Type | Tests | Example |
|---|---|---|
| Recall | Factual retention | "What was the original error message?" |
| Artifact | File tracking | "Which files have we modified?" |
| Continuation | Task planning | "What should we do next?" |
| Decision | Reasoning chain | "What did we decide about the Redis issue?" |

## Six Evaluation Dimensions

1. **Accuracy** -- technical details correct (largest variation between methods)
2. **Context Awareness** -- reflects current conversation state
3. **Artifact Trail** -- knows which files read/modified (universally weak)
4. **Completeness** -- addresses all parts of the question
5. **Continuity** -- work can continue without re-fetching
6. **Instruction Following** -- respects stated constraints

## Three-Phase Workflow for Large Codebases

1. **Research Phase**: Explore architecture, compress into structured analysis.
2. **Planning Phase**: Convert to implementation spec (5M tokens -> ~2,000 words).
3. **Implementation Phase**: Execute against spec + active working files.

## Key Gotchas

- Never compress tool definitions or schemas (destroys agent functionality).
- Compressed summaries hallucinate facts -- validate against source material.
- File paths get paraphrased or dropped -- preserve identifiers verbatim.
- Early turns contain irreplaceable constraints -- protect from compression.
- 95% compression applied 3x = only 0.0125% of tokens remain.
- Code and prose need different compression (code is not redundant).
