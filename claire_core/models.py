"""Models sir adds from the app (or `claire2 models`), and which one Claire uses.

`config.py` says what Claire runs out of the box. Models added here are kept in
`models.json` in the state folder, with the choice of which to use, and that
choice wins over `config.py` from the next start (an environment variable or a
command-line flag still wins over both). Nothing here edits `config.py`.

A model runs on one of:
    llama   llama.cpp's llama-server, started by Claire, from a .gguf file
    lmstudio  a model in LM Studio's library, loaded through LM Studio
    splash  a profile in config.py's WORKER_PROFILES (configured by hand)
"""

import json
import os
import re
import shutil
import struct
import threading
import urllib.request

FILE = "models.json"
FIRST_PORT = 8090
DEFAULT_CONTEXT = 32768
MIN_CONTEXT = 2048
_lock = threading.Lock()


class ModelError(Exception):
    pass


def path(settings):
    return os.path.join(settings.state_dir, FILE)


def load(settings_or_path):
    target = settings_or_path if isinstance(settings_or_path, str) else path(settings_or_path)
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"active": "", "models": {}}
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    active = data.get("active") if isinstance(data.get("active"), str) else ""
    return {"active": active, "models": {k: v for k, v in models.items() if isinstance(v, dict)}}


def _save(settings, data):
    target = path(settings)
    os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
    temporary = target + ".writing"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(temporary, target)


# -- reading a model file ------------------------------------------------------

_SCALAR = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


def gguf_info(file_path):
    """What a .gguf file says about itself: architecture, name, trained
    context, size. Reads the header only; raises ModelError if it is not a
    GGUF file."""
    try:
        size = os.path.getsize(file_path)
        f = open(file_path, "rb")
    except OSError as exc:
        raise ModelError(f"cannot read {file_path}: {exc.strerror or exc}")
    with f:
        if f.read(4) != b"GGUF":
            raise ModelError("that file is not a GGUF model (.gguf)")

        def read(fmt):
            raw = f.read(struct.calcsize(fmt))
            if len(raw) != struct.calcsize(fmt):
                raise ModelError("the model file ends early; it may be incomplete")
            return struct.unpack("<" + fmt, raw)[0]

        def string():
            n = read("Q")
            if n > 1 << 24:
                raise ModelError("the model file's header is damaged")
            return f.read(n).decode("utf-8", "replace")

        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                inner, count = read("I"), read("Q")
                if inner == 8:
                    for _ in range(count):
                        string()
                elif inner in _SCALAR:
                    f.seek(struct.calcsize(_SCALAR[inner]) * count, 1)
                else:
                    raise ModelError("the model file's header is damaged")
                return None
            if kind not in _SCALAR:
                raise ModelError("the model file's header is damaged")
            return read(_SCALAR[kind])

        read("I")                                    # version
        read("Q")                                    # tensor count
        found = {}
        for _ in range(min(read("Q"), 100000)):
            key = string()
            found[key] = value(read("I"))
            arch = found.get("general.architecture")
            if arch and f"{arch}.context_length" in found and "general.name" in found:
                break
    arch = found.get("general.architecture") or ""
    return {"architecture": arch, "name": found.get("general.name") or "",
            "context_length": int(found.get(f"{arch}.context_length") or 0),
            "size_bytes": size}


# -- what is available -----------------------------------------------------------

