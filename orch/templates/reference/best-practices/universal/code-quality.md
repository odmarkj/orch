# Code Quality Best Practices

Guidance for writing clean, maintainable code and managing technical debt -- from SOLID principles and refactoring patterns through legacy modernization, code review practices, and performance profiling. Quality is not about perfection; it is about making code easy to change safely.

Code quality compounds over time. Small improvements in naming, structure, and test coverage make future changes faster and less risky. Conversely, shortcuts that save minutes today cost hours in debugging and onboarding later. The practices here focus on pragmatic quality: measurable, automatable, and aligned with delivery goals.

---

## Clean Code Principles

- **Meaningful names** -- variables, functions, and classes should reveal intent; if a name requires a comment to explain, rename it
- **Small functions** -- each function should do exactly one thing; target 5-15 lines; if you need a comment to separate sections, extract a function
- **Single level of abstraction** -- a function should not mix high-level orchestration with low-level detail; extract helper functions to maintain a consistent level
- **DRY with judgment** -- eliminate true duplication (same concept, same reason to change); tolerate similar-looking code that serves different concerns
- **Minimize function arguments** -- prefer zero to three arguments; group related parameters into a data class or configuration object
- **Avoid boolean flags** -- `render(true)` is unclear; split into `renderForPrint()` and `renderForScreen()` or use an enum
- **Express intent through structure** -- use early returns, guard clauses, and descriptive variable names instead of nested conditionals
- **Composition over inheritance** -- prefer injecting behavior over deep class hierarchies; inheritance creates tight coupling

## SOLID Principles

- **Single Responsibility** -- a class should have one reason to change; if you describe a class with "and," it has too many responsibilities
- **Open/Closed** -- extend behavior through new classes or strategy patterns, not by modifying existing code; use interfaces and polymorphism
- **Liskov Substitution** -- subtypes must be substitutable for their base types without breaking correctness; if a subclass overrides behavior in surprising ways, the hierarchy is wrong
- **Interface Segregation** -- prefer small, focused interfaces over large ones; clients should not depend on methods they do not use
- **Dependency Inversion** -- high-level modules depend on abstractions (interfaces), not concrete implementations; inject dependencies through constructors

## Refactoring Patterns

- **Extract Method** -- when a function does multiple things, pull each logical section into its own named function; tests must stay green after each extraction
- **Extract Class** -- when a class has multiple responsibilities, move related fields and methods into a new class
- **Replace Conditional with Polymorphism** -- switch statements that branch on type are a signal to use an interface with concrete implementations
- **Introduce Value Object** -- replace primitive types (string email, int cents) with validated value objects that enforce invariants at construction
- **Inline unnecessary indirection** -- if a method just delegates to another with no added value, inline it; indirection without purpose is complexity
- **Rename for clarity** -- the cheapest refactoring with the highest impact; rename variables, functions, and classes until the code reads like prose
- **Replace Magic Numbers with constants** -- `if (retries > 3)` becomes `if (retries > MAX_RETRY_ATTEMPTS)`; the constant explains the business rule
- **Strangler fig for large-scale changes** -- build new alongside old, gradually redirect traffic, then remove old code; never rewrite from scratch
- **Branch by abstraction** -- introduce an interface, implement the new version behind it, switch over, then remove the old implementation
- **Mikado method** -- for tangled dependencies, draw a graph of what needs to change, work leaves first, and merge incrementally

## Code Smell Detection

- **Long methods (>20 lines)** -- extract smaller functions; each function should be readable without scrolling
- **Large classes (>200 lines)** -- split responsibilities into multiple classes; use composition over inheritance
- **Cyclomatic complexity >10** -- too many branches; simplify conditional logic, extract strategies, or use lookup tables
- **Duplicate code blocks >3 lines** -- extract into a shared function; but only if the duplication represents the same concept
- **Feature envy** -- a method that accesses another object's data more than its own should be moved to that object
- **Primitive obsession** -- using raw strings and ints where domain types (Email, Money, UserId) would prevent bugs
- **Long parameter lists** -- group related parameters into a parameter object or configuration dataclass
- **Dead code** -- unused functions, unreachable branches, and commented-out code should be deleted; version control preserves history
- **Switch statements on type** -- a signal to use polymorphism; the switch will need updating every time a new type is added

## Code Review Checklist

- **Correctness** -- does the code do what the PR description says? Are edge cases handled?
- **Security** -- input validation, authentication checks, SQL injection prevention, secrets not hardcoded
- **Performance** -- N+1 queries, unnecessary allocations, missing pagination, unbounded loops
- **Readability** -- clear naming, consistent style, appropriate comments, no unnecessary complexity
- **Testability** -- are there tests? Do they test behavior, not implementation? Is coverage adequate?
- **Error handling** -- are errors propagated correctly? Are failures logged? Are users shown appropriate messages?
- **Architectural consistency** -- does the change follow existing patterns? If it introduces a new pattern, is it justified?
- **Provide constructive feedback** -- suggest improvements with code examples; explain why, not just what; acknowledge good work
- **Review size matters** -- PRs over 400 lines get rubber-stamped; encourage small, focused pull requests

