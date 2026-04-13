# Kubernetes Best Practices

Kubernetes orchestration at scale requires consistent manifest standards, GitOps workflows, defense-in-depth security, and service mesh configuration. This reference distills operational wisdom for building production-grade platforms across managed and self-hosted clusters.

Covers: manifest best practices, Helm charts, GitOps with ArgoCD and Flux, Pod Security Standards, network policies, RBAC, service mesh (Istio and Linkerd), mTLS, traffic management, multi-tenancy, cost optimization, and observability.

---

## Manifest Best Practices

- **Always set resource requests and limits** -- requests guarantee scheduling; limits prevent noisy-neighbor resource starvation
- **Implement liveness and readiness probes** -- liveness restarts stuck containers; readiness removes unhealthy pods from service endpoints
- **Use specific image tags, never :latest** -- pinned tags ensure reproducible deployments; digest pins (`@sha256:...`) are even safer
- **Apply standard labels** -- `app.kubernetes.io/name`, `version`, `component`, `part-of`, `managed-by` enable filtering and tooling integration
- **Run as non-root with read-only filesystem** -- set `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, and drop ALL capabilities
- **Separate config from code** -- ConfigMaps for non-sensitive data, Secrets (encrypted) for credentials; mount or inject via envFrom
- **Use Kustomize overlays for environments** -- `base/` contains shared manifests; `overlays/dev/` and `overlays/prod/` patch environment-specific values
- **Validate before applying** -- `kubectl apply --dry-run=server`, `kubeval`, and `kube-score` catch errors before they hit the cluster

## Helm Charts

- **Standard chart structure** -- Chart.yaml, values.yaml, templates/, charts/, and tests/ in every chart
- **Document all values with comments** -- values.yaml is the chart's user interface; every field needs explanation
- **Use _helpers.tpl for shared logic** -- fullname, labels, and selector templates avoid duplication across manifests
- **Semantic versioning** -- chart version tracks chart changes; appVersion tracks the application; increment independently
- **Pin dependency versions explicitly** -- `dependencies` in Chart.yaml with `condition` flags for optional sub-charts
- **Environment-specific values files** -- `values-dev.yaml`, `values-prod.yaml` override defaults; install with `-f values-prod.yaml`
- **Lint and dry-run before packaging** -- `helm lint` and `helm template --dry-run` catch templating errors early
- **Helm test hooks** -- `test-connection.yaml` with `helm.sh/hook: test` validates connectivity after installation

## GitOps (ArgoCD / Flux)

- **Four OpenGitOps principles** -- declarative, versioned and immutable, pulled automatically, continuously reconciled
- **App of Apps pattern** -- a root ArgoCD Application manages child Application manifests; single entry point for the entire cluster
- **Separate repos for app and infra** -- application manifests in one repo, cluster infrastructure (ingress, monitoring, cert-manager) in another
- **Auto-sync with self-heal** -- `syncPolicy.automated.selfHeal: true` reverts manual kubectl changes back to Git-declared state
- **Prune orphaned resources** -- `prune: true` deletes resources removed from Git; prevents configuration drift
- **Progressive delivery** -- Argo Rollouts or Flagger for canary (10% -> 50% -> 100%) and blue-green deployments with automated analysis
- **Secret management outside Git** -- External Secrets Operator syncs from Vault/AWS Secrets Manager; Sealed Secrets encrypts in-repo
- **Flux Kustomization for multi-cluster** -- one Git repo, different `path:` per cluster; Flux reconciles each cluster independently

## Pod Security Standards

- **Restricted profile for production** -- namespace label `pod-security.kubernetes.io/enforce: restricted` blocks privileged containers
- **Baseline for migration** -- use `warn` and `audit` modes to identify violations before enforcing
- **Security context on every pod** -- `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`
- **Never run as root in production** -- `runAsNonRoot: true` with explicit `runAsUser: 1000`; use init containers for privileged setup

## Network Policies

- **Default deny all** -- start with empty `podSelector: {}` denying all ingress and egress; explicitly allow required traffic
- **Allow DNS egress** -- every namespace needs egress to kube-system on UDP 53; without it, DNS resolution fails
- **Microsegmentation by label** -- `allow-frontend-to-backend` policies restrict traffic to declared communication paths
- **CNI must support NetworkPolicy** -- Calico, Cilium, and Weave support it; default kubenet does not enforce policies

## RBAC

- **Least privilege for all service accounts** -- create specific Roles with only required verbs on required resources
- **Namespace-scoped Roles over ClusterRoles** -- limit blast radius; use ClusterRoles only for cluster-wide resources
- **Audit effective permissions** -- `kubectl auth can-i list pods --as system:serviceaccount:ns:sa-name` verifies access before deployment
- **Avoid cluster-admin bindings** -- grant the minimum ClusterRole needed; audit and rotate service account tokens

## Service Mesh - Istio

- **VirtualService for routing** -- weight-based traffic splitting, header-based routing, and URI matching for canary deployments
- **DestinationRule for policies** -- circuit breakers (`outlierDetection`), connection pools, and load balancing per service
- **Start simple, add complexity incrementally** -- basic routing first; add circuit breakers and fault injection only when needed
- **Set timeouts and retries explicitly** -- `timeout: 10s` with `retries.attempts: 3` and `perTryTimeout: 3s`; always set backoff
- **Traffic mirroring for safe testing** -- mirror production traffic to a canary at `mirrorPercentage: 100` without affecting users
- **Fault injection for chaos engineering** -- inject delays and HTTP errors to test resilience: `fault.delay.fixedDelay: 5s`
- **Gateway for ingress TLS** -- terminate TLS at the Istio ingress gateway with `credentialName` referencing a TLS secret

## Service Mesh - Linkerd

- **Automatic mTLS by default** -- Linkerd enables mTLS on injection without configuration; verify with `linkerd viz edges`
- **ServiceProfiles for per-route metrics** -- define routes with method and path patterns; get success rate, latency, and throughput per endpoint
- **Retry budgets prevent storms** -- `retryRatio: 0.2` limits retries to 20% of total requests; prevents cascading failures
- **TrafficSplit for canary** -- SMI-spec weighted splits: `stable: 900m` (90%), `canary: 100m` (10%)
- **Server and ServerAuthorization** -- define which service accounts can reach which servers; zero-trust at the mesh level

## mTLS Configuration

- **Start with PERMISSIVE mode** -- allows both plaintext and mTLS during migration; switch to STRICT after verifying all services
- **Short-lived workload certificates** -- 24h or less; service mesh handles automatic rotation
- **Port-level exceptions** -- disable mTLS on metrics ports (`mode: DISABLE` for port 9090) while keeping it strict on application ports
- **Monitor certificate expiry** -- alert when certificates are within 7 days of expiration; automate rotation
- **SPIFFE/SPIRE for identity** -- workload identity based on trust domains rather than network location; cross-cluster trust

## Multi-Tenancy

- **Namespace per tenant** -- resource quotas, limit ranges, and network policies per namespace enforce isolation
- **Resource quotas prevent overcommit** -- set CPU, memory, and object count limits per namespace
- **Priority classes for workload tiers** -- critical services get `PriorityClass` that preempts lower-priority workloads during contention
- **Developer self-service portals** -- abstract Kubernetes complexity; teams request resources through templates, not raw manifests

## Cost Optimization

- **Right-size resource requests** -- use VPA recommendations to match requests to actual usage; over-provisioning wastes capacity
- **Spot/preemptible nodes for stateless workloads** -- 60-90% cost reduction; handle interruptions with pod disruption budgets
- **KubeCost or OpenCost for visibility** -- per-namespace and per-team cost allocation enables chargeback and identifies waste
- **Cluster Autoscaler for elastic capacity** -- scale node pools based on pending pod demand; scale down idle nodes automatically
- **KEDA for event-driven scaling** -- scale to zero for intermittent workloads; scale based on queue depth, HTTP requests, or custom metrics

## Observability

- **Golden signals** -- monitor latency (P50, P99), traffic (RPS), errors (5xx rate), and saturation (CPU/memory) for every service
- **Prometheus + Grafana** -- ServiceMonitor CRDs auto-discover targets; pre-built dashboards for mesh and workload metrics
- **Distributed tracing** -- Jaeger or OpenTelemetry for cross-service request tracing; propagate trace context headers
- **Structured logging** -- Fluent Bit ships JSON logs to Loki or Elasticsearch; correlate with trace IDs
- **Alerting on SLOs** -- define error budgets (99.9% success rate); alert when burn rate exceeds threshold
- **Sample tracing appropriately** -- 100% in development, 1-10% in production; storage costs scale linearly with sampling rate
- **Kiali for mesh visualization** -- real-time service dependency graphs with traffic flow, error rates, and latency overlays

## Disaster Recovery

- **Velero for cluster backup** -- backs up Kubernetes resources and persistent volumes; schedule daily backups with retention policies
- **Multi-region active-passive** -- secondary cluster with synced state ready for failover; DNS-based traffic switching
- **Chaos engineering with Litmus** -- inject pod failures, network partitions, and node drains to validate resilience
- **RTO/RPO planning** -- define recovery targets per workload; test failover procedures quarterly
- **etcd backup and restore** -- critical for self-managed clusters; managed services handle this automatically

## Cluster Lifecycle

- **Upgrade strategy** -- rolling upgrades one minor version at a time; test workload compatibility in staging before production
- **Node pool management** -- separate node pools for system, general, and GPU workloads; drain before decommissioning
- **Image scanning in CI** -- Trivy or Grype scan container images for CVEs before pushing to registry; block critical vulnerabilities