def _tilde(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p and p.startswith(home) else p


def _splash_ready(settings, profile):
    import sys
    if sys.platform == "win32":
        return "Splash requires macOS; choose an LM Studio model on Windows"
    root = os.path.expanduser(str(getattr(settings.config, "SPLASH_ROOT", "") or ""))
    if not root or not os.path.isfile(os.path.join(root, "engine", "splash")):
        return "Splash is not installed (see CLAIRE_SPLASH_UPGRADE.md)"
    if not os.path.exists(os.path.expanduser(str(profile.get("path") or ""))):
        return "the model file is missing"
    return ""


def _problem(settings, entry):
    if entry["server"] == "llama":
        if not os.path.isfile(os.path.expanduser(entry.get("path") or "")):
            return "the model file is missing"
        if not shutil.which("llama-server"):
            return "llama-server is not installed (brew install llama.cpp)"
    return ""


def current(settings):
    """The name of the model these settings run: an added model, a Splash
    profile, or "config" for config.py's own model."""
    if getattr(settings, "model_choice", ""):
        return settings.model_choice
    if settings.brain_server == "splash" and settings.worker_profile:
        return settings.worker_profile
    return "config"


def catalogue(settings, running=None):
    """Every model Claire could use, for the app and `claire2 models`.
    `running` is the name of the model the service is using now."""
    data = load(settings)
    cfg = settings.config
    rows = []
    server = str(getattr(cfg, "BRAIN_SERVER", "llama") or "llama").lower() if cfg else "llama"
    if server != "splash":
        if server == "lmstudio":
            where = str(getattr(cfg, "LMSTUDIO_MODEL", "") or "")
        else:
            where = _tilde(os.path.expanduser(str(getattr(cfg, "MODEL_GGUF", "") or "")))
        rows.append({"name": "config", "label": "From config.py", "server": server, "where": where,
                     "context": int(getattr(cfg, "CONTEXT", 0) or 0) or None, "source": "config",
                     "problem": ""})
    for name, profile in settings.worker_profiles.items():
        rows.append({"name": name, "label": name, "server": "splash",
                     "where": profile.get("model") or _tilde(str(profile.get("path") or "")),
                     "context": int(profile.get("context") or 0) or None, "source": "config",
                     "problem": _splash_ready(settings, profile)})
    for name, entry in data["models"].items():
        rows.append({"name": name, "label": entry.get("label") or name, "server": entry["server"],
                     "where": entry.get("model") if entry["server"] == "lmstudio" else _tilde(entry.get("path") or ""),
                     "context": int(entry.get("context") or 0) or None, "source": "added",
                     "port": entry.get("port"), "problem": _problem(settings, entry)})
    chosen = data["active"] if data["active"] in {r["name"] for r in rows} else ""
    default = (settings.worker_profile_configured if server == "splash" else "config")
    for r in rows:
        r["selected"] = r["name"] == (chosen or default)
        r["running"] = r["name"] == running
    return {"models": rows, "selected": chosen or default, "running": running}


def lmstudio_library(settings, timeout=3):
    """Model keys LM Studio has downloaded, or None when it is not running."""
    url = str(getattr(settings.config, "LMSTUDIO_URL", "http://127.0.0.1:1234")).rstrip("/")
    try:
        with urllib.request.urlopen(f"{url}/api/v1/models", timeout=timeout) as r:
            models = json.load(r).get("models") or []
    except Exception:
        return None
    return [{"key": m.get("key"), "name": m.get("display_name") or m.get("key"),
             "context": m.get("max_context_length")}
            for m in models if m.get("key") and m.get("type", "llm") == "llm"]


# -- changing ------------------------------------------------------------------

def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "model"


def _ports_in_use(settings, data):
    cfg = settings.config
    used = {int(getattr(cfg, "PORT", 8080) or 8080), int(settings.service_port)}
    for entry in list(settings.worker_profiles.values()) + list(settings.roles.values()) + list(data["models"].values()):
        if entry.get("port"):
            used.add(int(entry["port"]))
    return used


def add(settings, server, file_path="", model="", label="", context=0):
    """Add a model. Returns its entry (with its name). Raises ModelError."""
    server = (server or "").strip().lower()
    if server not in ("llama", "lmstudio"):
        raise ModelError("a model runs on llama.cpp (a .gguf file) or LM Studio")
    with _lock:
        data = load(settings)
        entry = {"server": server}
        trained = 0
        if server == "llama":
            full = os.path.realpath(os.path.expanduser((file_path or "").strip()))
            if not file_path or not os.path.isfile(full):
                raise ModelError("choose the model's .gguf file")
            info = gguf_info(full)
            if re.search(r"(^|[-_.])mmproj", os.path.basename(full), re.I):
                raise ModelError("that is a vision projector, not a model; choose the model's own .gguf")
            if re.search(r"-0000[2-9]-of-\d+\.gguf$", full):
                raise ModelError("that is a later part of a split model; choose the part ending 00001-of-…")
            trained = info["context_length"]
            entry["path"] = full
            base = label or info["name"] or os.path.splitext(os.path.basename(full))[0]
        else:
            model = (model or "").strip()
            if not model:
                raise ModelError("give the LM Studio model's key, as LM Studio lists it")
            entry["model"] = model
            base = label or model.split("/")[-1]
        context = int(context or 0) or min(trained or DEFAULT_CONTEXT, DEFAULT_CONTEXT)
        ceiling = trained or int(settings.ctx_ceiling)
        if context < MIN_CONTEXT or context > ceiling:
            raise ModelError(f"context must be between {MIN_CONTEXT:,} and {ceiling:,} tokens for this model")
        entry["context"] = context
        entry["label"] = " ".join(base.split())[:60]
        if server == "llama":
            for other in data["models"].values():
                if other.get("path") == entry["path"]:
                    raise ModelError(f"that file is already added as “{other.get('label')}”")
            used, port = _ports_in_use(settings, data), FIRST_PORT
            while port in used:
                port += 1
            entry["port"] = port
        name, n = _slug(entry["label"]), 2
        taken = set(data["models"]) | set(settings.worker_profiles) | {"config"}
        while name in taken:
            name, n = f"{_slug(entry['label'])}-{n}", n + 1
        data["models"][name] = entry
        _save(settings, data)
        return dict(entry, name=name)


def choose(settings, name):
    """Use this model from Claire's next start. "" goes back to config.py."""
    with _lock:
        data = load(settings)
        known = set(data["models"]) | set(settings.worker_profiles) | {"config", ""}
        if name not in known:
            raise ModelError(f"no model named {name!r}")
        data["active"] = "" if name == "config" else name
        _save(settings, data)


def remove(settings, name):
    with _lock:
        data = load(settings)
        if name not in data["models"]:
            raise ModelError("only a model you added can be removed; config.py's stay")
        del data["models"][name]
        if data["active"] == name:
            data["active"] = ""
        _save(settings, data)