## Legacy Modernization

- **Strangler fig pattern** -- wrap the legacy system behind an API; route new functionality to modern code; gradually migrate old features
- **Add tests before refactoring** -- write characterization tests that capture current behavior; these are your safety net for changes
- **Golden master testing** -- capture full output of a legacy process and compare after changes; detects any behavioral drift
- **Feature flags for gradual rollout** -- deploy modern replacements behind flags; enable for a small percentage of traffic; monitor before full rollout
- **Maintain backward compatibility** -- provide adapter layers and compatibility shims; document breaking changes with clear migration paths
- **Incremental delivery** -- migrate one module at a time; each phase should be independently deployable and rollback-safe
- **Dependency injection for testability** -- introduce seams where you can inject test doubles without modifying legacy internals

## Performance Profiling

- **Profile before optimizing** -- measure to find actual bottlenecks; do not guess where the performance problem is
- **Flame graphs for CPU hotspots** -- visualize call stacks to identify which functions consume the most CPU time
- **Heap analysis for memory leaks** -- take heap snapshots before and after load; compare to find objects that are not being collected
- **Database query profiling** -- examine execution plans with EXPLAIN ANALYZE; add indexes for frequent queries; eliminate N+1 patterns with batch loading
- **N+1 query elimination** -- use DataLoader, eager loading, or batch queries; each additional round trip to the database adds latency
- **Connection pool sizing** -- too few connections causes queuing; too many overwhelms the database; benchmark to find the right size
- **Benchmark before and after** -- measure performance before refactoring and after; reject changes that regress performance without justification
- **Cache strategically** -- cache-aside for read-heavy data; write-through for consistency; invalidate on domain events, not TTL alone

## Complexity Management

- **Cyclomatic complexity budget** -- set a team standard (e.g., max 10 per function); enforce in CI with static analysis tools
- **Cognitive complexity over cyclomatic** -- SonarQube's cognitive complexity better reflects how hard code is to understand; prefer it for review
- **Reduce nesting depth** -- use early returns and guard clauses to flatten deeply nested conditionals
- **Module boundaries** -- define clear interfaces between modules; a module should be understandable without reading its dependencies
- **Dependency graphs** -- visualize import relationships with tools like pydeps; if the graph has cycles or excessive fan-out, restructure
- **ADRs for significant decisions** -- record architectural decisions with context, options considered, and rationale; prevents re-debating settled questions

## Technical Debt Management

- **Track debt explicitly** -- tag tech debt items in your issue tracker; estimate impact and effort; prioritize alongside features
- **Boy Scout Rule** -- leave every file you touch slightly better than you found it; rename a variable, extract a function, add a missing test
- **Debt ceiling** -- allocate 10-20% of each sprint to debt reduction; adjust based on team velocity and debt accumulation rate
- **Refactoring triggers** -- refactor when: modifying a file with >10 complexity, adding features to a >200 line class, or fixing the third bug in the same module
- **Never refactor without tests** -- if the code lacks tests, add characterization tests first; refactoring without a safety net creates new bugs

## Design Patterns (Applied Pragmatically)

- **Factory** -- use when object creation involves complex logic or conditional type selection; do not use for simple constructors
- **Strategy** -- replace conditional behavior selection with interchangeable implementations; inject the strategy at construction
- **Observer** -- decouple event producers from consumers; use domain events for cross-aggregate coordination
- **Repository** -- abstract persistence behind an interface; the domain layer never knows about SQL or ORM details
- **Decorator** -- add behavior (logging, caching, retry) without modifying the original class; wrap at the composition root
- **Use patterns only where they add clear value** -- a pattern applied without a matching problem is unnecessary complexity

## Anti-Patterns

- **Premature optimization** -- optimizing code that is not a measured bottleneck wastes time and introduces complexity
- **Gold plating** -- adding features, abstractions, or patterns that are not required by current tests or business needs
- **Shotgun surgery** -- a single change requiring edits across many files signals poor cohesion; restructure to localize changes
- **God class** -- a class that knows too much and does too much; split into focused collaborators
- **Lava flow** -- dead code, unused configurations, and abandoned experiments that nobody dares to remove; delete them
- **Copy-paste programming** -- duplicating code instead of extracting shared logic; leads to bug fixes that miss some copies
- **Speculative generality** -- building for hypothetical future requirements; YAGNI (You Ain't Gonna Need It)
