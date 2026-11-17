"""Serve the app against a scripted model, with a realistic amount of state.

Used by run.sh. Everything lives in ~/.claire-ui-test (the sandbox needs a
folder outside temp) and is deleted when the server stops. No real model is
started and the real vault and state are never touched.
"""
import glob, os, shutil, sys, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = os.path.expanduser("~/.claire-ui-test")
shutil.rmtree(BASE, ignore_errors=True)
os.makedirs(BASE + "/state/bench")
os.environ["CLAIRE_STATE"] = BASE + "/state"
os.environ["CLAIRE_VAULT"] = BASE + "/vault"
sys.path.insert(0, ROOT)
from claire_core import boot
boot.server_spec = lambda s, r: None          # never start a real model here
from claire_core.engine import Core
from claire_core.hub import Hub
from claire_core.llm import ChatResult
from claire_core.service import Service
from claire_core.settings import Settings

for f in glob.glob(os.path.expanduser("~/.claire/bench/*.json")):
    shutil.copy(f, BASE + "/state/bench/")
# A tiny stand-in model file, for adding a model in the app.
from tests.test_models import fake_gguf
os.makedirs(BASE + "/models")
fake_gguf(BASE + "/models/tiny-chat.gguf", name="Tiny Chat", context=8192)
proj = BASE + "/uploader"
os.makedirs(proj + "/build")
open(proj + "/client.py", "w").write("def upload(path):\n    return send(path)\n\n\ndef send(path):\n    return True\n")
open(proj + "/test_client.py", "w").write("import unittest\nfrom client import upload\n\n\nclass T(unittest.TestCase):\n    def test_upload(self):\n        self.assertTrue(upload('a'))\n")

def call(name, **kw):
    body = "".join(f"<{k}>\n{v}\n</{k}>\n" if k in ("content", "task", "search", "replace") else f"<{k}>{v}</{k}>\n" for k, v in kw.items())
    return f"Next step.\n<{name}>\n{body}</{name}>"
def done(text, why="Finished, reporting."):
    return f"{why}\n<done>\n<summary>\n{text}\n</summary>\n</done>"

HOME = [
    done("Hey sir. Nothing running right now, and the inbox has one note for you: `web_search` broke earlier and I have been using a stand-in.\n\nWhat are we building?", "He is saying hello."),
    call("spawn_task", title="Add retry to the upload client", workdir=proj,
         task="Uploads fail permanently on the first network error. Add a retry with exponential backoff (3 attempts) to upload() in client.py. Do not change the public function signatures. Finished means the existing tests pass."),
    done("On it. I have started a task for the retry in the uploader; I will tell you how it went.", "Task started."),
    done("The retry is in.\n\n- `upload()` now tries up to **3 times** with exponential backoff\n- the public signatures are unchanged\n- the suite passes: `Ran 1 test ... OK`\n\nOne thing I would flag: there is no test that covers a failed-then-successful upload yet. Want me to add one?\n\n    for attempt in range(3):\n        try:\n            return send(path)\n        except OSError:\n            time.sleep(2 ** attempt)", "Relaying the report."),
    call("spawn_task", title="Clean the build folder", workdir=proj,
         task="Remove the build folder in this project."),
    done("Started. That one deletes files, so it will stop and ask you before it runs.", "Task started."),
    done("The build folder is still there. You said no to the delete, so the task left it alone.", "Relaying."),
    done("You sent me some markup: <img src=x onerror=document.title=1> and <script>document.title='pwned'</script>. I am showing it as text, not running it.", "Echoing."),
    done("A fresh chat, started from the command center.", "Answering."),
]
TASK = [
    "Finding where upload is defined.\n<search_files>\n<pattern>def upload</pattern>\n</search_files>",
    call("read_file", path="client.py"),
    call("edit_file", path="client.py", search="def upload(path):\n    return send(path, retries)", replace="x"),
    call("write_file", path="client.py", content="import time\n\n\ndef upload(path):\n    for attempt in range(3):\n        try:\n            return send(path)\n        except OSError:\n            if attempt == 2:\n                raise\n            time.sleep(2 ** attempt)\n\n\ndef send(path):\n    return True"),
    call("run_command", command="python3 -m unittest -q"),
    done("Added a retry with exponential backoff to `upload()` in client.py (3 attempts, sleeping 1s then 2s). Signatures unchanged.\n\nRan `python3 -m unittest -q`: 1 test, OK.\n\nNot done: there is no test for a failed-then-successful upload."),
    call("run_command", command="rm -rf build"),
    done("Left the build folder alone: the delete was not approved."),
]

