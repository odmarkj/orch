# Architecture Best Practices

Guidance for choosing and implementing backend architecture patterns -- from monolith-to-microservices decisions through DDD tactical patterns, CQRS, event sourcing, and saga orchestration. These principles help teams avoid premature decomposition while building systems that can evolve with changing requirements.

The patterns here are complementary, not mutually exclusive. Clean Architecture provides the structural foundation, DDD supplies the modeling vocabulary, CQRS optimizes read/write separation, event sourcing captures full audit trails, and sagas coordinate distributed transactions. Start simple and layer complexity only when the domain demands it.

---

## Architecture Selection

- **Monolith-first rule** -- start with a well-structured monolith; extract services only when you have evidence of independent scaling or deployment needs
- **Strangler fig pattern** -- gradually replace monolith functionality by routing new features through microservices while the legacy system remains operational
- **Service decomposition by bounded context** -- organize services around DDD subdomains, not technical layers; each service owns its data and domain language
- **Database-per-service** -- avoid shared databases between services; accept eventual consistency to preserve loose coupling
- **Shared kernel sparingly** -- when two contexts must share a small sub-model (e.g., a Money value object), govern it explicitly and version it carefully

## Clean Architecture

- **Dependency rule** -- all imports point inward; domain and use-case layers never import from adapters or infrastructure
- **Four rings** -- Entities (core models) > Use Cases (application rules) > Interface Adapters (controllers, gateways) > Frameworks & Drivers (outermost)
- **Port/adapter boundary** -- every layer crossing uses an abstract interface; swap implementations without touching business logic
- **Controller responsibility** -- parse request, call use case, map response; no business logic lives in controllers
- **Test without infrastructure** -- use in-memory adapters in tests; if a use case requires a running database, the dependency rule is violated

## Hexagonal Architecture

- **Driving vs driven ports** -- driving ports accept commands from the outside world (HTTP, CLI); driven ports reach outward (database, payment gateway)
- **Adapter swappability** -- replace PostgreSQL with DynamoDB or Stripe with a mock by changing only the adapter, never the domain core
- **Technology decisions at the edges** -- defer framework and database choices to the outermost layer; the core remains framework-free
- **Domain core purity** -- no framework imports, no ORM decorators on domain entities; map between ORM models and domain entities in the repository adapter

## Domain-Driven Design

- **Bounded contexts over universal models** -- isolate a coherent model per subdomain; avoid one model that serves the entire system
- **Context mapping** -- define relationships explicitly: Anti-Corruption Layer, Open Host Service, Shared Kernel, or Conformist
- **Ubiquitous language** -- every term in code matches the term domain experts use; rename code to match the business, not the reverse
- **Aggregates as consistency boundaries** -- only the root entity is accessible from outside; enforce all invariants within the aggregate
- **Value objects for validation** -- make invalid states unrepresentable: `Email("bad")` raises at construction, not deep in business logic
- **Domain events for decoupling** -- aggregates emit events; other aggregates react via subscriptions, never by importing each other
- **Aggregate sizing heuristic** -- if you load thousands of child objects to modify one, the aggregate is too large; split it and use references

## CQRS

- **Separate command and query models** -- write models enforce invariants; read models are denormalized for query performance
- **Command handlers validate before mutating** -- all validation happens in the handler; queries never trigger side effects
- **Independent schema evolution** -- read and write models change at different rates; do not couple their schemas
- **Eventual consistency SLA** -- define acceptable propagation delay; implement read-your-writes consistency when users expect immediate feedback
- **Projections are rebuildable** -- design read models so they can be rebuilt from the event stream at any time
- **Start simple** -- do not adopt CQRS unless you have genuinely different read/write performance requirements

## Event Sourcing

- **Events are immutable facts** -- never delete or modify stored events; append corrections as new compensating events
- **Version events from day one** -- plan for schema evolution with upcasters; backward/forward compatibility prevents data migration pain
- **Stream IDs include aggregate type** -- use `Order-{uuid}` format for easy filtering and category projections
- **Snapshotting for long-lived aggregates** -- periodically snapshot state to avoid replaying thousands of events on every load
- **Correlation and causation IDs** -- flow these through every event for end-to-end tracing and debugging
- **Idempotent event handlers** -- use event IDs for deduplication; handlers may receive the same event more than once
- **Optimistic concurrency control** -- check expected version on append; reject conflicting writes with a concurrency error

## Event Store Design

