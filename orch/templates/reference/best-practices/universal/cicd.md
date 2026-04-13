# CI/CD Best Practices

Guidance for designing multi-stage deployment pipelines -- from build optimization and security scanning through deployment strategies, approval gates, and rollback automation. A well-designed pipeline balances speed with safety, enabling teams to deploy frequently with confidence.

The pipeline is the last line of defense before production. Every stage should have a clear purpose: build stages produce artifacts, test stages validate quality, gates enforce human or metric-based approval, and deployment stages use progressive delivery to limit blast radius. Treat pipeline configuration as production code -- version it, review it, test it.

---

## Pipeline Architecture

- **Fail fast** -- run quick checks (lint, unit tests, secret scanning) before slow ones (E2E, security scans, integration tests)
- **Parallel execution** -- run independent jobs concurrently; lint, unit tests, and SAST can all run in parallel after the build stage
- **Artifact promotion** -- build the Docker image once; push to a registry; promote the same image through staging and production
- **Caching** -- cache dependency layers and build artifacts between runs; copy dependency manifests before source code in Dockerfiles
- **Environment parity** -- keep staging infrastructure as close to production as possible; differences cause "works in staging" failures
- **Idempotent deploys** -- re-running a deployment with the same inputs must produce the same result; design for safe retries

## Pipeline Stages

- **Source** -- checkout code, resolve dependency graph, validate commit signatures
- **Build** -- compile, package, containerize, sign artifacts; use multi-stage Docker builds for smaller images
- **Test** -- unit tests, integration tests, SAST/SCA security scans; gate on coverage thresholds
- **Staging deploy** -- deploy to staging with smoke tests; validate basic functionality before proceeding
- **Integration tests** -- E2E tests, contract tests, performance baselines against the staging environment
- **Approval gate** -- manual reviewer approval or automated metric-based gate before production
- **Production deploy** -- canary, blue-green, or rolling strategy with progressive traffic shifting
- **Verification** -- deep health checks, synthetic monitoring, error rate validation post-deployment
- **Rollback** -- automated rollback triggered by health check or metric threshold failure

## Deployment Strategies

- **Rolling deployment** -- gradual pod replacement with zero downtime; best for most stateless services; `maxSurge: 2, maxUnavailable: 1`
- **Blue-green deployment** -- run two identical environments; switch traffic instantly by updating the service selector; rollback by switching back
- **Canary deployment** -- shift traffic progressively (10% > 25% > 50% > 100%) with metric validation at each step; requires Argo Rollouts or service mesh
- **Feature flags** -- deploy code without releasing functionality; toggle features per user segment; instant rollback without redeployment
- **Recreate** -- stop all old pods, then start new ones; acceptable only for dev/test or batch jobs where downtime is tolerable
- **Decision framework** -- use rolling for most services, blue-green for high-risk database migrations, canary for high-traffic metric-driven rollouts

## Approval Gates

- **Environment protection rules** -- configure required reviewers in GitHub/GitLab environment settings; the approval gate blocks deployment automatically
- **Automated metric gates** -- use Argo Rollouts AnalysisTemplates to block promotion when error rate exceeds threshold; set `inconclusiveLimit` to fail fast on missing metrics
- **Time-based gates** -- delay production deployment by 30 minutes after staging; gives time for manual verification
- **Multi-approver requirements** -- require sign-off from both engineering lead and QA for critical services
- **Change freeze enforcement** -- implement gate policies that block deployments during maintenance windows or holiday periods

## Health Checks

- **Shallow vs deep health endpoints** -- `/ping` returns 200 even when dependencies are broken; `/health/ready` verifies database, cache, and queue connectivity
- **Use deep readiness checks for pipeline gates** -- the deployment verification step should call the deep health endpoint, not the shallow one
- **Post-deployment verification script** -- poll the health endpoint with retries (12 attempts, 10 seconds apart); fail the pipeline if health never reports OK
- **Deployment annotations** -- send deployment markers to Grafana/Datadog so metric changes can be correlated with specific deployments

## Rollback Strategies

- **Automated rollback on failure** -- if health check or metric verification fails, run `kubectl rollout undo` automatically in the pipeline
- **Backward-compatible migrations** -- make database migrations additive (add nullable columns, add tables); never DROP COLUMN until old code is fully retired
- **Keep undo scripts versioned** -- pair every migration with a rollback script; test rollback in staging before applying to production
- **Canary abort** -- Argo Rollouts automatically rolls back the canary if the AnalysisTemplate reports failure; no manual intervention needed
- **Revision history** -- `kubectl rollout history` shows previous revisions with change-cause annotations; roll back to any specific revision

