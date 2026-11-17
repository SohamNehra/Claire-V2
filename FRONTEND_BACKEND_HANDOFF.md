# Latest handoff — cleaner workspace and project continuity (2026-11-11)

This update supersedes the earlier dashboard/mini-chat design below. The user
approved the simpler UI direction and asked for durable work memory separate
from identity.

## User-visible changes

- Command center: rotating Claire core, one **Talk to Claire** entry, actual task
  totals (all / active / completed / failed-stopped), current background tasks,
  and the next three scheduled tasks. Removed the slogan, mini-chat, repeated
  task sidebar, capability/approval dashboard cards, and decorative copy.
- Talk to Claire goes to the persistent home chat. The composer appears only in
  Chats; typing and replying happen there. Counters open the matching task view.
- System Monitor: per-chat context usage, approvals, repair notes, available RAM,
  and task capacity. The context meter is an estimate, updated during generation,
  at turn completion, and on reload. It is not global RAM or summed token usage.
- Capabilities, controller configuration, benchmark reports (formerly Model lab),
  and repair notes (formerly Inbox) are in the expandable System details area.
  Sandbox status is there too. Memory vault and Skills remain directly accessible.
- Memory vault now lists `work` notes, hiding their rollover metadata from the
  rendered view while preserving it in the markdown files.

## Task accounting and capacity

`GET /status` adds:
- `task_counts`: SQL totals over **all** persisted task rows, excluding home.
  active = idle/queued/running/waiting; completed = done; failed =
  error/stopped/interrupted; cancelled is separate but included in total.
- `context`: `{used, limit, estimated: true}` for home. Thread detail also returns
  `thread.usage`. Existing usage SSE events now update during streaming and before
  final answers. The meter uses the viewed task's context when a task chat is open.
- `capacity`: memory snapshot, active worker count, effective task limit, actual
  server slot count, reserved chat slots, automatic mode, and limiting reason.

Task rows are authoritative SQLite records, not files maintained by a model.
The separate work log provides readable project history; it cannot change counts.

`resources.py` adds dependency-free macOS/Linux memory telemetry and conservative
admission. macOS available RAM is estimated from free + inactive pages; Linux
uses MemAvailable. Unknown telemetry never increases concurrency. Defaults:
`AUTO_PARALLEL=True`, `MAX_TASK_WORKERS=4`, `RAM_RESERVE_GB=4`, `TASK_RAM_GB=2`,
`MODEL_RUNTIME_GB=4` (set in config.py to override). Reserve is the greater of
4 GiB and 10% of physical RAM. Running tasks are allowed to finish when limits
shrink; new tasks queue. A loaded server with multiple slots keeps one for chat.
Unknown/single-slot servers stay at one task, sharing that server with chat.
Manual `AUTO_PARALLEL=False` preserves TASK_WORKERS behavior.

New managed model processes pass a memory check (GGUF bytes × 1.25 plus the
runtime allowance and reserve). Existing servers are never killed, resized,
duplicated, or downloaded automatically. Configured scout/clerk roles still start
on demand. This is budgeted admission, not a guarantee against OOM. Current real
server: **2 brain slots, so 1 task slot with 1 reserved for chat**; the 64 GiB Mac
has roughly 20–22 GiB available during verification. More RAM alone cannot change
an already-running llama-server's slot count. See upstream `/props` / `total_slots`:
https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md

## Project-work memory

Existing persistence was verified before migration: **30 saved home messages,
3 task reports, 4 project notes, 2 session notes, 1 learned note**. All 30 messages
survived an actual service restart. No identity notes were changed.

New files in the user's vault:
- `work/recent.md`: dated current-project goals, decisions, task starts/outcomes.
- `work/old.md`: updates older than 30 days; retained indefinitely and searchable.

