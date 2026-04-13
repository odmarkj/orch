# Django Best Practices

Django 5.x introduces first-class async support across views, middleware, and ORM operations,
while retaining the "batteries included" philosophy that makes it productive for everything
from prototypes to enterprise SaaS platforms. This reference covers the patterns that
separate maintainable Django applications from unmaintainable ones.

The guidance below assumes PostgreSQL as the database, Redis for caching and message brokering,
Celery for background tasks, and DRF for API development. It prioritizes Django's built-in
features before reaching for third-party packages, and emphasizes ORM optimization since
database queries are the most common source of Django performance problems.

---

## Async Views and Middleware

- **Use async views for I/O-bound endpoints** -- `async def my_view(request)` enables non-blocking DB and HTTP calls
- **Deploy with ASGI for async support** -- Uvicorn, Daphne, or Hypercorn instead of WSGI-only Gunicorn
- **Write async middleware for cross-cutting concerns** -- async `__call__` prevents blocking the event loop
- **Keep sync views for simple CRUD** -- async adds complexity; use it only where concurrency matters
- **Use async ORM operations where available** -- Django 5.x supports `await Model.objects.aget()` and friends
- **Avoid mixing sync and async in one view** -- use `sync_to_async` only when wrapping unavoidable sync libraries

## ORM Optimization

- **Use select_related for ForeignKey joins** -- `queryset.select_related("author", "category")` eliminates N+1 queries
- **Use prefetch_related for M2M and reverse FK** -- loads related sets in a separate query instead of per-row
- **Add db_index=True on frequently filtered fields** -- check `EXPLAIN ANALYZE` to verify index usage
- **Use .only() and .defer() for large models** -- fetch only the columns you need for a given view
- **Annotate and aggregate in the database** -- `queryset.annotate(total=Sum("amount"))` beats Python-side loops
- **Use .exists() instead of len(qs)** -- avoids loading entire querysets just to check for records
- **Avoid queryset evaluation in templates** -- evaluate in the view and pass concrete lists or dicts
- **Profile with django-silk or Debug Toolbar** -- see actual queries per request before optimizing blindly
- **Use .values() or .values_list() for reporting** -- skip model instantiation when you need raw data
- **Batch bulk operations** -- `bulk_create`, `bulk_update` with `batch_size` for large inserts
- **Use Subquery and OuterRef for correlated queries** -- avoids pulling large datasets into Python

## Custom Managers and QuerySets

- **Define custom managers for domain queries** -- `User.active.premium()` reads better than chained filters
- **Chain custom querysets** -- `UserQuerySet` with methods like `.active()`, `.premium()` composes naturally
- **Keep business logic out of managers** -- managers filter data; services apply business rules
- **Use manager methods for complex aggregations** -- encapsulate multi-join queries behind descriptive names
- **Set default manager carefully** -- `objects = ActiveManager()` can silently hide soft-deleted records
- **Use as_manager() on custom QuerySets** -- combines queryset methods with manager interface cleanly

## DRF API Development

- **Design serializers as DTOs** -- separate read and write serializers for different field sets
- **Use ModelSerializer for CRUD, plain Serializer for custom actions** -- avoid fighting the framework
- **Implement pagination on all list endpoints** -- `PageNumberPagination` or `CursorPagination` for large datasets
- **Use ViewSets with routers for RESTful resources** -- consistent URL patterns with minimal boilerplate
- **Override get_queryset() for scoped access** -- filter by `self.request.user` to enforce row-level security
- **Return proper HTTP status codes** -- 201 for creation, 204 for deletion, 400 for validation errors
- **Use throttle classes per endpoint** -- `UserRateThrottle` and `AnonRateThrottle` prevent abuse
- **Version APIs via URL prefix** -- `/api/v1/` is explicit and easy to route at the load balancer

## Django Channels and WebSockets

- **Use Channels for real-time features** -- notifications, chat, live dashboards over WebSocket
- **Implement channel layers with Redis** -- `channels_redis` as the backing store for pub/sub
- **Separate WebSocket consumers by concern** -- one consumer class per feature (chat, notifications)
- **Authenticate on WebSocket connect** -- validate tokens in the `connect` method, reject unauthorized
- **Use groups for broadcast** -- `channel_layer.group_send` pushes to all subscribers efficiently
- **Handle disconnect cleanup** -- remove users from groups in the `disconnect` method

## Celery Background Tasks

- **Use Celery for anything over a few seconds** -- report generation, email, payment processing
- **Set task_acks_late=True** -- acknowledge after completion so crashed tasks get redelivered
- **Configure soft and hard time limits** -- `task_soft_time_limit=300, task_time_limit=360`
- **Make tasks idempotent** -- use idempotency keys and check-before-write for safe re-execution
- **Use task chains for multi-step workflows** -- `chain(extract.s(), transform.s(), load.s())`
- **Monitor queue depth** -- alert when backlog grows; it signals worker capacity problems
- **Route tasks to named queues** -- separate fast (email) from slow (reports) workloads

## Caching with Redis

