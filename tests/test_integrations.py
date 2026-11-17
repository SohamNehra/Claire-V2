"""Integrations Claire builds: written in a task's folder, checked in the
sandbox, installed only with sir's approval, run with only their own
credentials."""

import json
import os
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from claire_core.connectors import ConnectorError, builder
from tests.helpers import Env


class Service(BaseHTTPRequestHandler):
    """A tiny web service with a key: lists and adds notes."""
    items = []

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("X-Key") != "real-key":
            return self._send(401, {"error": "bad key"})
        self._send(200, {"items": self.items})

    def do_POST(self):
        if self.headers.get("X-Key") != "real-key":
            return self._send(401, {"error": "bad key"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.items.append(body["text"])
        self._send(201, {"ok": True})


MANIFEST = {
    "name": "notes_api", "title": "Notes API", "version": 1,
    "description": "Lists and adds notes in sir's notes service.",
    "hosts": ["127.0.0.1"],
    "docs": ["https://notes.example.com/api"],
    "credentials": [{"key": "NOTES_KEY", "label": "Notes API key",
                     "how": "Open the notes service, Settings → API, and copy the key shown there."}],
    "actions": [
        {"name": "list_items", "effect": "read", "params": {}, "required": [],
         "description": "Every note, newest last."},
        {"name": "add_item", "effect": "change", "params": {"text": "the note's text"},
         "required": ["text"], "description": "Add one note."}],
    "check": {"action": "list_items", "args": {}},
}

SERVER = '''from claire_kit import action, http, secret, run

BASE = "http://127.0.0.1:{port}/v1/items"


@action("list_items")
def list_items():
    return http("GET", BASE, headers={{"X-Key": secret("NOTES_KEY")}}).json()["items"]


@action("add_item")
def add_item(text):
    http("POST", BASE, headers={{"X-Key": secret("NOTES_KEY")}}, json_body={{"text": text}})
    return "added"


if __name__ == "__main__":
    run()
'''

TEST = '''from claire_kit import testing, call
import server

with testing.fixtures({{"GET http://127.0.0.1:{port}/v1/items": {{"items": ["one"]}},
                       "POST http://127.0.0.1:{port}/v1/items": {{"ok": True}}}},
                      secrets={{"NOTES_KEY": "k"}}) as sent:
    assert call("list_items") == ["one"]
    assert call("add_item", text="two") == "added"
    assert sent[1].body == {{"text": "two"}}
    assert sent[0].headers["X-Key"] == "k"
print("all tests passed")
'''


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.core = self.env.core
        if not self.core.sandbox.available:
            self.skipTest("integrations are checked in the OS sandbox")
        self.addCleanup(self.core.connectors.close)
        Service.items = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Service)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.port = self.http.server_address[1]
        self.folder = os.path.join(self.env.work, "notes_api")

    def write(self, manifest=None, server=None, test=None):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, "integration.json"), "w") as f:
            json.dump(manifest or MANIFEST, f)
        with open(os.path.join(self.folder, "server.py"), "w") as f:
            f.write(server or SERVER.format(port=self.port))
        with open(os.path.join(self.folder, "test.py"), "w") as f:
            f.write(test or TEST.format(port=self.port))
        shutil.copyfile(builder.KIT, os.path.join(self.folder, "claire_kit.py"))

    def tool(self, **args):
        session = self.env.session()
        text, failed, _ = session._execute("build_integration", args)
        return text

    def test_a_new_integration_passes_its_own_check(self):
        out = self.tool(action="new", name="youtube", title="YouTube", host="www.googleapis.com",
                        description="Search YouTube and read video details.")
        self.assertIn("edit exactly these files:\n  youtube/integration.json", out)
        self.assertIn("PASSED: YouTube is ready to submit", self.tool(action="check", name="youtube"))
        self.assertIn("already has files", self.tool(action="new", name="youtube", host="x.example.com"))

    def test_every_problem_in_a_manifest_is_listed_at_once(self):
        bad = dict(MANIFEST, name="Bad Name", hosts=["https://api.example.com/v1"], docs=[],
                   credentials=[{"key": "lower", "label": "x", "how": "x"}],
                   actions=[{"name": "go", "effect": "maybe", "description": "x"}])
        with self.assertRaises(ConnectorError) as caught:
            builder.validate(bad)
        text = str(caught.exception)
        for part in ("name:", "hosts:", "UPPER_CASE", "effect is", "action go: a description", "docs:"):
            self.assertIn(part, text)
        with self.assertRaises(ConnectorError):
            builder.validate(dict(MANIFEST, name="gmail"))              # a built-in's name

    def test_code_that_goes_around_the_kit_is_refused(self):
        for line, expected in (("import requests", "imports requests"),
                               ("from subprocess import run", "imports subprocess"),
                               ("import os\nos.system('ls')", "os.system()"),
                               ("eval('1')", "calls eval()")):
            self.write(server=line + "\n" + SERVER.format(port=self.port))
            with self.assertRaises(ConnectorError) as caught:
                builder.check(self.core, self.folder)
            self.assertIn(expected, str(caught.exception))

    def test_the_code_and_the_manifest_must_offer_the_same_actions(self):
        self.write(server=SERVER.format(port=self.port).replace('@action("add_item")', '@action("add_note")'))
        with self.assertRaises(ConnectorError) as caught:
            builder.check(self.core, self.folder)
        self.assertIn("missing @action in server.py: add_item", str(caught.exception))
        self.assertIn("not declared in integration.json: add_note", str(caught.exception))

    def test_a_failing_test_is_reported_with_its_output(self):
        self.write(test=TEST.format(port=self.port).replace('== ["one"]', '== ["two"]'))
        with self.assertRaises(ConnectorError) as caught:
            builder.check(self.core, self.folder)
        self.assertIn("test.py failed", str(caught.exception))
        self.assertIn("AssertionError", str(caught.exception))

    def test_checks_run_offline_and_with_the_real_kit(self):
        # A test that reaches for the real service fails: checks have no network.
        online = ("import urllib.request\nurllib.request.urlopen('http://127.0.0.1:%d/v1/items', timeout=3)\n"
                  % self.port)
        self.write(test=online)
        with self.assertRaises(ConnectorError) as caught:
            builder.check(self.core, self.folder)
        self.assertIn("test.py failed", str(caught.exception))
        # A copy of the kit edited to skip the host list changes nothing.
        self.write(server=SERVER.format(port=self.port).replace("http://127.0.0.1:", "https://elsewhere.example:"))
        with open(os.path.join(self.folder, "claire_kit.py"), "a") as f:
            f.write("\ndef _allowed(host):\n    return None\n")
        with self.assertRaises(ConnectorError) as caught:
            builder.check(self.core, self.folder)
        self.assertIn("elsewhere.example is not one of this integration's hosts", str(caught.exception))

    def test_install_waits_for_sir_and_then_works_with_his_credentials(self):
        self.write()
        self.env.allow = False
        out = self.tool(action="submit", name="notes_api")
        self.assertIn("did not approve", out)
        asked = self.env.asked[-1]
        self.assertEqual((asked["kind"], asked["flag"]), ("integration", "NEW INTEGRATION"))
        for part in ("Install the integration Notes API (`notes_api`)", "Reaches: 127.0.0.1",
                     "Needs from you: Notes API key (NOTES_KEY)", "Only reads (no asking each time): list_items",
                     "Changes things (asks you every time): add_item", "tests passed in the sandbox"):
            self.assertIn(part, asked["title"])
        self.assertFalse(os.path.exists(builder.installed_dir(self.core.settings, "notes_api")))

        self.env.allow = True
        out = self.tool(action="submit", name="notes_api")
        self.assertIn("waits for sir to enter Notes API key (NOTES_KEY)", out)
        conn = self.core.connectors
        row = next(r for r in self.core.store.integrations() if r["provider"] == "built")
        self.assertNotEqual(row["status"], "connected")
        waiting = self.env.session(kind="home").system_prompt()
        self.assertIn(f"installed as `{row['id']}`, not working yet", waiting)    # known, but not usable yet
        self.assertNotIn(f"connected as `{row['id']}`", waiting)
        view = conn.set_credentials(row["id"], {"NOTES_KEY": "real-key"})
        self.assertEqual((view["status"], view["account_label"]), ("connected", "Notes API"), view["error"])
        self.assertEqual({o["name"]: o["writes"] for o in view["operations"]},
                         {"list_items": False, "add_item": True})
        asked = []
        out = conn.call(row["id"], "add_item", {"text": "milk"}, ask=lambda t, f: asked.append(t) or True)
        self.assertEqual(len(asked), 1)                                   # a change asks
        self.assertEqual(Service.items, ["milk"])
        self.assertIn("milk", conn.call(row["id"], "list_items", {}, ask=lambda *a: self.fail("reading asks")))
        with self.assertRaises(ConnectorError):
            conn.set_credentials(row["id"], {"OTHER_KEY": "x"})            # only what it declares

        # An update says what changed, keeps sir's credentials, and keeps the old version.
        newer = dict(MANIFEST, actions=MANIFEST["actions"] + [
            {"name": "count_items", "effect": "read", "params": {}, "required": [],
             "description": "How many notes there are."}])
        self.write(manifest=newer, test=TEST.format(port=self.port) + '\nwith testing.fixtures({"GET http://127.0.0.1:%d/v1/items": {"items": ["one"]}}, secrets={"NOTES_KEY": "k"}):\n    assert call("count_items") == 1\n' % self.port, server=SERVER.format(port=self.port).replace(
            'if __name__', '@action("count_items")\ndef count_items():\n    return len(list_items())\n\n\nif __name__'))
        self.tool(action="submit", name="notes_api")
        self.assertEqual(self.env.asked[-1]["flag"], "UPDATE INTEGRATION")
        self.assertIn("Changed since the installed version: new actions: count_items", self.env.asked[-1]["title"])
        self.assertEqual(conn._row(row["id"])["status"], "connected")
        self.assertEqual(conn.call(row["id"], "count_items", {}, ask=None).split("\n")[-1], "1")
        history = os.path.join(builder.installed_root(self.core.settings), ".history", "notes_api")
        self.assertEqual(len(os.listdir(history)), 1)

    def test_an_installed_integration_cannot_be_changed_by_a_task(self):
        self.write()
        self.tool(action="submit", name="notes_api")
        installed = os.path.join(builder.installed_dir(self.core.settings, "notes_api"), "server.py")
        result = self.core.sandbox.run(f"echo hacked >> '{installed}'", cwd=self.env.work)
        self.assertNotEqual(result.returncode, 0)
        with open(installed) as f:
            self.assertNotIn("hacked", f.read())


