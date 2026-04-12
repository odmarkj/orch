# Deployment, CI/CD & Infrastructure Agent Skills

Deployment skills cover platform-specific deploy workflows (Vercel, Netlify, Cloudflare, Render), infrastructure-as-code (Terraform/HashiCorp), CI/CD pipeline management, and release engineering. The strongest official collections come from Cloudflare (wrangler + Workers), Netlify (12 skills covering their full platform), HashiCorp (11 Terraform skills), and Expo (mobile deployment). OpenAI publishes deploy skills for Vercel, Netlify, Cloudflare, and Render.

Key patterns: Cloudflare's wrangler skill is the single entry point for all Cloudflare services. Netlify skills are granular -- separate skills for functions, edge functions, blobs, DB, CDN, forms, and caching. HashiCorp covers the full Terraform lifecycle from style guide to testing to multi-environment stacks. Garry Tan's gstack provides a complete release engineering workflow (ship, land-and-deploy, canary monitoring).

---

## Platform Deploy Skills

### Cloudflare
- **cloudflare/wrangler** -- Deploy Workers, KV, R2, D1, Vectorize, Queues, Workflows
- **cloudflare/durable-objects** -- Stateful coordination with RPC, SQLite, WebSockets
  Source: https://officialskills.sh/cloudflare/skills/

### Netlify
- **netlify/netlify-cli-and-deploy** -- CLI setup, local dev, deployment workflows
- **netlify/netlify-deploy** -- Automated deployment workflow
- **netlify/netlify-functions** -- Serverless API endpoints and background tasks
- **netlify/netlify-edge-functions** -- Low-latency edge middleware and geolocation
- **netlify/netlify-config** -- Reference for netlify.toml configuration
- **netlify/netlify-frameworks** -- Deploy web frameworks with SSR support
- **netlify/netlify-caching** -- CDN caching and cache purging
  Source: https://officialskills.sh/netlify/skills/

### Vercel
- **openai/vercel-deploy** -- Deploy apps to Vercel with preview or production options
  Source: https://officialskills.sh/openai/skills/vercel-deploy

### Render
- **openai/render-deploy** -- Deploy to Render's cloud platform using Git-backed services
  Source: https://officialskills.sh/openai/skills/render-deploy

### Multi-Platform (OpenAI)
- **openai/cloudflare-deploy** -- Deploy apps using Workers, Pages, and platform services
- **openai/netlify-deploy** -- Automate Netlify deployments with CLI auth and environment support
  Source: https://officialskills.sh/openai/skills/

## Infrastructure as Code

### HashiCorp Terraform (11 skills)
- **hashicorp/terraform-style-guide** -- HCL code following official style conventions
- **hashicorp/new-terraform-provider** -- Scaffold a new provider using Plugin Framework
- **hashicorp/provider-resources** -- Implement resources and data sources
- **hashicorp/provider-test-patterns** -- Acceptance test patterns
- **hashicorp/run-acceptance-tests** -- Run acceptance tests with Go
- **hashicorp/refactor-module** -- Transform monolithic configs into reusable modules
- **hashicorp/terraform-search-import** -- Discover and bulk import cloud resources
- **hashicorp/terraform-stacks** -- Multi-environment, multi-region, multi-account
- **hashicorp/terraform-test** -- Testing with .tftest.hcl files
  Source: https://officialskills.sh/hashicorp/skills/

### Community
- **antonbabenko/terraform-skill** -- Terraform best practices
  Source: https://github.com/antonbabenko/terraform-skill
- **zxkane/aws-skills** -- AWS infrastructure automation and cloud architecture
  Source: https://github.com/zxkane/aws-skills

## CI/CD & Release Engineering

### Expo Mobile Deploy
- **expo/expo-cicd-workflows** -- CI/CD workflows for Expo projects
- **expo/expo-deployment** -- Deploy Expo apps to production
- **expo/expo-dev-client** -- Build and distribute dev clients via TestFlight
  Source: https://officialskills.sh/expo/skills/

### Git & Release Workflows
- **garrytan/ship** -- Release Engineer: sync main, run tests, audit coverage, push, open PR
- **garrytan/land-and-deploy** -- Merge PR, wait for CI and deploy, verify production health
- **garrytan/canary** -- SRE post-deploy monitoring: console errors, perf regressions, page failures
- **garrytan/benchmark** -- Baseline page load times, Core Web Vitals, resource sizes
  Source: https://officialskills.sh/garrytan/skills/
- **openai/yeet** -- Stage, commit, push, open PR via CLI
  Source: https://officialskills.sh/openai/skills/yeet
- **openai/gh-fix-ci** -- Debug and fix failing GitHub Actions PR checks
  Source: https://officialskills.sh/openai/skills/gh-fix-ci