- **Append-only with global ordering** -- maintain both per-stream version and a global position for subscription cursors
- **Technology selection** -- EventStoreDB for pure event sourcing, PostgreSQL for existing stacks, Kafka for high-throughput streaming
- **Subscription checkpointing** -- store the last processed global position per subscriber; resume from checkpoint on restart
- **Transactional outbox pattern** -- write domain events to an outbox table in the same transaction as aggregate state; a relay process publishes to the broker

## Saga Orchestration

- **Orchestration vs choreography** -- use orchestration when you need explicit step tracking and centralized visibility; use choreography for loose coupling
- **Every step must be idempotent** -- commands may be replayed on broker reconnect; guard with idempotency keys
- **Design compensations as the most critical path** -- compensating actions must always succeed (treat "not found" as already compensated)
- **Per-step timeouts** -- never wait indefinitely; each step has different latency characteristics requiring independent deadlines
- **Compensate in strict reverse order** -- when two steps complete before a failure is detected, rolling back out of order leaves inconsistent state
- **Log every state transition** -- record `saga_id`, `step_name`, and `old_state -> new_state` on every change for debugging stuck sagas
- **Dead letter queue for failed compensations** -- retry with backoff, then alert for manual intervention; never silently drop compensation failures

## Resilience Patterns

- **Circuit breaker** -- fail fast on repeated downstream errors; transition through Closed > Open > Half-Open states before recovery
- **Retry with exponential backoff and jitter** -- prevent thundering herd; set retry budgets to cap total retry attempts across the fleet
- **Bulkhead isolation** -- isolate thread pools and connection pools per dependency so one slow service cannot exhaust shared resources
- **Graceful degradation** -- return cached or partial responses when dependencies are unavailable rather than failing the entire request
- **Timeout propagation** -- propagate deadlines across service boundaries; a downstream call should never outlive the upstream timeout
- **Health checks at three levels** -- liveness (process alive), readiness (can serve traffic), startup (initialization complete)

## Inter-Service Communication

- **Sync for queries, async for commands** -- use REST or gRPC when you need an immediate response; use message queues or event streaming for fire-and-forget commands
- **API gateway as single entry point** -- route client requests through an API gateway that handles authentication, rate limiting, and request routing
- **Backend-for-Frontend (BFF)** -- create client-specific backend services when mobile and web need different data shapes and aggregation logic
- **Event schema evolution** -- version event schemas for backward and forward compatibility; use schema registries for validation in streaming platforms
- **Dead letter queues** -- route unprocessable messages to a DLQ for inspection; never silently drop failed messages
- **Exactly-once semantics** -- achieve through idempotent consumers and deduplication; true exactly-once delivery is a myth in distributed systems

## Caching Strategies

- **Cache-aside (lazy loading)** -- check cache first, load from source on miss, populate cache; simplest pattern for read-heavy workloads
- **Write-through** -- update cache and source simultaneously; provides consistency but adds write latency
- **Cache invalidation on domain events** -- invalidate cache entries when the underlying data changes; event-driven invalidation is more reliable than TTL alone
- **HTTP caching** -- use ETags and Cache-Control headers for API responses; conditional requests reduce bandwidth and server load
- **Multi-layer caching** -- combine CDN (static assets), API gateway (response cache), application (in-memory), and database (query cache) layers

## Data Consistency

- **CAP theorem trade-offs** -- in a partition, choose consistency (CP) or availability (AP) based on business requirements; most web services prefer AP
- **Eventual consistency is the default** -- design UIs and workflows to tolerate propagation delay; use optimistic UI patterns for responsive user experiences
- **Strong consistency when required** -- financial transactions and inventory reservations may need synchronous consistency; accept the latency cost deliberately
- **Idempotent operations everywhere** -- design all write operations to be safely retried; use request IDs for deduplication

## Anti-Patterns

- **Distributed monolith** -- microservices that share a database or require synchronized deployments provide all the cost of distribution with none of the benefits
- **Shared database between services** -- creates implicit coupling; changes to one service's schema break others silently
- **Circular imports between layers** -- if use cases import adapters, introduce an abstract port; if two aggregates import each other, use domain events
- **Framework decorators on domain entities** -- SQLAlchemy columns or Pydantic fields on entities break domain purity; map in the repository adapter
- **Logic in controllers** -- when a controller grows beyond request parsing and response mapping, extract the logic into a use case
- **Global timeout for sagas** -- each step has different latency; a single timeout causes spurious compensations during peak load
- **Premature microservices** -- splitting a monolith before understanding bounded contexts creates fragmented systems that are harder to change than the original
