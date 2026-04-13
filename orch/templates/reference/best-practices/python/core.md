# Python Core Best Practices

Modern Python 3.12+ development demands a disciplined approach to type safety, async
programming, error handling, and project organization. This reference distills actionable
guidance from the current ecosystem -- uv for package management, ruff for linting, Pydantic
for validation, and structlog for observability -- into decision frameworks you can apply
immediately.

The patterns below favor explicitness over cleverness, composition over inheritance, and
fail-fast validation over defensive coding. They assume a production context where code is
read far more often than written, and where runtime failures cost real money.

---

## Type Safety and Static Analysis

- **Annotate all public signatures** -- every function, method, and class attribute should carry type hints
- **Use modern union syntax** -- prefer `str | None` over `Optional[str]` (Python 3.10+)
- **Run strict type checking in CI** -- `mypy --strict` or `pyright` catches errors before runtime
- **Define Protocols for interfaces** -- structural typing via `Protocol` replaces ABC for duck-typed contracts
- **Use Generic classes** -- `Repository[T, ID]` preserves type info across reusable components
- **Narrow types with guards** -- `if x is None: raise ...` lets the checker refine downstream types
- **Bound TypeVars meaningfully** -- `ModelT = TypeVar("ModelT", bound=BaseModel)` restricts generics
- **Create type aliases for complex types** -- `Handler = Callable[[Request], Awaitable[Response]]`
- **Minimize Any usage** -- acceptable only for truly dynamic data or untyped third-party code
- **Use Callable Protocols for named params** -- `class OnProgress(Protocol): def __call__(self, current: int, total: int) -> None`
- **Enable incremental adoption** -- use per-module mypy overrides to tighten strictness gradually on legacy code

## Async Patterns and Concurrency

- **Stay fully sync or fully async** -- mixing creates hidden blocking; don't straddle a call path
- **Use asyncio for concurrent I/O** -- database, network, and file operations benefit from non-blocking
- **Offload CPU work with asyncio.to_thread()** -- keeps the event loop responsive for mixed workloads
- **Limit concurrency with Semaphore** -- `asyncio.Semaphore(10)` prevents overwhelming downstream services
- **Use asyncio.gather for parallel tasks** -- fan out independent I/O calls, collect results together
- **Handle CancelledError explicitly** -- clean up resources, then re-raise to propagate cancellation
- **Set timeouts on every await** -- `asyncio.wait_for(coro, timeout=30)` prevents indefinite hangs
- **Use async context managers** -- `async with` ensures cleanup for connections and pools
- **Prefer httpx over requests** -- async-native HTTP client avoids blocking the event loop
- **Use asyncio.Queue for producer-consumer** -- bounded queues provide natural backpressure
- **Batch concurrent operations** -- process items in chunks to avoid overwhelming resources
- **Use asyncio.Lock for shared mutable state** -- coroutines can interleave at any await point

## Error Handling

- **Validate inputs at boundaries** -- check all params before expensive operations begin
- **Use specific exception types** -- `ValueError`, `TypeError`, not bare `Exception`
- **Include actionable context in messages** -- explain what failed, why, and how to fix it
- **Chain exceptions with raise...from e** -- preserves the full debug trail for root-cause analysis
- **Convert to domain types early** -- parse strings into enums and typed objects at system edges
- **Use Pydantic for complex validation** -- structured errors with field-level detail for free
- **Handle partial failures in batches** -- track succeeded/failed per item, don't abort on first error
- **Never swallow exceptions silently** -- `except Exception: pass` hides bugs permanently

## Project Structure

- **Use src/ layout** -- `src/mypackage/` prevents accidental uninstalled imports
- **One concept per file** -- split at 300-500 lines or when a file serves multiple responsibilities
- **Define __all__ in every __init__.py** -- makes the public interface explicit and discoverable
- **Prefer flat hierarchies** -- add directory depth only for genuine sub-domains
- **Use absolute imports** -- `from myproject.services import UserService` over relative imports
- **Separate layers** -- API handlers, service logic, and data access in distinct modules
- **Match file names to class names** -- `UserService` lives in `user_service.py`

## Packaging and Tooling (uv, ruff)

- **Use uv for package management** -- 10-100x faster than pip, handles venvs and Python versions
- **Configure everything in pyproject.toml** -- single source for build, lint, type-check, and test config
- **Use ruff as the sole linter/formatter** -- replaces black, isort, and flake8 in one fast tool
- **Lock dependencies with uv lock** -- reproducible installs across environments
- **Use src/ layout with setuptools or hatchling** -- `[tool.setuptools.packages.find] where = ["src"]`
- **Pin Python version with .python-version** -- `uv python pin 3.12` for consistency
- **Run uv run instead of activating venvs** -- `uv run pytest` auto-activates the environment
- **Migrate from pip with uv add -r** -- `uv add -r requirements.txt` imports existing dependency files
- **Use dynamic versioning** -- setuptools-scm derives versions from git tags automatically

