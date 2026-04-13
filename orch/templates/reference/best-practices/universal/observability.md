# Observability Best Practices

Guidance for building production-ready monitoring, logging, and tracing systems -- from the four golden signals through SLO implementation, distributed tracing, and dashboard design. Observability is about asking arbitrary questions of your system without deploying new code.

The three pillars of observability -- metrics, logs, and traces -- work together. Metrics tell you something is wrong, traces show you where, and logs explain why. Correlation between these pillars (via trace IDs and correlation IDs) transforms isolated data points into actionable insights.

---

## Four Golden Signals

- **Latency** -- measure the time to service a request; distinguish between successful and failed request latency (failed requests may be fast)
- **Traffic** -- measure demand on your system: HTTP requests per second, transactions per second, or messages consumed per second
- **Errors** -- measure the rate of failed requests; include both explicit errors (5xx) and implicit errors (200 with wrong content, or SLA violations)
- **Saturation** -- measure how full your service is; track CPU, memory, disk I/O, and queue depth; alert before resources are exhausted

## RED and USE Methods

- **RED for services** -- Rate (requests/second), Errors (error rate), Duration (latency distribution); apply to every customer-facing service
- **USE for resources** -- Utilization (percent busy), Saturation (queue length), Errors (error count); apply to CPU, memory, disk, and network
- **Choose the right method** -- RED tells you about user experience; USE tells you about infrastructure health; use both together

## SLI/SLO Implementation

- **SLI measures user-perceived reliability** -- availability SLI = successful requests / total requests; latency SLI = requests below threshold / total requests
- **SLO sets an internal target** -- aim for achievable targets (99.9%, not 100%); consider user expectations, business needs, and cost of reliability
- **Error budget = 1 minus SLO target** -- at 99.9% SLO, you have 43.2 minutes of error budget per month; track consumption rate continuously
- **Error budget policy** -- at 100% remaining: normal velocity; at 50%: consider postponing risky changes; at 10%: freeze non-critical changes; at 0%: feature freeze
- **Multi-window burn rate alerts** -- combine short (5m) and long (1h, 6h) windows to detect both fast and slow budget consumption while reducing false positives
- **Start with user-facing services** -- measure what matters to users first; internal service SLOs derive from customer-facing SLOs
- **Review SLOs quarterly** -- adjust targets based on actual performance, user feedback, and business changes

## Distributed Tracing

- **Trace = end-to-end request journey** -- each trace contains spans representing individual operations across services
- **OpenTelemetry as the standard** -- vendor-neutral instrumentation for traces, metrics, and logs; supports auto-instrumentation for most frameworks
- **Context propagation is mandatory** -- inject trace context (W3C Trace Context headers) into every inter-service call; without propagation, traces break at service boundaries
- **Jaeger for open-source tracing** -- production deployment with Elasticsearch backend; generates service dependency graphs automatically
- **Tempo for Grafana-native tracing** -- object-storage backend (S3); integrates tightly with Grafana, Loki, and Prometheus for unified observability
- **Sampling strategies** -- probabilistic (1-10% in production), rate-limiting (max N traces/second), or adaptive based on error status
- **Add meaningful span attributes** -- include user_id, request_id, db.system, db.statement; these make traces searchable and actionable
- **Monitor tracing overhead** -- target less than 1% CPU impact; use batch span processors and appropriate sampling rates

## Prometheus Configuration

- **Consistent metric naming** -- use `prefix_name_unit` format (e.g., `http_request_duration_seconds`); document custom metrics
- **Recording rules for expensive queries** -- pre-compute frequently queried expressions like error rates and percentiles; reduces dashboard load time
- **Scrape interval 15-60 seconds** -- 15s for application metrics, 30-60s for infrastructure; shorter intervals increase storage cost
- **Kubernetes service discovery** -- use pod annotations (`prometheus.io/scrape: "true"`) for automatic target discovery
- **High availability** -- run multiple Prometheus instances scraping the same targets; use Thanos or Cortex for long-term storage and global view
- **Relabeling for cleanup** -- drop unnecessary labels, rename for consistency, and filter targets using relabel_configs
- **Validate configuration** -- run `promtool check config` and `promtool check rules` before deploying changes

## Grafana Dashboards

- **Information hierarchy** -- critical metrics (big numbers) at the top, key trends (time series) in the middle, detailed metrics (tables, heatmaps) at the bottom
- **RED dashboard per service** -- request rate, error rate, and latency percentiles (p50, p95, p99) on a single view
- **USE dashboard per resource** -- CPU utilization, memory usage, disk I/O, and network traffic for infrastructure monitoring
- **Template variables for flexibility** -- use query variables for namespace, service, and environment; allow drilling down without creating separate dashboards
- **Meaningful thresholds and colors** -- green/yellow/red thresholds aligned with SLO targets; consistent color scheme across all dashboards
- **Dashboard as code** -- version dashboards in Git; provision via Terraform or Ansible; treat dashboard changes like code changes
- **Panel descriptions** -- add context to every panel explaining what the metric means and what action to take when it is abnormal

