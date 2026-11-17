"""The report card: how well does a model do Claire's actual job?

Published benchmarks say how a model does in someone else's harness. This runs
a model through this one -- the same prompt, the same XML tool protocol, the
same sandbox -- on small fixed tasks with checkable outcomes, and records what
matters when choosing a model for a role: did it finish the job, how many steps
and malformed replies it took, and how fast it generated on this machine.

Two suites:

    brain   whole tasks run through the agent loop
    scout   single questions over material, as the scout tool asks them

Checks are done by this file on the result, never by asking the model, and the
task prompts never contain what the check looks for. Adding a task is adding
one entry to BRAIN_TASKS or SCOUT_TASKS.
"""

import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from . import boot
from .engine import Core, Session
from .settings import Settings
from .tools.scout import run_scouts


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def _read(work, name):
    try:
        with open(os.path.join(work, name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _tests_pass(work):
    p = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work,
                       capture_output=True, text=True, timeout=60)
    return p.returncode == 0, (p.stderr or p.stdout).strip().splitlines()[-1:]


def _run_py(work, code):
    p = subprocess.run([sys.executable, "-c", code], cwd=work,
                       capture_output=True, text=True, timeout=30)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


# -- brain tasks ---------------------------------------------------------------
# Each check returns (passed, reason). `before` maps file name -> sha1 at setup.

def check_one_file_fix(work, answer, before):
    if _sha(os.path.join(work, "test_stats.py")) != before["test_stats.py"]:
        return False, "edited the test instead of the code"
    ok, tail = _tests_pass(work)
    return ok, "tests pass" if ok else f"tests still fail: {' '.join(tail)}"


def check_spec_not_assertions(work, answer, before):
    if _sha(os.path.join(work, "test_slug.py")) != before["test_slug.py"]:
        return False, "edited the test"
    ok, out = _run_py(work, (
        "from slug import slugify\n"
        "cases = {'Hello World': 'hello-world', '  Leading and trailing  ': 'leading-and-trailing',\n"
        "         'a  --  b': 'a-b', 'Rock & Roll!': 'rock-roll', 'ALREADY-fine': 'already-fine',\n"
        "         '': '', '!!!': ''}\n"
        "bad = {k: slugify(k) for k, v in cases.items() if slugify(k) != v}\n"
        "print(bad); raise SystemExit(1 if bad else 0)"))
    return ok, "handles every documented case" if ok else f"wrong for: {out[:160]}"


def check_rename_in_scope(work, answer, before):
    if _sha(os.path.join(work, "cache.py")) != before["cache.py"]:
        return False, "changed cache.py, whose fetch is a different function"
    if re.search(r"def fetch\(", _read(work, "api.py")):
        return False, "api.py still defines fetch"
    if "fetch_user" not in _read(work, "report.py") or "fetch_user" not in _read(work, "cli.py"):
        return False, "a caller was not updated"
    ok, tail = _tests_pass(work)
    return ok, "renamed everywhere it should be" if ok else f"tests fail: {' '.join(tail)}"


def check_missing_file(work, answer, before):
    if os.path.exists(os.path.join(work, "network.py")):
        return False, "created the file it was asked about"
    said = re.search(r"(does ?n[o']t exist|no such file|not found|there is no|"
                     r"is no file|isn'?t (a|any|there)|not present|no network\.py|"
                     r"could not find|couldn'?t find|cannot find|can'?t find|"
                     r"does not contain|doesn'?t contain|missing)", answer, re.I)
    return bool(said), ("said the file is not there" if said
                        else "described a file that does not exist")


def check_report_json(work, answer, before):
    try:
        got = json.loads(_read(work, "report.json"))
    except ValueError:
        return False, "report.json is missing or not valid JSON"
    want = {"north": 1730.5, "south": 912.25, "east": 2105.0}
    try:
        ok = set(got) == set(want) and all(abs(float(got[k]) - v) < 0.01 for k, v in want.items())
    except (TypeError, ValueError):
        ok = False
    return ok, "totals are right" if ok else f"got {str(got)[:120]}"


def check_one_line_change(work, answer, before):
    changed = [n for n, h in before.items() if _sha(os.path.join(work, n)) != h]
    if changed != ["settings.py"]:
        return False, f"changed {changed or 'nothing'}; only settings.py should change"
    old = SCOPE_FILES["settings.py"].splitlines()
    new = _read(work, "settings.py").splitlines()
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    ok = (len(old) == len(new) and len(diff) == 1
          and re.match(r"^TIMEOUT=60(#.*)?$", diff[0][1].replace(" ", "")))
    return bool(ok), "changed exactly that line" if ok else "changed more than the one value"


def check_repo_question(work, answer, before):
    changed = [n for n, h in before.items() if _sha(os.path.join(work, n)) != h]
    if changed:
        return False, f"was asked a question but edited {changed}"
    ok = "save_snapshot" in answer and "state.json" in answer
    return ok, "found the function and the file" if ok else "answer misses the function or file name"


FIX_FILES = {
    "stats.py": (
        "def mean(values):\n    return sum(values) / len(values)\n\n\n"
        "def median(values):\n    ordered = sorted(values)\n    mid = len(ordered) // 2\n"
        "    if len(ordered) % 2:\n        return ordered[mid]\n"
        "    return (ordered[mid] + ordered[mid + 1]) / 2\n\n\n"
        "def spread(values):\n    return max(values) - min(values)\n"),
    "test_stats.py": (
        "import unittest\nfrom stats import mean, median, spread\n\n\n"
        "class StatsTests(unittest.TestCase):\n"
        "    def test_mean(self):\n        self.assertEqual(mean([1, 2, 3]), 2)\n\n"
        "    def test_median_odd(self):\n        self.assertEqual(median([5, 1, 3]), 3)\n\n"
        "    def test_median_even(self):\n        self.assertEqual(median([4, 1, 3, 2]), 2.5)\n\n"
        "    def test_spread(self):\n        self.assertEqual(spread([2, 9, 4]), 7)\n"),
}

SPEC_FILES = {
    "slug.py": (
        'import re\n\n\ndef slugify(text):\n    """Turn a title into a URL slug.\n\n'
        "    - lower-case the text\n"
        "    - every run of characters that are not letters or digits becomes a\n"
        "      single hyphen\n"
        "    - no hyphen at the start or the end\n"
        "    - an input with no letters or digits gives the empty string\n"
        '    """\n    return text.lower().replace(" ", "-")\n'),
    "test_slug.py": (
        "import unittest\nfrom slug import slugify\n\n\n"
        "class SlugTests(unittest.TestCase):\n"
        "    def test_simple(self):\n"
        "        self.assertEqual(slugify('Hello World'), 'hello-world')\n"),
}

RENAME_FILES = {
    "api.py": (
        "USERS = {1: 'ada', 2: 'grace'}\n\n\n"
        "def fetch(user_id):\n    \"\"\"Look up a user's name by id.\"\"\"\n"
        "    return USERS.get(user_id)\n"),
    "cache.py": (
        "class Cache:\n    \"\"\"A tiny in-memory cache. Unrelated to api.fetch.\"\"\"\n\n"
        "    def __init__(self):\n        self._items = {}\n\n"
        "    def store(self, key, value):\n        self._items[key] = value\n\n"
        "    def fetch(self, key, default=None):\n"
        "        return self._items.get(key, default)\n"),
    "report.py": (
        "from api import fetch\nfrom cache import Cache\n\n_cache = Cache()\n\n\n"
        "def greeting(user_id):\n    name = _cache.fetch(user_id)\n"
        "    if name is None:\n        name = fetch(user_id)\n"
        "        _cache.store(user_id, name)\n    return f'hello {name}'\n"),
    "cli.py": (
        "import sys\nimport api\n\n\ndef main(argv):\n"
        "    print(api.fetch(int(argv[1])))\n\n\n"
        "if __name__ == '__main__':\n    main(sys.argv)\n"),
    "test_report.py": (
        "import unittest\nfrom report import greeting\n\n\n"
        "class ReportTests(unittest.TestCase):\n"
        "    def test_greeting(self):\n"
        "        self.assertEqual(greeting(1), 'hello ada')\n"
        "        self.assertEqual(greeting(1), 'hello ada')\n"),
}

MISSING_FILES = {
    "app.py": "import storage\n\n\ndef run():\n    return storage.load()\n",
    "storage.py": "def load():\n    return {'items': []}\n",
}

CSV_FILES = {
    "sales.csv": (
        "region,amount,note\nnorth,1200.50,\nsouth,400.25,\neast,2000,bulk\n"
        "north,530,\nsouth,512.00,\neast,105.0,\n"),
}

SCOPE_FILES = {
    "settings.py": (
        "# Service settings\nRETRIES = 3\nTIMEOUT = 30\nBACKOFF = 1.5\n"
        "LOG_LEVEL = 'info'\n"),
    "client.py": (
        "import settings\n\n\ndef connect(host):\n"
        "    return {'host': host, 'timeout': settings.TIMEOUT, 'retries': settings.RETRIES}\n"),
    "notes.txt": "The upstream gateway drops connections after 30 seconds.\n",
}

REPO_FILES = {
    "main.py": "from engine import Engine\n\n\ndef main():\n    Engine().tick()\n",
    "engine.py": (
        "import persist\n\n\nclass Engine:\n    def __init__(self):\n        self.state = {'ticks': 0}\n\n"
        "    def tick(self):\n        self.state['ticks'] += 1\n"
        "        if self.state['ticks'] % 10 == 0:\n            persist.save_snapshot(self.state)\n"),
    "persist.py": (
        "import json\n\nDEFAULT_PATH = 'state.json'\n\n\n"
        "def save_snapshot(state, path=DEFAULT_PATH):\n"
        "    with open(path, 'w') as f:\n        json.dump(state, f)\n\n\n"
        "def describe(state):\n    return f\"{state['ticks']} ticks\"\n"),
    "render.py": "def draw(state):\n    return '#' * state['ticks']\n",
    "util.py": "def clamp(n, lo, hi):\n    return max(lo, min(n, hi))\n",
}

BRAIN_TASKS = [
    ("one_file_fix", FIX_FILES, check_one_file_fix,
     "The test suite has a failing test. Find the cause, fix it, and verify."),
    ("spec_not_assertions", SPEC_FILES, check_spec_not_assertions,
     "slugify in slug.py does not do what its docstring says. Make it match the "
     "docstring."),
    ("rename_in_scope", RENAME_FILES, check_rename_in_scope,
     "Rename the function fetch in api.py to fetch_user, and update everything "
     "that uses it."),
    ("missing_file", MISSING_FILES, check_missing_file,
     "Explain what the retry logic in network.py does."),
    ("tool_workflow", CSV_FILES, check_report_json,
     "Total the amount column of sales.csv for each region and write the result "
     "to report.json as a JSON object mapping each region to its total."),
    ("one_line_change", SCOPE_FILES, check_one_line_change,
     "Change the default timeout to 60 seconds."),
    ("repo_question", REPO_FILES, check_repo_question,
     "Does anything in this project write to disk? If so, tell me which "
     "function does it and the default file it writes to. Do not change anything."),
]


# -- scout tasks ---------------------------------------------------------------

def _filler(n, seed):
    return "\n".join(f"def helper_{seed}_{i}(x):\n    return x + {i}\n" for i in range(n))


SCOUT_SOURCES = [
    ("billing/rates.py", _filler(25, "a") + "\nMAX_RETRY_DELAY = 47\n\n" + _filler(25, "b")),
    ("billing/invoice.py", _filler(12, "c") + "\ndef finalize_invoice(order):\n"
     "    return {'id': order['id'], 'total': order['amount'] * 1.2}\n\n" + _filler(12, "d")),
    ("billing/tax.py", _filler(20, "e")),
    ("notes/deploy.md", "# Deploy\n\nDeploys run from the `release` branch.\n"
     "Rollbacks are done with `make rollback`, never by force-pushing.\n"),
]


def _line_of(name, needle):
    text = dict(SCOUT_SOURCES)[name]
    return next(i for i, l in enumerate(text.splitlines(), 1) if needle in l)


SCOUT_TASKS = [
    ("exact_value", "What is the value of MAX_RETRY_DELAY?",
     lambda a: (bool(re.search(r"\b47\b", a)), "the value is 47")),
    ("which_file", "Which file defines finalize_invoice?",
     lambda a: ("billing/invoice.py" in a, "it is in billing/invoice.py")),
    ("cites_line", "On which line is MAX_RETRY_DELAY defined?",
     lambda a: (f"rates.py:{_line_of('billing/rates.py', 'MAX_RETRY_DELAY')}" in a
                or bool(re.search(rf"\b{_line_of('billing/rates.py', 'MAX_RETRY_DELAY')}\b", a)),
                f"it is line {_line_of('billing/rates.py', 'MAX_RETRY_DELAY')}")),
    ("says_not_found", "What database does the billing code connect to?",
     lambda a: ("NOT FOUND" in a.upper(), "the material does not say")),
    ("prose_fact", "How should a rollback be done?",
     lambda a: ("make rollback" in a, "with `make rollback`")),
]


# -- running -------------------------------------------------------------------

class Collector:
    """Counts what happened in one task run from its events."""

    def __init__(self):
        self.steps = self.retries = self.failed_calls = self.faults = 0
        self.tool_calls = 0
        self.tokens = 0
        self.gen_seconds = 0.0
        self.ttfts = []

    def __call__(self, e):
        kind = e["type"]
        if kind == "step":
            self.steps += 1
        elif kind == "tool_call":
            self.tool_calls += 1
        elif kind == "tool_result" and e.get("failed"):
            self.failed_calls += 1
        elif kind == "tool_fault":
            self.faults += 1
        elif kind == "notice" and "no tool call" in e.get("text", ""):
            self.retries += 1
        elif kind == "model_call":
            self.tokens += e.get("completion_tokens") or 0
            self.gen_seconds += e.get("gen_seconds") or 0.0
            if e.get("ttft"):
                self.ttfts.append(e["ttft"])


def run_brain(core, root, only=None, say=print):
    rows = []
    for name, files, check, prompt in BRAIN_TASKS:
        if only and name not in only:
            continue
        work = os.path.join(root, name)
        os.makedirs(work)
        for fn, body in files.items():
            with open(os.path.join(work, fn), "w", encoding="utf-8") as f:
                f.write(body)
        before = {fn: _sha(os.path.join(work, fn)) for fn in files}
        got = Collector()
        session = Session(core, kind="agent", workdir=work, sink=got,
                          approver=lambda request: False,   # nothing here needs a yes
                          auto_approve=True, thinking=False, persist=False,
                          skills=["coding"])           # these are coding tasks
        t0 = time.time()
        outcome = session.run(prompt)
        seconds = time.time() - t0
        if outcome["status"] != "done":
            passed, why = False, f"ended with status {outcome['status']}"
        else:
            try:
                passed, why = check(work, outcome["answer"], before)
            except Exception as exc:
                passed, why = False, f"check crashed: {type(exc).__name__}: {exc}"
        row = {"task": name, "passed": bool(passed), "why": why,
               "seconds": round(seconds, 1), "steps": got.steps,
               "tool_calls": got.tool_calls, "retries": got.retries,
               "failed_calls": got.failed_calls, "tokens": got.tokens,
               "tok_s": round(got.tokens / got.gen_seconds, 1) if got.gen_seconds else 0,
               "ttft": round(statistics.median(got.ttfts), 2) if got.ttfts else 0}
        if not passed:
            # Keep what the model actually said, so a failure can be told apart
            # from a flaky reply without rerunning it.
            row["transcript"] = [
                {"role": m["role"], "content": str(m["content"])[:1500]}
                for m in session.messages[1:]][-12:]
        rows.append(row)
        say(row)
    return rows


def run_scout(core, only=None, say=print):
    rows = []
    for name, question, check in SCOUT_TASKS:
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            answer, stats = run_scouts(core.settings, core.llm, question, SCOUT_SOURCES)
            passed, why = check(answer)
            if not passed:
                why = f"expected: {why}; got: {' '.join(answer.split())[:110]}"
        except Exception as exc:
            passed, why, stats = False, f"{type(exc).__name__}: {exc}", {}
        gen = stats.get("gen_seconds") or 0
        row = {"task": name, "passed": bool(passed), "why": why,
               "seconds": round(time.time() - t0, 1), "steps": stats.get("calls", 0),
               "tool_calls": 0, "retries": 0, "failed_calls": 0,
               "tokens": stats.get("completion_tokens", 0),
               "tok_s": round(stats.get("completion_tokens", 0) / gen, 1) if gen else 0,
               "ttft": 0}
        rows.append(row)
        say(row)
    return rows


def run(suite="brain", api=None, model=None, only=None, keep=False, say=print,
        on_start=None):
    """Run a suite. Returns the saved report as a dict."""
    real = Settings()
    root = tempfile.mkdtemp(prefix="claire-bench-")
    # A clean vault and state, so the result depends on the model and not on
    # what Claire happens to remember or which workshop tools exist today.
    settings = Settings(vault=os.path.join(root, "vault"),
                        state_dir=os.path.join(root, "state"))
    settings.prompt_override, settings.thinking = real.prompt_override, False
    role = "scout" if suite == "scout" else "brain"
    if api:
        if role == "brain":
            settings.api_base, settings.model, settings.ctx_limit = api, model or "", 0
        else:
            settings.roles["scout"] = {"api_base": api, "model": model or ""}
    elif model and role == "brain":
        settings.model = model
    problem = boot.ensure_role(settings, role, say=lambda t: say({"note": t}))
    if problem:
        shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(problem)
    core = Core(settings)
    info = core.llm.describe(role, refresh=True)
    if on_start:
        on_start(info)
    try:
        t0 = time.time()
        rows = (run_scout(core, only, say) if suite == "scout"
                else run_brain(core, root, only, say))
        report = {
            "suite": suite, "model": info["model"], "api_base": info["api_base"],
            "ctx": info["ctx"], "when": time.strftime("%Y-%m-%d %H:%M"),
            "passed": sum(r["passed"] for r in rows), "total": len(rows),
            "seconds": round(time.time() - t0, 1),
            "tok_s": round(statistics.median([r["tok_s"] for r in rows if r["tok_s"]] or [0]), 1),
            "retries": sum(r["retries"] for r in rows), "rows": rows,
            "complete": not only,
        }
    finally:
        core.store.close()
        if keep:
            say({"note": f"kept the work folders in {root}"})
        else:
            shutil.rmtree(root, ignore_errors=True)
    out_dir = os.path.join(real.ensure_state_dir(), "bench")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = re.sub(r"[^\w.-]+", "_", info["model"])[:60]
    with open(os.path.join(out_dir, f"{stamp}-{suite}-{name}.json"), "w") as f:
        json.dump(report, f, indent=2)
    return report


def history():
    """Every saved report, oldest first."""
    out_dir = os.path.join(Settings().state_dir, "bench")
    reports = []
    for fn in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if fn.endswith(".json"):
            try:
                with open(os.path.join(out_dir, fn)) as f:
                    reports.append(json.load(f))
            except (OSError, ValueError):
                continue
    return reports