`WorkLog` uses stable ids and timestamps in markdown comments. Record updates are
atomic; archive is written before recent is pruned, and duplicate ids make a
crash/retry safe. A process/file lock serializes updates. Unstructured manual text
and malformed metadata are preserved rather than deleted. Rollover runs hourly
while Claire is open, at service start, and when work memory is read or written;
offline time is caught up on next start/access. Age is measured from each entry's
last update. No model call is used to decide age or move entries.

Automatic updates come from task creation, completion, failure, and interrupted
work recovered after restart. Reports remain in `tasks/`; the work log points to
the full report. `remember kind=work` adds explicit project updates from chat;
its `note` is the project name. Home tool instructions now distinguish current
project activity from personal identity/preferences. Task `remember kind=project`
also updates the recent work view. General chat history is still saved normally;
not every conversation is blindly copied into the work log.

Home loads a bounded recent-work digest; task contexts load entries matching their
workdir. Existing open chats refresh shared memory at the next turn. Both recent
and archived work are in vault recall; excerpts now center around a matching term
so a result deep in a long archive is not hidden behind its header. Detailed old
project notes remain in `projects/` and can still be recalled.

Seeded only the actual Claire 2 project described by the user, with the current
UI, orchestration, and persistence work. No other current projects were invented.

## Verification

- Full backend suite: **163 tests passed**, including RAM admission/queue resume,
  SQL counts beyond the default thread-list page, context persistence, automatic
  work records, cross-chat refresh, 30-day rollover, interrupted rollover recovery,
  concurrent updates, malformed metadata preservation, and archive recall.
- Frontend: **49 checks passed**; includes status buckets and removal of the old
  slogan/mini-chat, as well as existing validation and async refresh checks.
- Browser: real dashboard counts 3 total/3 completed; chat entry navigates to
  `#home` with history intact; current-work notes visible in Memory vault.
- Isolated browser fixture: 1 active/1 completed/1 failed, failed-count filtering,
  upcoming schedule navigation, diagnostics expansion, and context meter changing
  after a message (4.3k / 32.8k, estimated).

Remaining provider-specific limits from the previous handoff still apply. No
Google account was connected, no production schedule was created, and no model
process was replaced. Backups: `/private/tmp/claire-clean-ui/original/`.

---

# Current integration status — 2026-11-11, Codex follow-up

This section supersedes the earlier draft-only frontend notes below. The user has
explicitly authorized backend edits. Fable's changes were preserved and extended.

## Completed and verified

- **Central workspace:** one persistent home conversation plus task chats; the
  dashboard keeps its composer on home and shows background tasks alongside it.
  Hide earlier messages is a browser-only view change; there is no frontend
  `/reset` call. User-facing “mission” wording is gone.
- **Controller settings:** now reads `GET /config`, showing real brain/scout/clerk
  roles, context sizes, live availability, task workers, brain slots, scout
  parallelism, and step limit. Removed the ineffective model/delegation draft
  editor. Configuration is explicitly read-only: edit `config.py`, then restart.
  Home and tasks share the brain model with separate conversation contexts.
- **Integrations:** setup forms come from `GET /integrations/providers`; save uses
  `POST /integrations` with `{provider,name,config,secrets}`. Connection details
  expose connect, test, disconnect, and removal. `connect` returns a Google login
  link or tests a non-OAuth connection. Status and discovered operations are real.
  Gmail and Calendar are read-only. API writes remain opt-in and ask each time.
  Secret inputs are not put into localStorage or chat, and closing a setup dialog
  clears its form. Non-secret drafts from the earlier frontend remain migratable.
- **Browser:** the Browser card configures an MCP connector with
  `npx -y @playwright/mcp@latest --user-data-dir=./browser-profile` and
  `keep_alive: true`. Saving does not launch it; Connect & test does. It uses a
  dedicated profile in the connector's sandbox folder. No tools are automatically
  allowed by this UI; tool calls retain approval checks. This is a setup preset,
  not a claim that Playwright is installed or a travel-site flow was tested.
