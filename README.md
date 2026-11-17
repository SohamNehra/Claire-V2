# Claire

> **New here? Start with [GETTING_STARTED.md](GETTING_STARTED.md)**: clone,
> pick a model, and have Claire running in your browser in about 15 minutes.

A local coding agent that runs entirely on your own machine. No cloud API, no
account, no per-token cost — it talks to a model you're running yourself via
[llama.cpp](https://github.com/ggerganov/llama.cpp), and it edits files, runs
shell commands, and reports back the same way a terminal coding assistant
would. Python standard library only — zero third-party dependencies.

```
  claire ❯ add tests for utils.py

  ◇ thinking
    Reading utils.py first to see what it actually does.

  ⏺ read_file utils.py 2s
    └ utils.py (lines 1-48 of 48):

  ⏺ write_file test_utils.py 6s
    └ Created test_utils.py (31 lines).

  ⏺ run_command python3 -m pytest test_utils.py 3s
    └ exit=0

  ✔ done 12s
```

Everything it does is confined to the directory you launch it from, and any
shell command it wants to run stops for your approval first (unless you opt
out with `--yolo`).

---

## claire2: the core service (in progress)

`claire.py` above is v1: one file, one conversation, one terminal. `claire2`
runs the same agent on a new core (`claire_core/`) where the loop emits events
instead of printing, so several threads can run at once and any client can
watch them. v1 keeps working unchanged while v2 is built out.

```
claire2                    work in this directory, as v1 does
claire2 "task"             one-shot
claire2 home               talk to Claire in her home thread
claire2 app                the same, in your browser, with everything else
claire2 tasks              task threads and how each ended
claire2 approvals          answer anything a background task is waiting on
claire2 tools              every tool and its health
claire2 inbox              notes Claire has left for you
claire2 bench [brain|scout]   measure a model on Claire's own tasks
claire2 connectors         services Claire can reach: add, connect, test, remove
claire2 schedules          recurring jobs: pause, resume, run, remove
claire2 autostart on|off   start the service when you log in
claire2 skills             instructions Claire can load: add FILE.md, show, remove
claire2 models             the model Claire uses: add FILE.gguf, add-lmstudio KEY, use NAME, remove NAME
claire2 status | serve
claire2 stop [--unload]    stop the service (--unload: and the model, freeing its memory)
```

**Chats and tasks.** In the app there is no main chat: every chat is its own
conversation, started with New chat or by typing on the command center or a
project page. (`claire2 home` keeps one ongoing conversation for the terminal.)
When you ask for real work, Claire starts a task thread with its own context
and working directory, and tells you the outcome in the chat that asked when it
reports back. Chats carry her identity (`identity/` in the vault, yours to
edit); task threads get the working method, their project's memory and the
shared index. The service starts itself in the background and
keeps running so tasks can finish; `claire2 stop` ends it.

**Projects.** Chats and tasks are organised in projects, each a folder of
work with a memory of its own. A task is filed under the project whose folder
it works in; the first task in a new folder (the git repository it sits in,
when there is one) creates that project. OS folders such as Desktop or
Downloads, scratch folders and Claire's own state never become projects on
their own. You can also add a project by hand, with a folder or as a plain
group (Browse… opens the Finder's folder chooser), and move a chat into or out
of one. Claire can do the same when you ask her in a chat ("make a project for
~/code/shop", "delete the shop project", "put this chat in shop"); removing a
project only takes it off the list, and she cannot delete folders. A chat outside every project follows the work across all of them.

Each project's memory is a folder in the vault, `projects/<name>/`:
`PROJECT.md` says what the project is and how work is done there (yours to
edit, in the app or by hand; Claire adds to it with `remember kind=project`),
`work/` is the dated record of what was done, written automatically as each
task starts and ends, and `tasks/` holds every finished task's report. A chat
or task in a project is given that project's `PROJECT.md` and latest work, and
no other project's. `recall` searches everything but ranks the thread's own
project first. The global `work/` log keeps one short line per task across
every project, so any chat and the command center can track several
pieces of work at once. Single-file notes from before (`projects/<name>.md`)
and earlier work records are moved into the project's folder the first time
it is used; tasks from before are filed when the service starts. Removing a
project from the list keeps its chats, tasks and memory folder.

**Shared memory.** Every chat and task is also given `MEMORY.md`, an index
Claire writes from the vault: what she knows about you (`identity/sir.md`),
recent work across all projects, each project, each learned note, recent
sessions and open inbox notes. Each is one line ending with the file that holds
the detail, so a thread knows what exists without carrying it all; `recall`
with that path reads the file in full, and `recall` with words searches. The
index is rewritten from the files whenever they change; edit the files, not
the index. Chats keep the slowly changing part in their system prompt and get
recent work with each turn, so the prompt stays reusable between turns.

**The app.** `claire2 app` opens a local page served by the same service:
a sidebar of projects holding their chats and tasks (as in a code workspace),
a command center showing the work under way in every project, a page per
project with its `PROJECT.md` and recent work, your chats, each
task's step-by-step trace as it happens, approval
requests with Allow and Deny, the inbox, a read-only view of the vault, tool
health, and the report cards. It is one page with no external libraries and
works offline. The link carries the service token once; the page trades it for
an HttpOnly, SameSite=Strict cookie and drops it from the address. Because the
page shows text a model wrote after reading the web, nothing is ever inserted
as HTML and a strict Content-Security-Policy forbids inline script.
`tests/ui/run.sh` drives the app in headless Chrome against a scripted model.

**Safety.** Three layers, strongest first:

1. *OS sandbox* (macOS Seatbelt), enforced by the kernel on every shell command
   and everything it spawns. A task can write only inside its own working
   directory and temp, and cannot read credentials (ssh, cloud, git, keychains,
   browser profiles) or Claire's own state. Programs that hand work to
   something outside the sandbox (`osascript`, `open`, `launchctl`) cannot be
   launched.
2. *Forbidden list.* Commands that could destroy the machine (`rm -rf ~`,
   `sudo`, disk formatting, shutdown) are refused. No approval or mode changes
   that.
3. *Approval.* Dangerous-looking commands always ask, including in `--yolo` and
   in background tasks. A command that needs to reach outside the sandbox must
   say so, and that always asks too. An unanswered request expires as a no.
   Sending output to `/dev/null` or the standard streams is not counted as
   dangerous.

**Human-free mode** (the switch at the bottom of the app's sidebar) lets Claire
go ahead without asking: package installs, commands outside the sandbox,
browser and connector steps, schedules, network access for her own code, and
installing integrations she built. She still asks before anything that deletes
(`rm`, `rmdir`, `git clean`, a connector's delete or remove actions), `git push`
and hard resets, stopping processes, piping a download into a shell, sending,
paying, signing in or uploading, closing a tab you had open, and requests to
local addresses; anything not yet classified asks too. The mode only removes
approvals: the sandbox and the forbidden list are unchanged, only you can switch
it (there is no tool for it), and every request it answers is kept in the
approvals table as `auto` and noted in that thread. It is stored in
`mode.json` in the state folder and survives restarts.

On a machine with no sandbox (Linux, Windows) the core falls back to v1's
pattern guards, and the workshop below is switched off.

**Workshop and self-healing.** Claire can run Python she writes (`run_code`) and
keep working code as a tool (`make_tool`). That code runs under a stricter
profile: it can read the task's folder, write only to a scratch folder, sees
nothing else in your home directory, and has no network unless you approve it.
A tool is registered only after its own test passes in that sandbox, and each
registration is a git commit under `~/.claire/workshop`.

When a tool turns out to be broken, as opposed to misused, Claire is told so
instead of being left to retry it. She gets the result another way, may
register a stand-in, and leaves a note in `inbox/` in the vault with the error,
the arguments and the traceback. Later threads are told which tool is broken
and what to use instead. After you repair it: `claire2 tools reset NAME`.
Built-in tools cannot be replaced from the workshop.

**Skills.** A skill is a markdown file of instructions for one kind of work:
how to review code, how you want reports written, how to research something.
Claire sees the list with a one-line description of each, and a task gets a
skill in one of three ways: the home thread names it when starting the task,
the task loads it itself with `use_skill`, or the skill is marked
`always: true` and every task starts with it. `coding` is built in (it is the
code-specific half of v1's prompt: root cause, scope, tests as the authority,
auditing) and is loaded by default when you run `claire2` in a directory. Yours
live in `skills/` in the vault; add one with `claire2 skills add FILE.md` or in
the app. One of yours with a built-in's name replaces it. Only you can write
skills: there is no tool that lets a model create or change one. Choosing a
skill is the model's judgement and is not perfectly reliable, which is what
`always: true` is for.

Built-in procedures cover `building-software`, `debugging`, `testing`, `uat`,
`researching-apis`, and `reviewing-changes`, alongside `coding`, `integrations`
and `building-integrations`. The integration builder loads API research, testing
and UAT with its coding instructions. UAT distinguishes offline-tested, installed,
connected and accepted against a real account; a passing fixture test alone is
not a live verification. Integrations are hot-loaded after approved installation.

Skill frontmatter uses one-line fields: `when`, comma-separated `requires` and
`kinds` (`agent` and/or `home`), and `steps` (0–150). Uploads and body edits preserve
these fields. Dependencies are loaded for explicitly named skills too; cycles
are deduplicated. Available descriptions remain visible if automatic routing
fails. New skills and edits are picked up on the next turn without restarting.
Changes to Claire's Python engine require restarting the service.

For LM Studio's Qwen3.8 27B, normal mode sends `reasoning_effort: none`;
explicit/deeper thinking defaults to `medium`. Set
`CLAIRE_LMSTUDIO_REASONING_EFFORT=low|medium|xhigh` to choose its depth. `low` still enables reasoning on this
model. Other model mappings are unchanged. Token-limit truncation is recorded
and never executed as a partial tool call.

Run `python3 -m tests.live_integration_eval` for a disposable local-model
integration-repair exercise with independent acceptance cases. It uses synthetic
API data and fresh state, and does not install anything or read saved credentials.
Use `--api URL --model ID` to evaluate a different local configuration.

Integration checks require each advertised action to succeed through
`claire_kit.call` in offline tests and verify function arguments against the
manifest. Tests must still assert the request and response contract: action
coverage alone does not establish correctness. Credentialed builds must declare
a valid read-only connection check. The kit's fixture transport honors allowed
HTTP errors and preserves empty JSON request bodies just like real requests.

**Models.** The app's **Models** page (or `claire2 models`) adds a model by its
`.gguf` file (Browse… opens the Finder) or from LM Studio's library, reads the
file's own trained context, and switches Claire to it: **Restart Claire now**
unloads the old model, freeing its memory, and starts a fresh service on the new
one (it waits while any chat or task is working). Added models and the choice are
kept in `models.json` in the state folder, which wins over `config.py`; a
command-line flag or environment variable still wins over both, and
`CLAIRE_MODELS=off` ignores it. An added llama.cpp model gets its own
`llama-server` on port 8090 and up.

**One model.** The uncensored Qwen3.8 27B, through Splash 1.6.1, holds the
conversation (the `talk` role: bounded local file actions and delegation) and
does the coding tasks. It stays loaded. The Qwen3.5 9B that used to hold the
conversation is configured but not loaded; one line in `ROLES` brings it back.
Its Splash server serves the model's native 262,144-token context (raised from
32,768 on 2026-11-17: the model is hybrid, only every fourth layer keeps a KV
cache, so the full window costs about 8 GiB of int8 cache, allocated as it is
used, inside the 36 GiB ceiling). Conversations still send a bounded recent
window, for speed. Named `standard27` and `fast35` worker profiles are also available; see
[CLAIRE_SPLASH_UPGRADE.md](CLAIRE_SPLASH_UPGRADE.md) for why one model, what it
costs, model selection, lifecycle, installation, rollback and measured speed.
Replies are generated at temperature 0 (`TEMPERATURE` in `config.py`). The controller uses
schema-constrained JSON decisions, a bounded conversation window, and on-demand
history retrieval. Full conversations remain stored. The worker receives the
specific brief, relevant skills and worker tools, without the home persona or
global diary. Its default file reads are 120-line windows; explicit ranges remain
available.

`CONTROLLER_ENABLED`, `CONTROLLER_HISTORY_CHARS`, `CONTROLLER_MAX_STEPS`,
`WORKER_CONTEXT`, `MODEL_IDLE_SECONDS`, `PRELOAD_MODEL` and `ROLES` in `config.py` control this setup.
`MAX_TASK_WORKERS = 1` prioritizes interactive latency; additional coding tasks
queue. The explicit deep-mode toggle selects the strong model for that chat.
Worker reasoning remains stable throughout each turn to preserve the prompt
cache. A failed controller can hand off the original request with observed
attempts; refusals and denied permissions never trigger a bypass.

Run the actual new paths with:

```sh
python3 -m tests.live_controller_eval   # HTTP conversation/action/routing checks
python3 -m tests.live_worker_eval       # independent coding outcomes
python3 -m tests.live_pipeline_eval     # controller → worker → report, chat during work
python3 -m tests.live_integration_eval  # independent integration acceptance cases
```

These use disposable state and fixtures. The controller evaluation does not run
the integration/research tasks it dispatches. The older `claire2 bench` remains
available for comparisons with its legacy prompt. See `CLAIRE_ROUTING_REVIEW.md`
for measurements, the model choice and practical limits.

**Habits.** Ask Claire to make something a habit ("every weekday at nine, give
me a brief") and she creates a recurring job, after you approve it. Each run is
an ordinary task thread that keeps its result in its own chat, listed with its
project and on the command center. Jobs are hers, kept in her own
database and listed by `claire2 schedules` and in the app; nothing is added to
the system crontab. They run only while the service is running, so
`claire2 autostart on` starts it at login. A job that came due while the service
was down runs once when it returns if it is under 12 hours late; a job never
overlaps its own previous run; and a job cannot fire more often than every ten
minutes.

**Connectors.** `claire2 connectors add gmail|calendar|mcp|api` connects an
outside service. Gmail and Calendar sign in through your browser and are
read-only. MCP reaches anything that has an MCP server, local or remote. `api`
is any HTTPS API with a token. The model never holds a credential: it names a
connector and an action, and the backend attaches the token, which lives in the
macOS keychain where nothing Claire runs can read it. A connector can only talk
to the host it was set up for. Reading goes ahead; anything that changes data
outside this machine asks you first, and every MCP tool asks unless you listed
it as allowed. What comes back is marked as outside data, not instructions.
Gmail and Calendar need an OAuth client of your own from the Google Cloud
console ("Desktop app").

**Roles and scouts.** `ROLES` in `config.py` maps each job to a model: the
brain plans and writes, a scout reads, a clerk summarises. A role with no entry
runs on the brain, so one model is enough to start. The `scout` tool hands
files or a web page to the scout model with one question and returns a short
cited report, so the text itself never enters the brain's context; several
bundles are read at once. Give a role a `gguf` and a `port` and its server is
started on first use.

**Report card.** `claire2 bench` runs a model through Claire's own loop on
small fixed tasks with checkable outcomes (a failing test, a docstring the code
does not honour, a rename with a decoy, a question about a file that does not
exist, a data task, a one-line change, a question about an unfamiliar project)
and records pass or fail, steps, malformed replies and tokens per second.
`claire2 bench scout` does the same for the scout role. Use `--api URL` to test
a model before touching `config.py`, and `claire2 bench --history` to compare.
The task prompts never contain what the checks look for; keep it that way when
adding tasks to `claire_core/bench.py`.

**Layout.** One module per job in `claire_core/`: `engine.py` (the loop),
`protocol.py` (XML parsing), `llm.py` (model roles), `guard.py` (sandbox and
command rules), `vault.py` (memory), `projects.py` (projects and where work is filed), `store.py` (threads, approvals, tool
health), `hub.py` and `service.py` (threads and the local API), `render.py` and
`cli.py` (the terminal), `web/` (the app). A new built-in tool is one file in
`claire_core/tools/` with a `TOOLS` list, plus its doc in
`claire_core/prompts/tools/`; the system prompt's tool section is generated.

Tests: `python3 -m unittest discover -s tests -t .`

---

## Requirements

- **Python 3.9+**
- **A GGUF model file.** Anything compatible with llama.cpp works. This was
  built and tested against `Qwen3.8-27B-Uncensored-Q4_K_M.gguf`, but any
  reasonably capable instruct/coder model will work — smaller models will be
  less reliable at following the tool-call format.
- **llama.cpp**, specifically `llama-server` — the part of llama.cpp that
  serves an OpenAI-compatible API. This is the only supported backend: it's
  the one that honours per-request "thinking on/off," which Claire depends on
  to stay fast on simple steps and only pay for deep reasoning when a step
  actually fails. LM Studio can serve the same model but ignores that flag
  ([bug #1990](https://github.com/lmstudio-ai/lmstudio-bug-tracker/issues/1990)),
  so reasoning mode can't be toggled per-request there.

---

## 1. Clone it

```bash
git clone <your-repo-url> claire
cd claire
```

## 2. Get llama-server running

### macOS

```bash
brew install llama.cpp
```

This installs `llama-server` on your `PATH`, which is all the launcher script
needs — it starts the server for you.

### Windows

You have two options. **WSL is the easier, better-supported path** — Claire's
launcher script is bash, and the sticky status bar and colored output rely on
ANSI terminal escapes that a real POSIX terminal (or Windows Terminal) handles
correctly.

**Option A — WSL (recommended)**

1. Install WSL if you don't have it: `wsl --install` from an admin PowerShell,
   then restart.
2. Inside the WSL shell, follow the **macOS/Linux steps on this page exactly**
   — install llama.cpp there (build from source, or grab a Linux release from
   the [llama.cpp releases page](https://github.com/ggml-org/llama.cpp/releases)),
   clone the repo inside WSL's filesystem, and run `claire` as normal.

**Option B — native Windows (PowerShell / cmd), no WSL**

1. Download a Windows build from the
   [llama.cpp releases page](https://github.com/ggml-org/llama.cpp/releases)
   (a `...-bin-win-*.zip` containing `llama-server.exe`), or build it yourself
   with CMake. Put it somewhere on your `PATH`.
2. The `claire` launcher script is bash and won't run natively here, so start
   `llama-server` yourself instead of relying on autostart:

   ```powershell
   llama-server.exe --jinja -m C:\path\to\your-model.gguf -c 0 -ngl 999 --port 8080 --host 127.0.0.1
   ```

3. Run the agent directly with Python (see step 4 below) instead of via the
   `claire` script.
4. Use **Windows Terminal**, not the legacy `cmd.exe` console — the sticky
   status bar and colored output need real ANSI escape support, which
   Windows Terminal has on by default and the legacy console may not.

## 3. Point it at your model

Open `config.py` and set `MODEL_GGUF` to your model's path:

```python
MODEL_GGUF = "~/models/your-model.gguf"     # macOS/Linux/WSL, ~ is expanded
MODEL_GGUF = "C:/models/your-model.gguf"    # native Windows, forward slashes are fine
```

That's the one line most people need to change. Everything else in
`config.py` has a documented, sensible default — context window, GPU
offload, host/port, timeouts, where memory is stored. Run:

```bash
python3 config.py
```

at any time to see what every setting currently resolves to, and whether the
model file was actually found.

## 4. Run it

**macOS / Linux / WSL** — from the project directory:

```bash
./claire                       # interactive
./claire "add tests for utils.py"    # one-shot
```

The `claire` script starts `llama-server` automatically if nothing is
already listening on the configured port, then runs the agent. To run it
from any directory without the `./`, symlink it onto your `PATH`:

```bash
ln -s "$(pwd)/claire" ~/.local/bin/claire
```

Then `cd` into whatever project you want it working on and just run `claire`
— it can only read and write inside the directory you launch it from.

**Native Windows (no WSL)** — with `llama-server.exe` already running from
step 2:

```powershell
python claire.py                       # interactive
python claire.py "add tests for utils.py"   # one-shot
```

There's no autostart on this path, so start the server first each time (or
leave it running in its own window).

---

## Usage

```
claire                    interactive session
claire "task"             one-shot: run the task, print the result, exit
claire --yolo "task"      skip approval for shell commands (dangerous ones still prompt)
claire --thinking "task"  start in deep-reasoning mode (slower, smarter)
claire --model ID         use a different model than the one currently loaded
```

Interactive-only commands:

| Command | Effect |
|---|---|
| `/t` | Toggle thinking mode (fast ↔ deep) |
| `/y` | Toggle yolo mode (approval on ↔ off) |
| `reset` | Clear the conversation, keep long-term memory |
| `exit` / `quit` | Quit |
| Ctrl-C | Interrupt the current task; the session stays open |

Environment variables override `config.py` for a single run without editing
the file: `CLAIRE_API`, `CLAIRE_MODEL`, `CLAIRE_CTX`, `CLAIRE_THINKING=1`,
`CLAIRE_YOLO=1`, `CLAIRE_VAULT`. For example:

```bash
CLAIRE_CTX=65536 claire      # cap context for this run only
```

---

## Safety

- **File access is confined** to the directory you launched Claire from —
  every path is resolved and checked; `../` escapes are refused.
- **Shell commands stop for approval** by default (arrow keys or `y`/`n` to
  answer, Enter confirms the highlighted choice). `--yolo` skips this, except
  for commands matching a always-prompt pattern (`rm -rf`, `sudo`, `git push`,
  `git reset --hard`, `curl | sh`, and similar) — those prompt no matter what.
- Under `--yolo`, a command that would write **outside** the project
  directory is refused outright rather than silently allowed, since nothing
  can prompt you to catch it.
- These are pattern matches, not a real sandbox. For genuinely untrusted
  work, run Claire inside a VM or container.
- If you're running an uncensored model, treat `--yolo` accordingly — use it
  in throwaway directories and clean git working trees, not on anything you
  can't restore.

---

## Memory

Claire keeps long-term notes as plain markdown in `~/claire-vault` (override
with `CLAIRE_VAULT` or the `VAULT` setting in `config.py`). Open that folder
as an Obsidian vault to read, edit, or delete anything she's remembered —
she picks up your edits on the next run.

---

## Troubleshooting

Run `python3 config.py` first — it shows every setting, what it resolves to,
whether the model file exists, and whether a server is currently reachable.

| Symptom | Likely cause |
|---|---|
| `ERROR: model not found at: ...` | Fix `MODEL_GGUF` in `config.py` |
| `llama-server is not on your PATH` | Install llama.cpp, or set `AUTOSTART_SERVER = False` and run the server yourself |
| Hangs / empty replies with `--thinking` off | Some backends ignore the "disable reasoning" flag — this project targets `llama-server` specifically for that reason |
| Windows: sticky status bar looks broken/garbled | Use Windows Terminal, not the legacy `cmd.exe` console |
| `cannot reach the model server` | `llama-server` isn't running, or `API_BASE`/`HOST`/`PORT` in `config.py` don't match where it's listening |

Directory locations and access are supplied by the runtime. Claire can inspect
non-secret directory names without asking you to spell Desktop's absolute path.
File changes retain their target and permission checks. Unsupported directory
access claims can get one compact 27B specialist check; ordinary conversation
returns to 9B on the next turn. Coding handoffs retain an existing project even
if the controller omits the optional workdir argument. See
[the navigation review](CLAIRE_NAVIGATION_REVIEW.md) and
`tests/navigation_eval_results.json` for measured results and limitations.

**Hide previous chats and tasks.** The sidebar has separate Hide previous /
Show previous controls for conversations (under Chats) and finished tasks
(under Projects; it applies inside every project); the Tasks page has the same
task control. Visibility is remembered in this browser. The open chat,
active work and approvals stay visible. New conversations and new activity in a
hidden conversation appear automatically. Hiding changes the view, retaining
stored conversations, task results and Claire's context.

Idle sleep: after 30 minutes with no user activity and no queued/running work,
Claire unloads her configured local models from RAM. Commands, tool calls and
approval waits keep her awake until they finish. The service and scheduler stay
running; a new message or due scheduled job reloads the model it needs. Browsing
and typing in the app resets the idle timer, but background status polling does
not. Merely viewing the app does not reload a sleeping model. Chats, memory and
schedules remain on disk. Change `MODEL_IDLE_SECONDS` in `config.py` (or
`CLAIRE_MODEL_IDLE_SECONDS` in the service environment); `0` disables sleep.
Remote and externally managed model endpoints are left running. LM Studio uses
its [model unload API](https://lmstudio.ai/docs/developer/rest/unload); standalone
llama-server processes must exactly match Claire's configured command.


**Computer use.** The `computer` tool operates allowed native Mac apps using
scoped observations and control references. Browser tab creation/navigation and
closure return verified receipts and track the tab this conversation created.
Changed windows or tab lists stop the action. Native Accessibility reads avoid
the System Events UI-tree bridge; keyboard/menu fallbacks remain available.
A visible control lease keeps the display awake while GUI work runs and releases
that assertion when the task ends.

Visual questions use a separate, image-capable local model on demand: `vision`
is the economical default; `vision_deep` is optional and requires enough free
RAM. The text controller receives its observation rather than unreadable image
bytes. Newly loaded vision models unload after use; coding stays on its existing
model. Set `ROLES`, `VISION_UNLOAD_AFTER_USE`, and `COMPUTER_MAX_STEPS` in
`config.py`. See [the computer-use review](CLAIRE_COMPUTER_UPGRADE.md) for the
contracts, measured checks, and remaining limits.
