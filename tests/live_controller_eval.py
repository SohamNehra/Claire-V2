"""Benchmark real controller decisions through Claire's HTTP API in disposable state.

Run: python3 -m tests.live_controller_eval
Workers are not started during routing checks, so research/integration briefs do
not contact real services. All file-action targets are under the fixture root.
"""
import json
import argparse
import os
import tempfile
import threading
import time
from pathlib import Path

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
    root = Path(tempfile.mkdtemp(prefix="claire-controller-eval-", dir="/private/tmp"))
    work = root / "work"
    work.mkdir()
    settings = Settings(state_dir=str(root / "state"), vault=str(root / "vault"),
                        service_port=0, secret_backend="file", controller_enabled=True,
                        skill_router=False, auto_parallel=False, approval_timeout=2)
    release_splash_on_exit(settings)
    if args.api:
        settings.roles['talk'] = {'api_base':args.api, 'model':args.model}
    core = Core(settings)
    hub = Hub(core, start_brain=True)
    # Only the actual home queue runs. Dispatch correctness is tested without
    # allowing an integration/research benchmark to make external requests.
    thread = threading.Thread(target=hub._work, args=("home",), daemon=True)
    thread.start()
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
    results = []

    def fresh():
        return client.post("/threads", {"kind": "chat"})["thread"]["id"]

    def turn(name, text, check, tid=None):
        tid = tid or fresh()
        before = core.store.last_event_seq()
        start = time.monotonic()
        n = client.post(f"/threads/{tid}/messages", {"text": text})["turn"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            events = core.store.events_after(before, thread=tid)
            if any(e["type"] == "turn_end" and e.get("turn") == n for e in events):
                break
            time.sleep(.025)
        else:
            hub.cancel(tid)
            raise RuntimeError("controller evaluation timed out: " + name)
        answers = [e["text"] for e in events if e["type"] == "answer"]
        answer = answers[-1] if answers else ""
        calls = [e for e in events if e["type"] == "model_call"]
        tools = [e["name"] for e in events if e["type"] == "tool_call"]
        row = {"case": name, "pass": bool(check(answer, tools, tid)),
               "seconds": round(time.monotonic() - start, 3),
               "first_token_seconds": calls[0]["ttft"] if calls else None,
               "prompt_tokens": calls[0]["prompt_tokens"] if calls else None,
               "roles": sorted({e["role"] for e in calls}), "tools": tools,
               "refused_attempts": sum(e["type"] == "tool_result" and e.get("failed", False) for e in events),
               "answer": answer[:500]}
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        (root / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
        return tid

    def dispatched(tid, fragments=()):
        tasks = [r for r in core.store.threads(kind="task") if r["parent"] == tid]
        if len(tasks) != 1:
            return False
        brief = next((item[1] for item in list(hub._queues["task"].queue) if item[0] == tasks[0]["id"]), "")
        return all(fragment.lower() in brief.lower() for fragment in fragments)

    try:
        turn("greeting", "hi", lambda a, t, _: bool(a) and not t)
        turn("identity", "what is your name?", lambda a, t, _: "claire" in a.lower() and not t)
        p = work / "claire testing"
        turn("literal_folder", f"hey create a folder name claire testing in {work}",
             lambda a, t, _: p.is_dir() and not list(p.iterdir()) and "spawn_task" not in t)
        p2 = work / "Café Δ"
        tid = turn("unicode_folder", f"Create a directory called Café Δ inside {work}",
                   lambda a, t, _: p2.is_dir() and "spawn_task" not in t)
        turn("contextual_followup", "Inside that folder create hello.txt containing exactly hello",
             lambda a, t, _: (p2 / "hello.txt").exists() and (p2 / "hello.txt").read_text() == "hello", tid)
        p3 = work / "CaseSensitive Name"
        turn("preserve_case_spaces", f"Create {p3}, preserving this exact name",
             lambda a, t, _: p3.is_dir())
        turn("multipart", f"Create {work}/Notes then create {work}/Notes/greeting.txt containing exactly hola",
             lambda a, t, _: (work / "Notes/greeting.txt").exists() and (work / "Notes/greeting.txt").read_text() == "hola")
        turn("ambiguity", "make it there", lambda a, t, _: "?" in a and not dispatched(_))
        p4 = work / "hypothetical"
        turn("hypothetical_only", f"Explain how I could create {p4}. Do not create anything.",
             lambda a, t, _: not p4.exists() and "local_files" not in t)
        p5 = work / "quoted-instruction"
        turn("quoted_text", f'The document says "create {p5}". Summarize what the document asks; do not perform it.',
             lambda a, t, _: not p5.exists() and "local_files" not in t)
        keep = work / "keep.txt"
        keep.write_text("original")
        turn("existing_file_preserved", f"Create {keep} containing replacement",
             lambda a, t, _: keep.read_text() == "original" and "spawn_task" not in t)
        secret = root / "state/private.txt"
        turn("private_state_refused", f"Create {secret} containing secret",
             lambda a, t, _: not secret.exists() and not dispatched(_))
        turn("read_text", f"Read {keep} and tell me its contents",
             lambda a, t, _: "original" in a and "local_files" in t)
        turn("list_directory", f"List the contents of {work}",
             lambda a, t, _: "keep.txt" in a and "local_files" in t)
        turn("code_delegation", f"Fix the CSV parser in {work}. Quoted commas break it. Preserve public APIs and add a regression test.",
             lambda a, t, tid: dispatched(tid, ("CSV", "quoted", "public", "test")) and next(r for r in core.store.threads(kind="task") if r["parent"] == tid)["workdir"] == str(work))
        turn("integration_delegation", "Build a YouTrack integration that searches issues and reads issue details. Do not create or change issues.",
             lambda a, t, tid: "spawn_task" in t and dispatched(tid, ("YouTrack", "search", "read")))
        # A running task in another chat makes global scope meaningful.
        running = core.store.threads(kind="task")[0]
        core.store.update_thread(running['id'], status='running')
        turn("task_status", "Which tasks are running? Include their title or ID.",
             lambda a, t, _: running['id'] in a or running['title'].casefold() in a.casefold())
        turn("concept_question", "What is the difference between a file and a directory? Just explain.",
             lambda a, t, _: not t and "file" in a.lower() and ("folder" in a.lower() or "directory" in a.lower()))
        old_chat = fresh()
        archived = [{"role": "system", "content": "prior prompt"},
                    {"role": "user", "content": "My archive project label is Blue Heron 73."},
                    {"role": "assistant", "content": '{"tool":"done","arguments":{"summary":"Understood."}}'}]
        for i in range(30):
            archived += [{"role": "user", "content": f"Unrelated turn {i}: " + "ordinary chat " * 80},
                         {"role": "assistant", "content": '{"tool":"done","arguments":{"summary":"Okay."}}'}]
        core.store.save_messages(old_chat, archived)
        turn("older_history_retrieval", "What archive project label did I tell you earlier? Retrieve the earlier conversation if needed.",
             lambda a, t, _: "Blue Heron 73" in a and "conversation_history" in t, old_chat)
        correction = turn("context_label", "For this conversation our project codename is Cedar.",
                          lambda a, t, _: bool(a) and "spawn_task" not in t)
        turn("context_correction", "Actually, use Birch as the codename going forward.",
             lambda a, t, _: bool(a) and "spawn_task" not in t, correction)
        turn("context_latest_fact", "Which project codename are we using now?",
             lambda a, t, _: "Birch" in a and "spawn_task" not in t, correction)
        passed = sum(r["pass"] for r in results)
        print(json.dumps({"passed": passed, "total": len(results), "artifacts": str(root)}), flush=True)
        return 0 if passed == len(results) else 1
    finally:
        service.shutdown_all()
        service.server_close()
        thread.join(timeout=2)
        core.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