class SkillExampleTests(unittest.TestCase):
    """The worked example in the building-integrations skill is real code that
    passes a real check -- an example Claire follows must not be wrong."""

    setUp = BuildTests.setUp

    @staticmethod
    def block(text, heading):
        part = text.split(heading, 1)[1]
        lines, started = [], False
        for line in part.splitlines()[1:]:
            if line.startswith("    ") or (started and not line.strip()):
                started = True
                lines.append(line[4:])
            elif started:
                break
        return "\n".join(lines).strip() + "\n"

    def test_the_youtube_example_in_the_skill_passes_check(self):
        import claire_core.skillset as skillset
        with open(os.path.join(skillset.BUILTIN, "building-integrations.md")) as f:
            text = f.read()
        os.makedirs(os.path.join(self.folder, "fixtures"), exist_ok=True)
        manifest = json.loads(self.block(text, "## Step 3: integration.json"))
        self.folder = os.path.join(self.env.work, manifest["name"])
        os.makedirs(os.path.join(self.folder, "fixtures"), exist_ok=True)
        with open(os.path.join(self.folder, "integration.json"), "w") as f:
            json.dump(manifest, f)
        with open(os.path.join(self.folder, "server.py"), "w") as f:
            f.write(self.block(text, "## Step 4: server.py"))
        with open(os.path.join(self.folder, "test.py"), "w") as f:
            f.write(self.block(text, "## Step 5: test.py"))
        with open(os.path.join(self.folder, "fixtures", "search.json"), "w") as f:
            json.dump({"items": [{"id": {"videoId": "abcdefghijk"}, "snippet": {
                "title": "Lofi beats", "channelTitle": "Chill", "publishedAt": "2026-01-02T03:04:05Z"}}]}, f)
        found, stage, output = builder.check(self.core, self.folder)
        shutil.rmtree(stage, ignore_errors=True)
        self.assertEqual(found["name"], "youtube")
        self.assertIn("all tests passed", output)


