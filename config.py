"""Claire's configuration. Edit this file; you should not need to touch claire.py.

Precedence, highest first:

    1. command line flags   (--model, --thinking, --yolo)
    2. environment variables (CLAIRE_MODEL, CLAIRE_CTX, ...)
    3. this file
    4. what the server reports about the model it actually loaded

Every value below is read by both `claire.py` and the `claire` launcher script,
so there is exactly one place to change when you switch models.
"""

import os

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

# Path to the .gguf weights the launcher should load. This is the one line most
# people need to change. ~ is expanded.
#
# The uncensored 27B stays the default coding/deep worker. The resident 9B
# talk/scout/clerk role below is unchanged. See CLAIRE_SPLASH_UPGRADE.md.
MODEL_GGUF = "~/.lmstudio/models/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
BRAIN_SERVER = "splash"
# Kept for the image specialists and for an explicit LM Studio fallback.
LMSTUDIO_MODEL = "qwen3.8-27b-uncensored"
LMSTUDIO_URL = "http://127.0.0.1:1234"
MODEL_MMPROJ = ""

# Display name shown in the header and sent as the API `model` field.
#   ""    -> ask the server what it loaded  <-- recommended
#   "..." -> force this label
# Leave it empty. A hardcoded name goes stale the moment you change MODEL_GGUF,
# and then the header confidently reports the wrong model. Run
# `python3 config.py` to see what "" currently resolves to.
MODEL_NAME = ""

# Context window in tokens.
#   0     -> use the model's own trained maximum  <-- recommended
#   N     -> force N, even if the model was not trained that long
# Going above the trained maximum needs RoPE scaling and degrades recall. Set
# too high by hand and compaction never fires, so every request overruns the
# window; too low and you waste the model. 0 cannot be wrong.
CONTEXT = 0

# Safety rail for CONTEXT = 0. A model advertising a huge window would otherwise
# try to allocate a KV cache larger than your RAM at startup. Raise it if you
# have the memory. As a rough guide, this model costs ~9.6 GB of KV cache at
# 262144 tokens with q8_0, and cost scales linearly with context.
CONTEXT_CEILING = 262144

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = 8080

# Full API base URL.
#   ""    -> http://HOST:PORT/v1, built from the two values above
#   "..." -> use this instead, e.g. LM Studio on :1234, ollama, a remote box
# Empty keeps HOST/PORT as the single source of truth; setting both is how they
# end up contradicting each other.
API_BASE = ""

# When true the launcher starts llama-server if nothing answers on PORT.
# Set false if you start the server yourself (LM Studio, ollama, a remote box).
AUTOSTART_SERVER = True

# Extra llama-server flags.
GPU_LAYERS = 999          # -ngl. 999 offloads everything it can.
KV_CACHE_TYPE = "q8_0"    # -ctk/-ctv. q8_0 roughly halves cache memory.
EXTRA_SERVER_ARGS = []    # e.g. ["--rope-scaling", "yarn", "--rope-scale", "2"]
SERVER_LOG = "~/.claire-server.log"

# Pinned local runtime and drafts, installed once, outside temporary storage.
SPLASH_HOME = "~/.local/share/claire/splash"
SPLASH_ROOT = SPLASH_HOME + "/runtime/1.6.1"
WORKER_PROFILE = "uncensored27"  # or standard27 / fast35; restart Claire after editing
# CLAIRE_WORKER_PROFILE or --worker-profile selects a profile for one process.
# These are text/coding profiles. The existing vision specialists stay separate.
# "prefix_cache" is the disk quota for prompt state Splash keeps across restarts
# (under the state directory); "0" turns it off.
WORKER_PROFILES = {
    # 262144 is this model's native window. It is hybrid (a KV cache on every
    # fourth layer only), so the full window is ~8 GiB of int8 cache, allocated
    # as it is used, inside max_memory. Raised from 32768 on 2026-11-17.
    "uncensored27": {"server": "splash", "model": "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF:Q4_K_M",
        "path": MODEL_GGUF, "draft": SPLASH_HOME + "/drafts/27b",
        "port": 8080, "context": 262144, "max_memory": "36G",
        "prefix_cache": "2G"},
    "standard27": {"server": "splash", "model": "lmstudio-community/Qwen3.8-27B-MLX-5bit",
        "path": "~/.lmstudio/models/lmstudio-community/Qwen3.8-27B-MLX-5bit",
        "draft": SPLASH_HOME + "/drafts/27b", "port": 8082,
        "context": 32768, "max_memory": "36G", "prefix_cache": "2G"},
    "fast35": {"server": "splash", "model": "lmstudio-community/Qwen3.6-35B-A3B-GGUF:Q4_K_M",
        "path": "~/.lmstudio/models/lmstudio-community/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-Q4_K_M.gguf",
        "draft": SPLASH_HOME + "/drafts/35b", "port": 8083,
        "context": 32768, "max_memory": "36G", "prefix_cache": "2G"},
}

