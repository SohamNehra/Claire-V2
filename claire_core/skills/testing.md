---
name: testing
description: Design and run automated tests for code or integrations, covering observable behavior, failure paths and real boundaries.
when: writing tests; fixing a bug; verifying a feature or integration; regression testing
steps: 50
kinds: agent
---
# Test requirements, not your implementation

Translate the request into a small case matrix before writing tests: setup/input,
expected observable result, and how to exercise it. Use requirements, authoritative
docs and recorded errors as the oracle. Do not generate both a fictional API
response and matching code and call the agreement evidence of compatibility.

Find the existing runner and execute the relevant baseline. For a bug, capture a
case that fails before the fix and passes after it. Assert externally visible
results, persisted state and outgoing requests where those are the contract.
Avoid checking private helper names or source wording unless that is the contract.

Select cases proportional to the change:
- ordinary success and empty/no-result success;
- missing/invalid input and a useful error, with no unintended side effect;
- boundary values, pagination/limits, malformed or incomplete responses;
- denied access, expired credentials, timeouts, rate limits and remote errors;
- repeated calls, cancellation and cleanup when state or processes are involved.
Do not add retries for writes unless their idempotency is established.

For Claire-built integrations, test every public action via claire_kit.call with
offline fixtures. Assert method, path, query, authentication header (fake value),
body and useful returned fields. Include an explicit failure assertion: catch Fail
only around the call expected to fail; fail the test if it unexpectedly succeeds.
Use separate fixture scopes for successive pages or errors; fixture replies are
not a live account test. Use http(..., allow=(404,)) only when that status is an
intentional part of the action's documented contract.

Run focused tests after the change, then the relevant broader suite/build once.
Inspect exit status and assertions, not a printed "passed" message. Fix the cause;
do not weaken existing assertions to make the suite green. A changed requirement
can justify a test update, but explain the new expectation and preserve coverage.
Report commands, results, and which boundaries remained mocked or untested.
