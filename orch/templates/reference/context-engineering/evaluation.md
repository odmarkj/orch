# Evaluation Methods for Agent Systems

This file covers how to evaluate agent systems that are non-deterministic and often lack single correct answers. It covers multi-dimensional rubrics, LLM-as-judge at scale, end-state evaluation for stateful agents, test set design, context engineering evaluation, and continuous monitoring. Consult this when testing agent performance systematically, validating context engineering choices, measuring improvements, building quality gates, or comparing configurations.

---

## Core Principles

- **Evaluate outcomes, not execution paths** -- agents may find alternative valid routes.
- **Multi-dimensional rubrics** over single scores -- one number hides critical failures.
- **LLM-as-judge for scale** + human review for edge cases and subtle biases.

## Performance Variance (BrowseComp Finding)

| Factor | Variance Explained | Implication |
|---|---|---|
| Token usage | 80% | More tokens = better performance |
| Tool calls | ~10% | More exploration helps |
| Model choice | ~5% | Better models multiply efficiency |

Prioritize model upgrades over token increases. Evaluate with production-realistic token limits.

## Multi-Dimensional Rubric

Score each dimension independently, then compute weighted aggregates:

| Dimension | Weight For |
|---|---|
| Factual accuracy | Knowledge tasks |
| Completeness | Research tasks |
| Citation accuracy | Trust-sensitive contexts |
| Source quality | Authoritative outputs |
| Tool efficiency | Cost-sensitive systems |

Map to 0.0-1.0. Passing threshold: 0.7 general, 0.9 high-stakes. Fail if any single dimension falls below minimum.

## LLM-as-Judge

Build evaluation prompts with: clear task description, output under test, ground truth when available, scale with explicit level descriptions, request for structured judgment with reasoning.

**Critical**: Use a different model family than the agent being evaluated (avoid self-enhancement bias).

## Test Set Design

- Start with 20-30 cases early, scale to 50+ for reliable signal.
- Stratify by complexity: simple, medium, complex, very complex.
- Report scores per stratum alongside overall.
- Sample from real usage, add edge cases, ensure complexity coverage.

```python
test_set = [
    {"name": "simple_lookup", "complexity": "simple",
     "input": "What is the capital of France?"},
    {"name": "multi_step", "complexity": "complex",
     "input": "Analyze Q1-Q4 sales data and create trend summary"},
]
```

## End-State Evaluation (Stateful Agents)

For agents that mutate files/databases/configs, evaluate whether final state matches expectations rather than how the agent got there. Define expected assertions and verify programmatically.

## Context Engineering Evaluation

- Run agents with different context strategies on same test set.
- Compare quality scores, token usage, efficiency metrics.
- Run degradation tests at different context sizes to find performance cliffs.

## Continuous Monitoring

- Integrate evaluation into development workflow (run on every change).
- Sample production interactions continuously.
- Alert at: 0.85 pass rate (warning), 0.70 pass rate (critical).
- Track trends over time windows.

## Building an Evaluation Framework

1. Define quality dimensions before writing eval code.
2. Create rubrics with clear, descriptive level definitions.
3. Build test sets from real usage + edge cases, 50+ cases, stratified by complexity.
4. Implement automated pipelines running on every significant change.
5. Establish baseline metrics before making changes.
6. Track metrics over time for trend analysis.
7. Supplement with human review on regular cadence.

## Key Gotchas

- Overfitting evals to specific code paths: write against outcomes, not surface patterns.
- LLM-judge self-enhancement bias: use different model family.
- Test set contamination: keep eval sets versioned and separate from prompt/training data.
- Metric gaming: cross-validate automated metrics against human judgments.
- Single-dimension scoring hides failures. Always report per-dimension scores.
- <50 examples = unreliable signal with high variance. Report confidence intervals.
- Easy examples inflate scores. Stratify and weight to prevent dominance.
- Eval is not one-time. Quality drifts as models update and tools change.
