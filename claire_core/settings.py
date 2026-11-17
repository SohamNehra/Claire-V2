"""Resolved settings: config.py, then environment variables, then flags.

v1 kept these as module globals in claire.py, which meant one conversation per
process. Here they are one object that every session is handed, so several
threads can run side by side with different modes.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        import config
        return config
    except ImportError:
        return None


def _expand(path):
    return os.path.realpath(os.path.expanduser(str(path)))


class Settings:
    def __init__(self, **overrides):
        self.config = cfg = _load_config()

        def conf(name, default):
            return getattr(cfg, name, default) if cfg else default

        def env(name, default, cast=str):
            raw = os.environ.get(name)
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        def flag(name, default):
            raw = os.environ.get(name)
            return default if raw is None or raw == "" else raw == "1"

        # -- model -----------------------------------------------------------
        self.api_base = env("CLAIRE_API", cfg.api_base() if cfg
                            else "http://127.0.0.1:8080/v1")
        self.brain_server = str(conf("BRAIN_SERVER", "llama") or "llama").lower()
        # Qwen3.8 supports low/medium/xhigh; use a balanced thinking mode for
        # ordinary retries rather than sending every minor failure to xhigh.
        effort = env("CLAIRE_LMSTUDIO_REASONING_EFFORT", conf("LMSTUDIO_REASONING_EFFORT", "medium"))
        self.lmstudio_reasoning_effort = effort if effort in ("low", "medium", "xhigh") else "medium"
        self.model = env("CLAIRE_MODEL", conf("MODEL_NAME", "") or (
            conf("LMSTUDIO_MODEL", "") if self.brain_server == "lmstudio" else "") or "")
        self.ctx_limit = env("CLAIRE_CTX", conf("CONTEXT", 0), int)
        self.ctx_ceiling = env("CLAIRE_CTX_CEILING",
                               conf("CONTEXT_CEILING", 262144), int)
        # A small model picks the skills each request needs (skillroute.py).
        self.skill_router = flag("SKILL_ROUTER", True)
        self.max_tokens = 8000
        # 0 is greedy: the same prompt gives the same answer, and a draft model
        # or n-gram lookup has its guesses accepted most often, so it is also
        # the fastest setting on a server that speculates.
        self.temperature = env("CLAIRE_TEMPERATURE", conf("TEMPERATURE", 0.0), float)
        # role -> {"api_base": ..., "model": ...}. A role that is not listed
        # runs on the brain, so the system works with a single model loaded.
        self.roles = {}
        for role, entry in dict(conf("ROLES", {}) or {}).items():
            entry = dict(entry or {})
            # A role given a model file runs on its own local server.
            if (entry.get("gguf") or entry.get("server")=="splash") and entry.get("port") and not entry.get("api_base"):
                entry["api_base"] = f"http://127.0.0.1:{int(entry['port'])}/v1"
            self.roles[role] = entry
        self.scout_parallel = int(conf("SCOUT_PARALLEL", 3))
        self.brain_slots = int(conf("BRAIN_SLOTS", 2))
        self.controller_enabled = flag("CLAIRE_CONTROLLER", bool(conf("CONTROLLER_ENABLED", False)))
        self.vision_role = "vision"
        self.vision_deep_role = "vision_deep"
        self.vision_unload_after_use = bool(conf("VISION_UNLOAD_AFTER_USE", True))
        self.computer_max_steps = max(6, int(conf("COMPUTER_MAX_STEPS", 12)))
        self.controller_role = "talk"
        self.controller_history_chars = max(4000, int(conf("CONTROLLER_HISTORY_CHARS", 12000)))
        self.controller_max_steps = max(2, int(conf("CONTROLLER_MAX_STEPS", 6)))
        self.worker_context = int(conf("WORKER_CONTEXT", 32768))

        # -- agent behaviour -------------------------------------------------
        self.max_steps = env("CLAIRE_MAX_STEPS", conf("MAX_STEPS", 25), int)
        self.compact_at = conf("COMPACT_AT", 0.70)
        self.thinking = flag("CLAIRE_THINKING", conf("THINKING", False))
        self.auto_approve = flag("CLAIRE_YOLO", conf("AUTO_APPROVE", False))
        self.cmd_timeout = env("CLAIRE_CMD_TIMEOUT",
                               conf("COMMAND_TIMEOUT", 120), int)
        self.max_output = env("CLAIRE_MAX_OUTPUT",
                              conf("MAX_OUTPUT_CHARS", 15000), int)
        self.max_file_chars = 100_000
        self.skip_dirs = set(conf("SKIP_DIRS", {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".cache", ".idea"}))
        # Skills a thread started from the terminal begins with. Working in a
        # directory is almost always code.
        self.default_skills = list(conf("DEFAULT_SKILLS", ["coding"]))
        self.prompt_override = (os.environ.get("CLAIRE_PROMPT", "").strip()
                                or str(conf("SYSTEM_PROMPT", "")).strip())

        # -- storage ---------------------------------------------------------
        self.vault = _expand(env("CLAIRE_VAULT", conf("VAULT", "~/claire-vault")))
        self.state_dir = _expand(env("CLAIRE_STATE", conf("STATE_DIR", "~/.claire")))

        # -- safety ----------------------------------------------------------
        # "auto" uses the OS sandbox when there is one. "off" falls back to the
        # v1 pattern guards, and disables running code Claire wrote herself.
        self.sandbox = env("CLAIRE_SANDBOX", conf("SANDBOX", "auto"))
        self.sandbox_extra_write = [_expand(p) for p in
                                    conf("SANDBOX_EXTRA_WRITE", [])]
        self.secret_paths = list(conf("SECRET_PATHS", []))
        self.code_timeout = int(conf("CODE_TIMEOUT", 60))
        # Where connector tokens are kept: the macOS keychain when there is
        # one, otherwise a private file in the state directory.
        self.secret_backend = conf("SECRET_BACKEND", "auto")
        self.connector_timeout = int(conf("CONNECTOR_TIMEOUT", 60))
        self.approval_timeout = int(conf("APPROVAL_TIMEOUT", 900))

        # -- service ---------------------------------------------------------
        self.service_host = "127.0.0.1"          # never anything else
        self.service_port = env("CLAIRE_SERVICE_PORT",
                                conf("SERVICE_PORT", 8765), int)
        self.task_workers = int(conf("TASK_WORKERS", 1))
        self.auto_parallel = bool(conf("AUTO_PARALLEL", True))
        self.max_task_workers = max(1, int(conf("MAX_TASK_WORKERS", 4)))
        self.ram_reserve_gb = max(1., float(conf("RAM_RESERVE_GB", 4)))
        self.task_ram_gb = max(.25, float(conf("TASK_RAM_GB", 2)))
        self.model_runtime_gb = max(1., float(conf("MODEL_RUNTIME_GB", 4)))
        self.model_idle_seconds = max(0, env("CLAIRE_MODEL_IDLE_SECONDS",
            conf("MODEL_IDLE_SECONDS", 1800), float))
        self.schedule_grace_hours = float(conf("SCHEDULE_GRACE_HOURS", 12))
        # Load the main model when the service starts, not on the first message.
        self.preload_model = bool(conf("PRELOAD_MODEL", False))

        self.worker_profiles = dict(conf("WORKER_PROFILES", {}) or {})
        self.worker_profile_configured = conf("WORKER_PROFILE", "")
        self.worker_profile_explicit = bool(overrides.get("worker_profile") or os.environ.get("CLAIRE_WORKER_PROFILE"))
        self.worker_profile = overrides.get("worker_profile") or env("CLAIRE_WORKER_PROFILE", conf("WORKER_PROFILE", ""))
        self.brain_profile = {}
        # A model added in the app and run by llama-server: its file and port.
        self.brain_gguf = None
        self.brain_port = None
        self.model_choice = ""          # the model picked in the app, when one is in effect
        # Explicit custom endpoints/backends remain usable for tests and remote servers.
        custom = any(overrides.get(k) is not None for k in ("brain_server", "api_base"))

        def use_profile(name):
            if name not in self.worker_profiles:
                raise ValueError("Unknown worker profile: " + name)
            self.worker_profile = name
            self.brain_profile = dict(self.worker_profiles[name])
            self.brain_server = self.brain_profile["server"]
            self.api_base = env("CLAIRE_API", f"http://127.0.0.1:{int(self.brain_profile['port'])}/v1")
            self.model = env("CLAIRE_MODEL", self.brain_profile["model"])
            self.ctx_limit = env("CLAIRE_CTX", self.brain_profile.get("context", 32768), int)
            self.brain_slots = int(self.brain_profile.get("slots", 1))

        if self.worker_profile and self.worker_profiles and not custom:
            use_profile(self.worker_profile)

        for key, value in overrides.items():
            if value is not None:
                setattr(self, key, value)

        # The model picked in the app (models.json in the state folder) wins
        # over config.py. A flag, an override or an environment variable that
        # names a model still wins over it. CLAIRE_MODELS=off ignores it.
        explicit = (any(overrides.get(k) is not None for k in
                        ("worker_profile", "brain_server", "api_base", "model", "ctx_limit"))
                    or any(os.environ.get(k) for k in ("CLAIRE_WORKER_PROFILE", "CLAIRE_API", "CLAIRE_MODEL")))
        if not explicit and os.environ.get("CLAIRE_MODELS", "on") != "off":
            from . import models
            chosen = models.load(models.path(self))
            name = chosen["active"]
            entry = chosen["models"].get(name)
            if name in self.worker_profiles:
                use_profile(name)
                self.model_choice = name
            elif entry and entry.get("server") == "llama":
                self.worker_profile, self.brain_profile = "", {}
                self.brain_server = "llama"
                self.brain_gguf = os.path.expanduser(str(entry["path"]))
                self.brain_port = int(entry["port"])
                self.api_base = f"http://127.0.0.1:{self.brain_port}/v1"
                self.model = ""
                self.ctx_limit = int(entry["context"])
                self.model_choice = name
            elif entry and entry.get("server") == "lmstudio":
                self.worker_profile, self.brain_profile = "", {}
                self.brain_server = "lmstudio"
                self.api_base = str(conf("LMSTUDIO_URL", "http://127.0.0.1:1234")).rstrip("/") + "/v1"
                self.model = str(entry["model"])
                self.ctx_limit = int(entry["context"])
                self.model_choice = name

    # Derived locations, all under state_dir so there is one folder to back up
    # and one folder the sandbox has to keep Claire's own tasks out of.
    @property
    def db_path(self):
        return os.path.join(self.state_dir, "claire.db")

    @property
    def workshop_dir(self):
        return os.path.join(self.state_dir, "workshop")

    @property
    def token_path(self):
        return os.path.join(self.state_dir, "token")

    def ensure_state_dir(self):
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        return self.state_dir