## Structured Logging

- **JSON log format** -- structured logs are machine-parseable; include timestamp, level, service, trace_id, and message as top-level fields
- **Correlation IDs in every log** -- include the trace ID from the current span so logs can be correlated with traces and metrics
- **Log levels with discipline** -- ERROR for failures requiring attention, WARN for degraded behavior, INFO for significant events, DEBUG for development only
- **Centralized log aggregation** -- use Loki (Grafana-native), ELK Stack, or Splunk; configure retention based on compliance requirements and cost
- **Sanitize sensitive data** -- never log passwords, tokens, PII, or credit card numbers; use structured logging to control which fields are emitted

## Alerting

- **Alert on symptoms, not causes** -- alert when error rate exceeds SLO threshold, not when CPU is high (CPU might be high and users are fine)
- **Multi-window alerts reduce noise** -- require both short-window (5m) AND long-window (1h) conditions to fire; prevents alerting on brief spikes
- **Every alert needs a runbook** -- link to a document explaining what the alert means, how to investigate, and how to resolve it
- **Route alerts by severity** -- critical alerts page on-call immediately; warnings go to a Slack channel for next-business-day review
- **Alert fatigue prevention** -- review alert frequency monthly; silence or tune alerts that fire more than once per week without action
- **Test alert routing** -- periodically fire test alerts to verify that PagerDuty routing, Slack channels, and escalation policies work correctly

## Observability as Code

- **Version all configurations** -- Prometheus rules, Grafana dashboards, alerting policies, and scrape configs live in Git
- **CI/CD for monitoring changes** -- validate Prometheus rules with promtool in CI; deploy dashboard changes through the same pipeline as application code
- **Automated monitoring for new services** -- use templates and service discovery so new services get baseline monitoring without manual configuration
- **GitOps for alert management** -- changes to alert rules require code review; this prevents ad-hoc changes that drift from documented intent

## Cost Optimization

- **Sampling reduces storage cost** -- trace 1-10% of requests in production; sample 100% of errors and slow requests
- **Retention policies by tier** -- keep high-resolution data for 30 days, downsampled data for 1 year, long-term aggregates indefinitely
- **High-cardinality label management** -- avoid labels with unbounded values (user IDs, request IDs) on metrics; use traces for high-cardinality data
- **Evaluate open source vs commercial** -- Prometheus+Grafana+Loki is free and capable; DataDog and New Relic provide convenience at significant cost

## Incident Response Integration

- **Deployment annotations in dashboards** -- send deployment markers to Grafana so metric changes correlate with specific releases
- **Automated incident detection** -- SLO burn rate alerts trigger PagerDuty pages; include a link to the relevant dashboard and runbook
- **Blameless postmortems** -- use observability data (traces, metrics, logs) to reconstruct timelines; focus on systemic causes, not individual blame
- **Chaos engineering validation** -- inject faults (kill pods, add latency, drop messages) and verify that monitoring detects the problem within SLO thresholds
- **On-call handoff dashboards** -- create a single-page dashboard showing current SLO status, recent incidents, and active alerts for shift handoffs

## Capacity Planning

- **Trend analysis for growth** -- use Prometheus recording rules to track week-over-week growth in traffic, storage, and compute; extrapolate to plan scaling
- **Auto-scaling integration** -- tie Horizontal Pod Autoscaler (HPA) to custom Prometheus metrics (requests per pod, queue depth) rather than just CPU
- **Resource forecasting** -- use time-series forecasting on utilization metrics to predict when capacity additions are needed; plan months ahead, not days
- **Load test correlation** -- run load tests against staging while monitoring the same dashboards used in production; validate that alerts fire at expected thresholds

## Anti-Patterns

- **Monitoring after launch** -- implement monitoring before the first production deployment; not after the first incident
- **Vanity metrics** -- total request count since launch tells you nothing actionable; focus on rates, error percentages, and percentiles
- **Dashboards nobody watches** -- if a dashboard is not viewed weekly, it does not justify the maintenance cost; delete or consolidate
- **Alert without runbook** -- an alert that fires without guidance on what to do creates noise and erodes on-call trust
- **Logs as the only signal** -- logs are expensive to search and lack structure; use metrics for detection and traces for investigation
- **Too many dashboards** -- one dashboard per person creates maintenance burden and inconsistency; consolidate to service-level and infrastructure-level views
- **Ignoring trace sampling bias** -- if you only sample 1% of traces, rare errors may never appear; always sample 100% of errors and slow requests
