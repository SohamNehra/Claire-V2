---
name: reviewing-changes
description: Review a diff for functional regressions, missing requirements and integration failures, with actionable evidence.
when: reviewing code; auditing a changed feature; checking a multi-file change before handoff
requires: coding
steps: 45
kinds: agent
---
# Review from the user's failure modes

Start with the requested behavior and diff, then read changed code and its callers.
Trace input to output across module boundaries. Check setup, config, error paths,
state transitions and cleanup. Confirm new public actions are actually reachable
and that claimed tests exercise the delivered path.

Prioritize concrete correctness findings: wrong results, lost state, unhandled
failures, unapproved side effects, credential disclosure or broken compatibility.
For each finding give trigger, affected path, consequence and evidence. Confirm it
with a focused reproduction when practical. Style preferences are not bug reports.

Review tests against requirements: what would fail if the implementation were
wrong? Look for fixtures that merely repeat invented assumptions, ignored exit
codes, assertions that never execute and untested public actions. Check installed
or generated artifacts when they determine actual behavior.

Report the scope inspected and residual gaps honestly. Fix findings only when the
user requested fixes. Re-run the affected checks after a fix, and distinguish
confirmed defects from risks that still need evidence.