# ---------------------------------------------------------------------------
# Agent behaviour
# ---------------------------------------------------------------------------

# Which system prompt to use.
#   ""            -> the prompt built into claire.py
#   "claire_v2"   -> prompts/claire_v2.md
#   "/some/path"  -> that file
SYSTEM_PROMPT = ""

MAX_STEPS = 25            # hard cap on the agent loop for one task
COMPACT_AT = 0.70         # summarise older turns past this share of the context
COMMAND_TIMEOUT = 120     # seconds before run_command is killed
MAX_OUTPUT_CHARS = 15000  # truncate tool output past this

TEMPERATURE = 0           # greedy and repeatable; also what speculation is fastest at
THINKING = False          # start in deep-reasoning mode
AUTO_APPROVE = False      # start in yolo mode (skip command approval)

# Directories never walked by list_files / search_files.
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", ".idea"}

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

VAULT = "~/claire-vault"           # long-term notes
HISTORY_FILE = "~/.claire_history"  # readline history


# ---------------------------------------------------------------------------
# Core service (claire2)
# ---------------------------------------------------------------------------

# Which model does which job. A role that is not listed runs on the brain, so
# everything works with just the one model loaded.
#
#   brain   plans, writes code, talks to you        MODEL_GGUF above
#   scout   reads files or a page, answers one      a 4B-9B model is plenty
#           question about them (the scout tool)
#   clerk   summarises old turns when the context   a 3B-4B model is plenty
#           fills up
#
# Give a role a model file and a port and Claire starts a server for it the
# first time it is needed:
#
#   ROLES = {
#       "scout": {"gguf": "~/models/Qwen3.5-4B-Q4_K_M.gguf", "port": 8081,
#                 "context": 32768, "slots": 4},
#   }
#
# Or point a role at a server you run yourself, on this machine or another:
#
#   ROLES = {"scout": {"api_base": "http://192.168.1.20:8080/v1"}}
#
# Before promoting a model to a role, measure it: `claire2 bench brain` or
# `claire2 bench scout`, with --api to test a server without touching this file.
#
# 2026-11-16: the 27B worker is the conversation model too, so talk, scout and
# clerk are not listed and run on it. The 9B was there because the 27B made
# you wait; on Splash it no longer does. Measured at temperature 0, the wait
# before the first word of a reply in conversation is 1-2 s and stays there as
# the chat grows (see CLAIRE_SPLASH_UPGRADE.md).
#
# To bring the 9B back for conversation and background reading, add
#     "talk": dict(_SMALL), "scout": _SMALL, "clerk": _SMALL,
# to ROLES below. Its first word comes in about 0.6 s; its answers are weaker.
#
# "args" are extra llama-server flags for that role's server. ngram-simple drafts
# the next tokens from text already in the conversation, and the model checks
# them, so copied text comes out about twice as fast (rewriting a file 60 ->
# 127 tok/s) and every answer matched the server without it. ngram-mod changed
# one answer in that test and "ngram-mod,ngram-simple" stalled for minutes.
_SMALL = {"gguf": "~/.lmstudio/models/bartowski/Qwen_Qwen3.5-9B-GGUF/Qwen_Qwen3.5-9B-Q4_K_M.gguf",
          "port": 8081, "context": 131072, "slots": 4,
          "args": ["--spec-type", "ngram-simple"]}
ROLES = {
         **{name: dict(profile) for name, profile in WORKER_PROFILES.items()},
         "vision": {"server":"lmstudio", "api_base":"http://127.0.0.1:1234/v1",
                    "model":"qwen_qwen3.5-4b", "context":8192},
         "vision_deep": {"server":"lmstudio", "api_base":"http://127.0.0.1:1234/v1",
                         "model":"qwen/qwen3.8-27b", "context":8192}}
