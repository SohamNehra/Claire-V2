"""Starts model servers when nothing is listening.

v1 did this in the bash launcher, for one model. It lives here now because the
service needs it too, because the brain is started with the slots that let the
home thread and a task generate at once, and because a role given its own model
file in config.py (a scout, a clerk) gets a server of its own.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

from .resources import model_start_problem
from . import splash

_lock = threading.Lock()


def _healthy(api_base, timeout=3):
    root = api_base[:-3] if api_base.endswith("/v1") else api_base
    try:
        with urllib.request.urlopen(f"{root.rstrip('/')}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def server_spec(settings, role):
    """What it takes to start the role's server, or None if it is not ours to
    start (the role runs on the brain, or on a server someone else manages)."""
    cfg = settings.config
    if role == "brain":
        if not cfg or not getattr(cfg, "AUTOSTART_SERVER", True):
            return None
        if getattr(settings, "brain_server", "llama") == "splash":
            profile = getattr(settings, "brain_profile", {})
            if not profile or settings.api_base.rstrip("/") != f"http://127.0.0.1:{profile['port']}/v1":
                return None
            return splash.spec(settings, role, profile)
        if getattr(settings, "brain_server", "llama") == "lmstudio":
            return {"role": role, "kind": "lmstudio", "model": settings.model,
                    "url": str(getattr(cfg, "LMSTUDIO_URL", "http://127.0.0.1:1234")).rstrip("/"),
                    "context": settings.ctx_limit or min(settings.ctx_ceiling,
                        settings.worker_context if settings.controller_enabled else 131072),
                    "slots": settings.brain_slots}
        # A model picked in the app has its own file and port; otherwise config.py's.
        return {"role": role, "gguf": getattr(settings, "brain_gguf", None) or os.path.expanduser(str(cfg.MODEL_GGUF)),
                "host": cfg.HOST if not getattr(settings, "brain_gguf", None) else "127.0.0.1",
                "port": int(getattr(settings, "brain_port", None) or cfg.PORT),
                # A KV cache is allocated up front, so an unbounded context can
                # exhaust RAM before the model finishes loading. Cap it.
                "context": settings.ctx_limit or settings.ctx_ceiling,
                "slots": settings.brain_slots}
    entry = settings.roles.get(role) or {}
    if entry.get("server") == "splash" and entry.get("model"):
        return splash.spec(settings, role, entry)
    if entry.get("server") == "lmstudio" and entry.get("model"):
        base=str(entry.get("api_base") or settings.api_base).rstrip("/")
        return {"role":role,"kind":"lmstudio","model":entry["model"],
                "url":base[:-3] if base.endswith("/v1") else base,
                "context":int(entry.get("context",16384)),"slots":1,"load_timeout":180}
    if not entry.get("gguf") or not entry.get("port"):
        return None
    return {"role": role, "gguf": os.path.expanduser(str(entry["gguf"])),
            "host": "127.0.0.1", "port": int(entry["port"]),
            "context": int(entry.get("context", 32768)),
            "slots": int(entry.get("slots", settings.scout_parallel)),
            "args": [str(a) for a in entry.get("args") or []]}


def server_command(spec, cfg):
    kv = getattr(cfg, "KV_CACHE_TYPE", "q8_0")
    cmd = ["llama-server", "-m", spec["gguf"], "--jinja",
           "-c", str(spec["context"]), "-ctk", kv, "-ctv", kv,
           "-ngl", str(getattr(cfg, "GPU_LAYERS", 999)),
           "-np", str(spec["slots"]),
           "--port", str(spec["port"]), "--host", spec["host"]]
    if spec["role"] == "brain":
        mmproj = os.path.expanduser(str(getattr(cfg, "MODEL_MMPROJ", "") or ""))
        if mmproj and os.path.isfile(mmproj):
            cmd += ["--mmproj", mmproj]              # lets the model see images
        cmd += list(getattr(cfg, "EXTRA_SERVER_ARGS", []))
    cmd += list(spec.get("args") or [])              # a role's own flags, from ROLES
    return cmd


def _lmstudio_model(spec, timeout=5):
    """LM Studio's entry for the spec's model, or None if LM Studio is not up."""
    try:
        with urllib.request.urlopen(f"{spec['url']}/api/v1/models", timeout=timeout) as r:
            models = json.load(r).get("models") or []
    except Exception:
        return None
    return next((m for m in models if m.get("key") == spec["model"]), {})


def _ensure_lmstudio(spec, say=None):
    """Make sure LM Studio has the model loaded. Returns None, or what is wrong."""
    entry = _lmstudio_model(spec)
    if entry is None:
        return (f"LM Studio is not answering at {spec['url']}. Open LM Studio (its local "
                f"server starts with it), then try again.")
    if not entry:
        return f"LM Studio has no model {spec['model']!r}. Check LMSTUDIO_MODEL in config.py."
    if entry.get("loaded_instances"):
        return None
    if say:
        say(f"loading {spec['model']} in LM Studio...")
    body = json.dumps({"model": spec["model"], "context_length": int(spec["context"])}).encode()
    req = urllib.request.Request(f"{spec['url']}/api/v1/models/load", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=spec.get("load_timeout",900)) as r:
            json.load(r)
    except Exception as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return f"LM Studio could not load {spec['model']}: {detail or exc}"
    return None if (_lmstudio_model(spec) or {}).get("loaded_instances") else \
        f"LM Studio did not load {spec['model']}"


def ensure_role(settings, role, wait=180, say=None):
    """Make sure the role's model is reachable. Returns None, or what is wrong.

    A role with no model of its own is served by the brain, so asking about it
    is asking about the brain.
    """
    entry = settings.roles.get(role) or {}
    if role != "brain" and not entry.get("api_base"):
        return ensure_role(settings, "brain", wait, say)
    api_base = settings.api_base if role == "brain" else entry["api_base"]
    spec = server_spec(settings, role)
    if spec and spec.get("kind") == "splash":
        with _lock:
            try:
                return splash.ensure(settings, spec, _processes, _process_fingerprint, say)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                return f"Splash could not start the {role}: {exc}"
    if spec and spec.get("kind") == "lmstudio":
        with _lock:
            return _ensure_lmstudio(spec, say)
    if _healthy(api_base):
        return None
    if spec is None:
        return (f"cannot reach the {role} model at {api_base}, and it is not "
                f"configured to be started automatically.")
    with _lock:
        if _healthy(api_base):               # another thread started it
            return None
        if not os.path.isfile(spec["gguf"]):
            return (f"{role} model not found at {spec['gguf']}. Check config.py."
                    " To use a model of your own, add it in the app (Models) or with: claire2 models add FILE.gguf")
        if not shutil.which("llama-server"):
            return ("llama-server is not on your PATH. Install llama.cpp, or set "
                    "AUTOSTART_SERVER = False and point API_BASE at your server.")
        problem = model_start_problem(settings, spec)
        if problem:
            return problem
        cfg = settings.config
        log = os.path.expanduser(str(getattr(cfg, "SERVER_LOG", "~/.claire-server.log")))
        if role != "brain":
            log = f"{os.path.splitext(log)[0]}-{role}.log"
        if say:
            say(f"starting llama-server for the {role}...")
        with open(log, "ab") as out:
            process = subprocess.Popen(server_command(spec, cfg), stdout=out,
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            _processes[process.pid] = process
        deadline = time.time() + wait
        while time.time() < deadline:
            if _healthy(api_base, timeout=2):
                if say:
                    say("server ready.")
                return None
            time.sleep(1)
        return f"llama-server for the {role} did not come up within {wait}s. Check {log}"


def ensure_brain(settings, wait=180, say=None):
    return ensure_role(settings, "brain", wait, say)


# A process handle lets us reap our children after unloading. Older servers can
# be adopted after a service restart only when their full command matches config.
_processes = {}


def _local_url(url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.hostname in ('127.0.0.1', 'localhost', '::1')


def lifecycle_spec(settings, role):
    entry = settings.roles.get(role) or {}
    if role != 'brain' and not entry.get('api_base') and entry.get('server')!='lmstudio':
        role = 'brain'
    spec = server_spec(settings, role)
    if spec is None:
        return None
    if spec.get('kind') == 'lmstudio':
        return spec if _local_url(spec['url']) else None
    return spec if spec['host'] in ('127.0.0.1', 'localhost', '::1') else None


def _process_fingerprint(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ''


def _matching_servers(spec, cfg):
    """Exact configured llama-server commands only; never LM Studio children."""
    expected = server_command(spec, cfg)
    args = ' '.join(expected[1:])
    result = subprocess.run(['ps', '-axo', 'pid=,command='],
                            capture_output=True, text=True, timeout=5, check=True)
    matches = []
    for row in result.stdout.splitlines():
        fields = row.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid, executable, actual_args = fields
        if (pid.isdigit() and os.path.basename(executable) == expected[0]
                and actual_args == args):
            fingerprint = _process_fingerprint(int(pid))
            # The pid may already have been reused between the listing and
            # this read. Verify the command again, including its start time.
            parts = fingerprint.split(None, 5)
            current = parts[5].split(None, 1) if len(parts) == 6 else []
            if (len(current) == 2 and os.path.basename(current[0]) == expected[0]
                    and current[1] == args):
                matches.append((int(pid), fingerprint))
    return matches


def _unload_llama(spec, cfg):
    import signal
    matches = _matching_servers(spec, cfg)
    if not matches:
        api = f"http://{spec['host']}:{spec['port']}/v1"
        if _healthy(api):
            raise RuntimeError(f"{spec['role']} server does not match Claire's configured process; left running")
        return
    for pid, fingerprint in matches:
        if _process_fingerprint(pid) != fingerprint:
            raise RuntimeError(f"{spec['role']} server changed before unloading; left running")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            child = _processes.get(pid)
            if child is not None:
                child.poll()  # reap a terminated child before checking ps
            if _process_fingerprint(pid) != fingerprint:
                _processes.pop(pid, None)
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"{spec['role']} server did not exit after unload")


def _unload_lmstudio(spec):
    entry = _lmstudio_model(spec)
    if entry is None:
        raise RuntimeError(f"LM Studio is not answering at {spec['url']}")
    for instance in entry.get('loaded_instances') or []:
        ident = instance.get('id')
        if not ident:
            raise RuntimeError(f"LM Studio returned no instance id for {spec['model']}")
        req = urllib.request.Request(f"{spec['url']}/api/v1/models/unload",
            data=json.dumps({'instance_id': ident}).encode(),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            json.load(response)
    remaining = _lmstudio_model(spec)
    if remaining is None or remaining.get('loaded_instances'):
        raise RuntimeError(f"Could not verify {spec['model']} was unloaded")


def unload_models(settings, kinds=None):
    """Unload just Claire's configured local models, deduplicating role aliases.

    Remote/external endpoints have no lifecycle contract and are left alone.
    Errors are retained so the service can report and retry a partial unload.
    `kinds` limits the unload to those server kinds, e.g. ("splash",).
    """
    seen, unloaded, errors = set(), [], []
    with _lock:
        for role in ['brain'] + list(settings.roles):
            spec = server_spec(settings, role)
            if spec is None:
                continue
            if kinds is not None and spec.get('kind', 'llama') not in kinds:
                continue
            studio = spec.get('kind') == 'lmstudio'
            if studio and not _local_url(spec['url']):
                continue
            if not studio and spec['host'] not in ('127.0.0.1', 'localhost', '::1'):
                continue
            key = (('lmstudio', spec['url'], spec['model']) if studio
                   else ('splash', spec['host'], spec['port'], spec['path']) if spec.get('kind')=='splash'
                   else ('llama', spec['host'], spec['port'], os.path.realpath(spec['gguf'])))
            if key in seen:
                continue
            seen.add(key)
            try:
                if spec.get('kind')=='splash':
                    splash.unload(spec, _processes, _process_fingerprint)
                elif studio:
                    _unload_lmstudio(spec)
                else:
                    _unload_llama(spec, settings.config)
                unloaded.append(role)
            except Exception as exc:
                errors.append(f'{role}: {type(exc).__name__}: {exc}')
    return {'unloaded': unloaded, 'errors': errors}


def unload_role(settings, role):
    """Release a separately leased local model without disturbing other roles."""
    spec=lifecycle_spec(settings,role)
    if spec is None:return
    with _lock:
        if spec.get('kind')=='splash':splash.unload(spec, _processes, _process_fingerprint)
        elif spec.get('kind')=='lmstudio':_unload_lmstudio(spec)
        else:_unload_llama(spec,settings.config)
