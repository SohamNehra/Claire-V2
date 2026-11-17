"""Evaluate the actual focused worker with independent coding checks.

Run: python3 -m tests.live_worker_eval [--only one_file_fix spec_not_assertions]
All projects and state are disposable; no real integrations or credentials are used.
"""
import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from claire_core.bench import BRAIN_TASKS
from claire_core.engine import Core, Session
from claire_core.settings import Settings
from tests.live_cleanup import release_splash_on_exit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", choices=[t[0] for t in BRAIN_TASKS])
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="claire-worker-eval-", dir="/private/tmp"))
    settings = Settings(state_dir=str(root / "state"), vault=str(root / "vault"),
                        secret_backend="file", controller_enabled=True, skill_router=False,
                        auto_parallel=False)
    release_splash_on_exit(settings)
    core = Core(settings)
    rows = []
    try:
        for name, files, check, prompt in BRAIN_TASKS:
            if args.only and name not in args.only:
                continue
            work = root / name
            work.mkdir()
            for fn, body in files.items():
                (work / fn).write_text(body)
            before = {fn: hashlib.sha1((work / fn).read_bytes()).hexdigest() for fn in files}
            events = []
            session = Session(core, kind="agent", workdir=str(work), profile="worker",
                              skills=["coding"], auto_approve=True, approver=lambda r: False,
                              persist=False, sink=events.append)
            start = time.monotonic()
            outcome = session.run(prompt)
            try:
                passed, why = (check(str(work), outcome["answer"], before)
                               if outcome["status"] == "done" else (False, outcome["status"]))
            except Exception as exc:
                passed, why = False, repr(exc)
            calls = [e for e in events if e["type"] == "model_call"]
            row = {"case": name, "pass": bool(passed), "why": why,
                   "seconds": round(time.monotonic() - start, 2), "steps": len(calls),
                   "first_prompt_tokens": calls[0]["prompt_tokens"] if calls else None,
                   "first_token_seconds": calls[0]["ttft"] if calls else None,
                   "status": outcome["status"]}
            rows.append(row)
            print(json.dumps(row), flush=True)
            (work / "transcript.json").write_text(json.dumps(session.messages, indent=2))
            (root / "results.json").write_text(json.dumps(rows, indent=2))
        print(json.dumps({"passed": sum(r["pass"] for r in rows), "total": len(rows), "artifacts": str(root)}), flush=True)
        return 0 if all(r["pass"] for r in rows) else 1
    finally:
        core.connectors.close()
        core.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
