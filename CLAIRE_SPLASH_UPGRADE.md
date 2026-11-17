# Claire's Splash worker integration

JonathanColetti's uncensored Qwen3.8 27B Q4_K_M runs through the pinned Splash
1.6.1 runtime and, since 2026-11-16, is the one model for conversation, coding
and background reading. The Qwen3.5 9B that used to hold the conversation is no
longer loaded; it stays configured in `config.py` (`_SMALL`) and one line in
`ROLES` brings it back. The existing LM Studio image specialists remain available.

## One model for conversation and work

The 9B existed because the 27B made you wait. Measured on this Mac at
temperature 0 with the 27B as the conversation model:

| | Wait before the first word | Whole reply |
|---|---|---|
| Nothing else running | 1.7-2.1 s, the same at turn 2 and turn 12 | 2.3-3.2 s |
| While a coding task runs | 2.1-7.7 s | 2.4-5.7 s, once 8.5 s |
| First message after the model is loaded from disk | add about 16 s | |

Three things make that hold:

- **The conversation view is primed.** Its last message, the runtime facts, is
  rewritten for every call. Splash resumes only from the end of a prompt it has
  processed, so a view sharing all but its last message with the previous one
  reused nothing past the system prompt and the whole chat was read again:
  2-2.6 s and growing with every message. `LLM.prime` sends the view without
  the runtime facts first, for one token, which leaves a resume point there.
- **Facts that never change are in the system prompt** (`standing_facts`), read
  once. The "Current capability" line stays in the runtime facts: moved up, the
  27B failed the live navigation check `recovery_false_promise`.
- **The model stays loaded** (`MODEL_IDLE_SECONDS = 0`) and is loaded when the
  service starts (`PRELOAD_MODEL`). It holds about 20 GiB.
- **The system prompt is read at startup, twice.** Splash keeps a resume point
  at the end of a shared beginning only after two different prompts have
  branched there, so the first two messages after any change to the system
  prompt each waited about 10 s. Preload sends two one-token requests instead.
  From an empty cache the service is loaded and warm 23 s after it starts and
  the first "hi dude" is answered in 1.7 s.

What one model costs:

- A chat request waits behind a long read the coding task is doing: about 6 s
  per request while another request reads thousands of new tokens, against
  0.5 s alone. `--decode-share` and `--disable-ane` do not change that (the
  second makes it 9-14 s). A coding task starting with nothing cached reads its
  5K-token prompt for 15-18 s and holds chat up for that long.
- The 9B answered its first word in 0.6 s and never waited on the worker.
- `vision_deep` is a second 27B in LM Studio. With the main model always
  loaded it will usually be refused for memory; the 4B `vision` role still fits.

Checked on the 27B as conversation model, 2026-11-16: `live_controller_eval`
22/22, `live_navigation_eval` 12/12, `live_file_read_eval` 3/3,
`live_pipeline_eval`, `live_computer_eval`.

## Available workers

| Profile | Existing target | Endpoint |
|---|---|---|
| `uncensored27` (default) | JonathanColetti uncensored 27B Q4_K_M GGUF | `127.0.0.1:8080` |
| `standard27` | lmstudio-community standard 27B MLX 5-bit | `127.0.0.1:8082` |
| `fast35` | lmstudio-community Qwen3.6 35B-A3B Q4_K_M GGUF | `127.0.0.1:8083` |

The selected profile serves conversation as well as coding. Enabling `fast35` does not promote
it to the default or establish that it is as capable as the dense 27B.

Choose a worker for a terminal task:

```sh
claire2 --worker-profile standard27 "implement the change and run the tests"
claire2 --worker-profile fast35 "implement the change and run the tests"
```

To change the app's worker, edit `WORKER_PROFILE` in `config.py` and restart
Claire after current tasks finish. A foreground service can instead select it
with `claire2 serve --worker-profile standard27`; `CLAIRE_WORKER_PROFILE` is the
equivalent environment override. A running service keeps its current settings.
The model configuration endpoint reports the active profile and available
profiles. Models load on demand, rather than all being loaded at service boot.

## Lifecycle and serving limits

- Each Splash worker serves a **32,768-token context per request**, with a
  **36 GiB memory ceiling**. Since 2026-11-17 the `uncensored27` profile serves
  the model's native **262,144 tokens**: its architecture keeps a KV cache on
  only every fourth layer, so the whole window is about 8 GiB of int8 cache,
  allocated as used. Claire reads the advertised live context rather
  than falling back to the model's larger trained context. The worker prompt
  compaction and token truncation protections remain in force.
- `--verify-rows all` is used for every profile, including the 35B MoE. The
  DFlash2 drafts are trained companions; the target checkpoint stays the same.
- Both normal (`reasoning_effort=none`) and thinking (`medium`) requests are
  supported. The existing XML tool protocol and permission checks are unchanged.
- Claire owns the idle timer. Splash's independent idle release is disabled.
  `MODEL_IDLE_SECONDS` is 0, so models stay loaded; set to a number of seconds,
  managed servers stop only after all work has been idle that long and the next
  request reloads its model. Passive status polls do not keep it awake.