- **MCP session fix:** new boolean `config.keep_alive` is opt-in (old default of
  closing after each call remains). Connections reuse one initialized session,
  serialize calls, and close on disconnect/service shutdown. A failed call drops
  the session and is never automatically replayed. Stdio response timeout now
  has an absolute deadline, so unrelated stdout cannot extend it indefinitely.
- **Scheduler:** create/save paused, enable, pause, run now, remove, and inspect
  run history are connected to Fable's APIs. Each run opens its own task chat.
  Backend validates cron, timezone, and workdir; browser validation gives earlier
  feedback. This is Claire's persisted scheduler, not a list of arbitrary OS cron
  jobs. Claire's service must be running. Offline draft fallback remains.
- **Skills:** the built-in `coding` skill already contained the coding sections
  from `prompts/claire_v2.md` (v1 prompt v2). Added explicit provenance and notes
  explaining that v2 core instructions own verification/reporting/tool schemas.
  Avoid loading the old v1 tool protocol or stale session-memory claims.
  `always: true` is supported through skill upload/edit and loads at task creation.
  Fixed task creation to record these implicit skills in the task metadata too.
- **UI reliability:** concurrent refresh callers now await the same request;
  connector status/operations refresh in the open dialog; schedule history loads
  fresh data; setup forms survive background SSE redraws. App startup runs after
  all page state declarations. Compact laptop layout keeps controller + task board
  side by side, while smaller screens stack them.

## Verification evidence

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t .`:
  **146 tests passed**. Covers scheduler, Google OAuth/refresh/revoke with a fake
  provider, HTTP API calls, local + remote MCP, approvals, sandboxing, keychain,
  skills, persistence, sessions, and service routes. New tests cover persistent
  MCP reuse/disconnect/shutdown, failed-call non-replay, and always-skill API flow.
- `node tests/ui/frontend_checks.cjs`: **37 checks passed**, including concurrent
  refresh joining, unavailable vs failed endpoints, validation, and no reset API.
- **Real local model:** Qwen3.6-35B-A3B-Q4_K_M, isolated state/workspace. Home
  started a coding task; task loaded `coding,verification-report`, reproduced an
  off-by-one bug, edited the implementation, passed both tests, ended its report
  with `CHECKED`, and reported back to home. No production conversation was used.
  Evidence: `/private/tmp/claire-finish/live-result.json`; workspace:
  `/Users/soham/claire-live-verify-k1vr1g7d/project`.
- **Browser UI, disposable service:** saved and tested an HTTP MCP connector,
  discovered `lookup` with its approval label, retested without stale status;
  saved a paused schedule, enabled it (next run displayed), paused it, ran it,
  opened its task chat, and saw completed run history; created an always-loaded
  skill through the editor; checked Google and Playwright setup forms.
- Restarted the real service only after verifying no active/queued/waiting tasks.
  Its model is live. No user account connector or real recurring job was created.

## Remaining boundaries

- Google requires a user-provided OAuth Desktop client and actual account login.
  OAuth mechanics were verified with fixtures, not the user's mailbox/calendar.
- Real Playwright startup/site operation is not yet verified. The preset follows
  official Microsoft documentation, and MCP transport/session behavior is tested;
  individual providers still need their own credentials/dependencies and checks.
  Reference: https://github.com/microsoft/playwright-mcp
- MCP is a basic stdio/streamable-HTTP client, not a promise of compatibility with
  every server. Remote MCP token auth is supported; generic MCP OAuth is not.
- Fable's empty-promise guard retries once. It improves model behavior, but is a
  heuristic, not a guarantee that every natural-language promise starts work.
- Model-role changes are not editable live through the UI. Desktop packaging
  (DMG/Windows installer) remains a separate step; this web frontend is reusable.
- Backups of pre-follow-up files: `/private/tmp/claire-finish/original/`.

---

# Historical handoff (earlier stages; current status above takes precedence)

# Claire frontend ↔ backend handoff

Date: 2026-11-11. Frontend: Codex. Backend: Fable / Claude.
Status: frontend implemented; integration, scheduler, and runtime model configuration services are pending.
This document proposes contracts; it does not claim they already exist in the backend.

## Product decisions agreed with the user

1. One persistent central controller conversation coordinates work. Actionable objectives become separate task contexts; task reports return to the controller. The user can inspect any task chat or ask Claire for a status briefing.
2. The central command center shows the controller conversation and background tasks together. Sending from the overview keeps the user there. A full controller history view remains available.
3. (Superseded 2026-11-11: sir asked for it. "Clear chat" now exists on chats with Claire, behind a confirmation; it removes only that chat's messages, never tasks, schedules, approvals or memory.) Originally: no destructive Clear button. Hide earlier messages is a browser-local view preference. Show earlier messages restores them; no reset API is called.
4. Use task / objective / goal terminology throughout the UI.
5. Integrations should prefer provider sign-in once, with token authentication only where necessary. Browser automation uses a dedicated profile. Users choose access and can disconnect.
6. Scheduler displays actual recurring jobs, their cron/time zone, next/last run, and linked task histories. Drafting a schedule is distinct from activating it.
7. Desktop packaging comes later; current interface remains local, dependency-free HTML/CSS/JS.

## What is implemented now

### Controller and tasks (existing APIs, functional)
- `#overview`: persistent home controller feed (latest 10 visible trace entries) alongside background tasks; full history opens `#home`.
- The overview and panels send to `/threads/home/messages`. Panels return to the overview on send; overview does not jump to a standalone chat.
- `#task/<id>` opens a task's separate conversation; its composer posts directly to that task. Draft inputs are kept separately in memory per thread while navigating.
- `#panel/tasks`: all tasks returned by the existing service, status filters (all/active/completed/attention), execution/chat links, and Ask Claire for an update (prefills controller input, does not silently submit).
- Direct task creation still uses `POST /threads` and opens that task's thread. Asking Claire naturally uses the existing `spawn_task` tool.
- Existing approvals, task cancellation, deep mode, memory, tools, reports, and SSE retained.
- Home history hide cutoff uses event sequence IDs, stored only as a UI preference. Backend messages/events are untouched. No frontend `/reset` call remains.
- The UI cannot recover history already deleted by the previous reset action.

