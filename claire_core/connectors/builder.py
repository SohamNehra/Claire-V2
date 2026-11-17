"""Building, checking and installing the integrations Claire writes herself.

The life of one:

    new      scaffolded in the task's folder: integration.json, server.py,
             test.py and the kit, all of which Claire then edits.
    check    the manifest is validated, server.py is read for things the kit
             exists to replace (sockets, subprocesses, eval), and the tests run
             in the strict sandbox with no network and no home directory,
             against a fresh copy of the kit so an edited copy changes nothing.
    submit   a passing check is put to sir as an approval: what the
             integration reaches, which credentials it needs, which actions
             change things, and -- for an update -- what changed. Approved, it
             is copied into Claire's state, where no task can write; the
             version it replaces is kept in .history.

An installed integration runs as a sandboxed MCP server (connectors/built.py),
started per call with only its own credentials.
"""

import json
import os
import re
import shutil
import sys
import time
import uuid

from . import ConnectorError

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit", "claire_kit.py")
NAME = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
ACTION = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$|^127\.0\.0\.1$|^localhost$")
RESERVED = {"google", "gmail", "calendar", "mcp", "api", "browser", "notes", "mac", "built"}
FILES = re.compile(r"^(?:[\w-]+\.(?:py|json|md)|fixtures/[\w.-]+\.json)$")
MAX_BYTES = 400_000
# What the kit is there to replace. Not a security boundary -- the sandbox is --
# but a sign the code went around the kit, and so around its host list.
BANNED = [
    (re.compile(r"^\s*(?:import|from)\s+(socket|ssl|subprocess|multiprocessing|ctypes|urllib|http\.client"
                r"|requests|httpx|aiohttp|pycurl|asyncio)\b", re.M),
     "imports {0}: make web requests with http() from claire_kit, which keeps to the hosts in integration.json"),
    (re.compile(r"\bos\.(system|popen|exec\w*|spawn\w*|fork)\s*\("), "calls os.{0}(): an integration runs no programs"),
    (re.compile(r"(?<![\w.])(eval|exec|compile|__import__)\s*\("), "calls {0}(): an integration runs only its own code"),
]


class BuildError(ConnectorError):
    """Something about an integration Claire is building is wrong."""


def installed_root(settings):
    return os.path.join(settings.state_dir, "integrations")


def installed_dir(settings, name):
    return os.path.join(installed_root(settings), name)


