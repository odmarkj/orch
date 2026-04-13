# API Design Best Practices

Guidance for designing REST and GraphQL APIs that are intuitive, scalable, and maintainable -- from resource modeling and HTTP semantics through cursor-based pagination, error handling, and the DataLoader pattern. A well-designed API is the most important interface in your system because it outlives any single implementation.

API design is a contract with your consumers. Breaking changes erode trust and create migration burden. Version from day one, document comprehensively, and design for the 80% use case while providing escape hatches for the 20%. The goal is an API that developers can use correctly without reading the source code.

---

## REST Resource Design

- **Resources are nouns, not verbs** -- use `/users`, `/orders`, `/products`; never `/createUser` or `/getOrderById`
- **Plural nouns for collections** -- `GET /api/users` returns a list; `GET /api/users/{id}` returns one; consistency reduces cognitive load
- **Shallow nesting preferred** -- `GET /api/users/{id}/orders` is fine; `GET /api/users/{id}/orders/{oid}/items/{iid}/reviews` is too deep; flatten to `/api/order-items/{id}/reviews`
- **Consistent URL structure** -- establish a pattern (e.g., `/api/v1/{resource}`) and follow it everywhere; inconsistency confuses consumers
- **Resource relationships via sub-resources** -- `POST /api/users/{id}/orders` creates an order for a user; the URL communicates the relationship

## HTTP Semantics

- **GET is safe and idempotent** -- never use GET to create, modify, or delete data; GET requests must be repeatable without side effects
- **POST creates resources** -- return 201 Created with a Location header pointing to the new resource; include the created resource in the response body
- **PUT replaces the entire resource** -- the request body must contain all fields; missing fields are set to defaults, not left unchanged
- **PATCH for partial updates** -- send only the fields to update; return 200 OK with the updated resource
- **DELETE returns 204 No Content** -- the response body is empty on success; return 404 if the resource does not exist, 409 if deletion would violate constraints
- **Idempotency keys for POST** -- accept an `Idempotency-Key` header; if a duplicate request arrives, return the cached response instead of creating a duplicate

## Status Codes

- **200 OK** -- successful GET, PATCH, PUT; include the resource in the response body
- **201 Created** -- successful POST; include Location header and created resource
- **204 No Content** -- successful DELETE; empty body
- **400 Bad Request** -- malformed request syntax (unparseable JSON, missing required headers)
- **401 Unauthorized** -- authentication required or invalid credentials
- **403 Forbidden** -- authenticated but insufficient permissions for this resource
- **404 Not Found** -- resource does not exist at the requested URL
- **409 Conflict** -- state conflict (duplicate email, version mismatch, resource in use)
- **422 Unprocessable Entity** -- request is well-formed but fails business validation (invalid email format, price below zero)
- **429 Too Many Requests** -- rate limit exceeded; include `Retry-After` header
- **500 Internal Server Error** -- unexpected server failure; log the details, return a generic message to the client

## Error Handling

- **Consistent error response structure** -- every error returns the same JSON shape: `error` (code), `message` (human-readable), `details` (field-level errors), `path`, `timestamp`
- **Validation errors list affected fields** -- return an array of `{field, message, value}` objects so the client can display errors next to the correct form field
- **Machine-readable error codes** -- use string codes like `VALIDATION_ERROR`, `NOT_FOUND`, `RATE_LIMITED` alongside human messages for programmatic handling
- **Never expose internal details** -- stack traces, SQL queries, and internal service names must not appear in error responses; log them server-side

## Pagination

- **Always paginate collections** -- unbounded list endpoints are a denial-of-service vector and a performance problem; default to 20 items per page, max 100
- **Cursor-based pagination for large datasets** -- encode an opaque cursor (base64 of last-seen ID); supports stable iteration even when new items are inserted
- **Offset-based pagination for simple cases** -- `page=2&page_size=20` is simpler to implement; acceptable when the dataset is small and rarely changing
- **Include pagination metadata** -- return `total`, `page`, `page_size`, `pages` (offset) or `next_cursor`, `has_more` (cursor) so clients know when to stop
- **Link headers for discoverability** -- include `rel="next"`, `rel="prev"`, `rel="first"`, `rel="last"` in the Link header for RESTful navigation

## API Versioning

- **URL versioning is the simplest** -- `/api/v1/users` is explicit, easy to route, and easy to test; the downside is multiple URLs for the same resource
- **Header versioning keeps URLs clean** -- `Accept: application/vnd.api+json; version=2`; harder to test but more RESTful
- **Plan for breaking changes from day one** -- even if you only have v1, the versioning infrastructure should be in place
- **Deprecation strategy** -- announce deprecation with a timeline (minimum 6 months); return `Sunset` and `Deprecation` headers; monitor usage before removal
- **Additive changes are not breaking** -- adding a new optional field to a response is backward-compatible; removing or renaming a field is breaking

## Filtering, Sorting, and Search

