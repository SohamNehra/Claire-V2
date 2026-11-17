---
name: uat
description: Verify a finished change through the user-facing workflow and report accepted, failed or blocked requirements with evidence.
when: user acceptance testing; end-to-end verification; before handing off a built app or integration
steps: 50
kinds: agent
---
# Accept the user journey

Read the original request and list its acceptance cases. For each, record the
starting state, user action, expected result, observed result and PASS/FAIL/BLOCKED.
For a small task these can stay in your report; for a long build keep them in the
work folder's WORKLOG.md. UAT evaluates the delivered behavior; it does not replace
the testing skill's automated checks.

Use the real entry point: CLI invocation, app flow, or use_connector action. A
backend helper test does not prove the UI reaches it. For a stateful change check
that revisiting/reloading preserves the intended state, and that errors are visible
and actionable. Verify empty state, normal success, and the most consequential
failure. Check cancellation/cleanup if the task starts background work.

For an integration, distinguish these milestones explicitly:
1. Offline check passed: contract tests and registered actions were checked.
2. Installed: submit was approved and the integration exists in live state.
3. Connected: credentials were entered in the app and the read check succeeded.
4. Accepted live: a requested read action returned useful real data through
   use_connector. A successful empty result may be valid; say it was empty.

Use existing live state rather than claiming an old task report proves connection.
Do not restart Claire to discover an integration: approved installs are hot-loaded.
Do not repeat a denied install or perform a real external write just to test it.
Use fixtures for writes unless the user authorized a specific live test and its
side effects. Never obtain or paste credentials into a task or test file.

Missing credentials or an unavailable UI means that acceptance case is BLOCKED;
finish the available cases and name the exact action needed to unblock it. Never
label the whole journey passed because mocks passed. Report failures with the
observed error and reproduce them after the fix before changing their status.
