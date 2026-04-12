# Reference Library Index

## How to use this library

When working on a feature or module, scan the categories below and read files
whose topics overlap with the work at hand. Match broadly by category -- if
building UI, read design-related files. If setting up infrastructure, read infra
and deployment files. These are guidance documents that inform your decisions,
not prescriptive instructions. They are a compass, not a manual.

Do not load all files at once. Read the descriptions below and pull in only the
files relevant to your current task's category.

---

## Context Engineering

Foundational patterns for designing AI agent systems, managing context windows,
and building reliable multi-step workflows.

- `context-engineering/fundamentals.md` -- Foundational concepts: what context is, attention mechanics, context components (system prompts, tool definitions, documents, history), progressive disclosure, and budgeting. Start here when designing new agent systems or debugging unexpected context-related behavior.

- `context-engineering/compression-strategies.md` -- Techniques for compressing context in long-running sessions: anchored iterative, opaque, and regenerative compression, artifact trail management, structured summaries, and probe-based evaluation. Consult when sessions exceed limits or agents "forget" earlier work.

- `context-engineering/degradation-patterns.md` -- Five patterns of context failure (lost-in-middle, poisoning, distraction, confusion, clash) with detection signals and mitigations. Consult when agent performance degrades unexpectedly during conversations.

- `context-engineering/multi-agent-patterns.md` -- When and how to distribute work across multiple model instances: supervisor, peer-to-peer, and hierarchical patterns, context isolation, consensus, and failure modes. Consult when single-agent limits constrain task complexity.

- `context-engineering/memory-structures.md` -- Persistence layer for agent continuity: working memory through temporal knowledge graphs, framework comparison (Mem0, Zep, Letta, LangMem, Cognee), retrieval strategies, and consolidation. Consult when building cross-session knowledge retention.

- `context-engineering/tool-development.md` -- Designing tools as contracts between deterministic systems and non-deterministic agents: consolidation, architectural reduction, description engineering, response formatting, error design, and MCP naming. Consult when creating or debugging agent tools.

- `context-engineering/filesystem-integration.md` -- Using the filesystem as context overflow: scratch pads, plan persistence, sub-agent communication via files, dynamic skill loading, and agent self-modification. Consult when tool outputs bloat the context window or state must persist across turns.

- `context-engineering/hosted-agents.md` -- Building background agents in remote sandboxes: image registry, snapshots, warm pools, framework selection, speed optimizations, self-spawning, API layer, multiplayer support. Consult when scaling agents beyond local execution.

- `context-engineering/optimization.md` -- Four strategies for extending effective capacity: KV-cache optimization, observation masking, compaction, and context partitioning. Can double or triple effective capacity without larger models. Consult when optimizing cost, latency, or capacity.

- `context-engineering/evaluation.md` -- Evaluating non-deterministic agent systems: multi-dimensional rubrics, LLM-as-judge at scale, end-state evaluation, test set design, context engineering evaluation, and continuous monitoring. Consult when building quality gates or measuring improvements.

- `context-engineering/llm-as-judge.md` -- Using LLMs to evaluate other LLM outputs: direct scoring, pairwise comparison, rubric generation, bias mitigation (position, verbosity, self-preference), and scaling strategies. Consult when building automated evaluation pipelines.

- `context-engineering/project-practices.md` -- Applying context engineering at the project level: structuring documentation for AI consumption, managing context budgets across features, team workflows, decision logging, and anti-patterns. Consult when setting up new projects or improving team AI workflows.

- `context-engineering/cognitive-architecture.md` -- BDI (Belief-Desire-Intention) model for agents: goal decomposition, reflective loops, memory architecture tiers, conflict resolution, and coherence across long tasks. Consult when designing complex planners or debugging goal-tracking failures.

---

## Agent Skills by Organization

Curated skills from major technology organizations. Each file lists the org's
official skills with their purpose and key guidance.

- `agent-skills/by-org/anthropic.md` -- Anthropic's 16 official skills: document creation (Word, PowerPoint, Excel, PDF), visual design (generative art, canvas, frontend), web artifacts, MCP server building, and testing. Reference implementations for the skill format.

- `agent-skills/by-org/vercel.md` -- Vercel's 7 skills covering React best practices, Next.js patterns, component composition, caching strategies, web design guidelines, React Native, and Next.js upgrades. Authoritative for Next.js and React Server Components.

- `agent-skills/by-org/google.md` -- Skills across Google Gemini (API development), Google Labs Stitch (design-to-code), and Workspace CLI (17 productivity tools). Covers standard/Vertex/Live/Interactions API modes.

- `agent-skills/by-org/stripe.md` -- 2 skills for integration best practices and SDK/API version upgrades. Essential for payment flows, subscriptions, and checkout.

- `agent-skills/by-org/cloudflare.md` -- 6 skills for Workers, Durable Objects, AI agent SDK, MCP servers, web performance, and Wrangler CLI. Covers edge computing patterns.

- `agent-skills/by-org/aws.md` -- Community AWS skill plus HashiCorp Terraform for multi-cloud. Also covers Azure SDK patterns (133 skills) for comparison.

- `agent-skills/by-org/supabase.md` -- PostgreSQL best practices for Supabase: schema design, queries, RLS policies, and database optimization. Plus pointers to Neon, ClickHouse, and DuckDB.

- `agent-skills/by-org/tailwind.md` -- No official Tailwind skill, but covers Tailwind usage across Vercel, Anthropic, Expo, and Google Labs skills. Best proxy sources for Tailwind patterns.

---

## Agent Skills by Domain

Skills grouped by functional area across all organizations. Use these when
working in a specific domain regardless of which vendor's tools you're using.

- `agent-skills/by-domain/frontend.md` -- React/Next.js, React Native, design-to-code (Figma, Stitch), animation (GSAP), web design, and mobile native. Strongest from Vercel, GSAP, Expo, and Figma.

- `agent-skills/by-domain/backend.md` -- Database best practices, API frameworks, serverless platforms, messaging/eventing, and CMS. Microsoft dominates with 100+ Azure SDK skills. Supabase and Neon for Postgres.

- `agent-skills/by-domain/deployment.md` -- Platform deploys (Vercel, Netlify, Cloudflare, Render), IaC (Terraform), CI/CD, and release engineering. Cloudflare wrangler as single entry point, Netlify's 12 granular skills.

- `agent-skills/by-domain/testing.md` -- Playwright web testing, property-based testing, Terraform tests, LLM evaluation, and TDD workflows. Anthropic's webapp-testing, Trail of Bits' property testing.

- `agent-skills/by-domain/security.md` -- Trail of Bits (21 security audit skills), OpenAI threat modeling, Better Auth (7 auth skills), Microsoft Entra ID. Community cybersecurity collection (753 skills).

- `agent-skills/by-domain/ai-ml.md` -- LLM APIs, ML training (Hugging Face 13 skills), agent frameworks, generative media (fal.ai 15 skills), vector search, and MCP servers. Covers full ML lifecycle.

---

## Patterns

Best practices and templates drawn from the Claude Code ecosystem.

- `patterns/claude-md-patterns.md` -- Effective patterns for writing CLAUDE.md files: structural templates, section types, scannable formatting, layered files, and concrete examples. Covers project context, build commands, coding standards, workflow rules, and memory protocols.

- `patterns/workflow-patterns.md` -- Claude Code workflow patterns: agent skills, hooks, slash commands, status lines, orchestration, and automation. Covers the major categories of Claude Code extensibility.
