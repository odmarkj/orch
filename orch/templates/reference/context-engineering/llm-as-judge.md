# Advanced Evaluation: LLM-as-Judge

This file covers production-grade techniques for using LLMs as judges: direct scoring, pairwise comparison, rubric generation, bias mitigation, and scaling strategies. Consult this when building automated evaluation pipelines, comparing model outputs, establishing consistent quality standards, debugging inconsistent evaluation results, or designing A/B tests for prompt/model changes.

---

## Two Primary Approaches

### Direct Scoring
Use when objective criteria exist (factual accuracy, instruction following, toxicity). Single LLM rates one response on a defined scale.

### Pairwise Comparison
Use for subjective preferences (tone, style, persuasiveness). LLM compares two responses and selects better one. Higher human-judge agreement than direct scoring for preference tasks.

**Decision tree**: Ground truth exists? -> Direct Scoring. Preference/quality judgment? -> Pairwise. Neither? -> Reference-based evaluation.

## The Bias Landscape

| Bias | Description | Mitigation |
|---|---|---|
| Position | First-position preference | Evaluate twice with swapped positions + majority vote |
| Length | Longer = higher score | Prompt to ignore length; length-normalized scoring |
| Self-Enhancement | Models prefer own outputs | Use different model for generation vs evaluation |
| Verbosity | Excessive detail scores high | Criteria-specific rubrics penalizing irrelevant detail |
| Authority | Confident tone = higher score | Require evidence citation; fact-checking layer |

## Direct Scoring Implementation

Three components: clear criteria, calibrated scale, structured output.

**Scale selection**:
- 1-3: Binary with neutral, lowest cognitive load
- 1-5: Standard Likert, best balance (default)
- 1-10: Only with detailed per-level rubrics

**Always require justification before the score** -- improves reliability 15-25%.

```
For each criterion:
1. Find specific evidence in the response
2. Score according to rubric (1-{max} scale)
3. Justify your score with evidence
4. Suggest one specific improvement
```

## Pairwise Comparison Implementation

Position bias mitigation protocol:
1. First pass: A first, B second
2. Second pass: B first, A second
3. If passes disagree: return TIE with reduced confidence
4. Consistent winner: averaged confidence

**Always swap positions** -- single-pass is corrupted by position bias.

## Rubric Generation

Reduces evaluation variance by 40-60%. Include:
1. **Level descriptions**: Clear boundaries for each score level
2. **Characteristics**: Observable features defining each level
3. **Examples**: Representative text per level
4. **Edge cases**: Guidance for ambiguous situations
5. **Scoring guidelines**: General consistency principles

**Strictness calibration**: Lenient (encouraging iteration) -> Balanced (production) -> Strict (safety-critical).

## Metric Selection

| Task Type | Primary Metrics | Secondary |
|---|---|---|
| Binary (pass/fail) | Recall, Precision, F1 | Cohen's kappa |
| Ordinal (1-5 rating) | Spearman's rho, Kendall's tau | Weighted kappa |
| Pairwise preference | Agreement rate, Position consistency | Confidence calibration |
| Multi-label | Macro-F1, Micro-F1 | Per-label precision/recall |

Prioritize systematic disagreement patterns over absolute agreement rates.

## Scaling Strategies

1. **Panel of LLMs (PoLL)**: Multiple models as judges, aggregate votes. More reliable for high-stakes.
2. **Hierarchical**: Fast cheap model for screening, expensive model for edge cases.
3. **Human-in-the-loop**: Automate clear cases, route low-confidence to humans. Build feedback loops.

## Key Gotchas

- Scoring without justification lacks grounding. Always require evidence first.
- Single-pass pairwise = position bias corrupted results. Always swap and check consistency.
- Overloaded criteria measuring multiple things produce unreliable scores. One criterion = one measurable aspect.
- Missing edge case guidance causes inconsistent handling. Include explicit resolution rules.
- High-confidence wrong judgments are worse than low-confidence ones. Calibrate to evidence strength.
- Rubric drift: schedule periodic reviews and re-anchor against fresh human-annotated examples.
- Minor eval prompt wording changes cause 10-20% score swings. Version-control prompts and regression test.
- Uncontrolled length bias: add explicit length-neutrality instructions and validate with controlled test pairs.
