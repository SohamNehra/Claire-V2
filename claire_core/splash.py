"""Lifecycle for locally installed Splash servers; no runtime downloads here.

Ownership records survive a Claire service restart. A record is accepted only
while the process start time/command and the HTTP instance still match, so idle
unload never kills an unrelated listener or a reused PID.
"""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from .resources import GIB, memory_snapshot


def spec(settings, role, entry):
    cfg = settings.config
    return dict(role=role, kind='splash', model=entry['model'],
                path=os.path.realpath(os.path.expanduser(entry['path'])),
                draft=os.path.realpath(os.path.expanduser(entry['draft'])),
                root=os.path.realpath(os.path.expanduser(getattr(cfg, 'SPLASH_ROOT', ''))),
                host='127.0.0.1', port=int(entry['port']),
                context=int(entry.get('context', 32768)),
                max_memory=str(entry.get('max_memory', '36G')),
                language_only=bool(entry.get('language_only', True)),
                prefix_cache=str(entry.get('prefix_cache', '0')),
                verify_rows='all', state_dir=settings.state_dir,
                load_timeout=int(entry.get('load_timeout', 300)))


def command(s):
    root = Path(s['root'])
    cmd = [str(root/'python/bin/python3'), '-m', 'install.launcher', 'serve',
           '--model', s['model'], '--model-dir', s['path'],
           '--draft-model', s['draft'], '--port', str(s['port']),
           '--host', s['host'], '--max-context', str(s['context']),
           '--max-memory', s['max_memory'], '--verify-rows', s['verify_rows'],
           '--idle-release', 'off']
    if s.get('prefix_cache', '0') in ('', '0'):
        cmd += ['--max-cache-disk', '0']
    else:
        # Kept on disk across restarts, so a worker reloaded after idle unload
        # does not read its long system prompt again (measured on the 27B:
        # first token in 0.2 s instead of 10 s for a 4K-token prompt).
        cmd += ['--max-cache-disk', s['prefix_cache'], '--max-cache-disk-writes', '20G',
                '--persistent-cache', '--cache-dir', cache_dir(s)]
    if s['language_only']:
        cmd.append('--language-only')
    return cmd


def cache_dir(s):
    return str(Path(s['state_dir'])/'cache'/f"splash-{s['port']}")


def _json(s, route, timeout=3):
    with urllib.request.urlopen(f"http://{s['host']}:{s['port']}{route}", timeout=timeout) as response:
        return json.load(response)


def ready(s):
    try:
        if _json(s, '/ready').get('status') != 'ready':
            return False
        models = _json(s, '/v1/models').get('data') or []
        return any(m.get('id') == s['model'] and m.get('owned_by') == 'splash' for m in models)
    except Exception:
        return False


def _record_path(s):
    return Path(s['state_dir'])/'model-servers'/f"splash-{s['port']}.json"


def _identity(s):
    return {k: s[k] for k in ('root', 'path', 'draft', 'model', 'host', 'port',
                              'context', 'max_memory', 'language_only', 'verify_rows',
                              'prefix_cache')}


def _write_record(s, pid, fingerprint, instance=None):
    path = _record_path(s)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = dict(spec=_identity(s), pid=pid, fingerprint=fingerprint, instance=instance)
    temporary = path.with_suffix(f'.{os.getpid()}.tmp')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f)
    os.replace(temporary, path)


def _owned(s, fingerprint):
    try:
        record = json.loads(_record_path(s).read_text())
    except (OSError, ValueError):
        return None
    if record.get('spec') != _identity(s):
        return None
    pid = record.get('pid')
    if type(pid) is not int or pid <= 1 or not record.get('fingerprint'):
        return None
    if fingerprint(pid) != record['fingerprint']:
        return None
    # Launcher exec preserves PID but changes its command. Only the fingerprint
    # saved after readiness is adoptable by another Claire process.
    return record


def _listening(s):
    with socket.socket() as sock:
        sock.settimeout(.5)
        return sock.connect_ex((s['host'], s['port'])) == 0


def _size(path):
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    return sum(q.stat().st_size for q in p.glob('*.safetensors'))