# Images are inspected on demand; release a specialist loaded for each use.
VISION_UNLOAD_AFTER_USE = True
COMPUTER_MAX_STEPS = 12
CONTROLLER_ENABLED = True
CONTROLLER_HISTORY_CHARS = 10000
CONTROLLER_MAX_STEPS = 6
WORKER_CONTEXT = 32768
MAX_TASK_WORKERS = 1  # prioritize interactive latency; additional coding tasks queue

# How many bundles of files the scout tool reads at once. Only faster than 1
# when the scout's server has that many slots.
SCOUT_PARALLEL = 3

# Slots on the brain. Two lets the home thread answer while a task is working.
# Each slot gets an equal share of CONTEXT, so 2 slots at 262144 is 131072 each.
BRAIN_SLOTS = 2

SERVICE_PORT = 8765       # the local service, bound to 127.0.0.1 only
TASK_WORKERS = 1          # task threads running at once
# Seconds without user activity or pending work before local models are
# unloaded; 0 keeps them loaded. The 27B takes about 16 s to load, which is the
# wait this setup exists to remove, so it stays loaded (about 20 GiB) and
# PRELOAD_MODEL loads it when the service starts instead of on the first message.
# The service/scheduler stay alive either way. 30 * 60 restores the old idle sleep.
MODEL_IDLE_SECONDS = 0
PRELOAD_MODEL = True
STATE_DIR = "~/.claire"   # threads, approvals, the workshop, the service token

# "auto" runs every shell command, and all code Claire writes herself, inside
# the OS sandbox (macOS). "off" falls back to v1's pattern guards and disables
# run_code and self-built tools entirely.
SANDBOX = "auto"

# Extra folders a task's shell commands may write to, beyond its own working
# directory and temp. Package caches are the usual reason, e.g. ["~/.npm"].
SANDBOX_EXTRA_WRITE = []

# Extra paths (relative to ~, or absolute) nothing Claire runs may read. The
# built-in list already covers ssh, cloud and git credentials, keychains and
# browser profiles; see SECRET_PATHS in claire_core/guard.py.
SECRET_PATHS = []

CODE_TIMEOUT = 60         # seconds before run_code or a workshop tool is killed
APPROVAL_TIMEOUT = 900    # seconds a background task waits for you, then is denied

# A scheduled job that came due while the service was not running still runs
# when it comes back, once, if it is no later than this. Older than that, the
# run is recorded as missed. Start the service at login with
# `claire2 autostart on` so this rarely matters.
SCHEDULE_GRACE_HOURS = 12

# Where connector tokens are kept: "auto" uses the macOS keychain when there
# is one, and otherwise a private file in STATE_DIR. "file" forces the file.
SECRET_BACKEND = "auto"
CONNECTOR_TIMEOUT = 60    # seconds before a connector call gives up

# ---------------------------------------------------------------------------
# Plumbing. Nothing below here is meant to be edited.
# ---------------------------------------------------------------------------

def _path(value):
    return os.path.expanduser(str(value)) if value else value


def api_base():
    if not API_BASE and BRAIN_SERVER == "splash":
        name = os.environ.get("CLAIRE_WORKER_PROFILE") or WORKER_PROFILE
        if name not in WORKER_PROFILES:
            raise ValueError("Unknown worker profile: " + name)
        profile = WORKER_PROFILES[name]
        return f"http://127.0.0.1:{profile['port']}/v1"
    if not API_BASE and BRAIN_SERVER == "lmstudio":
        return LMSTUDIO_URL.rstrip("/") + "/v1"
    return API_BASE or f"http://{HOST}:{PORT}/v1"


def as_shell():
    """Emit the values the launcher needs, as shell assignments.

    The launcher runs `eval "$(python3 config.py --sh)"` so that bash and Python
    can never disagree about which model or port is in play.
    """
    quoted = lambda v: "'" + str(v).replace("'", "'\\''") + "'"
    lines = [
        f"CLAIRE_MODEL_GGUF={quoted(_path(MODEL_GGUF))}",
        f"CLAIRE_HOST={quoted(HOST)}",
        f"CLAIRE_PORT={quoted(PORT)}",
        f"CLAIRE_CTX_CONF={quoted(CONTEXT)}",
        f"CLAIRE_CTX_CEILING={quoted(CONTEXT_CEILING)}",
        f"CLAIRE_NGL={quoted(GPU_LAYERS)}",
        f"CLAIRE_KV={quoted(KV_CACHE_TYPE)}",
        f"CLAIRE_SERVER_LOG={quoted(_path(SERVER_LOG))}",
        f"CLAIRE_AUTOSTART={quoted('1' if AUTOSTART_SERVER else '0')}",
        f"CLAIRE_EXTRA_ARGS={quoted(' '.join(EXTRA_SERVER_ARGS))}",
    ]
    return "\n".join(lines)