### Integrations (frontend complete; connection execution pending)
Route: `#panel/integrations`.
- Catalogue cards: Gmail, Browser (Playwright), Custom MCP, API service.
- Setup dialogs save only non-secret setup drafts: provider, display name, transport, endpoint/package name.
- Remote addresses must use HTTPS, or HTTP on localhost/127.0.0.1/::1; embedded credentials, query strings, and fragments rejected. Local stdio setup accepts a package name only, never executes it.
- No password/API token input exists while backend setup is unavailable. No credentials stored in localStorage, prompts, vault notes, or logs.
- Explicit labels: setup draft, not connected, connection service not connected. Saving a draft does not connect an account or start a browser/server.
- Optional live read adapter: `GET /integrations`. Live connection status/account/scopes display if the response is supported. Connection details are currently read-only; OAuth, test, revoke and disconnect controls require backend implementation plus frontend wiring.

### Scheduler (frontend complete; job execution pending)
Route: `#panel/scheduler`.
- Optional live read adapter: `GET /schedules`.
- Live rows display job state, objective, cron, explicit IANA time zone, next/last run and reported run history. A run's thread ID opens its task chat.
- Local draft create/edit/remove: title, objective, cron, time zone, optional workdir.
- Presets: daily 09:00, weekdays 09:00, hourly; custom numeric five-field cron supports lists/ranges/steps. No seconds field or named months/weekdays.
- Browser validation checks cron ranges and the time-zone identifier; it does not compute next execution times.
- Drafts always show “Draft · not scheduled” and “Not scheduled” next run. They are not OS cron entries.
- Active/paused counts are unknown (—) when service is unavailable. The frontend cannot discover cron jobs from conversation text or inspect the system crontab.
- Filters: all/active/paused/drafts. No Run now/Pause/Activate mutation is wired yet.