- **Query parameters for filtering** -- `GET /api/users?status=active&role=admin`; use consistent parameter naming across all endpoints
- **Sort with direction prefix** -- `?sort=-created_at` for descending, `?sort=name` for ascending; support multiple sort fields with comma separation
- **Search with a query parameter** -- `?search=john` or `?q=john` for full-text search across relevant fields
- **Sparse fieldsets** -- `?fields=id,name,email` lets clients request only the fields they need; reduces payload size

## HATEOAS

- **Hypermedia links make APIs discoverable** -- include `_links` in responses with `self`, `next`, `update`, `delete` URLs so clients do not hardcode paths
- **Reduces client coupling** -- clients follow links instead of constructing URLs; server-side URL changes do not break clients
- **Pragmatic adoption** -- full HATEOAS is rare in practice; at minimum, include `self` links and pagination links

## Rate Limiting

- **Token bucket or sliding window** -- limit requests per client (API key or IP); return headers: `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
- **Different limits by endpoint** -- authentication endpoints need stricter limits than read endpoints; write endpoints need stricter limits than reads
- **429 with Retry-After** -- when a client exceeds the limit, return 429 Too Many Requests with a `Retry-After` header indicating when to retry
- **Distributed rate limiting** -- use Redis or an API gateway for consistent rate limiting across multiple service instances

## GraphQL Schema Design

- **Schema-first development** -- design the schema before writing resolvers; the schema is the API contract; treat it like a public interface
- **Relay cursor pagination** -- use `Connection`, `Edge`, and `PageInfo` types for paginated fields; `first`/`after` for forward, `last`/`before` for backward
- **Input/Payload pattern for mutations** -- accept a single `input` argument; return a payload with the result and an `errors` array for structured error handling
- **Custom scalars for domain types** -- define `DateTime`, `Email`, `Money`, `URL` scalars; they self-document and enforce validation at the schema level
- **Deprecation via directives** -- use `@deprecated(reason: "Use firstName/lastName")` instead of removing fields; consumers migrate on their own timeline
- **Union types for polymorphic results** -- `union SearchResult = User | Post | Comment` lets clients handle each type with inline fragments
- **Interfaces for shared contracts** -- `interface Node { id: ID! }` ensures consistent identification across types; enables generic queries like `node(id: ID!): Node`
- **Thin resolvers** -- resolvers delegate to a service layer; business logic never lives inside a resolver function

## DataLoader Pattern

- **Solve N+1 queries** -- when resolving a list of users with their orders, a naive resolver issues one query per user; DataLoader batches all IDs into a single query
- **Batch and cache per request** -- DataLoader collects all `.load(id)` calls in the current tick, executes one batch query, and caches results for the request lifetime
- **Create loaders per request** -- instantiate new DataLoader instances in the request context to prevent cross-request cache leaks
- **Apply to every relationship field** -- any resolver that fetches data based on a parent ID should use a DataLoader, not a direct query

## GraphQL Security

- **Query depth limiting** -- reject queries deeper than a threshold (e.g., 10 levels) to prevent deeply nested resource exhaustion attacks
- **Query complexity analysis** -- assign cost to each field (list fields multiply by page size); reject queries exceeding a total complexity budget
- **Persisted queries** -- in production, accept only pre-registered query hashes instead of arbitrary query strings; prevents query injection
- **Rate limiting by complexity** -- a single complex query can be more expensive than ten simple ones; rate limit based on total cost, not just request count

## REST vs GraphQL Decision

- **REST when** -- simple CRUD, public APIs, HTTP caching is critical, team is experienced with REST conventions
- **GraphQL when** -- multiple clients need different data shapes, deeply nested relationships, mobile bandwidth optimization, rapid frontend iteration
- **Both is valid** -- many teams use REST for simple external APIs and GraphQL for complex internal ones

## API Documentation

- **OpenAPI/Swagger for REST** -- auto-generate interactive documentation; include request/response examples for every endpoint
- **GraphQL introspection** -- built-in schema discovery; use GraphQL Playground or GraphiQL for interactive exploration
- **Code examples in documentation** -- show curl, Python, and JavaScript examples for common operations; developers copy-paste before reading prose
- **Changelog for breaking changes** -- maintain a public changelog documenting every API change with migration instructions

## Anti-Patterns

- **Verb-based endpoints** -- `/api/createUser` violates REST principles; use HTTP methods on resource nouns instead
- **Exposing database schema** -- API structure should reflect the domain model, not database tables; decouple with DTOs
- **Inconsistent error formats** -- returning different error shapes from different endpoints forces clients to handle multiple formats
- **Missing pagination** -- an endpoint that returns all records invites performance problems and potential denial-of-service
- **Ignoring HTTP semantics** -- using POST for idempotent operations or GET for mutations breaks caching and client expectations
- **Tight coupling to implementation** -- changing internal service structure should not require API consumers to update their code
- **No versioning strategy** -- adding versioning after the API is public requires migrating all existing consumers simultaneously
