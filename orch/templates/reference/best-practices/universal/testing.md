# Testing Best Practices

Comprehensive guidance for test-driven development, test strategy, and quality engineering -- from the red-green-refactor cycle through property-based testing and mutation testing. These practices ensure that tests serve as living documentation, provide fast feedback, and build confidence for refactoring.

TDD is not just about writing tests first; it is a design discipline that produces cleaner interfaces, smaller functions, and better separation of concerns. The test pyramid ensures appropriate investment at each level, while advanced techniques like property-based testing and mutation testing validate the quality of the tests themselves.

---

## TDD Red-Green-Refactor

- **Write the failing test first** -- define expected behavior before any implementation; the test must fail for the right reason (missing implementation, not syntax errors)
- **Verify failure is meaningful** -- a test that fails with ImportError or TypeError is broken, not "red"; fix test infrastructure before proceeding
- **Implement the simplest code that passes** -- resist the urge to add features, optimizations, or error handling beyond what the test demands
- **Fake it, then triangulate** -- start by returning hard-coded values; generalize only when multiple tests force it
- **Refactor only when green** -- never change behavior and structure simultaneously; keep all tests passing during refactoring
- **Run tests after every change** -- fast feedback is the foundation of TDD; if the suite takes more than a few seconds, optimize it
- **One behavior per test** -- each test should verify exactly one thing; multiple assertions are fine if they verify a single logical behavior

## TDD Schools

- **Chicago School (state-based)** -- test the output given an input; prefer real collaborators over mocks; good for inside-out development
- **London School (interaction-based)** -- test that the correct messages are sent to collaborators; use mocks and stubs; good for outside-in development
- **Outside-in TDD** -- start with an acceptance test that describes user-visible behavior, then drill down to unit tests
- **Inside-out TDD** -- start with the innermost domain logic and build outward; best for libraries and algorithmic code
- **Double-loop TDD** -- outer acceptance test stays red while inner unit tests cycle through red-green-refactor until the acceptance test passes

## Test Pyramid

- **Unit tests form the base** -- fast, isolated, no I/O; test individual functions, methods, and classes; aim for the majority of tests here
- **Integration tests in the middle** -- verify component interactions, database queries, and API contracts; slower but catch wiring bugs
- **End-to-end tests at the top** -- validate full user workflows; keep the count small because they are slow and brittle
- **Contract tests between services** -- verify that API consumers and providers agree on interface shape without deploying both
- **Do not invert the pyramid** -- too many E2E tests and too few unit tests creates a slow, fragile suite that discourages TDD

## Test Organization

- **Arrange-Act-Assert (AAA) pattern** -- clearly separate setup, execution, and verification; each section should be visually distinct
- **Descriptive test names** -- use `should_X_when_Y` or `test_X_given_Y` naming; the test name should explain the requirement without reading the body
- **One test file per module** -- mirror source directory structure in the test directory for easy navigation
- **Shared fixtures via factories** -- use factory functions or builder patterns for test data; avoid global fixtures that create hidden coupling
- **Test independence** -- each test must run in isolation with no dependency on execution order or shared mutable state
- **Fast feedback loop** -- target sub-second unit test execution; use incremental test runners and watch mode during development

## Mocking Strategies

- **Mock at the boundary, not the internals** -- mock external services, databases, and third-party APIs; avoid mocking internal collaborators unless using London School TDD
- **In-memory adapters over mocks** -- for Clean Architecture, implement in-memory versions of repository ports; they validate contracts better than mock assertions
- **Test doubles taxonomy** -- stubs return canned data, mocks verify interactions, fakes have working implementations, spies record calls for later assertion
- **Do not mock what you do not own** -- wrap third-party libraries behind your own interface; mock the wrapper, not the library directly
- **Verify behavior, not implementation** -- assert on outputs and side effects, not on which internal methods were called in which order

## Property-Based Testing

- **Define properties, not examples** -- express invariants like "sorting is idempotent" or "serialization round-trips preserve data" rather than specific input/output pairs
- **Use Hypothesis (Python), fast-check (JS), or QuickCheck (Haskell)** -- let the framework generate hundreds of random inputs automatically
- **Shrinking reveals minimal failures** -- when a property fails, the framework reduces the input to the smallest reproducing case
- **Combine with example-based tests** -- property tests catch unexpected edge cases; example tests document specific business requirements
- **Test algebraic properties** -- commutativity, associativity, idempotency, and round-trip encode/decode are powerful universal properties