### Controller configuration (frontend draft only)
Route: `#panel/controller`.
- Shows actual controller model/context from `/status` and a clearly conceptual controller/task diagram.
- Local draft: controller model ID, task model ID (empty means inherit), maximum parallel tasks, delegation preference.
- Saving explicitly does not apply settings. No runtime config mutation API exists yet.
- Separate task contexts do not require separately loaded models. Do not display configured worker counts/model roles as live until backend reports them.

## Optional GET response contracts already consumed by frontend

All routes inherit existing local service authentication and origin checks. Never return raw secrets.
404 or 501 means feature unavailable. Other errors are displayed as failed loads, not empty success. Invalid response shapes are errors. Data is fetched on opening the relevant page and through its Refresh button; no continuous polling is added.

### GET /integrations
```json
{
  "integrations": [
    {
      "id": "gmail-personal",
      "provider": "gmail",
      "name": "Personal Gmail",
      "status": "connected",
      "account_label": "user@example.com",
      "scopes": ["Read mail", "Search mail"]
    }
  ]
}
```
Provider IDs used in catalogue: `gmail`, `browser`, `mcp`, `api`; unknown provider entries still render safely.
Suggested states: connected, disconnected, needs_login, error, connecting. Frontend renders reported status, never infers authentication from draft existence.

### GET /schedules
```json
{
  "schedules": [
    {
      "id": "morning-brief",
      "title": "Morning inbox briefing",
      "task": "Summarize unread mail and highlight messages needing a reply.",
      "cron": "0 9 * * 1-5",
      "timezone": "Asia/Kolkata",
      "workdir": "",
      "status": "active",
      "next_run_at": "2026-11-12T03:30:00Z",
      "last_run_at": "2026-11-11T03:30:00Z",
      "runs": [
        {"started_at": "2026-11-11T03:30:00Z", "status": "done", "thread_id": "t1111-example"}
      ]
    }
  ]
}
```
Dates: ISO-8601 with offset or Unix seconds (not milliseconds). Next/last/run timestamps are displayed in the job's reported time zone. Missing/invalid times show “Not reported.” Use exact `active`/`paused` states for their filters.
Jobs created by Claire via a future scheduling tool must appear in this same list; no separate UI-only job store.

## Backend capabilities still needed

### Integrations / credentials
- Trusted integration manager, OAuth authorization-code flow with PKCE as applicable, refresh/revoke/reconnect lifecycle, dedicated browser profile, MCP discovery and call adapter.
- Provider OAuth application registration and consent/scopes. Gmail initial scope is read/search; sending is separately enabled and confirmed.
- Protected OS credential store for tokens. Backend owns credentials, not the model or generic shell tools. Allowlisted connector operations receive credentials internally.
- Treat retrieved email/web/server content as data, not instructions. Tool permissions and consequential-action approvals must be enforced on the backend, not trusted from model text or MCP annotations alone.
- Do not weaken the filesystem sandbox or expose the ordinary personal browser profile to general shell commands to make integration work.
- Proposed mutations (NOT called by this frontend): `POST /integrations` (validated setup), `POST /integrations/{id}/connect`, `/test`, `/disconnect`; secure credential enrollment separate from drafts. OAuth start should return a provider-approved URL and explicit account/scope context.
- Local MCP server execution needs controlled installation/launch configuration and credential scoping; saving a frontend draft never executes its package.

