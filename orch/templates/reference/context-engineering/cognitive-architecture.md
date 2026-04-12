# Cognitive Architecture for AI Agents

This file covers the Belief-Desire-Intention (BDI) model and related cognitive architectures applied to AI agent systems. It addresses how to structure agent reasoning, maintain coherent goals across multi-step tasks, manage competing objectives, and build agents that can explain their decision-making process. Consult this when designing complex agent systems, building multi-step planners, creating agents that need to reason about tradeoffs, or debugging agents that lose track of their goals mid-task.

---

## BDI Model for Agents

### Beliefs (What the agent knows)
- Current state of the codebase, environment, and task
- Derived from: file reads, tool outputs, conversation history
- Must be refreshed: beliefs can become stale as the environment changes
- Implementation: structured context sections, state summaries

### Desires (What the agent wants to achieve)
- High-level goals from the user's request
- Quality constraints (correctness, style, performance)
- Implicit goals (don't break existing functionality, follow conventions)
- Implementation: goal decomposition in system prompt, task hierarchies

### Intentions (What the agent is committed to doing next)
- Current plan of action, derived from beliefs + desires
- Should be revisable when beliefs change (new information)
- Implementation: explicit plan state, task lists, checkpoint reasoning

## Practical Architecture Patterns

### Goal Decomposition
```
User request → High-level goal
  → Sub-goals (ordered by dependency)
    → Actions (concrete tool calls)
      → Verification (check if sub-goal met)
        → Next sub-goal or revise plan
```

### Reflective Loops
1. **Act**: Execute the next planned action
2. **Observe**: Read the result
3. **Reflect**: Does this match expectations? Is the plan still valid?
4. **Revise**: Adjust plan if needed, otherwise continue

### Memory Architecture
- **Working memory**: Current task context, recent tool outputs (in conversation)
- **Short-term memory**: Session-level state, decisions made, paths explored (task tracking)
- **Long-term memory**: Project knowledge, user preferences, past solutions (persistent files)

## Goal Conflict Resolution

When an agent faces competing objectives:
1. **Priority ordering**: Safety > correctness > convention > style > performance
2. **User intent trumps convention**: If the user explicitly asks for something, prefer their intent over project conventions
3. **Minimize side effects**: When multiple approaches achieve the goal, prefer the one with fewer changes
4. **Ask when uncertain**: If the tradeoff is significant and unclear, surface it to the user

## Maintaining Coherence Across Long Tasks

- **Checkpoint summaries**: Periodically write down what's been done and what's remaining
- **Goal re-anchoring**: Before each major action, re-read the original goal
- **Drift detection**: Compare current direction against original intent
- **Explicit state transitions**: Mark when moving between phases (research → implementation → testing)

## Anti-Patterns

- **Goal amnesia**: Losing track of the original objective after many tool calls
- **Sunk cost**: Continuing a failing approach because of prior investment
- **Scope creep**: Adding unrequested improvements that dilute focus
- **Analysis paralysis**: Reading more files instead of acting on sufficient information
- **Tunnel vision**: Focusing on one sub-goal while ignoring its impact on others
