# Tool Design for Agents

This file covers designing tools as contracts between deterministic systems and non-deterministic agents. It covers the consolidation principle, architectural reduction, description engineering, response format optimization, error message design, and MCP naming. Consult this when creating new tools for agent systems, debugging tool-related failures, optimizing existing tool sets, or standardizing tool conventions.

---

## Core Principle

If a human engineer cannot definitively say which tool should be used in a given situation, an agent cannot do better. Reduce the tool set until each tool has one unambiguous purpose.

## The Tool-Agent Interface

- Tool descriptions load directly into agent context and steer reasoning.
- Write descriptions that answer: what it does, when to use it, what it returns.
- Include format examples, expected patterns, explicit constraints.
- Namespace tools under common prefixes as collections grow (`db_*`, `web_*`).

## Consolidation Principle

Build single comprehensive tools instead of multiple narrow overlapping tools. Vercel reduced from 17 specialized tools to 2 general-purpose tools with better performance.

**When NOT to consolidate**: Tools have fundamentally different behaviors, serve different contexts, or must be independently callable. Over-consolidation (too many parameters/modes) is equally problematic.

## Architectural Reduction

Push consolidation to its extreme: replace most specialized tools with primitive, general-purpose capabilities.

**File System Agent Pattern**: Provide direct filesystem access through a single command execution tool. Agent uses standard Unix utilities (grep, cat, find, ls). Works because models understand filesystems deeply and can chain primitives flexibly.

**Reduce when**: Data layer is well-documented; model has sufficient reasoning; specialized tools constrain rather than enable.

**Add complexity when**: Data is messy/undocumented; domain requires specialized knowledge; safety constraints required; operations genuinely benefit from structured workflows.

## Description Engineering

Structure every description to answer four questions:
1. **What** does the tool do? (Avoid vague language)
2. **When** should it be used? (Direct triggers and indirect signals)
3. **What inputs** does it accept? (Types, constraints, defaults, format examples)
4. **What** does it return? (Output format, structure, error conditions)

```python
def get_customer(customer_id: str, format: str = "concise"):
    """
    Retrieve customer information by ID.
    Use when: User asks about customer details, need customer context.
    Args:
        customer_id: Format "CUST-######" (e.g., "CUST-000001")
        format: "concise" for key fields, "detailed" for complete record
    Returns: Customer object with requested fields
    Errors:
        NOT_FOUND: Customer ID not found
        INVALID_FORMAT: ID must match CUST-###### pattern
    """
```

## Response Format Optimization

Offer concise vs. detailed response formats. Document when to use each so agents learn to select appropriately. Concise for confirmations; detailed when full context drives decisions.

## Error Message Design

Every error must be actionable for agents: state what went wrong and how to correct it.
- Retry guidance for retryable errors
- Corrected format examples for input errors
- Specific missing fields for incomplete requests

## MCP Tool Naming

Always use fully qualified names: `ServerName:tool_name`
```python
# Correct
"Use the BigQuery:bigquery_schema tool to retrieve table schemas."
# Incorrect -- may fail with multiple servers
"Use the bigquery_schema tool..."
```

## Tool Collection Limits

- Limit to 10-20 tools for most applications.
- Use namespacing for logical groupings when more needed.
- Implement umbrella tools that route to specialized sub-tools.

## Tool-Testing Agent Pattern

Feed observed tool failures to an agent to diagnose and improve descriptions. Achieves 40% reduction in task completion time.

## Key Gotchas

- Vague descriptions force agents to guess -- state exact database, query format, return shape.
- Cryptic parameter names (`x`, `val`, `param1`) are unusable.
- Inconsistent naming across tools (`id` vs `identifier` vs `customer_id`) creates confusion.
- MCP namespace collisions when multiple servers expose similar names.
- Tool description rot: treat descriptions as code, version and review them.
- Over-consolidation: >8-10 parameters or fundamentally different use cases = split the tool.
- Parameter explosion overwhelms agent decision-making. Provide defaults and group into presets.
