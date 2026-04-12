# Context Engineering Fundamentals

This file covers the foundational concepts of context engineering for AI agent systems: what context is, how attention mechanics work, the anatomy of context components (system prompts, tool definitions, retrieved documents, message history, tool outputs), and strategies for progressive disclosure and context budgeting. Consult this when designing new agent systems, debugging unexpected agent behavior related to context, optimizing token usage, or onboarding to context engineering concepts.

---

## Core Principle

Treat context as a finite attention budget, not a storage bin. Every token competes for the model's attention. The engineering problem is maximizing utility per token against three constraints:
- Hard token limit
- Effective capacity ceiling (60-70% of advertised window)
- U-shaped attention curve penalizing middle-positioned information

## Four Assembly Principles

1. **Informativity over exhaustiveness** -- include only what matters for the current decision; retrieve additional info on demand.
2. **Position-aware placement** -- place critical constraints at beginning and end (85-95% recall); middle drops to 76-82%.
3. **Progressive disclosure** -- load skill names/summaries at startup; full content only when activated.
4. **Iterative curation** -- context engineering is ongoing, not a one-time prompt-writing exercise.

## Context Components

### System Prompts
- Organize with XML tags or Markdown headers (background, instructions, tool guidance, output format).
- Calibrate instruction altitude: too-low hardcodes brittle logic; too-high is vague. Aim for heuristic-driven instructions.
- Start minimal, add instructions reactively based on observed failures.

### Tool Definitions
- Answer three questions: what it does, when to use it, what it returns.
- Keep tool set minimal. Schemas inflate 2-3x after JSON serialization.
- 10 tools with moderate schemas can consume 5,000-8,000 tokens before any message.

### Retrieved Documents
- Maintain lightweight identifiers (file paths, stored queries) and load data just-in-time.
- Strong identifiers (`customer_pricing_rates.json`) > weak identifiers (`data/file1.json`).
- Split at natural semantic boundaries, not arbitrary character limits.

### Message History
- Serves as scratchpad memory for tracking progress and task state.
- Monitor growth; apply compaction before it crowds out active instructions.
- Replace stale tool outputs with compact summaries.

### Tool Outputs
- Can reach 83.9% of total tokens in agent trajectories.
- Apply observation masking: replace verbose outputs with compact references.
- Retain only 5 most recently accessed file contents.

## Attention Mechanics

- n tokens = n-squared pairwise attention relationships.
- Effective capacity: 60-70% of advertised window. A 200K model degrades around 120-140K tokens.
- Complex retrieval accuracy can drop to 15% at extreme lengths.

## Progressive Disclosure Levels

1. **Skill selection** -- names and descriptions at startup; full content on demand.
2. **Document loading** -- summaries first; detail sections when task requires.
3. **Tool result retention** -- recent results in full; compress/evict older results.

If activated, load fully rather than partially -- partial loads create confusing gaps.

## Context Budgeting

- Allocate explicit budgets per component.
- Trigger compaction at 70-80% utilization.
- Sub-agent compression ratio: explore with tens of thousands of tokens, return 1,000-2,000 token summary.

## Hybrid Context Strategies

| Volatility | Strategy |
|---|---|
| Low (project conventions, team standards) | Pre-load at session start |
| High (code state, external data, user-specific) | Retrieve just-in-time |

For complex multi-hour tasks, maintain a structured notes file (NOTES.md) updated as the agent works.

## Key Gotchas

- Nominal window is not effective capacity. Budget for 60-70%.
- ~4 chars/token breaks for code (2-3), URLs (each slash/dot is a token), non-English (1-2).
- Tool schemas inflate 2-3x after serialization.
- Message history balloons silently in agentic loops; set hard token ceiling.
- Critical instructions in the middle get lost (10-40% less recall).
- Mixing instruction altitudes causes inconsistent behavior.