- **Cache expensive querysets** -- `cache.get_or_set("key", queryset_fn, timeout=300)`
- **Use per-view caching for read-heavy pages** -- `@cache_page(60 * 15)` for 15-minute TTL
- **Invalidate on write** -- `cache.delete("user:{id}")` in post_save signals or service methods
- **Use cache versioning** -- `cache.set("key", value, version=2)` for schema changes
- **Cache at multiple levels** -- database query cache, view cache, and template fragment cache
- **Use Redis for session storage** -- faster than database-backed sessions under load
- **Set bounded TTLs** -- never cache without an expiry; stale data causes subtle bugs

## PostgreSQL Features

- **Use JSONField for semi-structured data** -- avoid EAV tables; query with `field__key` lookups
- **Use ArrayField for simple lists** -- `tags = ArrayField(CharField())` with `__contains` lookups
- **Leverage full-text search** -- `SearchVector`, `SearchQuery`, `SearchRank` before adding Elasticsearch
- **Use database constraints** -- `UniqueConstraint`, `CheckConstraint` in Meta enforce data integrity
- **Use ExclusionConstraint for ranges** -- prevent overlapping bookings or date ranges at the DB level
- **Run data migrations for backfills** -- separate schema migrations from data migrations for safety

## Testing with pytest-django

- **Use pytest-django with fixtures** -- `@pytest.mark.django_db` and `db` fixture for database access
- **Create test data with factory_boy** -- `UserFactory.create_batch(5)` over manual Model.objects.create
- **Use DRF's APIClient for endpoint tests** -- `client.post("/api/users/", data)` with auth headers
- **Test permissions separately** -- verify 403 for unauthorized and 200 for authorized users
- **Use TransactionTestCase for signals** -- standard TestCase wraps in transactions, hiding signal issues
- **Mock external services only** -- use real database, real cache; mock Stripe, email providers
- **Assert on response schema** -- verify shape and types, not just status codes

## JWT and OAuth2 Authentication

- **Use djangorestframework-simplejwt for JWT** -- handles access/refresh token lifecycle out of the box
- **Store refresh tokens server-side** -- revocation requires a server-side record
- **Set short access token TTL** -- 5-15 minutes; use refresh tokens for longer sessions
- **Implement token blacklisting** -- `rest_framework_simplejwt.token_blacklist` for logout
- **Use OAuth2 for third-party login** -- django-allauth or python-social-auth for Google, GitHub, etc.
- **Never store tokens in localStorage** -- use httpOnly cookies to prevent XSS token theft
- **Add CORS headers carefully** -- whitelist specific origins, not `*`, in production

## Deployment with Gunicorn

- **Use Gunicorn with uvicorn workers for ASGI** -- `gunicorn myapp.asgi:application -k uvicorn.workers.UvicornWorker`
- **Set workers to 2*CPU+1** -- standard formula for sync workers; fewer for async
- **Serve static files with WhiteNoise** -- or CDN in production; never Django's dev server
- **Use django-storages for media files** -- S3, GCS, or Azure Blob for user-uploaded content
- **Configure health check endpoints** -- `/health/` that tests DB connectivity for load balancer probes
- **Use django-environ for settings** -- or pydantic-settings for typed config from environment variables
- **Enable security middleware** -- `SecurityMiddleware`, HSTS, SECURE_SSL_REDIRECT in production

## Database Migrations

- **Keep migrations small and focused** -- one logical change per migration file
- **Separate schema and data migrations** -- `RunPython` in its own migration for backfills
- **Test migrations against production-like data** -- large table ALTERs can lock the database
- **Use --check in CI** -- `python manage.py makemigrations --check` fails if models diverge from migrations
- **Squash old migrations periodically** -- `squashmigrations` reduces startup time and migration chains
- **Add indexes concurrently** -- use `AddIndexConcurrently` to avoid table locks on large tables
- **Never edit deployed migrations** -- create new migrations to fix issues; editing breaks existing databases

## HTMX Integration

- **Use HTMX for interactive UIs without SPA complexity** -- progressive enhancement over REST endpoints
- **Return HTML fragments from views** -- partial templates for `hx-swap` targets, not full pages
- **Use django-htmx for request detection** -- `request.htmx` tells you if the request came from HTMX
- **Implement infinite scroll with hx-trigger** -- `revealed` trigger loads next page automatically
- **Use hx-boost for navigation** -- turns regular links into AJAX requests for SPA-like feel
- **Keep server-side state** -- sessions and Django forms work naturally with HTMX, unlike SPAs

## Security Best Practices

- **Enable SecurityMiddleware and HSTS** -- force HTTPS in production with `SECURE_HSTS_SECONDS`
- **Use CSRF protection on all state-changing forms** -- Django includes it by default; never disable it
- **Parameterize all raw SQL** -- `cursor.execute("... WHERE id = %s", [user_id])` prevents injection
- **Configure CORS with django-cors-headers** -- whitelist specific origins, never use `CORS_ALLOW_ALL_ORIGINS`
- **Use django-guardian for object-level permissions** -- row-level access control beyond model-level perms
- **Audit sensitive actions** -- log authentication events, permission changes, and data exports
- **Run safety check in CI** -- `pip-audit` or `safety check` catches known vulnerabilities in dependencies
