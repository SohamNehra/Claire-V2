# Claire tool review — 12 November 2026

Quick review of all 29 registered built-in tools, their documentation, argument
parsing, failure classification and relevant call paths. The registry/help check
found no missing manuals or unknown controller/worker tool names. The configured
9B conversation and uncensored 27B coding models were retained.

## Batch 1 — files, reading and task handoffs

Reviewed `local_files`, `read_file`, `write_file`, `edit_file`, `list_files`,
`search_files`, `scout`, `spawn_task`, `list_tasks`, `task_result`.

Fixed:

- Missing write content could silently empty an existing file. Missing replacement
  text could silently delete a matched block. Both now require an explicit value;
  an explicitly empty value still supports intentional emptying/deletion.
- Whitespace-tolerant edits could choose the first of several matches and double
  indentation. They now reject ambiguity and retain relative replacement indent.
- Empty files were reported as read errors. Oversized lines could return a
  continuation pointing to the same line forever. Large-file line counts were
  derived from an undisclosed prefix, making later ranges inaccessible. Reads
  now stream with bounded line buffers, count actual lines, support later ranges,
  and give an actionable error for a line that cannot fit the response budget.
- A single-file search/list could return an empty result. Missing paths and
  invalid numeric read/search arguments now produce explicit input errors.
- Recursive searches and scout reads opened file symlinks without checking each
  resolved target. They now apply workspace/private-path checks per file.
- Directory listings now stop at their stated entry cap. Scout discovery enforces
  its file cap before reading an unbounded tree, reports truncated source tails,
  rejects lines too large for its context, and accepts a literal single filename
  containing a comma through the `path` argument.
- Invalid skill names were checked after creating a task workspace. Validation
  now precedes workspace creation.

The previous natural-file-read and project-handoff fixes were retained.

## Batch 2 — skills, memory, protocol and execution results

Reviewed `remember`, `recall`, `use_skill`, `conversation_history`, `tool_help`,
`think_harder`, `done`, plus the shared execution/parser paths.

Fixed:

- Markdown fences were stripped inside generated file contents, and literal
  `<think>` data could disappear. Only an outer response fence and leading
  reasoning block are removed now.
- A literal closing write-tool tag could cause a shortened content block to be
  executed. That ambiguous write is rejected; the write-tool manual explains
  the safely quoted heredoc alternative.
- Denied operations and negative process exit codes could be displayed as
  successful tool steps. They are now classified as unsuccessful operations.
- Expected filesystem errors and cancellation were reported as defects in the
  tool implementation. They now return operational errors without repair notes.
- A removed/unavailable tool could crash the error handler. It returns a normal
  availability error instead.
- An unknown memory kind was silently redirected to another storage category.
  The tool now returns its supported kinds instead of saving in the wrong place.
- “I will fix/edit/test…” could end a controller turn after only reads. These
  plans are checked; an unresolved repeated plan reaches bounded recovery rather
  than being marked done. Denials are returned without another action attempt.

Skill loading/dependency behavior and retained-history lookup were reviewed;
existing regression coverage remains in place.

## Batch 3 — shell, Git and web

Reviewed `run_command`, `git`, `web_search`, `fetch_url`.

Fixed:

- Failed Git invocations with nonempty error output could appear successful.
- The read-only branch query could create a branch when given a positional name.
  A branch filter now uses `--list`.
- Option-like Git path/reference arguments could inject flags such as `--output`.
  They are rejected; path inspection applies the shared private-path settings.
- Local URL approval could be skipped in auto-approve mode. It is now explicit
  in every mode. Trailing-dot localhost names and public IPv6 addresses are
  classified correctly.
- Plain-text/JSON web responses lost literal markup. HTML conversion now applies
  only to HTML, preserving text/JSON bodies.
- Search redirect targets were URL-decoded twice, changing encoded path segments.
  They are decoded once.

## Batch 4 — integrations, scheduling and extensibility

Reviewed `use_connector`, `build_integration`, `schedule_task`, `list_schedules`,
`pause_schedule`, `message_task`, `run_code`, `make_tool`.

Fixed:

- Integration skill-loading notices could hide an installation denial from the
  result classifier. Failure/denial markers now stay first.
- An installed integration whose connection test failed was reported as a
  successful tool result. It now clearly reports the partial failure.
- Known connector-service outages were described as broken tool code, inviting
  pointless repair work. They are reported as service availability failures.
- A recurring project task could omit workdir and later execute in a fresh
  workspace. The schedule tool now recovers the explicitly supplied project path
  using the same resolver as task handoff.

Workshop execution/testing and recurring-job/change approvals were inspected.
No real integration was installed, no recurring job was created, and no connected
user service was mutated during this review.

## Validation

- 29 new focused contract regressions, alongside the existing suite.
- Full suite: 356 tests passed.
- Isolated real-model coding handoff: original project assigned; independent
  project tests passed. Dispatch 2.053 s, greeting while coding 3.433 s, total
  52.92 s. The first smoke attempt exposed the unfinished-fix promise above;
  the repeated check passed after correction.
- Read-only public smoke checks: Python documentation fetch and a documentation
  web search both returned usable content (0.41 s and 0.82 s in this run).
- No model changes were needed for this review.

## Remaining work outside this quick pass

1. Consolidate tool schemas, worker XML parameters and Markdown help into one
   definition. Their duplication is a recurring source of contract drift.
2. Worker XML remains an awkward format for arbitrary nested delimiter data.
   The common shortened-write path now fails closed; a schema-based worker
   protocol should be benchmarked separately rather than rushed into this patch.
3. `fetch_url` still needs transport-level DNS validation/pinning for a full
   private-network boundary. Explicit local-address checks are not a complete
   DNS-rebinding defense.
4. Provider-specific integration UAT needs representative API fixtures/live
   read-only checks. Mocked error contracts do not establish that every external
   service/action works.
5. Completion checks still use bounded language heuristics and observed receipts;
   they are not a general proof that every possible user goal was achieved.

This is a finite batch review, not a claim that every tool/model interaction is
now error-free. Measurements and source hashes are in
`tests/tool_review_results.json`.
