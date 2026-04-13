# FastAPI Best Practices

FastAPI 0.100+ with Annotated types, Pydantic V2, and SQLAlchemy 2.0 async represents the
modern stack for high-performance Python APIs. Its design philosophy -- type hints as the
source of truth for validation, serialization, and documentation -- eliminates entire
categories of bugs when used correctly.

This reference covers the patterns that matter in production: dependency injection for clean
architecture, async-first database access, resilience patterns for external service calls,
and observability that lets you debug at 3 AM without deploying new code. It assumes a
microservices context where each service owns its data and communicates via HTTP or message
queues.

---

## Annotated Types and Modern Patterns

- **Use Annotated for dependency injection** -- `user: Annotated[User, Depends(get_current_user)]` is the modern style
- **Prefer Annotated over default values** -- separates validation metadata from business defaults
- **Define reusable type aliases** -- `CurrentUser = Annotated[User, Depends(get_current_user)]` used across routes
- **Use Path, Query, Body with Annotated** -- `item_id: Annotated[int, Path(ge=1)]` for validated path params
- **Leverage lifespan events** -- `@asynccontextmanager async def lifespan(app)` for startup/shutdown resources
- **Use APIRouter for modular routing** -- one router per domain; mount in the main app with prefixes
- **Return proper status codes** -- `status_code=201` for creation, `204` for deletion

## Pydantic V2 Validation

- **Design models as API contracts** -- separate request and response schemas; never expose ORM models
- **Use Field for constraints** -- `Field(min_length=1, max_length=100, examples=["Alice"])` enriches docs
- **Use field_validator for custom rules** -- `@field_validator("email")` normalizes and validates per-field
- **Use model_validator for cross-field rules** -- `mode="after"` validates relationships between fields
- **Use computed_field for derived values** -- read-only properties that appear in serialized output
- **Configure model_config strict=True** -- prevents silent type coercion in critical paths
- **Use discriminated unions** -- `Annotated[Cat | Dog, Field(discriminator="type")]` for polymorphic payloads
- **Separate create/update/response schemas** -- `UserCreate`, `UserUpdate`, `UserResponse` with different fields

## SQLAlchemy 2.0+ Async

- **Use async sessions throughout** -- `async_sessionmaker` with `asyncpg` for non-blocking database access
- **Inject sessions via Depends** -- `session: Annotated[AsyncSession, Depends(get_session)]` per request
- **Use select() over query()** -- `await session.execute(select(User).where(...))` is the 2.0 style
- **Configure connection pooling** -- `pool_size=20, max_overflow=10` based on expected concurrency
- **Eagerly load relationships** -- `selectinload(User.orders)` in the query to prevent N+1 in async context
- **Use Alembic for migrations** -- async-aware with `run_async` in env.py for async engines
- **Implement repository pattern** -- abstract data access behind `UserRepository(session)` for testability
- **Manage transactions explicitly** -- `async with session.begin()` for multi-statement atomicity

## Dependency Injection

- **Use Depends for all external resources** -- database, cache, auth, config injected into handlers
- **Create dependency hierarchies** -- `get_user_service(repo=Depends(get_repo), cache=Depends(get_cache))`
- **Use yield dependencies for cleanup** -- `yield session; await session.close()` ensures resource release
- **Cache dependencies with Depends(fn, use_cache=True)** -- default behavior; same instance per request
- **Define Protocols for testability** -- `class Cache(Protocol): async def get(self, key: str) -> str | None`
- **Avoid global state** -- inject everything; never import a global db session into route handlers
- **Use dependency overrides in tests** -- `app.dependency_overrides[get_db] = lambda: test_db`

## Microservices Architecture

- **One service per bounded context** -- each service owns its database and API contract
- **Communicate via async HTTP or message queues** -- httpx for sync calls, RabbitMQ/Kafka for events
- **Use Pydantic Settings for config** -- `class Settings(BaseSettings)` loads from environment variables
- **Implement health check endpoints** -- `/health` for liveness, `/ready` for dependency checks
- **Version APIs explicitly** -- `/api/v1/` prefix or header-based versioning for breaking changes
- **Use correlation IDs across services** -- propagate `X-Correlation-ID` header in all outbound requests
- **Design for eventual consistency** -- services may have stale data; don't rely on synchronous reads

## Rate Limiting

- **Use slowapi for route-level limiting** -- `@limiter.limit("10/minute")` on sensitive endpoints
- **Implement per-user and per-IP limits** -- different thresholds for authenticated vs anonymous traffic
- **Return 429 with Retry-After header** -- tell clients exactly when they can retry
- **Use Redis as the rate limit backend** -- shared state across multiple application instances
- **Apply stricter limits to auth endpoints** -- login and password reset are brute-force targets
- **Use Semaphore for internal concurrency limits** -- `asyncio.Semaphore(10)` caps concurrent external calls

## Circuit Breaker

