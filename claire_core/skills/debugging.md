---
name: debugging
description: Diagnose a reproducible software or integration failure, prove the root cause, fix it and check regression behavior.
when: an error, crash, wrong output, hang, slowdown or intermittent failure needs investigation
requires: coding, testing
steps: 60
kinds: agent
---
# Diagnose before changing

Capture expected versus actual behavior and the smallest reproducing input. Read
the exact error and trace the actual entry point to the first incorrect value.
Check configuration, current connection state and environment before attributing
the result to a model or changing unrelated code.

Keep a concise evidence log for a long investigation: symptom, hypothesis, test,
observation, next action. Compare plausible causes with a discriminating check;
do not rewrite several components on a hunch. For slowness, measure time or token
use by stage before optimizing: routing, prompt construction, inference, tools,
network, retries. Distinguish a timeout from a slow successful operation.

Change the smallest cause supported by the reproduction. Add the regression
case, run it and relevant neighboring behavior. Re-check the original entry point.
A passing unit test cannot erase a still-failing live observation. If the same
failure repeats, inspect the new evidence and change the hypothesis or report a
specific blocker. Do not disable validation, approvals or sandboxing to hide it.

Finish with what failed, the demonstrated cause, the change and the checks run.
Keep unconfirmed explanations labeled as hypotheses.