def load_manifest(folder):
    path = os.path.join(folder, "integration.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise BuildError(f"there is no integration.json in {folder}")
    except ValueError as exc:
        raise BuildError(f"integration.json is not valid JSON: {exc}")


def validate(data, building=True):
    """A clean manifest, or BuildError listing every problem at once.
    building=False reads one already installed, from before a rule existed."""
    problems = []
    if not isinstance(data, dict):
        raise BuildError("integration.json must be a JSON object")
    name = str(data.get("name") or "")
    if not NAME.match(name):
        problems.append("name: 3-31 lowercase letters, digits or underscores, starting with a letter")
    elif name in RESERVED:
        problems.append(f"name: {name!r} belongs to a built-in connector; choose another")
    title = " ".join(str(data.get("title") or "").split())
    if not 2 <= len(title) <= 60:
        problems.append("title: what sir sees, 2-60 characters (e.g. \"YouTube\")")
    description = " ".join(str(data.get("description") or "").split())
    if not 10 <= len(description) <= 400:
        problems.append("description: one or two sentences saying what it lets Claire do")
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list) or len(hosts) > 10 \
            or not all(isinstance(h, str) and HOST.match(h.lower()) for h in hosts):
        problems.append("hosts: a list of up to 10 host names it calls, e.g. [\"www.googleapis.com\"] "
                        "(no https://, no paths)")
        hosts = []
    credentials = data.get("credentials", [])
    clean_creds = []
    if not isinstance(credentials, list) or len(credentials) > 8:
        problems.append("credentials: a list (at most 8) of {key, label, how}")
        credentials = []
    for c in credentials:
        if not isinstance(c, dict) or not KEY.match(str(c.get("key") or "")):
            problems.append("credentials: each key is UPPER_CASE_WITH_UNDERSCORES, e.g. YOUTUBE_API_KEY")
            continue
        if not str(c.get("label") or "").strip() or len(str(c.get("how") or "").strip()) < 15:
            problems.append(f"credentials {c.get('key')}: give a label and `how` -- the steps sir "
                            f"follows to get it, specific enough to follow without searching")
            continue
        kind = c.get("kind", "secret")
        if kind not in ("secret", "url"):
            problems.append(f"credentials {c['key']}: kind is \"secret\" (a key or token; the default) "
                            f"or \"url\" (the address of sir's own instance, e.g. his YouTrack)")
            continue
        clean_creds.append({"key": c["key"], "label": " ".join(str(c["label"]).split())[:80],
                            "how": " ".join(str(c["how"]).split())[:600], "kind": kind})
    actions = data.get("actions")
    clean_actions = []
    if not isinstance(actions, list) or not 1 <= len(actions) <= 30:
        problems.append("actions: a list of 1-30 actions")
        actions = []
    for a in actions:
        if not isinstance(a, dict) or not ACTION.match(str(a.get("name") or "")):
            problems.append("actions: each name is lowercase_with_underscores")
            continue
        n = a["name"]
        if a.get("effect") not in ("read", "change"):
            problems.append(f"action {n}: effect is \"read\" (only looks) or \"change\" (creates, "
                            f"sends, edits or deletes anything) -- sir is asked before every change")
        if len(" ".join(str(a.get("description") or "").split())) < 10:
            problems.append(f"action {n}: a description saying what it does and returns")
        params = a.get("params") or {}
        if not isinstance(params, dict) or len(params) > 20 or not all(
                ACTION.match(str(k)) and isinstance(v, str) and v.strip() for k, v in params.items()):
            problems.append(f"action {n}: params maps each lowercase name to a description")
            params = {}
        required = a.get("required") or []
        if not isinstance(required, list) or any(r not in params for r in required):
            problems.append(f"action {n}: required lists only names that are in params")
            required = []
        clean_actions.append({"name": n, "description": " ".join(str(a.get("description") or "").split())[:300],
                              "effect": a.get("effect"), "params": params, "required": required})
    docs = data.get("docs")
    if not building and not docs:
        docs = []
    elif not isinstance(docs, list) or not 1 <= len(docs) <= 10 or not all(
            isinstance(d, str) and re.match(r"^https://[^\s]{4,300}$", d) for d in docs):
        problems.append("docs: the https addresses of the documentation pages you read for the "
                        "endpoints and responses (1-10). Sir sees them when he approves: an "
                        "integration with no sources is a guess.")
        docs = []
    if not hosts and not any(c["kind"] == "url" for c in clean_creds):
        problems.append("hosts: list the host it calls -- or, for a service at an address of sir's own "
                        "(a self-hosted or per-team instance), declare a credential with kind \"url\"")
    names = [a["name"] for a in clean_actions]
    if len(set(names)) != len(names):
        problems.append("actions: two actions share a name")
    check = data.get("check")
    if check is not None:
        spec = next((a for a in clean_actions if isinstance(check, dict) and a["name"] == check.get("action")), None)
        if spec is None or spec["effect"] != "read" or not isinstance(check.get("args", {}), dict):
            problems.append("check: {\"action\": <a read action>, \"args\": {...}} -- run after sir "
                            "enters credentials, to prove they work")
        elif (set(check.get("args", {})) - set(spec["params"]) or
              any(check.get("args", {}).get(k) in (None, "") for k in spec["required"])):
            problems.append("check: supply the read action's required arguments and no unknown arguments")
    elif building and clean_creds:
        problems.append("check: an integration needing credentials must have a read action to test them")
    if problems:
        raise BuildError("integration.json has problems:\n- " + "\n- ".join(dict.fromkeys(problems)))
    return {"name": name, "title": title, "description": description,
            "hosts": [h.lower() for h in hosts], "credentials": clean_creds, "actions": clean_actions,
            "check": check, "version": data.get("version", 1), "docs": docs}