## Code Style

- **Set line length to 120** -- modern standard for readability on current displays
- **Use Google-style docstrings** -- Args, Returns, Raises sections for all public APIs
- **Follow PEP 8 naming strictly** -- `snake_case` functions, `PascalCase` classes, `SCREAMING_CASE` constants
- **Group imports by origin** -- stdlib, third-party, local with blank lines between
- **Automate in CI** -- `ruff check --fix . && ruff format .` on every commit via pre-commit hooks
- **Use trailing commas in multi-line signatures** -- prevents noisy diffs when adding parameters

## Design Patterns

- **Choose the simplest solution first** -- a dict lookup often beats a factory/registry pattern
- **Single responsibility** -- each class has one reason to change; separate HTTP from business logic
- **Compose, don't inherit** -- combine objects for flexibility; inject dependencies for testability
- **Rule of three** -- wait for three instances before abstracting; duplication beats the wrong abstraction
- **Keep functions under 50 lines** -- extract when a function serves multiple distinct purposes
- **Use Protocol-based DI** -- define `Logger(Protocol)` and inject via constructor for easy testing

## Resilience and Fault Tolerance

- **Retry only transient errors** -- network timeouts and 429/502/503/504; never retry auth or validation failures
- **Use exponential backoff with jitter** -- `wait_exponential_jitter(initial=1, max=30)` via tenacity
- **Cap total retry duration** -- `stop_after_attempt(5) | stop_after_delay(60)` prevents infinite loops
- **Log every retry attempt** -- silent retries hide systemic degradation
- **Stack infrastructure decorators** -- `@traced @with_timeout @retry` keeps business logic clean
- **Degrade gracefully for non-critical paths** -- return cached or default values when recommendations fail

## Resource Management

- **Always use context managers** -- `with` and `async with` guarantee cleanup even on exceptions
- **Use ExitStack for dynamic resources** -- manages variable numbers of files or connections cleanly
- **Return False from __exit__** -- propagate exceptions unless suppression is explicitly intended
- **Use @contextmanager for simple cases** -- fewer lines than a full class-based implementation
- **Accumulate strings with list+join** -- `"".join(chunks)` avoids O(n^2) concatenation

## Observability

- **Use structlog for structured logging** -- JSON output with consistent fields for production queries
- **Propagate correlation IDs** -- thread a unique ID through all logs and spans per request
- **Track the four golden signals** -- latency, traffic, errors, saturation via Prometheus counters/histograms
- **Keep metric label cardinality bounded** -- never use user IDs as labels; they explode storage costs
- **Use semantic log levels consistently** -- INFO for operations, WARNING for retries, ERROR for failures requiring action

## Performance

- **Profile before optimizing** -- use cProfile, py-spy, or memory_profiler to find real bottlenecks
- **Use dict/set for lookups** -- O(1) membership vs O(n) list scans
- **Prefer comprehensions over loops** -- list comprehensions are faster and more idiomatic
- **Use generators for large datasets** -- constant memory regardless of input size
- **Cache expensive computations** -- `@functools.lru_cache` for pure functions
- **Batch I/O operations** -- reduce system calls and round trips

## Configuration

- **Externalize all config** -- environment variables via pydantic-settings, never hardcoded values
- **Fail fast on missing config** -- crash at startup with clear messages, not mid-request with NoneType errors
- **Provide dev defaults for non-secrets** -- `db_host: str = "localhost"` but require `db_password: str`
- **Namespace env vars** -- `DB_HOST`, `REDIS_URL`, `AUTH_SECRET_KEY` for easy `env | grep` debugging
- **Support secrets from files** -- `secrets_dir="/run/secrets"` for container deployments

## Background Jobs

- **Return job IDs immediately** -- enqueue work, return a poll URL, process asynchronously
- **Make every task idempotent** -- use check-before-write and idempotency keys for external calls
- **Set soft and hard time limits** -- prevent runaway tasks from consuming worker resources
- **Implement dead letter queues** -- capture permanently failed tasks for manual inspection
- **Use exponential backoff for retries** -- `countdown=2 ** self.request.retries * 60`

## Anti-Patterns to Avoid

- **Scattered retry logic** -- centralize in decorators or client wrappers, not copy-pasted per call
- **Double retry** -- retrying at both app and client layer multiplies attempts exponentially
- **Exposed ORM models in APIs** -- use response schemas/DTOs to decouple internal from external types
- **Blocking calls in async code** -- `time.sleep()` and `requests.get()` stall the entire event loop
- **Bare except with pass** -- silently swallows bugs; catch specific exceptions and log them
- **Missing input validation** -- crashes deep in code with cryptic errors instead of clear 400 responses
- **Over-mocking in tests** -- mock only external services; use integration tests for critical paths
- **Untyped collections** -- `list` without `list[User]` defeats the purpose of type checking
