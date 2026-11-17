# Current status — simplified UI and shared project memory

2026-11-11. The user approved the new, simpler UI and requested a separate
recent/old work log across chats. Earlier dashboard descriptions below are history.

- [x] Remove slogan, mini-chat, repeated task list, and excess dashboard copy.
- [x] Keep rotating Claire core, chat entry, task totals, active work, and Up next.
- [x] Move technical tools/reports/repair notes under System Monitor details.
- [x] Real context-use meter, task totals, and RAM/worker capacity.
- [x] RAM-aware worker admission and checks before starting configured models.
- [x] Verify existing chat storage: 30 messages retained across service restart.
- [x] Create `~/claire-vault/work/recent.md` and `work/old.md`, separate from identity.
- [x] Automatic task-work updates and cross-chat prompt refresh.
- [x] Automatic 30-day rollover with searchable archive and interruption recovery.
- [x] Seed the real Claire 2 project; expose both work files in Memory vault.
- [x] 179 backend tests; 49 frontend checks; browser navigation/state checks.
- [x] Responsive review at 1280×720 and 390×844; no mobile horizontal overflow.
- [x] Final service restarted while idle; chat history preserved; test service stopped.

Current capacity limit: the running brain server has 2 slots, allowing 1 task
while reserving 1 for chat. This update does not spawn duplicate model copies.
Real Google login, Playwright-site verification, and native installers remain
separate outstanding items. See the latest section of FRONTEND_BACKEND_HANDOFF.md.

---

# Current progress — 2026-11-11

**Status: implemented and verified; account/provider-specific live checks remain.**
The user has lifted the frontend-only restriction and authorized backend edits.

- [x] Preserve Fable/Claude's backend and conversation UI work.
- [x] Connect Integrations and Scheduler to their real APIs.
- [x] Show actual controller/model configuration instead of an unapplied draft.
- [x] Keep central conversation, background task chats, and task status visible.
- [x] Preserve conversation history when hiding earlier messages.
- [x] Verify v1 prompt v2's coding skill; document its source and harness adaptation.
- [x] Fix always-loaded skill metadata on newly created tasks.
- [x] Add persistent MCP sessions for stateful tools, with serialized access and cleanup.
- [x] Fix UI refresh races and stale connection/run state.
- [x] Backend regression suite: 146 passed; frontend checks: 37 passed.
- [x] Real local-model delegation → coding fix → tests → report → controller passed.
- [x] Browser verification of MCP setup/test, scheduler lifecycle/run history,
      task-chat navigation, and skill creation.
- [x] Responsive check: controller fits at 1280×720; mobile navigation works at
      390px with no horizontal overflow. Final browser error log is empty.
- [ ] Google login against the user's real account (needs OAuth setup and consent).
- [x] Real Playwright browsing workflow: navigate, type, click and read verified against real Chrome through the `browser` connector. Chrome opens for a task and closes when it ends, fails or is cancelled. A full run driven by the local model is still unverified.
- [ ] Native desktop installer / packaging (separate scope).

See `FRONTEND_BACKEND_HANDOFF.md` for exact APIs, design decisions, evidence,
changed behavior, and remaining limits. Earlier progress below is retained as
history; its “frontend only” and “backend pending” statements are superseded.

---

# Claire frontend progress

Updated: 2026-11-11
Owner: Codex (frontend); Claude / Fable retains backend ownership.
Status: Complete — frontend installed, browser-verified, and running.

## Scope
- User authorized replacing the existing frontend with a polished Jarvis-inspired interface.
- Only `claire_core/web/index.html`, `app.css`, `app.js`, frontend tests if needed, and this handoff document are in scope.
- No changes to Python/backend, configuration, model setup, APIs, authentication, sandboxing, or existing user data.
- Existing working tree already contains backend work. Do not revert or overwrite it.

## Design decisions
- Dark navy / cyan command center, retaining the Claire identity.
- Overview with an animated core, actual service/model state, mission summaries, and actionable shortcuts.
- Conversation remains accessible as a separate operation log; existing thread URLs remain compatible.
- All telemetry comes from existing endpoints/events. No invented performance charts or unsupported controls.
- Existing approvals, task creation, stop, deep mode, memory, tools, and model reports remain functional.
- No external assets or dependencies; keep CSP-safe DOM rendering and offline operation.
- Responsive layout, keyboard focus, and reduced-motion support.
- Frontend remains suitable for later embedding in a desktop application.

## Progress
- [x] Reviewed frontend, service contract, and existing UI test harness.
- [x] Preserved original frontend in a temporary backup outside the repository.
- [x] Implement command center and visual system.
- [x] Verify existing flows and responsive layouts.
- [x] Open the updated app and record validation / remaining limitations.

## Backend coordination
No API changes requested. Keep static routes `/`, `/app.css`, `/app.js` and existing JSON/SSE contracts compatible. Additional runtime telemetry can be discussed separately; frontend will not pretend missing telemetry is available.

