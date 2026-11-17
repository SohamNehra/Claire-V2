"""Exercise controller -> worker -> report, plus chat during coding, via HTTP.

Run: python3 -m tests.live_pipeline_eval. Uses only a disposable coding fixture.
"""
import hashlib
import json
import argparse
import tempfile
import threading
import time
from pathlib import Path

from claire_core.bench import BRAIN_TASKS
from claire_core.client import Client
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core.service import Service
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', help='conversation model endpoint; leaves the coding model unchanged')
    parser.add_argument('--model', default='')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="claire-pipeline-eval-", dir="/private/tmp"))
    work = root / "project"
    work.mkdir()
    name, files, check, prompt = BRAIN_TASKS[0]
    for fn, body in files.items():
        (work / fn).write_text(body)
    before = {fn: hashlib.sha1((work / fn).read_bytes()).hexdigest() for fn in files}
    settings = Settings(state_dir=str(root / "state"), vault=str(root / "vault"),
                        service_port=0, secret_backend="file", controller_enabled=True,
                        skill_router=True, auto_parallel=False, approval_timeout=2)
    release_splash_on_exit(settings)
    if args.api:
        settings.roles['talk'] = {'api_base':args.api, 'model':args.model}
    core = Core(settings)
    hub = Hub(core, start_brain=True).start()
    # Keep even an omitted-workdir fallback inside disposable state. A
    # separate folder preserves the pipeline's ability to catch wrong targets.
    fallback = root / 'unassigned-workspace'
    fallback.mkdir()
    resolve = hub.resolve_workdir
    hub.resolve_workdir = lambda raw, title='': resolve(raw or str(fallback), title)
    service = Service(hub)
    threading.Thread(target=service.serve_forever, kwargs={"poll_interval": .05}, daemon=True).start()
    client = Client(settings)
    client.base = f"http://127.0.0.1:{service.server_address[1]}"
    client.token = service.token
    try:
        started = time.monotonic()
        client.post("/threads/home/messages", {"text": f"In the project at {work}, {prompt}"})
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            tasks = core.store.threads(kind="task")
            if tasks:
                break
            time.sleep(.025)
        else:
            raise RuntimeError("controller did not dispatch work")
        task = tasks[0]
        dispatch_seconds = round(time.monotonic() - started, 3)
        chat = client.post("/threads", {"kind": "chat"})["thread"]["id"]
        greeting_start = time.monotonic()
        turn = client.post(f"/threads/{chat}/messages", {"text": "hey"})["turn"]
        while time.monotonic() < deadline:
            events = core.store.thread_events(chat)
            if any(e["type"] == "turn_end" and e.get("turn") == turn for e in events):
                break
            time.sleep(.025)
        greeting_seconds = round(time.monotonic() - greeting_start, 3)
        while time.monotonic() < deadline:
            task = core.store.thread(task["id"])
            reports = [e for e in core.store.thread_events("home") if e["type"] == "turn_start"
                       and e.get("text", "").startswith("[task report]")]
            if task["status"] in ("done", "error", "stopped", "cancelled") and reports and core.store.thread("home")["status"] == "idle":
                break
            time.sleep(.05)
        else:
            hub.cancel(task["id"])
            raise RuntimeError("pipeline did not finish")
        passed, why = check(str(work), task["summary"], before)
        assigned_original = Path(task["workdir"]).resolve() == work.resolve()
        passed = passed and assigned_original
        worker_calls = [e for e in core.store.thread_events(task["id"]) if e["type"] == "model_call"]
        home_calls = [e for e in core.store.thread_events("home") if e["type"] == "model_call"]
        report = {"pass": bool(passed and task["status"] == "done" and reports), "why": why,
                  "assigned_original_project": assigned_original, "dispatch_seconds": dispatch_seconds, "chat_during_coding_seconds": greeting_seconds,
                  "total_seconds": round(time.monotonic() - started, 2), "worker_status": task["status"],
                  "worker_prompt_tokens": worker_calls[0]["prompt_tokens"] if worker_calls else None,
                  "conversation_roles": sorted({e["role"] for e in home_calls}),
                  "worker_models": sorted({e.get("model") for e in worker_calls}), "artifacts": str(root)}
        (root / "results.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
        return 0 if report["pass"] else 1
    finally:
        service.shutdown_all()
        service.server_close()
        core.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
