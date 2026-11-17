---
name: building-software
description: Build or extend a program, CLI, backend or interface from a user requirement through a verified usable result.
when: implement a feature; build an app, script or tool; extend existing software
requires: coding, testing, uat
steps: 60
kinds: agent
---
# Build the requested outcome

Establish the smallest working user journey: input, expected output, where it runs,
and what counts as accepted. Infer routine choices from the repository; ask only
when a missing decision changes the deliverable. Preserve the user's constraints.

Read the entry point, relevant callers, configuration and existing test command.
Reuse the project's architecture and dependencies. First make one complete path
work across its boundaries; then add the other requested cases. An isolated helper
is insufficient when the entry point never invokes it.

For multi-stage work, keep a short WORKLOG.md in the work folder: request,
acceptance cases, decisions, files changed, commands/results, and remaining work.
Update it after meaningful results so a resumed task can continue from evidence.
Do not put credentials in it. A small edit does not need this extra file.

Run the project's actual build/start path with representative input. Check failures
and cleanup as well as success. Apply testing during implementation and UAT before
handoff. Load reviewing-changes for a requested review or a broad change with
multiple callers. Do not package or publish unless the request includes that work.

Finish with the runnable artifact, exact launch command, verification observed,
and any specific unverified requirement. A plan, generated code, or a green syntax
check alone does not establish that a feature works.