- RAM admission needs the target file size x 1.25 plus the runtime allowance,
  on top of the OS reserve. Measured on the 27B Q4_K_M (15.7 GiB file): 19 GiB
  loaded with its draft, 22 GiB after 14K-token prompts. Available memory is
  the kernel's free-memory level (what `memory_pressure` reports). Another
  active worker is never evicted to load a new one.
- With the 9B and the 27B both loaded beside a normal set of desktop apps, this
  64 GB Mac had about 13 GiB reclaimable and macOS reported memory pressure
  "warning". Without the 9B there is about 8 GiB more.
- Each profile keeps up to `prefix_cache` (2 GiB) of prompt state on disk under
  the state directory (`cache/splash-PORT`), across restarts. A worker reloaded
  after idle unload then answers its first request in 0.2 s instead of reading
  its 4K-token system prompt again for 10 s. `"prefix_cache": "0"` turns it off.
- A live evaluation (`tests/live_*_eval.py`) runs on throwaway state and stops
  the Splash worker it started when it exits. It cannot share the installed
  service's worker: run it while that worker is unloaded, or it reports the
  port as taken and leaves the worker alone.
- Persistent ownership records use the process start time/command and Splash's
  HTTP instance ID. They allow adoption after a Claire restart and prevent idle
  unloading from stopping unrelated listeners or a reused PID. Startup failures
  are reported with the log path and do not silently fall back to another model.

Logs are under `~/.claire/logs/splash-PORT.log`. Ownership records are under
`~/.claire/model-servers/`. These follow `CLAIRE_STATE` for disposable tests.

## Persistent installation

The runtime and trained drafts live under `~/.local/share/claire/splash/`.
Target models continue to live in their existing LM Studio model folders; they
are reused without modifying or copying them. Runtime startup works offline.
Only Splash's `install/paths.py` is adapted to store its writable data under the
persistent installation. The runtime is pinned rather than automatically updated.

The one-time installer verifies the pinned release archive and both draft
weight SHA-256 digests before installing:

```sh
python3 scripts/install_splash_runtime.py \
  --archive /path/to/splash-1.6.1-arm64-macos26.tar.gz \
  --draft-27b /path/to/Qwen3.8-27B-DFlash2 \
  --draft-35b /path/to/Qwen3.6-35B-A3B-DFlash2
```

The source release is [Splash 1.6.1](https://github.com/incoai/splash/releases/tag/1.6.1).
Drafts are [27B DFlash2](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2) and
[35B DFlash2](https://huggingface.co/incoai/Qwen3.6-35B-A3B-DFlash2).

## Verification and limits

`tests/test_splash.py` covers routing, metadata, thinking flags, ownership,
PID/HTTP-instance replacement, startup failure, and deduplicated idle unload.
`scripts/verify_splash.py --worker-checks --output /path/to/results` exercises
the real runtime with disposable projects/state: startup, actual generation,
normal/thinking mode, adoption by a separate Claire process, idle unload, and
reload on the next request. The focused worker checks use the existing seven
independent coding assertions; their transcripts and pass/fail results are saved.

The earlier matched 27B benchmark measured about 29 to 51 chat tokens/s and
490 to 238 seconds for the seven-task coding suite. The target GGUF was identical.
Those are measured workloads, not guaranteed rates for arbitrary contexts,
images, concurrent models, or reasoning. A small passing suite does not establish
universal quality equivalence between inference engines.

## Measured speed (2026-11-16, temperature 0, thinking off)

`python3 scripts/bench_speed.py --api URL --backend splash|llama` measures a
running server: decode speed on code, prose and a file rewrite, how fast a long
new prompt is read, the wait when only its end is new, and parallel requests.

| | 27B on Splash | 9B on llama-server |
|---|---|---|
| New code | 77-119 tok/s | 48-63 tok/s |
| Prose | 47-67 tok/s | 47-60 tok/s |
| Rewriting a file | 100-134 tok/s | 105-127 tok/s (60 without `ngram-simple`) |
| Reading a long new prompt | 330-350 tok/s | 530-640 tok/s |
| First token, 14K-token prompt already cached | 0.5 s | 0.2 s |
| Two requests at once, combined | 90 tok/s (no gain) | 60 tok/s |

The ranges are the same server measured hours apart: this Mac ran both models
about 25% slower late in a long session with memory pressure at "warning".
Within one server launch the figures repeat to within 2%.

Neither server reuses a reply it generated when that reply comes back as
history: the chat template writes a past reply differently from a new one, so
the next request reads it again. Long outputs therefore cost about a third
more on the 27B. Changing that means changing the prompt format.

For rollback, wait for active work to finish, unload Claire's managed models,
set `WORKER_PROFILE = ""` and `BRAIN_SERVER = "lmstudio"`, then restart Claire.
The original uncensored model and `LMSTUDIO_MODEL` setting are retained. Saved
conversations and the vault do not require migration.