### Scheduler
- Durable schedule/run store, trusted cron parser, explicit time-zone and DST semantics, next-run calculation, activation/pause/resume, run-now, and cancellation policy.
- Dispatch each execution as a task through the existing hub and link it to its schedule. Keep reports in the controller and allow opening task traces.
- Define missed-run behavior, overlap/concurrency, retry limits, idempotency, and required service uptime. Do not create duplicate runs after restart.
- Schedule activation is distinct from saving. Do not turn frontend local drafts into active jobs automatically on first connection.
- Proposed mutations (NOT called by frontend): `POST /schedules`, `POST /schedules/{id}/pause`, `/resume`, `/run`, `DELETE /schedules/{id}`. Agree on action-specific approval before wiring them.
- The requested scheduler could be service-managed rather than literal OS cron; the UI expects the service to report what is actually scheduled. If it wraps OS cron, expose only Claire-managed jobs with clear provenance.

### Controller / agent orchestration
- Existing home tools can spawn tasks, list them, and read their result; the HTTP API already supports direct messages to a task. The home model does not currently have a `send_task_message` tool. Add a validated home-to-task forwarding tool if Claire should direct/follow up with another task herself.
- Preserve controller state across app navigation/restarts. Context compaction should retain durable goals and task references; hiding history must never reset backend context.
- Existing `TASK_WORKERS` controls parallelism; current config is 1. Brain slots are not the same as independently loaded worker models.
- Add runtime configuration read/validate/apply endpoints for model roles, availability, worker concurrency, context limits, and delegation policy. Report actual effective state, distinguish proposed settings, handle active work safely.
- Consider `/threads` pagination and explicit parent/agent/model fields. Current UI can only show tasks returned by the existing list (default limit 50); no claim of a complete lifetime archive.

## Frontend storage and implementation notes
- Browser key: `claire.workspace.v2` (per origin). Contains `hiddenBefore`, `scheduleDrafts`, `integrationDrafts`, `controllerDraft`. It is a convenience store, not secure storage or a backend database. Drafts do not sync across browsers/devices.
- Drafts can include user-entered objectives or folder paths; never put secrets in these fields. No email contents or live task messages are persisted to this key.
- Storage failures are reported; drafts remain in memory for that tab only.
- Onboarding and scheduling dialogs live outside the redrawn view so SSE updates do not erase form edits.
- Backend content is rendered as text/DOM nodes; no dynamic innerHTML. Existing CSP, cookie authentication, static asset routes, and approval flow stay unchanged.
- Per-thread input drafts are in-memory only. Native dialog controls support keyboard focus/validation. Responsive layouts and reduced-motion support retained.
- No backend files changed by Codex. This document supersedes phase-one progress notes where they conflict (especially overview navigation, Clear/reset behavior, and the earlier “no API changes requested” statement).

---

# Backend status — 2026-11-11 (Fable)

The scheduler and integration services now exist. Everything below is implemented
and covered by `tests/` (123 tests) and was run live against Qwen3.6-35B-A3B. The
`GET` shapes this frontend already reads are honoured; fields marked *extra* are
additions it can ignore. No frontend file was changed.

All routes use the existing auth (cookie or bearer token, Host and Origin checks).
Errors are `{"error": "..."}` with 400 (bad input), 404 (no such id) or 403.
The message is written to be shown to the user as is.

## Events to listen for (existing `/events` stream)

| type | when | what to do |
|---|---|---|
| `schedule_changed` (`schedule`: id) | a job was created, paused, resumed, removed, or ran | refetch `GET /schedules` |
| `integrations_changed` | a connector was connected, tested, disconnected or removed | refetch `GET /integrations` |
| `approval_request` with `kind: "schedule"`, `flag: "RECURRING"` | Claire wants to create a recurring job | existing approval card; `title` is multi-line (name, when, blank line, brief), so render line breaks |
| `approval_request` with `kind: "connector"`, `flag: "CHANGES DATA"` | a connector action that changes something outside this machine | existing approval card |

## Scheduler

