# Claire two-model optimization — 12 November 2026

The initial optimization used the existing Qwen3.5 4B GGUF server as its resident conversational
controller and the installed uncensored Qwen3.8 27B as its coding worker in LM
Studio. This changes the app's execution path, not just its personality prompt.

## Why the folder request was slow

The original request sent 13,594 tokens to the 27B conversation model and waited
60.138 seconds for its first token. It then created a coding task that sent 9,410
tokens to the same model and waited another 42.621 seconds. Coding, testing, UAT
and software-building skills were loaded to create an empty directory. The mkdir
operation itself took approximately 0.03 seconds. LM Studio's logs showed prompt
processing throughout the initial minute.

## Architecture

- **Controller:** conversation, clarification, verified directory creation, new
  text files, directory listing, text reading, task status, memory and delegation.
  No general shell tool. Schema-constrained JSON decisions prevent malformed XML
  from becoming the everyday action protocol. Initial prompts in the capability
  suite were around 1,500 tokens.
- **Worker:** a standalone brief, relevant skills and coding tools. It receives
  neither the home persona nor unrelated conversation or work diaries. Coding
  checks began around 3,500 prompt tokens. The 32K loaded context bounds memory;
  large file reads default to 120 lines with explicit ranges available.
- **Memory:** only a bounded recent conversation is sent per request. Full
  history remains stored and can be searched through conversation_history.
  Older facts and corrected recent facts were exercised with the real model.
- **Escalation:** technical controller failure can create one worker task with
  the original request and observed attempts. An already-started task is not
  duplicated. Refused paths, approvals, credentials and cancellation remain
  boundaries. An unavailable controller server can use the strong model with
  the compact controller context.
- **Interactive priority:** one coding task at a time by default. Additional
  tasks queue; MAX_TASK_WORKERS remains configurable. Different local servers
  separate the roles, but both models still share the machine's GPU bandwidth.
- **Explicit deep mode:** opts that conversation into the strong model. Ordinary
  chat stays on the small model.

## Optimizations grounded in tests

The initial unconstrained XML controller was fast but unreliable. It sometimes
claimed an action had occurred without using a tool and guessed paths from an
example. That candidate protocol was rejected. Constrained JSON improved the
model's decisions, and the local file capability additionally verifies paths
against actual user requests and recently verified locations. It preserves
literal Unicode/spaces, refuses overwrite, and retains the shared code, state
and credential guards. It cannot create trusted skill or identity instructions.

Changing Qwen reasoning mode on individual tool errors invalidated a long
cached prompt: one diagnostic retry waited 116 seconds before its first token.
The new worker keeps the selected reasoning mode stable for the whole turn.
Explicitly requested worker thinking is still honored. Mutable workspace details
follow the stable worker instructions so different jobs can reuse their common
prefix. Routine chat skips the separate skill-router model call entirely.

The UI distinguishes request preparation from context processing and displays
the model relevant to the selected conversation or task.

## Verification and measurements

- 300 automated regressions passed at installation preparation.
- All existing headless browser checks passed, including approvals, live task
  reports, multiple chats, integration setup, skills and responsive layouts.
- Real controller HTTP tests passed 22/22, including ambiguous requests,
  hypothetical/quoted instructions, preservation of existing files, Unicode,
  follow-ups, old-history retrieval, corrected project facts and delegation.
- The uncensored worker passed 7/7 independent coding checks: bug fix, documented
  specification, scoped rename, missing-file grounding, data workflow, one-line
  change and repository analysis.
- The focused integration worker passed its offline check and independent
  acceptance cases in the synthetic catalog repair fixture. A separate long
  diagnostic run with per-error thinking toggles was stopped and is not counted
  as a passing evaluation.
- The complete HTTP handoff pipeline passed: 1.79 seconds to dispatch, 3.652
  seconds for another greeting during one coding job, 59.73 seconds for the
  independently verified coding result and its report to reach the conversation.

A normal-load HTTP run measured:

| Request | Total response time | First model token |
|---|---:|---:|
| Greeting | 0.965 s | 0.347 s |
| Identity question | 0.520 s | 0.209 s |
| Create named folder | 2.308 s | 0.184 s |
| Follow-up file creation | 1.383 s | 0.228 s |
| Folder plus file | 3.006 s | 0.594 s |
| Delegate coding | 2.674 s | 0.906 s |

Overload testing intentionally ran additional independent coding processes.
Latency increased materially: the same folder request took 12 seconds, and one
pipeline's greeting took 26 seconds. Those observations motivated the single
worker production default; they are retained in the evidence rather than
presented as normal interactive performance. GPU contention from unrelated apps
can still affect latency. These are local samples, not universal latency bounds.

Machine-readable measurements are in tests/routing_eval_results.json. Reusable
live commands are documented in README.md. Test fixtures use disposable state;
no real account workflows were executed or integrations installed by the tests.

## Installed-app verification

The final code is installed and the local service restarted. All 15 existing
chat IDs remained present. Verification used a temporary chat in the real app,
then removed only that chat and the empty verification folder.

| Real app request | Total time | First model token | Model role |
|---|---:|---:|---|
| `hey` | 0.729 s | 0.513 s | talk |
| Exact original `claire testing` Desktop request | 1.193 s | 0.144 s | talk |
| Create a new, uniquely named Desktop folder | 1.674 s | 0.146 s | talk |

Both folder requests had successful verified local_files results and used no
coding worker. The original folder already existed and its contents were
preserved. The newly created verification folder's literal name and empty state
were checked before cleanup. The live check exposed model title-casing of the
folder name; local path handling now restores the user's literal capitalization
when the match is unambiguous, without normalizing spaces or punctuation.

The installed-source suite passed 300 tests. The previous 22-case controller
stress run and seven-case worker run remain in the evidence with their load
conditions; slower concurrent results have not been replaced by these idle app
measurements.

## Practical limits

The fast local tool creates directories and new text files, lists directories
and reads text. It does not overwrite, rename or delete. Other work can be
handed to a worker with existing authorization/approval rules. The selected fast
server and uncensored worker are text routes; screenshot interpretation is not
provided by this two-model configuration. Accessibility/DOM text remains usable.
The previous vision model remains installed, but was unloaded from the active
coding slot to make room for the requested uncensored worker.

Relevant primary references: [Qwen3.5 4B model card](https://huggingface.co/Qwen/Qwen3.5-4B),
[llama.cpp structured-output server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
[LM Studio model loading](https://lmstudio.ai/docs/developer/rest/load),
[LM Studio inference stages](https://lmstudio.ai/docs/developer/rest/streaming-events).
Local behavior and timings above were measured rather than inferred from model size.

## Conversation reliability follow-up

The current shared conversation/reader server uses Qwen3.5 9B Q4_K_M. The
uncensored Qwen3.8 27B coding worker is unchanged. The follow-up fixes path
handling, actual-request provenance, unsupported progress/access claims and
omitted project workdirs. New measurements and limits are in
[CLAIRE_NAVIGATION_REVIEW.md](CLAIRE_NAVIGATION_REVIEW.md); the earlier 4B timing
records above remain historical measurements, not claims about the current 9B.
