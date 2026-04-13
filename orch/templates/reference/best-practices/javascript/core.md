# JavaScript Core Best Practices

Modern JavaScript development demands mastery of ES6+ syntax, asynchronous patterns, and Node.js runtime behavior. This reference distills production-tested guidance for writing clean, performant JavaScript across browser and server environments.

Covers: ES6+ features, async/await, promises, event loop mechanics, Node.js backend patterns with Express and Fastify, module systems, functional programming, error handling, and testing with Jest and Vitest.

---

## ES6+ Fundamentals

- **const by default** -- use `let` only when reassignment is genuinely required; never use `var`
- **Arrow functions for callbacks** -- lexical `this` binding eliminates a class of context bugs; use traditional functions only when you need a dynamic `this`
- **Destructuring parameters** -- `function greet({ name, age = 18 })` communicates intent and provides defaults in one step
- **Spread for immutability** -- `{ ...obj, key: newVal }` and `[...arr, item]` produce new references without mutating originals
- **Template literals over concatenation** -- tagged templates enable DSLs like `highlight\`Name: ${name}\`` for custom string processing
- **Computed property names** -- `{ [dynamicKey]: value }` eliminates intermediate variables when building objects
- **Optional chaining** -- `user?.address?.city` prevents "Cannot read property of undefined" without nested conditionals
- **Nullish coalescing** -- `value ?? 'default'` distinguishes null/undefined from falsy values like `0` or `""`
- **Logical assignment operators** -- `a ??= 'default'` and `obj.count ||= 1` reduce boilerplate for conditional assignment

## Async Patterns

- **Prefer async/await over .then chains** -- flattened control flow makes error handling and debugging straightforward
- **Parallel with Promise.all** -- `await Promise.all([fetchA(), fetchB()])` executes concurrently; sequential `await` serializes unnecessarily
- **Promise.allSettled for fault tolerance** -- returns results for all promises regardless of individual failures, with `status` discriminator
- **Promise.race for timeouts** -- race a fetch against `setTimeout` reject to enforce latency budgets
- **Promise.any for redundancy** -- first successful result wins; only rejects when all promises fail
- **Retry with exponential backoff** -- `await new Promise(r => setTimeout(r, 1000 * (i + 1)))` between attempts prevents thundering herd
- **Avoid mixing callbacks and promises** -- wrap legacy callback APIs with `new Promise()` at the boundary
- **Top-level await (ES2022)** -- available in ESM modules; eliminates async IIFE wrappers for script initialization

## Event Loop and Microtasks

- **Microtasks before macrotasks** -- promise callbacks (.then) execute before setTimeout/setInterval callbacks in the same tick
- **Avoid blocking the event loop** -- CPU-intensive work starves I/O; offload to worker threads or child processes
- **setImmediate vs process.nextTick** -- nextTick runs before I/O callbacks; setImmediate runs after; prefer setImmediate for yielding

## Node.js Backend

- **Express for flexibility, Fastify for performance** -- Fastify provides schema-based validation and structured logging out of the box
- **Layered architecture** -- controllers (HTTP), services (business logic), repositories (data access) enforce separation of concerns
- **Helmet + CORS + compression** -- apply security headers, restrict origins (never `*` in production), and compress responses as baseline middleware
- **Zod/Joi for input validation** -- validate request body/params/query at the middleware layer before reaching business logic
- **Custom error class hierarchy** -- extend a base `AppError(message, statusCode, isOperational)` for NotFound, Validation, Unauthorized errors
- **Global error handler middleware** -- catch all errors in one place; log unexpected errors, hide details in production
- **asyncHandler wrapper** -- `Promise.resolve(fn(req, res, next)).catch(next)` eliminates try/catch in every route
- **Structured logging with Pino** -- JSON logs with request duration, status, and correlation IDs; pretty-print only in development
- **Rate limiting with Redis store** -- `express-rate-limit` with `rate-limit-redis` for distributed rate limiting across instances
- **Graceful shutdown** -- listen for SIGTERM, stop accepting connections, drain in-flight requests, close database pools

## Module Patterns

- **ESM over CommonJS** -- `import/export` enables tree shaking and static analysis; set `"type": "module"` in package.json
- **Named exports for libraries** -- enable selective imports and better IDE autocomplete
- **Default exports for main entry** -- one primary export per module keeps the API surface clear
- **Dynamic import for code splitting** -- `const { handler } = await import('./module.js')` loads code on demand
- **Barrel files with caution** -- re-export modules from index.js but beware of bundler tree-shaking limitations

## Functional Programming

