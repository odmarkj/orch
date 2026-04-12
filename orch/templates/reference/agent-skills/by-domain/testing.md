# Testing Agent Skills

Testing skills span from web app testing with Playwright, to property-based testing, Terraform acceptance tests, LLM evaluation pipelines, and test-driven development workflows. The most notable official skills come from Anthropic (webapp-testing), HashiCorp (provider-test-patterns, terraform-test), and Trail of Bits (property-based-testing). Community skills cover TDD workflows, Playwright patterns, iOS simulator testing, and LLM eval auditing.

Key patterns: Anthropic's webapp-testing uses Playwright for local web apps. Trail of Bits emphasizes property-based testing across multiple languages. HashiCorp provides two distinct testing approaches -- acceptance tests for providers and the built-in .tftest.hcl framework. The community Playwright skills range from basic automation to 70+ production-tested patterns.

---

## Official Testing Skills

### Web App Testing
- **anthropics/webapp-testing** -- Test local web apps using Playwright
  Source: https://officialskills.sh/anthropics/skills/webapp-testing
- **openai/playwright** -- Automate real browser interactions for navigation, forms, scraping
  Source: https://officialskills.sh/openai/skills/playwright
- **openai/playwright-interactive** -- Persistent browser interaction via js_repl for iterative UI debugging
  Source: https://officialskills.sh/openai/skills/playwright-interactive
- **openai/develop-web-game** -- Build and test web games iteratively using Playwright with time-stepping
  Source: https://officialskills.sh/openai/skills/develop-web-game

### Infrastructure Testing
- **hashicorp/terraform-test** -- Built-in testing framework with .tftest.hcl files
- **hashicorp/provider-test-patterns** -- Acceptance test patterns for Terraform providers
- **hashicorp/run-acceptance-tests** -- Run acceptance tests using Go's test runner
  Source: https://officialskills.sh/hashicorp/skills/

### Security Testing
- **trailofbits/property-based-testing** -- Property-based testing for multiple languages and smart contracts
- **trailofbits/testing-handbook-skills** -- Fuzzers, static analysis, sanitizers
  Source: https://officialskills.sh/trailofbits/skills/

### QA Workflows
- **garrytan/qa** -- QA Lead: test app, find bugs, fix with atomic commits, auto-generate regression tests
- **garrytan/qa-only** -- QA Reporter: same methodology, report only, no code changes
  Source: https://officialskills.sh/garrytan/skills/

## Community Testing Skills

### Playwright
- **testdino-hq/playwright-skill** -- 70+ production-tested patterns: E2E, POM, CI/CD, migrations
  Source: https://github.com/testdino-hq/playwright-skill
- **lackeyjb/playwright-skill** -- Browser automation with Playwright
  Source: https://github.com/lackeyjb/playwright-skill

### TDD & Development Workflows
- **obra/test-driven-development** -- Write tests before implementing code
  Source: https://github.com/obra/superpowers/blob/main/skills/test-driven-development/SKILL.md
- **obra/testing-anti-patterns** -- Identify ineffective testing practices
  Source: https://github.com/obra/superpowers/blob/main/skills/testing-anti-patterns/SKILL.md

### Mobile Testing
- **conorluddy/ios-simulator-skill** -- Control iOS Simulator
  Source: https://github.com/conorluddy/ios-simulator-skill
- **ramzesenok/iOS-Accessibility-Audit-Skill** -- Audit iOS apps against accessibility norms
  Source: https://github.com/ramzesenok/iOS-Accessibility-Audit-Skill
- **truongduy2611/app-store-preflight-skills** -- Catch common App Store rejection mistakes
  Source: https://github.com/truongduy2611/app-store-preflight-skills

### LLM Evaluation
- **hamelsmu/eval-audit** -- Audit LLM eval pipelines and surface problems
- **hamelsmu/error-analysis** -- Identify failure modes in LLM pipelines
- **hamelsmu/generate-synthetic-data** -- Create synthetic test inputs for LLM evals
- **hamelsmu/write-judge-prompt** -- Design LLM-as-Judge evaluators
- **hamelsmu/validate-evaluator** -- Calibrate LLM judges against human labels
- **hamelsmu/evaluate-rag** -- Evaluate RAG retrieval and generation quality
  Source: https://github.com/hamelsmu/prompts/tree/main/evals-skills/skills/

### Pairwise/Combinatorial
- **omkamal/pypict-skill** -- Pairwise test generation
  Source: https://github.com/omkamal/pypict-claude-skill/blob/main/SKILL.md
