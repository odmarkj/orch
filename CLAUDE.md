This is being launched based on a conversation with Claude on the web. That conversation can be found here if needed: https://claude.ai/chat/9507f4a6-e5ec-4f11-bf83-9d15bdb7db14

<!-- curated-context:start -->
## Memory Protocol
When you make a project decision (architecture, design tokens, conventions,
API patterns, tech stack), append a one-line summary to `.claude/decisions.log`:
`[category] key: value`
For cross-project preferences (coding style, tool prefs, workflow patterns), use:
`[global:category] key: value`
For technology preferences that should be remembered across projects, use:
`[global:preferences] pref-type-name: description`
Examples:
`[global:preferences] pref-lang-python: Prefers for backend and ML`
`[global:preferences] pref-framework-nextjs: Prefers for React frontends`
`[global:preferences] pref-deploy-cloudflare: Deploys via wrangler CLI`
`[global:preferences] pref-tool-vitest: Prefers over Jest`
`[global:preferences] pref-style-tailwind: Prefers for CSS`
For data sources, schemas, and database connections, use category `data`:
`[data] data-file-bars: data/chocolate_bars.jsonl — canonical JSONL, fields: name, origin, rating`
`[data] schema-prisma: Prisma schema — models: ChocolateBar, Company, Origin`
Only log deliberate decisions, not exploratory steps.

<!-- curated-context:end -->