`GET /schedules` → `{"schedules": [...], "timezone": "Asia/Kolkata"}`. Each schedule is
as proposed above, plus *extra*: `description` (plain reading, e.g. "every weekday at
09:00"), `created_by` (`"user"` or `"claire"`), and `note` on each run. The top-level
`timezone` is this machine's zone, for defaulting the form.

| call | body | returns |
|---|---|---|
| `POST /schedules` | `{title, task, cron, timezone?, workdir?, active?}` | `{schedule}` — saved **paused** unless `active: true` |
| `POST /schedules/{id}/pause` | — | `{schedule}` (`next_run_at` becomes null) |
| `POST /schedules/{id}/resume` | — | `{schedule}` |
| `POST /schedules/{id}/run` | — | `{thread_id, started}`; `started: false` means the previous run is still going |
| `DELETE /schedules/{id}` | — | `{ok}` |

- Schedule `status`: `active` or `paused`.
- Run `status`: the linked task's status (`queued`, `running`, `waiting`, `done`,
  `stopped`, `error`, `cancelled`), or `missed` / `skipped` with `thread_id: null`.
- Cron dialect matches what the frontend validates: five numeric fields, lists,
  ranges, steps. Weekday 0 and 7 are both Sunday. Also enforced server-side: a job
  may not run more often than every 10 minutes; at most 50 active.
- Times are wall-clock in the job's zone. A time skipped by daylight saving does not
  run; a repeated one runs once.
- If the service was down at a due time, the job runs once on return if it is under
  12 hours late (`SCHEDULE_GRACE_HOURS`), otherwise that run is recorded `missed`.
  A job never overlaps itself. A due run is claimed atomically, so a restart cannot
  duplicate it.
- These are Claire's own jobs in her database, not OS cron. They run only while the
  service runs; `claire2 autostart on` starts it at login.
- To activate a local draft: `POST /schedules` with `active: true`. Drafts are never
  picked up automatically.
- Claire creates jobs herself with a `schedule_task` tool when asked to make
  something a habit; those appear in this same list with `created_by: "claire"`,
  and only after the approval above.

## Integrations

Built: `gmail`, `calendar` (both read-only, OAuth with PKCE), `mcp` (stdio and HTTP),
`api`. **Not built: `browser`.** Show the Browser card as unavailable.

`GET /integrations` → the proposed shape plus *extra*: `error` (why it is in the
`error` state), `config` (non-secret settings), `operations` (`[{name, description,
writes}]`, present when connected). `status` is one of `connected`, `disconnected`,
`connecting`, `error`. `needs_login` is not used: an expired sign-in is `error` with
a message saying to connect again.

`GET /integrations/providers` → `{"providers": [{provider, label, summary, oauth,
fields: [{key, label, secret, required}]}]}`. Build setup forms from `fields` rather
than hard-coding them; `secret: true` fields are credentials.

| call | body | returns |
|---|---|---|
| `POST /integrations` | `{provider, name, config: {...}, secrets: {...}}` | `{integration}`, status `disconnected` |
| `POST /integrations/{id}/connect` | — | OAuth kinds: `{auth_url, integration}` (status `connecting`). Others: `{integration}`, already tested |
| `POST /integrations/{id}/test` | — | `{integration}` |
| `POST /integrations/{id}/disconnect` | — | `{integration}`; revokes the token where the provider allows and deletes it locally |
| `DELETE /integrations/{id}` | — | `{ok}`; also deletes every stored credential |

- **OAuth flow:** open `auth_url` in a new tab. The provider returns the browser to
  `GET /oauth/callback` on this service, which finishes the sign-in and shows a short
  "connected, you can close this tab" page. Wait for `integrations_changed`, then
  refetch. The frontend never sees a code or a token.
- **Secrets:** values under `secrets` go straight to the macOS keychain and are never
  returned by any route. If the UI should not handle credentials at all, the same
  setup is available in the terminal: `claire2 connectors add gmail`.