class Fake:
    def __init__(self):
        self.scripts = {"home": list(HOME), "task": list(TASK)}
    def describe(self, role="brain", refresh=False):
        return {"role": role, "api_base": "http://127.0.0.1:8080/v1", "model": "Qwen3.6-35B-A3B-Q4_K_M", "ctx": 131072, "live": True}
    def chat(self, messages, role="brain", thinking=False, on_delta=None, cancel=None, max_tokens=None, temperature=None):
        lane = "home" if "This is your home thread" in messages[0]["content"] else "task"
        reply = self.scripts[lane].pop(0)
        if on_delta:
            for i in range(0, len(reply), 12):
                on_delta("content", reply[:i + 12]); time.sleep(0.004)
        return ChatResult(reply, "", 9000 + 400 * len(messages), len(reply) // 4, 0.4, len(reply) / 4 / 96.0)

core = Core(Settings(service_port=8799, approval_timeout=1200), llm=Fake())
core.settings.controller_enabled = False  # this fixture scripts the legacy XML protocol
core.settings.skill_router = False   # the scripted model answers only the scenario
v = core.vault; v.init()
v.remember("learned", "llama-server honours enable_thinking per request; LM Studio ignores it.", note="llama-cpp thinking", workdir=proj)
v.remember("learned", "This machine has no `python`; use `python3`.", note="python3 only", workdir=proj)
v.remember("project", "Tests run with `python3 -m unittest -q`.", workdir=proj)
v.inbox_note("tool-web_search", "`web_search` needs repair", "`web_search` failed while I was working in `~/code/uploader`.\n\n**Error:** the search page came back in a layout this tool cannot read (no result links found)\n\n**Called with:**\n- query: exponential backoff jitter")
core.store.tool_fault("web_search", "the search page came back in a layout this tool cannot read")
# An integration Claire built and sir approved, waiting for its key.
from claire_core.connectors import builder as _builder
_demo = _builder.installed_dir(core.settings, "weather")
os.makedirs(_demo, exist_ok=True)
shutil.copyfile(_builder.KIT, _demo + "/claire_kit.py")
open(_demo + "/integration.json", "w").write(__import__("json").dumps({
    "name": "weather", "title": "Weather", "version": 1, "description": "Today's forecast for a city.",
    "hosts": ["api.weather.example"], "docs": ["https://api.weather.example/docs"],
    "credentials": [{"key": "WEATHER_KEY", "label": "Weather API key",
                     "how": "weather.example → Account → API keys → Create, and copy it."}],
    "actions": [{"name": "today", "effect": "read", "params": {}, "required": [],
                 "description": "The forecast for today."}]}))
open(_demo + "/server.py", "w").write("from claire_kit import action, run\n\n@action('today')\ndef today():\n    return 'sunny'\n\nif __name__ == '__main__':\n    run()\n")
core.connectors.upsert_built(_builder.installed_manifest(core.settings, "weather"))
hub = Hub(core, start_brain=False).start()
service = Service(hub)
threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

def wait(pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if any(pred(e) for e in core.store.events_after(0)): return
        time.sleep(0.1)
    raise SystemExit("scenario stalled")
chat = hub.new_chat()["id"]       # no main chat: the scenario is an ordinary chat
def say(text):
    turn = hub.post(chat, text)
    wait(lambda e: e["type"] == "turn_end" and e.get("turn") == turn)
say("hey claire")
say("uploads keep dying on flaky wifi. can you add a retry to the upload client in ~/code/uploader? 3 attempts, backoff")
wait(lambda e: e["type"] == "answer" and "The retry is in" in e.get("text", ""))
say("nice. also clear out the build folder in there")
wait(lambda e: e["type"] == "approval_request")
tasks = core.store.threads(kind="task")
open(BASE + "/info.txt", "w").write(f"http://127.0.0.1:8799/?token={service.token}\n" + "\n".join(t["id"] + " " + t["title"] for t in tasks) + "\n")
print("ready", flush=True)
try:
    time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 900)
finally:
    service.shutdown_all(); service.server_close()
    shutil.rmtree(BASE, ignore_errors=True)