## Implementation notes
- Default `/` now opens command center; conversation is `#home`. Existing `#task/<id>` and `#panel/<name>` routes are preserved. Bare `#` now opens overview.
- Overview composer posts to existing home thread then opens its execution/conversation log. No new backend routes.
- Core animation stays mounted during SSE updates. Decorative engine bars are explicitly labeled as activity, not utilization.
- Added keyboard command focus (Cmd/Ctrl+K), explicit disconnected/model standby states, mobile approval count, task history retry, and rollback of send UI state on request failure.
- Approval cards now remain visible until the approval API succeeds.
- Dark theme is intentional regardless of OS theme. SVG assets are inline/static; model content still uses textContent.

## Final files changed by Codex
- `claire_core/web/index.html`: command-center shell, local SVG core/icons, overview template, system monitor, mission dialog, accessible mobile controls.
- `claire_core/web/app.css`: navy/cyan visual system; desktop, laptop, tablet, and mobile layouts; state animations; reduced-motion rule; focus styling.
- `claire_core/web/app.js`: overview rendering from existing state, mission/approval summaries, dashboard-to-home command submission, routing, keyboard shortcut, responsive drawer isolation, and frontend request error handling.
- `tests/ui/drive.mjs`: three new overview assertions and navigation to conversation before the existing conversation checks.
- `FRONTEND_PROGRESS.md`: this handoff.

## Validation
- JavaScript syntax checks passed for `app.js` and `tests/ui/drive.mjs`.
- 25 browser checks passed using the existing isolated `tests/ui/demo_server.py` fixture through the browser tool (listed below).
- Checked widths 320, 390, 1024, 1280, and 1440 with no page-level horizontal overflow. Reviewed real live app plus populated fixture.
- No browser JavaScript errors or warnings observed in the fixture.
- Existing full headless runner and backend unit suite were not rerun in this pass. The headless runner was updated for the intentional default-route change.
- Real application verified connected at port 8765 with its actual model state. No messages or missions submitted to the real application during frontend testing.

### Browser checks
- PASS: overview shows real mission and approval counts
- PASS: waiting core state
- PASS: token removed from URL
- PASS: conversation history and safe markdown render
- PASS: task brief, five steps, and failed step visible
- PASS: capabilities list and failure health
- PASS: inbox opens memory note
- PASS: model reports retained
- PASS: 390px layout has no horizontal overflow
- PASS: mobile sidebar hidden initially
- PASS: mobile menu opens
- PASS: mobile activity drawer opens
- PASS: approval denial updates dashboard and tab title live
- PASS: mission dialog accessible and fits mobile
- PASS: empty mission objective is blocked
- PASS: overview command opens conversation
- PASS: user and model markup remains inert text
- PASS: live task report arrives in conversation
- PASS: deep reasoning toggle persists API result
- PASS: 1440px layout has no horizontal overflow
- PASS: 1024px layout has no horizontal overflow
- PASS: 320px layout has no horizontal overflow
- PASS: Escape closes drawers and hidden navigation is inert
- PASS: command keyboard shortcut focuses input
- PASS: mission creation opens new task and renders backend error state

## Known boundaries / next owner notes
- The extra test mission successfully created a task and opened its trace. The scripted fixture had exhausted its canned task replies, so it returned `IndexError: pop from empty list`; frontend displayed the error correctly. This was not a live-model inference test.
- Model readiness and tool health use existing `/status` and refresh/event behavior. No CPU/GPU/RAM utilization endpoint exists in this UI; the engine bars are decorative state indicators, explicitly labeled as such while processing.
- The available mission list remains limited by the existing `/threads` response; overview shows the four most recent, prioritizing active missions.
- Closing/opening mobile drawers uses inert to remove hidden navigation from keyboard access. Escape and backdrop close them; Cmd/Ctrl+K focuses command input.
- Short windows scroll the central workspace while keeping the command interface visible.
- No Python, model, sandbox, authentication, backend API, configuration, or backend-owned documentation changes were made by Codex.
- No desktop wrapper or installer added. This frontend can be embedded later.
- Original frontend backup: `/private/tmp/claire-frontend-20261111/original/` (temporary; not a permanent release backup).
- Screenshot: `/Users/soham/.codex/visualizations/2026/11/11/01a1c9b9-324c-7e82-8d42-416c2a9898a0/claire-command-center.jpg`.

## Follow-up: controller workspace, integrations, scheduler (in progress)
User requested frontend-only expansion; Fable continues to own backend implementation.
- Replace user-facing mission terminology with tasks / objectives / goals.
- Keep the ongoing controller conversation in the central command center alongside background tasks.
- Remove destructive Clear/reset UI. Any history hiding must be local presentation only, with restore.
- Add Tasks, Integrations, Scheduler, and Controller settings pages.
- Integrations: OAuth-first connection design, browser sessions, custom MCP/API setup; no secrets in frontend storage.
- Scheduler: real job state when an API exists; explicit local drafts when unavailable. Frontend never creates OS cron jobs.
- Controller configuration drafts must not imply they changed the actual running model or delegation policy.
- Detailed backend contracts and current versus pending behavior will be recorded in FRONTEND_BACKEND_HANDOFF.md.