- **Setup fields:** `gmail` / `calendar`: `client_id` (config), `client_secret`
  (secret) from a Google Cloud "Desktop app" OAuth client. `api`: `base_url`, `auth`
  (`bearer` | `header` | `none`), `header_name`, `allow_write`, `test_path`, `notes`,
  secret `token`. `mcp`: `transport` (`stdio` | `http`), `command` (array or string)
  or `url`, `auto_allow` (tool names), `notes`; secrets `token` (http) or `env:NAME`
  (stdio).
- Address rules match the frontend's: HTTPS, or HTTP on this machine; no credentials,
  query or fragment.
- A local MCP server runs inside the sandbox with a folder of its own and is started
  per call. Every MCP tool asks for approval unless it is listed in `auto_allow`.

## Controller

- `GET /config` → the effective state: `roles` (`brain`, `scout`, `clerk`, each with
  `model`, `context`, `live`, `own_model`), `task_workers`, `brain_slots`,
  `scout_parallel`, `sandbox`, `timezone`. **Read-only.** Roles and limits are set in
  `config.py` and take effect on restart; there is no apply endpoint yet, so keep the
  controller draft labelled as not applied.
- Claire now has a `message_task` tool to send a follow-up to an existing task.
- `GET /threads?limit=N` accepts up to 500. Rows already include `parent`.
- Each home turn is stamped with the current time for the model only; the
  `turn_start` event text is still exactly what the user typed.
- `POST /threads/home/reset` still exists and is still destructive; the frontend is
  right not to call it.

## Still not built

Browser connector. Sending mail or creating calendar events. One-off reminders (only
recurring jobs). Applying model or worker changes at runtime. Cursor pagination.

---

# Update — 2026-11-11 evening (Fable)

The user asked for changes that touch the frontend, so three frontend files were
edited by the backend owner this time: `claire_core/web/app.js`, `app.css` and
`index.html`. `tests/ui/drive.mjs` gained checks for each. What changed:

- **Chats.** The "Controller history" page is now **Chats** (`#home`, `#task/<id>`):
  a list of every conversation on the left (the main chat pinned first, then each task
  chat, newest first) and the open one on the right. The conversation scrolls inside
  the page in `#chat-scroll`; `render()` carries its scroll position across redraws.
- **Chat rendering** (`traceNodes` and the helpers above it in `app.js`): the user's
  messages are `.msg.you` on the right, Claire's are `.msg.claire` with an avatar on
  the left. Reasoning and tool steps between messages are folded into one
  `details.work` line (open in task chats, closed in the main chat once finished).
  `spawn_task` renders as a card that opens the new task's chat, a task report as a
  card with the report folded inside, `schedule_task` as a habit card. The command
  center's recent-conversation panel uses the same function.
- **Skills page** (`#panel/skills`): list, read, create, edit, upload a `.md`, remove.
- The new-task dialog has a Skills field. `api()` takes an optional third argument for
  the HTTP method.
- User-facing wording no longer says "dad"; Claire addresses the user as "sir".

## Skills API

| call | body | returns |
|---|---|---|
| `GET /skills` | — | `{"skills": [{name, description, source, size, always}]}`; `source` is `builtin` or `user` |
| `GET /skills/{name}` | — | `{"skill": {...same, text}}` |
| `POST /skills` | `{name, description, text, always?}` or `{filename, text, name?, description?, always?}` | `{skill}`. The second form takes name and description from the file's own `---` header, or its filename and first heading |
| `DELETE /skills/{name}` | — | `{ok}`; 400 for a built-in |

- A user skill with a built-in's name replaces it; deleting it brings the built-in back.
- `always: true` loads the skill into every task without anyone choosing it.
- `POST /threads` accepts `skills: ["coding", ...]`; thread rows now carry `skills`
  (comma-separated) and `parent`.
- New events: `skills_changed` (refetch `GET /skills`), and `answer_discard` (live
  only: drop the answer that was streaming; Claire was sent back to do what she said
  she was doing).
