# Computer-use upgrade

The previous control path asked a text model to interpret connector manuals and
assemble keyboard actions. It could deny a configured capability, lose the tab
it created, send unreadable images to a text model, or claim a click after filling
a field. The new path supplies typed actions, scoped observations, and receipts.

## Runtime

- Conversation stays on the resident Qwen3.5 9B; coding stays on the existing
  uncensored Qwen3.8 27B. This change does not replace either model.
- `computer` is available to the controller and workers. Its controller schema
  requires action-specific fields, including both coordinates for visual clicks.
- Native macOS Accessibility reads expose actual windows and control trees,
  including Chromium accessibility activation and bounded AX message timeouts.
  Process IDs and window IDs bind observations and tab operations to an instance.
  Ambiguous instances fail explicitly. Keyboard shortcuts use a small fixed JXA adapter, checking the exact foreground
  window through CoreGraphics before sending input. Tab state and verification
  use direct AX reads; existing menu operations retain their JXA adapter.
  An optional macOS AX-to-CG window bridge supplies exact window IDs when
  AXWindowNumber is absent; conservative window metadata is the fallback.
- `new_tab(url)` verifies insertion and address; `close_tab(tab)` checks window,
  complete tab identity list, and the tracked index. Background title changes
  do not invalidate an otherwise unchanged tab identity. Only conversation-owned tabs bypass
  the existing-tab closure approval. Partial writes are reported as uncertain;
  they must be inspected before retrying.
- `inspect` returns expiring, conversation-scoped control references. Controls
  are checked for matching path, role, label and enabled state before mutation.
  Changed window IDs are checked again in the native action itself.
- `look` loads the installed Qwen3.5 4B vision specialist through LM Studio,
  captures only the selected window after loading, and returns an image-derived
  observation. `quality=deep` requests the installed vision-enabled 27B model
  when memory admission allows. Images never go to the text controller.
- The specialist receives a current visual question, not instructions to execute
  a future click. Action receipts and a later observation establish what actually
  happened. Model-generated visual descriptions are not execution receipts.
- Only vision models loaded by the inspection are unloaded afterward. An already
  loaded model remains loaded. Insufficient RAM never ejects the coding model.
  Activity leases protect active inspections from the 30-minute idle unload.
- The existing allow-list, major-action approvals, and visible control frame
  remain. The frame's process holds a display-awake assertion during GUI work;
  the assertion is released automatically when that process exits.
- Controller completion checks reject unsupported action claims, including a
  claim that Save was clicked when only `set_text` ran. Controller history has
  an explicit prompt budget; GUI work can use 12 steps without raising the cap
  for ordinary conversation. Reset/deletion invalidates scoped UI references.

## Validation

Automated coverage includes tab ownership, duplicate close, changed windows and
lists, denied approvals, stale references, URL validation, actual image payloads,
vision capability and memory admission, model cleanup, action schemas, and native
window-selection regressions. Run:

```sh
python3 -m unittest discover -q
node tests/ui/frontend_checks.cjs
node --check claire_core/connectors/mac_control.js
node --check claire_core/connectors/mac_overlay.js
```

Real workflows use the actual resident model and native Chrome in disposable
tabs. Existing tabs are preserved, form saves are checked by the fixture server,
and visual clicks are checked by a canvas hit handler rather than the model's
answer alone:

```sh
python3 -m tests.live_computer_eval
python3 -m tests.live_computer_uat
```

These are explicit live tests, not part of unit discovery. They require a usable
macOS desktop, allowed Chrome access, and the installed local models. They refuse
to compete with pending Claire work. Their temporary artifacts are kept outside
the repository. Final measured results are recorded below.

## Measured results

On this Mac with the actual resident Qwen3.5 9B controller and installed 4B
vision model:

| Check | Result | Time |
|---|---|---|
| Google new tab → verified address → close owned tab | Passed; seven existing tabs preserved; three model calls | 6.771 s |
| Fill project name → Save → verify fixture server value | Passed; five model calls | 25.966 s |
| Read blue filled rectangle and unseen code 5926 | Passed; actual screenshot; two model calls | 10.124 s |
| Locate orange circle → click → read ACTIVATED 5926 | Passed; canvas server confirmed the click; four model calls | 20.473 s |
| Python regression suite | 428 tests passed | about 34 s |
| Frontend checks | 66 passed | — |

The final visual test used `look → click_at → look` with no failed calls. The
larger 27B vision role was not benchmarked: memory admission correctly refused
loading it alongside the apps resident during testing. These are individual
local runs, not latency guarantees; model warmth, website response and browser
state affect timing. A previous System Events-only read path took 43.467 s in
a different, 27-tab window. That comparison motivated native reads but is not
a controlled speed ratio. Real-test artifacts remain in `/private/tmp`.

## Limits

This improves control reliability and recovery; it does not give a small local
model frontier-model reasoning. Vision can still misread colors, text or target
positions. Structured references are preferable when available; visual actions
need a fresh screenshot and subsequent verification. The larger vision model
is optional and can be refused when current RAM cannot accommodate it.
OS permission failures and unavailable desktops are reported rather than retried
through a coding model. A cold model wake-up still includes model-loading time.
