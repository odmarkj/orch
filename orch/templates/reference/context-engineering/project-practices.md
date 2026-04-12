# Project-Level Context Engineering Practices

This file covers how to apply context engineering principles at the project level rather than the individual prompt level. It addresses the organizational challenge of maintaining consistent, high-quality context across an entire codebase or product, including how to structure documentation for AI consumption, manage context budgets across features, and establish team workflows that produce good context as a natural byproduct of development. Consult this when setting up new projects, establishing team conventions for AI-assisted development, or improving how an existing codebase interacts with AI coding tools.

---

## Core Principle

Context engineering is a team discipline, not a one-time configuration. The quality of AI assistance scales with the quality and organization of project context.

## Documentation as Context

### Structure for AI Consumption
- Write README, CLAUDE.md, and architecture docs with the assumption that an AI will read them
- Lead with the most frequently needed information
- Use consistent formatting (headers, bullet lists, tables) over prose
- Include command examples that can be copy-pasted
- Keep files focused: one topic per file, max 200 lines

### Living Documentation Pattern
- Static docs: architecture decisions, coding standards, tech stack choices (human-maintained)
- Dynamic docs: current state, recent changes, active work (AI-assisted or auto-generated)
- Separate these clearly so tooling knows what to update vs preserve

## Context Budget Management

### Per-Feature Budgeting
- Estimate how much context each feature area needs
- Allocate token budget: 40% current task, 30% project context, 20% reference, 10% buffer
- Use progressive loading: summaries first, full docs on demand
- Monitor actual usage and adjust budgets based on task success rates

### Reducing Context Waste
- Remove outdated documentation aggressively
- Compress verbose docs into actionable summaries
- Use structured formats (tables, key-value pairs) over paragraphs
- Deduplicate information across files

## Team Workflows

### Decision Logging
- Log architectural decisions in a machine-readable format
- Include: what was decided, why, what alternatives were considered
- Make logs appendable (not requiring edits to existing entries)
- Reference decision logs in CLAUDE.md so AI can find them

### Code Review for Context
- Review PRs not just for code quality but for context quality
- Does the PR update relevant documentation?
- Are new conventions captured where AI tools can find them?
- Do commit messages explain the why, not just the what?

## Anti-Patterns

- **Context hoarding**: Putting everything in one massive file
- **Stale context**: Documentation that describes the project 6 months ago
- **Implicit knowledge**: Conventions that exist only in team members' heads
- **Over-specification**: Writing instructions so detailed they break when anything changes
- **Under-specification**: Writing guidance so vague it doesn't constrain anything