def probe_server(api_base=None, timeout=5):
    """Ask the server what it actually loaded. Returns {} if it isn't up.

    Shared with claire.py so there is one implementation of "what is really
    running", rather than two that can drift.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{api_base or api_base_default()}/models",
                                    timeout=timeout) as resp:
            entry = (json.load(resp).get("data") or [{}])[0]
    except Exception:
        return {}
    meta = dict(entry.get("meta") or {})
    if entry.get("context_length"):
        meta["n_ctx"] = int(entry["context_length"])
    meta["id"] = entry.get("id") or ""
    return meta


def api_base_default():
    return api_base()


def resolved():
    """What each setting actually evaluates to right now.

    Several fields are deliberately blank, meaning "derive this" -- blank is the
    correct value, not a missing one. This makes that visible instead of
    leaving the reader to guess.
    """
    meta = probe_server()
    live = bool(meta)
    name = MODEL_NAME or (os.path.basename(meta.get("id", "")).replace(".gguf", "")
                          if live else "?")
    trained = meta.get("n_ctx_train") or 0
    ctx = CONTEXT or min(meta.get("n_ctx") or trained or CONTEXT_CEILING, CONTEXT_CEILING)
    rows = [
        ("MODEL_GGUF", MODEL_GGUF, _path(MODEL_GGUF), "exists"
         if os.path.exists(_path(MODEL_GGUF)) else "MISSING"),
        ("MODEL_NAME", MODEL_NAME, name,
         "from server" if not MODEL_NAME and live else
         "server not running" if not MODEL_NAME else "forced"),
        ("CONTEXT", CONTEXT, ctx,
         f"model trained for {trained:,}" if (not CONTEXT and trained)
         else "capped by CONTEXT_CEILING" if not CONTEXT else "forced"),
        ("API_BASE", API_BASE, api_base(),
         "from HOST:PORT" if not API_BASE else "forced"),
        ("SYSTEM_PROMPT", SYSTEM_PROMPT, SYSTEM_PROMPT or "built-in",
         "compiled into claire.py" if not SYSTEM_PROMPT else "from prompts/"),
        ("EXTRA_SERVER_ARGS", EXTRA_SERVER_ARGS,
         " ".join(EXTRA_SERVER_ARGS) or "(none)", ""),
        ("VAULT", VAULT, _path(VAULT), ""),
    ]
    extra = []
    if live:
        if meta.get("n_params"):
            extra.append(f"parameters   {meta['n_params']:,}")
        if meta.get("size"):
            extra.append(f"file size    {meta['size'] / 2**30:.1f} GiB")
        if meta.get("ftype"):
            extra.append(f"quantisation {meta['ftype']}")
    return rows, extra, live


if __name__ == "__main__":
    import sys
    if "--sh" in sys.argv:
        print(as_shell())
        raise SystemExit(0)

    rows, extra, live = resolved()
    print("\nClaire configuration\n")
    print(f"  {'setting':<18} {'in config.py':<14} {'effective':<34} why")
    print("  " + "-" * 84)
    for key, raw, eff, why in rows:
        shown = '""' if raw == "" else ("[]" if raw == [] else str(raw))
        if len(str(eff)) > 33:
            eff = "..." + str(eff)[-30:]
        print(f"  {key:<18} {shown[:13]:<14} {str(eff):<34} {why}")
    print()
    if live:
        print("  loaded model")
        for line in extra:
            print(f"    {line}")
    else:
        print("  server is not running, so nothing could be detected.")
        print("  start it with `claire`, or set AUTOSTART_SERVER/API_BASE.")
    print('\n  A blank setting means "derive this" and is the recommended value.\n')


# Pin the status bar directly above the prompt and wipe it on submit, so it never
# accumulates in your scrollback. Set False for dumb terminals or if you want the
# old always-printed behaviour. Ignored when stdout is not a TTY.
STICKY_STATUS = True