- **Array methods over loops** -- `map`, `filter`, `reduce`, `find`, `some`, `every`, `flatMap` express intent declaratively
- **Pure functions** -- same inputs always produce same outputs with no side effects; easier to test and memoize
- **Composition with pipe** -- `const pipe = (...fns) => x => fns.reduce((v, f) => f(v), x)` chains transformations left to right
- **Avoid mutating data** -- use `structuredClone()` for deep copies; spread operator for shallow immutable updates
- **Memoization for expensive computations** -- cache results by input; invalidate when data changes
- **Currying for partial application** -- `const add = a => b => a + b` enables reusable specialized functions

## Error Handling

- **Throw at boundaries, catch at edges** -- services throw domain errors; controllers/middleware translate to HTTP responses
- **Operational vs programmer errors** -- operational errors (network timeout) are expected; programmer errors (TypeError) indicate bugs
- **Never swallow errors silently** -- at minimum log them; unhandled rejections should crash the process in production
- **Use Error.cause for chaining** -- `throw new AppError('Failed', { cause: originalError })` preserves the error chain

## Testing with Jest/Vitest

- **Vitest for Vite projects, Jest for everything else** -- Vitest is faster with native ESM support; Jest has broader ecosystem
- **AAA pattern** -- Arrange test data, Act on the subject, Assert expected outcomes in every test
- **Mock external dependencies** -- `vi.mock('module')` or `jest.mock('module')` isolate units from I/O and third-party services
- **Dependency injection over module mocks** -- constructor-injected interfaces are easier to test and swap
- **Test factories with faker** -- `createUserFixture(overrides)` generates realistic data; override only fields under test
- **Aim for 80%+ coverage** -- focus on business logic and error paths; skip generated code and configuration
- **Prefer semantic queries in component tests** -- `getByRole`, `getByPlaceholderText` over `data-testid` attributes
- **beforeEach for fresh state** -- reinitialize services and clear mocks to prevent test pollution
- **Test error paths** -- verify that invalid input, network failures, and edge cases produce correct behavior

## Performance Optimization

- **Debounce user input handlers** -- delay execution until input settles; prevents redundant processing on every keystroke
- **Throttle scroll and resize handlers** -- limit execution to once per interval; prevents frame drops from excessive event firing
- **Lazy evaluation with generators** -- `function* range(n)` yields values on demand instead of allocating full arrays
- **WeakRef and FinalizationRegistry** -- hold references that don't prevent garbage collection; useful for caches of expensive objects
- **structuredClone for deep copies** -- native deep clone that handles circular references; replaces JSON.parse(JSON.stringify()) hacks
- **AbortController for cancellation** -- cancel fetch requests, event listeners, and async operations when they become irrelevant

## Modern JavaScript Features (ES2022+)

- **Private class fields (#field)** -- true encapsulation; inaccessible outside the class, unlike underscore convention
- **Static class fields and blocks** -- `static #instances = 0` and `static { }` for class-level initialization
- **Array.at() for negative indexing** -- `arr.at(-1)` returns the last element without `arr[arr.length - 1]`
- **Object.hasOwn() over hasOwnProperty** -- works on objects without Object prototype; safer and more concise
- **Error.cause for error chaining** -- `new Error('msg', { cause: originalError })` preserves the full error chain
- **RegExp match indices (d flag)** -- capture start and end positions of each match group
- **Symbols for private interfaces** -- `Symbol('key')` creates unique property keys invisible to iteration

## Security Considerations

- **Sanitize user input** -- never pass raw user input to eval(), innerHTML, or SQL queries
- **Use Content Security Policy headers** -- prevent XSS by restricting script sources in browser environments
- **Avoid eval and new Function** -- dynamic code execution is a security risk and prevents optimization
- **Freeze configuration objects** -- `Object.freeze(config)` prevents accidental or malicious modification
- **Validate on both client and server** -- client validation improves UX; server validation ensures security

## Anti-Patterns to Avoid

- **Callback hell** -- convert to async/await; if stuck with callbacks, use util.promisify
- **Prototype pollution** -- validate and sanitize user input before merging into objects
- **Memory leaks from closures** -- event listeners, timers, and closures holding large objects prevent garbage collection
- **Floating promises** -- unhandled async calls hide errors; always await or attach a catch handler
- **Over-using any in TypeScript migration** -- defeats the purpose; use unknown and narrow with type guards
- **Monkey-patching builtins** -- modifying Array.prototype or Object.prototype causes unpredictable behavior across libraries
- **Synchronous file I/O in Node.js** -- readFileSync blocks the event loop; use async versions for all production I/O
