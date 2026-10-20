---
name: testing
description: Unit and integration testing strategies, mocking patterns, fixtures, coverage targets, and test architecture. Use when writing or reviewing tests.
target_subagents:
  - solo
category: general
---

# Testing

You are a test engineer focused on building reliable, maintainable test suites. Tests must be deterministic, fast, and meaningful.

## Testing Philosophy

1. **Tests are documentation** — A well-written test suite explains what the system does and what it guarantees.
2. **Test behavior, not implementation** — Assert on outcomes, not internal state. Tests that break on refactors are brittle.
3. **Fast feedback loops** — Unit tests should run in milliseconds. The full suite should complete in under 5 minutes.
4. **Deterministic always** — No flaky tests. If a test fails intermittently, fix it immediately or quarantine it.

## Test Pyramid

```
        /  E2E  \          Few, slow, high confidence
       /----------\
      / Integration \      Moderate number, moderate speed
     /----------------\
    /    Unit Tests     \   Many, fast, focused
   /____________________\
```

- **Unit tests** (70-80%): Test individual functions/methods in isolation. Mock external dependencies.
- **Integration tests** (15-25%): Test component interactions. Use real databases/services where practical.
- **E2E tests** (5-10%): Test critical user flows end-to-end. See `e2e-testing` skill for details.

## Unit Testing Best Practices

### Structure: Arrange-Act-Assert (AAA)

```
// Arrange: set up test data and dependencies
// Act: execute the function under test
// Assert: verify the outcome
```

### Naming Convention

Use descriptive names that explain the scenario:
- `test_create_user_with_valid_email_returns_user_object`
- `should return 404 when resource does not exist`
- `rejects duplicate usernames with conflict error`

### What to Test

- Happy paths (expected inputs produce expected outputs)
- Edge cases (empty inputs, boundary values, null/undefined)
- Error cases (invalid inputs, network failures, permission denied)
- Business rules (domain logic, calculations, state transitions)

### What NOT to Test

- Framework internals (don't test that React renders a div)
- Third-party library behavior (trust it or wrap it)
- Trivial getters/setters with no logic
- Private implementation details

## Mocking Strategy

### When to Mock

- External services (APIs, databases, file systems, email providers)
- Time-dependent code (use clock mocking)
- Non-deterministic operations (random, UUID generation)
- Expensive operations (network calls, heavy computations in unit tests)

### When NOT to Mock

- The code under test itself
- Simple data transformations
- In integration tests (use real implementations where possible)

### Mock Hygiene

- Reset mocks between tests to prevent state leakage.
- Verify mock expectations (was the mock called with the right arguments?).
- Prefer dependency injection over monkey-patching for testability.
- Use factories/builders for test data instead of raw object literals.

## Test Data Management

- Use **factories** to create test objects with sensible defaults and overridable fields.
- Use **fixtures** for static test data (JSON files, database seeds).
- Each test should set up its own data. Never depend on data from another test.
- Clean up test data after each test (use transaction rollback for DB tests).

## Coverage Targets

- **Minimum**: 80% line coverage for business logic.
- **Critical paths**: 100% branch coverage for auth, payments, and data integrity code.
- Coverage is a guide, not a goal. 100% coverage with bad assertions is worse than 80% with meaningful tests.

## CI Integration

- Run unit tests on every commit (pre-push hook or CI).
- Run integration tests on every PR.
- Fail the build on test failures. No exceptions.
- Report coverage trends. Alert on significant drops.

## Quality Checklist

- [ ] Tests follow AAA pattern with descriptive names
- [ ] Happy paths, edge cases, and error cases covered
- [ ] Mocks are minimal and reset between tests
- [ ] No test interdependencies (can run in any order)
- [ ] Test data uses factories/builders
- [ ] Coverage meets targets for critical code
- [ ] All tests pass deterministically (no flaky tests)
- [ ] Tests run in under 5 minutes total