def lint(folder):
    """Problems with the code: things the kit exists to replace."""
    problems = []
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".py") or fn in ("test.py", "claire_kit.py"):
            continue
        with open(os.path.join(folder, fn), encoding="utf-8", errors="replace") as f:
            text = f.read()
        for pattern, why in BANNED:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{fn}:{line} {why.format(m.group(1))}")
    return problems


TEMPLATE_SERVER = '''"""{title}: {description}"""

from claire_kit import action, http, secret, Fail, run


@action("ping")
def ping():
    """Replace this with the real actions. Each one is declared in integration.json."""
    response = http("GET", "https://{host}/")
    return {{"status": response.status}}


if __name__ == "__main__":
    run()
'''

TEMPLATE_TEST = '''"""Every action, against recorded answers: no network, no real credentials."""

from claire_kit import testing, call, Fail
import server

with testing.fixtures({{"GET https://{host}/": {{"ok": True}}}}) as sent:
    assert call("ping") == {{"status": 200}}
    assert sent[0].url == "https://{host}/"

print("all tests passed")
'''


def scaffold(folder, name, title, description, host):
    """A starting point that passes its own check, so the first check proves
    the pipeline and every later failure is about Claire's change."""
    if not NAME.match(name or "") or name in RESERVED:
        raise BuildError("name: 3-31 lowercase letters, digits or underscores, not a built-in's name")
    host = (host or "").strip().lower().removeprefix("https://").split("/")[0]
    if not HOST.match(host):
        raise BuildError("host: the service's API host name, e.g. www.googleapis.com")
    if os.path.isdir(folder) and os.listdir(folder):
        raise BuildError(f"{folder} already has files in it; check or edit them instead")
    os.makedirs(os.path.join(folder, "fixtures"), exist_ok=True)
    manifest = {"name": name, "title": title or name, "version": 1,
                "description": description or f"Lets Claire use {title or name}.",
                "hosts": [host], "credentials": [], "docs": [f"https://{host}/"],
                "actions": [{"name": "ping", "effect": "read", "params": {}, "required": [],
                             "description": "Placeholder: replace with the real actions."}]}
    with open(os.path.join(folder, "integration.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    fill = {"title": title or name, "description": manifest["description"], "host": host}
    with open(os.path.join(folder, "server.py"), "w", encoding="utf-8") as f:
        f.write(TEMPLATE_SERVER.format(**fill))
    with open(os.path.join(folder, "test.py"), "w", encoding="utf-8") as f:
        f.write(TEMPLATE_TEST.format(**fill))
    shutil.copyfile(KIT, os.path.join(folder, "claire_kit.py"))
    return ["integration.json", "server.py", "test.py", "claire_kit.py", "fixtures/"]


def _stage(core, folder, name):
    """The integration's own files, with the real kit, in a fresh folder."""
    root = os.path.join(core.settings.workshop_dir, "integrations")
    stage = os.path.join(root, f"{name}-{uuid.uuid4().hex[:8]}")
    os.makedirs(os.path.join(stage, "fixtures"), exist_ok=True)
    total = 0
    for base, _, files in os.walk(folder):
        for fn in files:
            rel = os.path.relpath(os.path.join(base, fn), folder)
            if rel == "claire_kit.py" or not FILES.match(rel.replace(os.sep, "/")):
                continue
            total += os.path.getsize(os.path.join(base, fn))
            if total > MAX_BYTES:
                shutil.rmtree(stage, ignore_errors=True)
                raise BuildError(f"the integration is over {MAX_BYTES // 1000} kB; keep large data out of it")
            shutil.copyfile(os.path.join(base, fn), os.path.join(stage, rel))
    shutil.copyfile(KIT, os.path.join(stage, "claire_kit.py"))
    return stage


def runner(folder, script):
    """argv that runs one of the integration's scripts with only its own
    folder importable."""
    code = ("import os, sys, runpy; d = sys.argv[1]; sys.path.insert(0, d); "
            "os.environ['CLAIRE_INTEGRATION_DIR'] = d; "
            "runpy.run_path(os.path.join(d, sys.argv[2]), run_name='__main__')")
    return [sys.executable, "-I", "-c", code, folder, script]


PROBE = '''import json, inspect
import claire_kit, server
for spec in claire_kit.manifest()["actions"]:
    fn = claire_kit._ACTIONS.get(spec["name"])
    if fn is None:
        continue
    signature = inspect.signature(fn)
    # All advertised arguments must be accepted; advertised optional arguments
    # must really be optional in Python.
    signature.bind(**{k: "example" for k in spec["params"]})
    signature.bind(**{k: "example" for k in spec.get("required", [])})
print("\\x1eACTIONS " + json.dumps(sorted(claire_kit._ACTIONS)))
'''

VERIFY = '''import json, runpy
import claire_kit
tested = set()
original = claire_kit.call
def tracked(name, **args):
    result = original(name, **args)
    tested.add(name)
    return result
claire_kit.call = tracked
runpy.run_path("test.py", run_name="__main__")
print("\\x1eTESTED " + json.dumps(sorted(tested)))
'''


def check(core, folder):
    """(manifest, staged folder) when the integration is sound, else
    BuildError with everything that is wrong."""
    folder = os.path.realpath(folder)
    if not os.path.isdir(folder):
        raise BuildError(f"there is no folder {folder}")
    manifest = validate(load_manifest(folder))
    for needed in ("server.py", "test.py"):
        if not os.path.isfile(os.path.join(folder, needed)):
            raise BuildError(f"{needed} is missing")
    problems = lint(folder)
    if problems:
        raise BuildError("server.py goes around the kit:\n- " + "\n- ".join(problems))
    sandbox = core.sandbox
    if not sandbox.available:
        raise BuildError("integrations are checked in the OS sandbox, and this machine has none")
    stage = _stage(core, folder, manifest["name"])
    scratch = os.path.join(stage, ".scratch")
    os.makedirs(scratch, exist_ok=True)
    with open(os.path.join(stage, "_probe.py"), "w", encoding="utf-8") as f:
        f.write(PROBE)
    env = {"PYTHONDONTWRITEBYTECODE": "1", "HOME": scratch, "TMPDIR": scratch}

    def run(script):
        return sandbox.run(runner(stage, script), cwd=stage, write_dirs=[scratch], strict=True,
                           network=False, read_dirs=[stage], timeout=60, env=env)
    probe = run("_probe.py")
    found = re.search(r"\x1eACTIONS (\[.*\])", probe.output)
    if probe.returncode != 0 or not found:
        shutil.rmtree(stage, ignore_errors=True)
        raise BuildError("server.py does not load:\n" + probe.output[-2500:])
    defined = set(json.loads(found.group(1)))
    declared = {a["name"] for a in manifest["actions"]}
    if defined != declared:
        shutil.rmtree(stage, ignore_errors=True)
        parts = []
        if declared - defined:
            parts.append("declared in integration.json but missing @action in server.py: "
                         + ", ".join(sorted(declared - defined)))
        if defined - declared:
            parts.append("in server.py but not declared in integration.json: " + ", ".join(sorted(defined - declared)))
        raise BuildError("the actions do not match:\n- " + "\n- ".join(parts))
    os.remove(os.path.join(stage, "_probe.py"))
    with open(os.path.join(stage, "_verify.py"), "w", encoding="utf-8") as f:
        f.write(VERIFY)
    tests = run("_verify.py")
    if tests.returncode != 0 or tests.timed_out:
        shutil.rmtree(stage, ignore_errors=True)
        why = "timed out after 60s" if tests.timed_out else f"exited {tests.returncode}"
        raise BuildError(f"test.py failed ({why}). Its output:\n" + tests.output[-3000:])
    reached = re.search(r"\x1eTESTED (\[.*\])", tests.output)
    tested = set(json.loads(reached.group(1))) if reached else set()
    missing = declared - tested
    if missing:
        shutil.rmtree(stage, ignore_errors=True)
        raise BuildError("test.py never successfully called these actions through claire_kit.call: "
                         + ", ".join(sorted(missing)) + ". Add offline fixture tests for each action.")
    os.remove(os.path.join(stage, "_verify.py"))
    shutil.rmtree(scratch, ignore_errors=True)
    return manifest, stage, tests.output.split("\x1eTESTED ")[0][-800:]


def summary(manifest, previous=None, lines_of_code=0):
    """What sir is asked to approve."""
    reads = [a["name"] for a in manifest["actions"] if a["effect"] == "read"]
    changes = [a["name"] for a in manifest["actions"] if a["effect"] == "change"]
    head = (f"{'Update' if previous else 'Install'} the integration {manifest['title']} "
            f"(`{manifest['name']}`), written by Claire.")
    out = [head, manifest["description"],
           "Reaches: " + ", ".join(manifest["hosts"] + [f"the address you enter for {c['label']}"
                                                         for c in manifest["credentials"] if c["kind"] == "url"]),
           "Needs from you: " + ("; ".join(f"{c['label']} ({c['key']})" for c in manifest["credentials"])
                                 or "nothing"),
           "Only reads (no asking each time): " + (", ".join(reads) or "none"),
           "Changes things (asks you every time): " + (", ".join(changes) or "none")]
    if previous:
        diff = []
        added = sorted(set(manifest["hosts"]) - set(previous.get("hosts", [])))
        removed = sorted(set(previous.get("hosts", [])) - set(manifest["hosts"]))
        if added:
            diff.append(f"new hosts: {', '.join(added)}")
        if removed:
            diff.append(f"no longer reaches: {', '.join(removed)}")
        old_kinds = {c["key"]: c.get("kind", "secret") for c in previous.get("credentials", [])}
        for c in manifest["credentials"]:
            if c["key"] in old_kinds and old_kinds[c["key"]] != c["kind"]:
                diff.append(f"{c['key']} is now " + ("an address you enter" if c["kind"] == "url" else "a secret"))
        gone = [a["name"] for a in previous.get("actions", [])
                if a["name"] not in {x["name"] for x in manifest["actions"]}]
        if gone:
            diff.append("removed actions: " + ", ".join(gone))
        old_actions = {a["name"]: a.get("effect") for a in previous.get("actions", [])}
        added = [a["name"] for a in manifest["actions"] if a["name"] not in old_actions]
        now_change = [a["name"] for a in manifest["actions"]
                      if a["effect"] == "change" and old_actions.get(a["name"]) == "read"]
        new_keys = sorted({c["key"] for c in manifest["credentials"]}
                          - {c["key"] for c in previous.get("credentials", [])})
        if added:
            diff.append("new actions: " + ", ".join(added))
        if now_change:
            diff.append("now change things: " + ", ".join(now_change))
        if new_keys:
            diff.append("new credentials: " + ", ".join(new_keys))
        out.append("Changed since the installed version: " + ("; ".join(diff) or "code only"))
    out.append("Built from: " + ", ".join(manifest.get("docs") or []))
    out.append(f"Code: {lines_of_code} lines. Its tests passed in the sandbox, offline, against "
               f"recorded answers; it is first tried for real when you enter its credentials.")
    return "\n".join(out)


def install(settings, stage, name):
    """Move a checked integration into place. The version it replaces goes to
    .history/<name>/<time>/. Returns the installed folder."""
    root = installed_root(settings)
    dest = installed_dir(settings, name)
    os.makedirs(root, mode=0o700, exist_ok=True)
    if os.path.isdir(dest):
        keep = os.path.join(root, ".history", name, time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        shutil.move(dest, keep)
    shutil.move(stage, dest)
    return dest


def is_template(manifest):
    """The scaffold, unchanged in substance: its one placeholder action."""
    return [a["name"] for a in manifest["actions"]] == ["ping"] and \
        manifest["actions"][0]["description"].startswith("Placeholder")


def strays(workdir, name, folder):
    """Other integration.json files for this name in the work folder --
    usually edits written to a path that was not the one meant."""
    found = []
    for base, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
        if "integration.json" in files and os.path.realpath(base) != os.path.realpath(folder):
            try:
                with open(os.path.join(base, "integration.json"), encoding="utf-8") as f:
                    if json.load(f).get("name") == name:
                        found.append(os.path.relpath(base, workdir) + "/")
            except (OSError, ValueError):
                continue
        if len(found) >= 5:
            break
    return found


def installed_manifest(settings, name):
    try:
        return validate(load_manifest(installed_dir(settings, name)), building=False)
    except BuildError:
        return None