- **Implement circuit breaker for external services** -- prevent cascading failures when dependencies are down
- **Track failure rates over sliding windows** -- open circuit at 50% failure rate over 10 requests
- **Return cached or default responses when open** -- degrade gracefully instead of returning 500
- **Half-open state allows probe requests** -- test if the service recovered before fully closing
- **Use tenacity for retry with circuit breaker** -- combine `retry_if_exception_type` with failure counting
- **Log state transitions** -- circuit open/close events are critical operational signals

## OAuth2 and JWT Authentication

- **Use python-jose or pyjwt for token handling** -- FastAPI's OAuth2PasswordBearer provides the scaffold
- **Separate access and refresh tokens** -- short-lived access (15m), longer refresh (7d) with rotation
- **Validate tokens in a reusable dependency** -- `get_current_user: Annotated[User, Depends(oauth2_scheme)]`
- **Implement RBAC with permission dependencies** -- `require_role("admin")` as a composable Depends
- **Store refresh tokens server-side** -- enables revocation on logout or security events
- **Use httpOnly cookies over localStorage** -- prevents XSS-based token theft
- **Configure CORS strictly** -- whitelist specific origins; never `allow_origins=["*"]` in production

## Testing with pytest-asyncio

- **Use pytest-asyncio for all async tests** -- `@pytest.mark.asyncio async def test_endpoint():`
- **Use httpx.AsyncClient as test client** -- `async with AsyncClient(app=app) as client:` for integration tests
- **Override dependencies for isolation** -- `app.dependency_overrides[get_db] = get_test_db`
- **Use factory_boy for test data** -- deterministic, composable fixtures over random or manual creation
- **Test error responses explicitly** -- verify 400, 401, 403, 404, 422 with correct error schemas
- **Use respx to mock external HTTP calls** -- record and replay for deterministic external service tests
- **Test WebSocket endpoints** -- `async with client.websocket_connect("/ws") as ws:`

## Structured Logging

- **Use structlog with JSON output** -- machine-parseable logs for production, human-readable for dev
- **Log request lifecycle events** -- request received, processing started, response sent with duration
- **Include correlation_id in every log** -- bind via `structlog.contextvars` in middleware
- **Use semantic log levels consistently** -- INFO for operations, WARNING for retries, ERROR for failures
- **Track the four golden signals** -- latency, traffic, errors, and saturation via Prometheus metrics
- **Never log sensitive data** -- mask tokens, passwords, and PII before they reach the logger
- **Use OpenTelemetry for distributed tracing** -- spans across service boundaries for end-to-end visibility

## Docker and Kubernetes Deployment

- **Use multi-stage Docker builds** -- builder stage installs deps, final stage copies only the venv
- **Run as non-root user** -- `USER appuser` in Dockerfile for security
- **Use Gunicorn with UvicornWorker** -- `gunicorn app:app -k uvicorn.workers.UvicornWorker -w 4`
- **Configure liveness and readiness probes** -- `/health` for liveness, `/ready` checks DB and Redis
- **Use Horizontal Pod Autoscaler** -- scale on CPU, memory, or custom Prometheus metrics
- **Mount config via ConfigMaps and Secrets** -- never bake environment-specific values into images
- **Use resource limits** -- set CPU and memory requests/limits to prevent noisy-neighbor problems
- **Implement graceful shutdown** -- handle SIGTERM in lifespan to drain connections before exit

## WebSocket Support

- **Use FastAPI's WebSocket class** -- `@app.websocket("/ws")` with `await websocket.accept()`
- **Authenticate on connect** -- validate tokens before accepting the WebSocket connection
- **Implement heartbeat/ping** -- detect stale connections; close after missed pongs
- **Use broadcast patterns with Redis Pub/Sub** -- fan out messages to all connected clients
- **Handle disconnections gracefully** -- catch `WebSocketDisconnect` and clean up state
- **Separate WebSocket routes by feature** -- `/ws/chat`, `/ws/notifications` for distinct consumers

## Message Queues

- **Use RabbitMQ for reliable task routing** -- dead letter exchanges, priority queues, acknowledgments
- **Use Kafka for event streaming** -- high-throughput, ordered, replayable event logs
- **Use Redis Pub/Sub for simple fan-out** -- lightweight notifications that don't need persistence
- **Make consumers idempotent** -- messages may be delivered more than once; use deduplication keys
- **Set message TTLs** -- prevent stale messages from being processed after they are no longer relevant
- **Monitor queue depth and consumer lag** -- alerts on growing backlog signal capacity issues
- **Use Celery or Dramatiq for task queues** -- structured retry, result backends, workflow composition
- **Design for at-least-once delivery** -- duplicate messages are inevitable; make handlers idempotent

## Error Handling and Responses

- **Use custom exception handlers** -- `@app.exception_handler(DomainError)` maps domain errors to HTTP responses
- **Return structured error bodies** -- `{"detail": "...", "code": "VALIDATION_ERROR"}` for machine-parseable errors
- **Use HTTPException for expected failures** -- `raise HTTPException(status_code=404, detail="User not found")`
- **Chain exceptions with raise...from e** -- preserves the original traceback for debugging
- **Validate request bodies with Pydantic** -- 422 responses include field-level error details automatically