## Coverage and Quality

- **Coverage targets** -- aim for 80% line coverage minimum, 75% branch coverage; require 100% on critical paths (authentication, payment)
- **Coverage measures execution, not correctness** -- a line can be covered without being meaningfully tested; use mutation testing to validate test quality
- **Mutation testing** -- tools like mutmut (Python) or Stryker (JS) inject small code changes; if tests still pass, they are too weak
- **Cyclomatic complexity triggers** -- refactor when complexity exceeds 10; split methods longer than 20 lines; keep classes under 200 lines
- **Track test growth rate** -- monitor the ratio of test code to production code; a declining ratio signals test discipline erosion

## Integration Test Boundaries

- **Test database with transactions** -- wrap each test in a transaction and roll back; avoids test pollution without slow database recreation
- **Use testcontainers for real dependencies** -- spin up PostgreSQL, Redis, or Kafka in containers for integration tests rather than mocking them
- **Contract tests at API boundaries** -- use Pact or similar to ensure consumers and providers evolve their interfaces compatibly
- **Separate slow tests from fast tests** -- mark integration and E2E tests so developers can run unit tests in seconds and the full suite in CI

## TDD Anti-Patterns

- **Test after implementation** -- writing tests after code produces tests that verify the implementation rather than the behavior
- **Tests that already pass** -- a test that passes immediately provides no evidence that the implementation is correct
- **Modifying tests to make them pass** -- if you change assertions to match buggy output, the test is worthless
- **Skipping the refactor phase** -- accumulating technical debt in green code defeats the purpose of TDD
- **Testing implementation details** -- tests coupled to internal method names or call sequences break on every refactoring
- **Complex test setup** -- if arranging a test requires more than a few lines, the production code has too many dependencies
- **Ignoring failing tests** -- a red test that is skipped or commented out erodes trust in the entire suite

## Legacy Code Testing

- **Characterization tests first** -- before refactoring legacy code, write tests that capture current behavior (even if it is buggy)
- **Golden master testing** -- capture full output of a legacy process; any deviation during refactoring signals a behavior change
- **Seam identification** -- find points where you can inject test doubles without modifying legacy code (constructor injection, method extraction)
- **Incremental TDD adoption** -- require TDD for all new code; add tests to legacy code only when modifying it
- **Approval testing** -- for complex output (reports, emails), snapshot the output and compare; useful when exact assertions are impractical

## Performance and Load Testing

- **Performance TDD** -- write a benchmark test that asserts response time before optimizing; the test proves the optimization worked
- **Load testing tools** -- k6 for developer-friendly scripting, JMeter for complex scenarios, Gatling for Scala-based pipelines, Locust for Python teams
- **Establish baselines before changes** -- record p50, p95, and p99 latency before any optimization; compare after to verify improvement
- **Capacity planning tests** -- determine the breaking point by gradually increasing load; know your limits before production traffic finds them

## Test Data Management

- **Factory functions over fixtures** -- factories create fresh data per test with sensible defaults; global fixtures create hidden coupling between tests
- **Meaningful test data** -- use realistic values ("alice@example.com", not "foo@bar"); meaningful data makes failures easier to diagnose
- **Database state isolation** -- wrap each test in a transaction and roll back; or use testcontainers for a fresh database per suite
- **Anonymize production data** -- if using production data for testing, strip PII before copying; comply with GDPR and data residency requirements

## CI/CD Integration

- **Run unit tests on every push** -- gate pull request merges on passing tests and minimum coverage thresholds
- **Parallel test execution** -- split test suites across CI workers to keep feedback under 5 minutes
- **Dynamic test selection** -- run only tests affected by changed files in PR builds; run the full suite on merge to main
- **Flaky test quarantine** -- automatically detect and isolate flaky tests; fix or remove them within a sprint
- **TDD compliance metrics** -- track test-first percentage, cycle time, and coverage trends on a team dashboard
- **Separate fast and slow suites** -- developers run unit tests locally in seconds; CI runs integration and E2E tests on every PR
- **Performance regression detection** -- compare benchmark results against baselines in CI; fail the build on significant regressions
