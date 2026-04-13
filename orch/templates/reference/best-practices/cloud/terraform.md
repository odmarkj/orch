# Terraform Best Practices

Infrastructure as Code with Terraform requires disciplined module design, state management, and governance to scale from a single project to enterprise-wide adoption. This reference captures the decision frameworks and patterns that keep Terraform codebases maintainable, secure, and testable.

Covers: module architecture, state management, multi-environment strategies, variable design, testing with Terratest, CI/CD integration, policy as code, enterprise governance, and migration patterns for Terraform and OpenTofu.

---

## Module Design

- **Standard module structure** -- every module contains `main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, a README, an `examples/` directory, and a `tests/` directory
- **Single responsibility per module** -- a VPC module handles networking only; pass outputs to an RDS module via composition rather than bundling resources
- **Hierarchical module architecture** -- root modules call child modules; child modules call leaf modules; avoid nesting deeper than three levels
- **Semantic versioning for modules** -- pin consumer references to `~> 1.2` for patch updates; use exact pins (`= 1.2.3`) in production root modules
- **Document all variables** -- every variable needs a `description` field; add `validation` blocks for format constraints like CIDR notation
- **Use locals for computed values** -- `locals { full_name = "${var.prefix}-${var.name}" }` keeps expressions out of resource blocks
- **Output important attributes** -- expose IDs, ARNs, and endpoints that downstream modules need; add descriptions to every output
- **Conditional resources with count/for_each** -- `count = var.create_igw ? 1 : 0` makes optional resources explicit; prefer `for_each` for named resources

## State Management

- **Remote state is mandatory** -- S3+DynamoDB (AWS), Azure Storage (Azure), GCS (GCP) provide locking, encryption, and team access
- **Treat state files as critical infrastructure** -- encrypt at rest, restrict access with IAM policies, enable versioning for rollback
- **One state file per environment per component** -- `prod/networking`, `prod/compute`, `staging/networking` isolate blast radius
- **State locking prevents concurrent corruption** -- DynamoDB for S3 backend; built-in for Azure and GCS; never disable locking
- **Import existing resources** -- `terraform import` brings unmanaged resources under Terraform control without recreating them
- **State move for refactoring** -- `terraform state mv` renames resources without destroy/recreate when restructuring modules
- **Backup before state surgery** -- always pull a copy with `terraform state pull` before import, move, or remove operations

## Multi-Environment Strategies

- **Separate backends over workspaces** -- directory-per-environment (`envs/prod/`, `envs/staging/`) provides clearer isolation than Terraform workspaces
- **Shared modules, environment-specific variables** -- modules live in `modules/`; each environment directory provides its own `terraform.tfvars`
- **Workspace pattern for lightweight environments** -- acceptable for dev/test ephemeral environments where full isolation is unnecessary
- **Variable precedence matters** -- CLI flags override `*.auto.tfvars` which override `terraform.tfvars` which override defaults; document the expected source
- **Environment promotion** -- apply to staging first, validate, then apply the same module versions to production; never skip staging

## Variables and Configuration

- **Use validation blocks** -- `validation { condition = can(regex(...)) }` catches invalid input before plan, not during apply
- **Prefer data sources over hardcoded values** -- `data "aws_ami" "latest"` fetches current values; hardcoded AMI IDs rot
- **Sensitive variables** -- mark with `sensitive = true` to redact from plan output; store actual values in Vault or environment variables
- **Complex type constraints** -- `type = map(object({ cidr = string, az = list(string) }))` documents and validates nested variable shapes
- **Default values for optional features** -- `default = []` or `default = {}` let callers omit optional collections cleanly

## Provider Management

- **Pin provider versions** -- `required_providers { aws = { version = "~> 5.0" } }` in `versions.tf` prevents surprise upgrades
- **Provider aliases for multi-region** -- `provider "aws" { alias = "us_west" region = "us-west-2" }` enables cross-region resource management
- **Lock file in version control** -- commit `.terraform.lock.hcl` so all team members use identical provider builds

## Testing

- **Terratest for integration tests** -- Go tests that `InitAndApply`, verify outputs with assertions, then `Destroy`
- **Plan validation in CI** -- `terraform plan -detailed-exitcode` fails the pipeline on unexpected changes
- **Static analysis with tfsec/Checkov** -- scan for security misconfigurations (open security groups, unencrypted storage) before merge
- **Module contract tests** -- verify that module outputs match expected types and values for known inputs
- **Pre-commit hooks** -- `terraform fmt`, `terraform validate`, and linting run automatically before every commit

## CI/CD Integration

- **Plan on PR, apply on merge** -- pull request shows plan diff for review; merge to main triggers apply with approval gate
- **Approval workflows for production** -- require manual approval or designated reviewer sign-off before production apply
- **Automated security scanning** -- tfsec, Checkov, or Terrascan in the pipeline catch policy violations before they reach infrastructure
- **Separate plan and apply steps** -- save plan to a file (`-out=tfplan`), then apply that exact plan; prevents drift between steps
- **Rollback strategy** -- revert the Git commit and re-apply; Terraform reconciles to the previous desired state

## Policy as Code

- **OPA/Rego for custom policies** -- `deny` rules evaluate plan JSON for compliance violations (e.g., "no public S3 buckets")
- **Sentinel for Terraform Cloud** -- embedded policy language with advisory, soft-mandatory, and hard-mandatory enforcement levels
- **Tag enforcement** -- require `Environment`, `Owner`, and `ManagedBy` tags on all taggable resources via policy
- **Cost controls** -- policies that reject instance types above a certain size or storage allocations beyond a threshold
- **Naming conventions** -- enforce consistent resource naming patterns through validation blocks and CI checks

## Enterprise Governance

- **Module registry** -- publish approved modules to a private registry (Terraform Cloud, Artifactory, S3-hosted); teams consume rather than copy
- **Service catalogs** -- pre-approved infrastructure patterns that developers can self-service without deep Terraform knowledge
- **RBAC with Terraform Cloud** -- team-based access to workspaces; restrict who can plan, apply, and manage state
- **Compliance frameworks** -- map CIS benchmarks, SOC2, and PCI-DSS requirements to specific Terraform resource configurations
- **Audit trails** -- Terraform Cloud logs who applied what and when; state versioning provides point-in-time infrastructure snapshots
- **Cost allocation** -- consistent tagging enables chargeback and showback reporting across teams and projects

## Advanced Patterns

- **Dynamic blocks** -- `dynamic "ingress" { for_each = var.ingress_rules }` generates repeated nested blocks from variables
- **Precondition/postcondition checks** -- `lifecycle { precondition { condition = ... } }` validates assumptions before resource creation
- **Resource targeting** -- `terraform apply -target=module.vpc` applies a subset during debugging; never use in production workflows
- **Moved blocks for refactoring** -- `moved { from = aws_instance.old to = module.compute.aws_instance.new }` preserves state during restructuring
- **OpenTofu compatibility** -- OpenTofu is a drop-in replacement for Terraform; migration requires provider registry updates and license review

## Troubleshooting and Operations

- **State corruption recovery** -- restore from versioned backend; use `terraform state pull` backups as last resort
- **Failed apply resolution** -- fix the code, re-run plan to verify the fix, then apply; partial applies leave state in intermediate state
- **Provider update strategy** -- pin minor versions, test upgrades in non-production first, read changelogs for breaking changes
- **Parallelism tuning** -- `terraform apply -parallelism=20` speeds up large applies; reduce if hitting API rate limits
- **Debug logging** -- `TF_LOG=DEBUG terraform plan` reveals provider API calls and state operations for troubleshooting
- **Deprecation management** -- track provider deprecation warnings; plan upgrades before resources are removed in future versions

## Resource Tagging Strategy

- **Mandatory tags** -- Environment, Owner, Team, ManagedBy (terraform), Project at minimum on all taggable resources
- **Cost allocation tags** -- enable AWS Cost Explorer or equivalent cloud billing tags for chargeback reporting
- **Merge tag maps** -- `tags = merge({ Name = var.name }, var.tags)` combines default and custom tags cleanly
- **Tag enforcement in CI** -- fail the pipeline if resources lack required tags; cheaper to catch early than to retroactively tag

## Anti-Patterns to Avoid

- **Monolithic root modules** -- one root module managing all infrastructure creates blast radius and slow plans; decompose by domain
- **Hardcoded values** -- magic strings and numbers in resource blocks; use variables with validation or data sources instead
- **Skipping plan review** -- auto-applying without human review in production; always review the plan diff
- **State file in Git** -- state contains secrets and changes constantly; use remote backends exclusively
- **Ignoring drift** -- manual console changes accumulate; schedule `terraform plan` runs to detect and remediate drift
- **Nested provider configurations** -- providers should only be configured in root modules; child modules should receive providers via `providers` argument
- **Using terraform destroy in CI** -- `destroy` is irreversible; require explicit manual confirmation for production teardowns
- **Wildcard provider versions** -- unpinned providers pull latest on every init; causes inconsistent behavior across team members
- **Ignoring count vs for_each tradeoffs** -- `count` uses numeric indices (fragile on reorder); `for_each` uses stable keys (prefer for named resources)
- **Copying modules instead of versioning** -- duplicate modules diverge over time; publish to a registry and consume via version constraints