if __name__ == "__main__":
    unittest.main()


class GuardTests(unittest.TestCase):
    """What the live run of "connect my YouTube" went wrong on, made impossible."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        if not self.env.core.sandbox.available:
            self.skipTest("integrations are checked in the OS sandbox")

    def run_tool(self, session, tool, **args):
        return session._execute(tool, args)[0]

    def test_writing_under_the_work_folders_own_name_is_refused_with_the_right_path(self):
        session = self.env.session()
        here = os.path.basename(self.env.work)
        text = self.run_tool(session, "write_file", path=f"{here}/youtube/server.py", content="x")
        self.assertIn("write 'youtube/server.py'", text)
        self.assertFalse(os.path.exists(os.path.join(self.env.work, here)))
        self.assertNotIn("ERROR", self.run_tool(session, "write_file", path="youtube/server.py", content="x"))

    def test_the_build_tool_loads_its_skill_and_the_skills_it_requires(self):
        session = self.env.session()
        out = self.run_tool(session, "build_integration", action="new", name="youtube",
                            host="www.googleapis.com", title="YouTube", description="Search videos.")
        self.assertIn("building-integrations skill is now loaded", out)
        self.assertEqual(session.skills, ["coding", "researching-apis", "testing", "uat", "building-integrations"])
        self.assertIn("youtube/integration.json\n  youtube/server.py", out)
        self.assertNotIn("now loaded", self.run_tool(session, "build_integration", action="check", name="youtube"))

    def test_the_template_cannot_be_submitted_and_stray_edits_are_found(self):
        session = self.env.session()
        self.run_tool(session, "build_integration", action="new", name="youtube",
                      host="www.googleapis.com", title="YouTube", description="Search videos.")
        stray = os.path.join(self.env.work, "elsewhere", "youtube")
        os.makedirs(stray)
        with open(os.path.join(stray, "integration.json"), "w") as f:
            json.dump(dict(MANIFEST, name="youtube"), f)
        out = self.run_tool(session, "build_integration", action="submit", name="youtube")
        self.assertIn("still the starting template", out)
        self.assertIn("Your edits went elsewhere: elsewhere/youtube/", out)
        self.assertEqual(self.env.asked, [])                       # sir was never asked

    def test_a_skill_named_for_a_task_brings_what_it_requires(self):
        from claire_core.hub import Hub
        hub = Hub(self.env.core, start_brain=False)
        hub.post = lambda *a, **k: 1
        row = hub.spawn_task("Build YouTube", "build it", self.env.work, skills=["building-integrations"])
        self.assertEqual(row["skills"], "coding,researching-apis,testing,uat,building-integrations")

    def test_a_tool_call_copied_from_its_documentation_is_refused(self):
        session = self.env.session(kind="home")
        from claire_core import protocol
        doc = self.env.core.registry.get("schedule_task").doc
        block = doc[doc.index("<schedule_task>"):doc.index("</schedule_task>") + len("</schedule_task>")]
        name, args = protocol.parse_tool_calls(block, ["schedule_task"])[0]
        text, failed, _ = session._execute(name, args)
        self.assertTrue(failed)
        self.assertIn("the example from schedule_task's documentation", text)
        self.assertEqual(self.env.asked, [])                       # nothing was put to sir
        ok = dict(args, task="Every weekday, summarise yesterday's merged pull requests for sir.")
        self.assertFalse(session._copied_example("schedule_task", ok))


class AddressTests(unittest.TestCase):
    """A service at sir's own address (his YouTrack, his GitLab): the address
    is a credential he enters, and the integration may reach only that host."""

    setUp = BuildTests.setUp
    write = BuildTests.write

    def test_an_address_sir_enters_is_the_host_it_may_reach(self):
        manifest = dict(MANIFEST, hosts=[], credentials=[
            {"key": "SERVICE_URL", "kind": "url", "label": "Your notes service address",
             "how": "The address you open the notes service at, e.g. https://notes.example.com"},
            MANIFEST["credentials"][0]])
        server = SERVER.format(port=0).replace('BASE = "http://127.0.0.1:0/v1/items"', "").replace(
            "http(\"GET\", BASE", "http(\"GET\", secret(\"SERVICE_URL\") + \"/v1/items\"").replace(
            "http(\"POST\", BASE", "http(\"POST\", secret(\"SERVICE_URL\") + \"/v1/items\"")
        test = TEST.format(port=self.port).replace('secrets={"NOTES_KEY": "k"}',
                                                   'secrets={"NOTES_KEY": "k", "SERVICE_URL": "http://127.0.0.1:%d/"}' % self.port)
        folder = self.folder
        os.makedirs(folder, exist_ok=True)
        for fn, body in (("integration.json", json.dumps(manifest)), ("server.py", server), ("test.py", test)):
            with open(os.path.join(folder, fn), "w") as f:
                f.write(body)
        session = self.env.session()
        out = session._execute("build_integration", {"action": "submit", "name": "notes_api"})[0]
        self.assertIn("waits for sir to enter", out, out)
        self.assertIn("Reaches: the address you enter for Your notes service address", self.env.asked[-1]["title"])
        conn = self.core.connectors
        row = next(r for r in self.core.store.integrations() if r["provider"] == "built")
        kinds = {c["key"]: c["secret"] for c in conn.view(row)["credentials"]}
        self.assertEqual(kinds, {"SERVICE_URL": False, "NOTES_KEY": True})     # the address is not hidden
        for bad in ("ftp://127.0.0.1", "https://user:pw@notes.example.com", "http://notes.example.com"):
            with self.assertRaises(ConnectorError, msg=bad):
                conn.set_credentials(row["id"], {"SERVICE_URL": bad, "NOTES_KEY": "real-key"})
        view = conn.set_credentials(row["id"], {"SERVICE_URL": f"http://127.0.0.1:{self.port}/",
                                                "NOTES_KEY": "real-key"})
        self.assertEqual(view["status"], "connected", view["error"])
        conn.call(row["id"], "add_item", {"text": "from my instance"}, ask=lambda *a: True)
        self.assertEqual(Service.items, ["from my instance"])

    def test_an_update_lists_every_change_and_old_installs_still_load(self):
        old = dict(MANIFEST, hosts=["placeholder.example.com"], docs=None,
                   credentials=[dict(MANIFEST["credentials"][0], key="SERVICE_URL")])
        old.pop("docs")
        installed = builder.validate(old, building=False)            # from before sources were required
        new = builder.validate(dict(MANIFEST, hosts=[], credentials=[dict(MANIFEST["credentials"][0],
                                    key="SERVICE_URL", kind="url")], actions=MANIFEST["actions"][:1]))
        text = builder.summary(new, installed, 10)
        for part in ("no longer reaches: placeholder.example.com", "SERVICE_URL is now an address you enter",
                     "removed actions: add_item", "Built from: https://notes.example.com/api",
                     "Reaches: the address you enter for Notes API key"):
            self.assertIn(part, text)
        with self.assertRaises(ConnectorError):
            builder.validate(old)                                     # but a new build needs sources

    def test_a_live_failure_of_a_built_integration_is_reported_to_claire(self):
        self.write()
        session = self.env.session()
        session._execute("build_integration", {"action": "submit", "name": "notes_api"})
        posted = []
        self.core.hub = type("Hub", (), {"post": lambda _, tid, text: posted.append((tid, text))})()
        self.addCleanup(setattr, self.core, "hub", None)
        row = next(r for r in self.core.store.integrations() if r["provider"] == "built")
        view = self.core.connectors.set_credentials(row["id"], {"NOTES_KEY": "wrong-key"})
        self.assertNotEqual(view["status"], "connected")
        self.assertEqual(posted[0][0], "home")
        self.assertTrue(posted[0][1].startswith("[integration report] The integration you built, Notes API"))
        self.assertIn("401", posted[0][1])
        posted.clear()
        self.core.connectors.set_credentials(row["id"], {"NOTES_KEY": "real-key"})
        self.assertEqual(posted, [])                                  # working: nothing to report

    def test_a_manifest_needs_a_host_or_an_address(self):
        with self.assertRaises(ConnectorError) as caught:
            builder.validate(dict(MANIFEST, hosts=[]))
        self.assertIn("kind \"url\"", str(caught.exception))
        with self.assertRaises(ConnectorError):
            builder.validate(dict(MANIFEST, credentials=[dict(MANIFEST["credentials"][0], kind="password")]))


class LiveStateTests(unittest.TestCase):
    """Claire is told what exists now, not what a task once reported."""

    setUp = BuildTests.setUp
    write = BuildTests.write

    def prompt(self):
        session = self.env.session(kind="home")
        session.refresh()
        return session.system_prompt()

    def test_the_list_of_built_integrations_follows_what_is_really_there(self):
        self.assertIn("Integrations you built, as they are right now", self.prompt())
        self.assertIn("- none are installed.", self.prompt())
        self.write()
        self.env.session()._execute("build_integration", {"action": "submit", "name": "notes_api"})
        self.assertIn("- notes_api: installed as `notes-api`, not working yet: waiting for sir", self.prompt())
        self.core.connectors.set_credentials("notes-api", {"NOTES_KEY": "real-key"})
        self.assertIn("- notes_api: connected as `notes-api`.", self.prompt())
        self.core.connectors.disconnect("notes-api", delete=True)
        shutil.rmtree(builder.installed_dir(self.core.settings, "notes_api"))
        self.assertIn("- none are installed.", self.prompt())