def _memory_problem(settings, s):
    if not settings.auto_parallel:
        return None
    memory = memory_snapshot()
    available = memory.get('available_bytes')
    if available is None:
        return 'RAM telemetry is unavailable; cannot safely start the Splash worker.'
    # Measured on the 27B Q4_K_M (15.7 GiB file): 19 GiB once loaded with its
    # draft, 22 GiB after 14K-token prompts. The draft is 3.6 GiB on disk but is
    # quantised to about a third of that at load, inside the 1.25 factor, so
    # adding its file size counted it twice.
    needed = _size(s['path'])*1.25 + settings.model_runtime_gb*GIB
    reserve = max(settings.ram_reserve_gb*GIB, (memory.get('total_bytes') or 0)*.1)
    if available < needed + reserve:
        return (f"Not starting {s['model']}: about {needed/GIB:.1f} GiB plus "
                f"{reserve/GIB:.1f} GiB reserve needed; {available/GIB:.1f} GiB available. "
                'Unload another idle large model first; active models were left running.')
    return None


def _stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)  # Splash shuts down its engine child itself.
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def ensure(settings, s, processes, fingerprint, say=None):
    existing = _owned(s, fingerprint)
    if _listening(s):
        if existing and ready(s):
            instance = _json(s, '/status').get('instance') or {}
            if (instance.get('id') == existing.get('instance')
                    and instance.get('pid') == existing['pid']):
                return None
        return (f"Port {s['port']} is occupied by an unowned or different model server. "
                'Claire left it running; stop it or choose another configured port.')
    root = Path(s['root'])
    if not all((root/p).is_file() for p in ('python/bin/python3', 'engine/splash', 'install/launcher.py')):
        return (f"Splash runtime is not installed at {root}. See CLAIRE_SPLASH_UPGRADE.md."
                " To use a model of your own, add it in the app (Models) or with: claire2 models add FILE.gguf")
    if not Path(s['path']).exists() or not (Path(s['draft'])/'config.json').is_file():
        return f"Missing Splash target or draft for {s['model']}. Check WORKER_PROFILES in config.py."
    problem = _memory_problem(settings, s)
    if problem:
        return problem
    logs = Path(s['state_dir'])/'logs'
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = logs/f"splash-{s['port']}.log"
    if say:
        say(f"starting Splash for {s['model']}...")
    env = dict(os.environ, HF_HUB_OFFLINE='1', HF_HOME=str(root.parent/'huggingface'))
    with log.open('ab') as out:
        process = subprocess.Popen(command(s), cwd=root, env=env, stdout=out,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    processes[process.pid] = process
    succeeded = False
    try:
        deadline = time.monotonic() + s['load_timeout']
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return f"Splash exited with status {process.returncode}. Check {log}"
            if ready(s):
                instance = _json(s, '/status').get('instance') or {}
                if instance.get('pid') != process.pid or not instance.get('id'):
                    return f"Splash instance identity could not be verified. Check {log}"
                current = fingerprint(process.pid)
                if not current:
                    return 'Splash process identity could not be verified.'
                _write_record(s, process.pid, current, instance['id'])
                succeeded = True
                if say:
                    say('Splash ready.')
                return None
            time.sleep(.5)
        return f"Splash did not become ready within {s['load_timeout']}s. Check {log}"
    finally:
        if not succeeded:
            _stop_child(process)
            processes.pop(process.pid, None)


def unload(s, processes, fingerprint):
    record = _owned(s, fingerprint)
    if record is None:
        if _listening(s):
            raise RuntimeError(f"Splash on port {s['port']} is not Claire's verified process; left running")
        return
    pid = record['pid']
    if fingerprint(pid) != record['fingerprint']:
        raise RuntimeError('Splash process changed before unload; left running')
    if _listening(s):
        instance = _json(s, '/status').get('instance') or {}
        if instance.get('id') != record['instance'] or instance.get('pid') != pid:
            raise RuntimeError('Splash HTTP instance changed before unload; left running')
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        child = processes.get(pid)
        if child is not None:
            child.poll()
        if fingerprint(pid) != record['fingerprint']:
            processes.pop(pid, None)
            _record_path(s).unlink(missing_ok=True)
            return
        time.sleep(.1)
    raise RuntimeError('Splash did not exit within 30 seconds after unload')
