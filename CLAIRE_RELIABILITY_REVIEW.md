# Claire skills and runtime review — 12 November 2026

Reviewed Claude's latest changes through `8d3d682` (the repository was clean at
that commit), then updated the skills and the runtime paths that consume them.

## Installed procedures

Six new skills: `building-software`, `debugging`, `testing`, `uat`,
`researching-apis`, and `reviewing-changes`. Together with `coding`,
`integrations`, and `building-integrations`, Claire now discovers nine procedures.

The integration-building procedure automatically loads coding, API research,
testing and UAT. It asks for a documented contract, a compact working record,
offline request/response tests, installation approval and honest acceptance
milestones. Explicit requests to build a named integration now take precedence
over advice to manually configure the generic API card.

UAT distinguishes offline-tested, installed, connected and accepted live. Missing
credentials are a blocked acceptance case, not evidence of success. Skills direct
Claire to verify through the actual entry point and report commands and observed
results. They do not grant permission to bypass approvals or modify the engine.

## Runtime findings and fixes

| Area | Finding and change |
|---|---|
| Qwen reasoning | LM Studio's Qwen3.8 27B still reasons at `low`; normal mode incorrectly used that setting. Verified that `none` disables reasoning on the loaded model. Normal mode now sends `none`; thinking/recovery defaults to `medium`, configurable with `CLAIRE_LMSTUDIO_REASONING_EFFORT`. Other model mappings are preserved. Helper roles falling back to this model receive the same handling. |
| Skill persistence | Uploads discarded prerequisites, applicability, routing hints and step budgets. Uploads and subsequent body edits now preserve them; Windows line endings are accepted. |
| Dependency loading | Direct sessions omitted prerequisites, runtime loading could recurse on cycles, and routing silently cut off dependency chains after depth three. All paths now share cycle-safe dependency expansion. |
| Routing recovery | The main model saw only names when the router missed a skill or was unavailable. Applicable descriptions now remain discoverable, without loading every skill body. |
| Long tasks | Compaction could remove the original request, including when summarization failed. The current request is now preserved verbatim alongside the recent exchanges. |
| Integration validation | A test that only printed success could pass. Checks now require successful public calls to every declared action, verify function/manifest argument compatibility, and validate credentialed builds' read checks. Coverage remains a minimum; tests must assert real contracts. |
| Offline HTTP | Fixtures ignored allowed HTTP error statuses, lost empty JSON bodies, and nested fixture scopes could switch the outer scope back to real networking. These behaviors now match the intended transport semantics. |
| Build state | An automatic skill-loading notice could hide an error marker from the engine. Errors retain their marker. A newly connected integration now exposes its connector tool in the installing session immediately. |
| Model truncation | The client discarded `finish_reason`, allowing the tolerant XML parser to execute a known-truncated write. Finish reasons are retained, and token-limited output is retried as a smaller call without executing the partial output. |
| Backend metadata | LM Studio was repeatedly queried for llama.cpp's unsupported `/props`. It now uses native metadata for the brain and roles sharing it. |

## Verification

- Baseline: 262 automated tests passed before changes.
- Final runtime suite: 279 tests passed with no skips reported.
- New regressions reproduced the central failures against the unchanged code.
- Live local service restarted only after confirming it was idle and had no
  pending approvals. All 14 existing chat IDs remained present. `/skills` returned
  all nine procedures and the correct integration prerequisites.
- `git diff --check` passed.

The reusable evaluation is `python3 -m tests.live_integration_eval`, run from the
repository. It creates disposable state and a synthetic integration, asks the
local model to repair it and write tests, then runs independent acceptance cases
that were not included in the model's request. It does not install an integration,
use saved credentials, or call a real service. It returns a failing exit status
when the model or independent checks fail.

A completed run before the reasoning-control fix passed the independent checks
in eight steps and 713 seconds. One implementation call alone used 7,465
completion tokens and about 512 seconds. A diagnostic fast-mode run reduced the
comparable implementation call to about 43 seconds, then encountered a failing
model-authored test. Its superseded `xhigh` recovery run was stopped; it is not
counted as a completed pass. A separate reusable-runner fixture-formatting issue
was corrected before the final evaluation.

The final reusable evaluation passed both the model's integration check and the
independent acceptance cases in eight steps and **175.4 seconds**, including
recovery from a harmless Git lookup in a non-repository work folder. This is
about **4.1× faster** than the completed 713-second run in this synthetic exercise.
The corresponding completion-token counts and timing summary are recorded in
`tests/live_eval_results.json`.

## Boundaries

This pass tested the harness and a synthetic local integration contract. There
are currently no installed Claire-built integrations in the configured state.
YouTube, YouTrack and Metabase account workflows were not exercised. Their
service-specific implementations still need official API research and live UAT
with the user's configured account. A single local evaluation is evidence about
that scenario, not a guarantee for every future task.

LM Studio's [native chat documentation](https://lmstudio.ai/docs/developer/rest/chat)
explains its reasoning controls. Compatibility-endpoint behavior in this review
was checked against the user's actual loaded model rather than assumed from a
provider-neutral parameter name.