## GitHub Actions Patterns

- **Pin action versions** -- use `@v4` not `@latest`; prevents supply chain attacks from upstream action changes
- **Reusable workflows** -- extract common patterns (test, build, deploy) into `workflow_call` workflows for consistency across repositories
- **Matrix builds** -- test across multiple language versions and operating systems in a single workflow; fail fast on first failure
- **Cache dependencies** -- use `actions/cache` or built-in caching (e.g., `setup-node` with `cache: npm`) to avoid re-downloading on every run
- **Minimal permissions** -- set `permissions` at the job level; grant only `contents: read` and `packages: write` as needed
- **Environment secrets** -- use environment-scoped secrets so production credentials are only available in the production deployment job

## GitLab CI Patterns

- **Stage-based pipeline** -- define stages (build, test, deploy) with jobs in each; jobs in the same stage run in parallel by default
- **Protected variables** -- mark secrets as protected so they are only available on protected branches; mask them to hide from job logs
- **Delayed start** -- use `when: delayed` with `start_in: 30 minutes` for time-based approval gates
- **Include templates** -- use `include: template` to share CI configurations across projects in an organization

## Secrets in Pipelines

- **Never hardcode secrets** -- use secret stores (Vault, AWS Secrets Manager) or platform-native secrets (GitHub encrypted secrets, GitLab variables)
- **HashiCorp Vault integration** -- use the `vault-action` for GitHub Actions or `vault kv get` in GitLab CI to fetch secrets at runtime
- **Mask in logs** -- use `echo "::add-mask::$SECRET"` in GitHub Actions to prevent secrets from appearing in build logs
- **Different secrets per environment** -- staging and production must use separate credentials; never share database passwords across environments
- **Secret scanning in CI** -- run TruffleHog or GitGuardian on every PR to catch accidentally committed secrets before merge
- **Rotate on compromise** -- have a documented runbook for rotating all secrets within hours; automate rotation where possible

## DORA Metrics

- **Deployment frequency** -- elite teams deploy multiple times per day; measure pipeline run count per day
- **Lead time for changes** -- time from commit to production deployment; elite target is under 1 hour
- **Change failure rate** -- percentage of deployments that cause a failure; elite target is under 5%
- **Mean time to recovery** -- time from incident detection to service restoration; elite target is under 1 hour
- **Track in dashboards** -- calculate these metrics from pipeline data and incident records; review monthly with the team

## Database Migration Safety

- **Additive migrations only** -- add nullable columns, add tables, add indexes; never DROP COLUMN or ALTER NOT NULL until old code is fully retired
- **Pair every migration with an undo script** -- version rollback scripts alongside forward migrations; test rollback in staging
- **Separate migration deploys from code deploys** -- apply the migration first, deploy the new code second; this ensures rollback does not leave schema/code mismatch
- **Zero-downtime index creation** -- use `CREATE INDEX CONCURRENTLY` (PostgreSQL) or online DDL to avoid locking tables during deployment

## Pipeline Security

- **Sign build artifacts** -- use cosign or Notary to sign Docker images; verify signatures before deployment
- **Least-privilege CI runners** -- CI jobs should not have admin access to production; scope credentials to the minimum required for each stage
- **Dependency pinning** -- pin GitHub Action versions to SHA hashes, not tags; prevents supply chain attacks from compromised actions
- **Audit pipeline changes** -- require code review for workflow file changes; `.github/workflows/` modifications should trigger the same review as production code

## Notification and Observability

- **Slack/Teams notifications on deploy** -- send success and failure messages to a deployment channel; include commit SHA, deployer, and environment
- **Deployment markers in monitoring** -- send annotations to Grafana or Datadog at deploy time; correlate metric changes with specific releases
- **Pipeline duration tracking** -- monitor total pipeline time; set alerts if build time regresses significantly; investigate cache misses or flaky steps

## Anti-Patterns

- **Manual deployments** -- any step that requires SSH-ing into a server is a rollback risk and an audit gap
- **Build artifacts not promoted** -- rebuilding in each environment introduces non-determinism; build once, deploy the same artifact everywhere
- **Missing rollback plan** -- if you cannot roll back in under 5 minutes, your deployment strategy is incomplete
- **Shallow health checks for gates** -- a `/ping` endpoint that returns 200 regardless of dependency health gives false confidence
- **Long-lived feature branches** -- branches that live for weeks accumulate merge conflicts and defer integration testing; merge daily
- **Skipping staging** -- deploying directly to production removes the safety net; at minimum, deploy to a canary before full rollout
- **Flaky tests blocking deploys** -- quarantine flaky tests immediately; a test suite that cries wolf trains teams to ignore failures
