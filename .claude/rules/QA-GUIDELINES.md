# QA guidelines

The job is not "are the tests passing". It is "would these tests notice if the code were
wrong".

## A test must be able to fail

- **Mutation is the evidence.** Before believing a test covers something, break that
  something and watch the test fail. One change at a time, restored afterwards. A mutation
  that survives is a finding, and it is the most useful thing a review can produce.
- **State the anti-vacuity condition in the test itself.** A test that exists because a
  payload must exceed a limit should assert that its fixture exceeds the limit. Otherwise it
  passes for the wrong reason the day someone shrinks the fixture.
- **A guard is not verified until it has been seen to bite.** For anything that refuses,
  rejects or fails closed, produce the failure on purpose and show the refusal.

## Vacuous patterns to reject

- Asserting a mock was called. That is evidence about the mock, not about the code. Prefer a
  real dependency or an observable result. Where a spy is genuinely the mechanism — proving
  an expensive call did *not* happen — assert both directions: that it ran when it should and
  did not when it should not.
- An assertion hidden inside a helper, so the test body reads as though it asserts nothing.
  Keep the helper, and make the expectation visible in the body too.
- `assertTrue` on something structurally always true. Assert the value, not its truthiness.
- An exception caught so broadly that the failing case still passes.
- A test whose name promises more than its body checks.

## Coverage

- Coverage is a tripwire, not a target. A test asserting nothing raises it exactly as well as
  a test asserting something.
- Read the table, not the total. This project once had 54 passing tests with all three engine
  adapters at 0% — the plumbing was tested hard and the logic not at all.
- A per-service floor stops a well-tested service from masking an untested one. Never lower a
  floor to make a change pass.

## Reporting

- Say what you tried, what was caught and by which test, and what was not.
- Distinguish "verified" from "not verified" every time. An unverifiable claim reported as
  verified is worse than no test at all.
- Restore the tree and show it is clean before you finish.
