import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request

from claire_core.client import Client, ServiceError
from claire_core.hub import Hub
from claire_core.llm import ChatResult
from claire_core.service import Service
from tests.helpers import Env, done


class RoutedLLM:
    """Separate scripts for the home thread and for task threads, since the
    two lanes call the model at the same time."""

    def __init__(self, home, task):
        self.scripts = {"home": list(home), "task": list(task)}
        self.lock = threading.Lock()
        self.seen = {"home": [], "task": []}

    def describe(self, role="brain", refresh=False):
        return {"role": role, "api_base": "http://fake/v1", "model": "fake",
                "ctx": 100000, "live": True}

    def chat(self, messages, role="brain", thinking=False, on_delta=None,
             cancel=None, max_tokens=None, temperature=None):
        lane = "home" if "This is your home thread" in messages[0]["content"] else "task"
        with self.lock:
            self.seen[lane].append(messages[-1]["content"])
            reply = self.scripts[lane].pop(0)
        if callable(reply):
            reply = reply(messages)
        if on_delta:
            on_delta("content", reply)
        return ChatResult(reply, "", 0, 0)


class ServiceCase(unittest.TestCase):
    def boot(self, home=(), task=()):
        self.env = Env(service_port=0, approval_timeout=5)
        self.llm = RoutedLLM(home, task)
        self.env.core.llm = self.llm
        self.hub = Hub(self.env.core, start_brain=False).start()
        self.service = Service(self.hub)
        threading.Thread(target=self.service.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.service.server_address[1]
        self.client = Client(self.env.core.settings)
        self.client.base = f"http://127.0.0.1:{self.port}"
        self.client.token = self.service.token
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.service.shutdown_all()
        self.service.server_close()
        self.env.close()

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.env.core.store.events_after(0)
            hit = [e for e in events if predicate(e)]
            if hit:
                return hit[0]
            time.sleep(0.05)
        self.fail("timed out; events: " + ", ".join(
            f"{e['thread']}:{e['type']}" for e in self.env.core.store.events_after(0)))

    def raw(self, path, headers=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {}, method=method,
                                     data=b"{}" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code


class AccessTests(ServiceCase):
    def test_only_a_caller_with_the_token_gets_in(self):
        self.boot()
        good = {"Authorization": f"Bearer {self.service.token}"}
        self.assertEqual(self.raw("/health"), 200)
        self.assertEqual(self.raw("/threads"), 403)
        self.assertEqual(self.raw("/threads", {"Authorization": "Bearer wrong"}), 403)
        self.assertEqual(self.raw("/threads", good), 200)
        self.assertEqual(self.raw("/threads/home/messages", method="POST"), 403)
        self.assertEqual(self.raw("/shutdown", method="POST"), 403)

    def test_a_web_page_or_a_rebound_hostname_is_refused_even_with_the_token(self):
        self.boot()
        good = {"Authorization": f"Bearer {self.service.token}"}
        self.assertEqual(self.raw("/threads", dict(good, Origin="https://evil.example")), 403)
        self.assertEqual(self.raw("/threads", dict(good, Host="evil.example")), 403)
        self.assertEqual(self.raw(
            "/threads", dict(good, Origin=f"http://127.0.0.1:{self.port}")), 200)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits do not describe Windows NTFS permissions")
    def test_the_token_file_is_private(self):
        self.boot()
        mode = os.stat(self.env.core.settings.token_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class AppTests(ServiceCase):
    def get(self, path, headers=None):
        """A request that does not follow redirects, like a browser's first hit."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        res = conn.getresponse()
        body = res.read().decode("utf-8", "replace")
        conn.close()
        return res, body

    def test_the_link_signs_the_browser_in_and_drops_the_token(self):
        self.boot()
        res, _ = self.get(f"/?token={self.service.token}")
        self.assertEqual(res.status, 303)
        self.assertEqual(res.getheader("Location"), "/")       # no token left behind
        cookie = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        signed_in = {"Cookie": cookie.split(";")[0]}
        page, html = self.get("/", signed_in)
        self.assertEqual(page.status, 200)
        self.assertIn('<script src="/app.js">', html)
        self.assertEqual(self.get("/threads", signed_in)[0].status, 200)

    def test_a_browser_without_sign_in_is_told_how_to_sign_in_and_gets_nothing(self):
        self.boot()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        page = caught.exception.read().decode()
        self.assertEqual(caught.exception.code, 403)
        for hint in ("claire2 app", 'claire2 app --browser "Google Chrome"', "claire2 app --print"):
            self.assertIn(hint, page)
        self.assertNotIn(self.service.token, page)
        self.assertNotIn("Set-Cookie", str(caught.exception.headers))

    def test_the_page_cannot_run_inline_or_foreign_script(self):
        self.boot()
        res, html = self.get("/", {"Authorization": f"Bearer {self.service.token}"})
        policy = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("unsafe", policy)
        self.assertNotIn("<script>", html)                     # nothing inline to allow
        self.assertNotIn(" onclick=", html)
        self.assertNotIn(" style=", html)

    def test_without_signing_in_there_is_no_app_and_no_data(self):
        self.boot()
        for path in ["/", "/app.js", "/app.css", "/vault", "/bench"]:
            self.assertEqual(self.get(path)[0].status, 403, path)
        self.assertEqual(self.get("/?token=wrong")[0].status, 403)
        stolen = {"Cookie": f"claire_{self.port}=wrong"}
        self.assertEqual(self.get("/threads", stolen)[0].status, 403)
        # A signed-in browser being used by another site is still refused.
        good = {"Cookie": f"claire_{self.port}={self.service.token}",
                "Origin": "https://evil.example"}
        self.assertEqual(self.get("/threads", good)[0].status, 403)

    def test_only_served_files_exist(self):
        self.boot()
        auth = {"Authorization": f"Bearer {self.service.token}"}
        for path in ["/../service.py", "/app.js/../../store.py", "/index.html", "/web/app.js"]:
            self.assertEqual(self.get(path, auth)[0].status, 404, path)

    def test_the_vault_is_browsable_but_only_the_vault(self):
        self.boot()
        self.env.core.vault.remember("learned", "Deploys go out on Fridays.",
                                     note="deploys", workdir=self.env.work)
        notes = self.client.get("/vault")["notes"]
        self.assertIn("learned/deploys.md", [n["path"] for n in notes])
        self.assertIn("Fridays", self.client.get("/vault/learned/deploys.md")["text"])
        for bad in ["/vault/..%2F..%2Fstate%2Ftoken", "/vault/learned/missing.md",
                    "/vault/..%2Fvault%2Flearned%2Fdeploys.txt"]:
            with self.assertRaises(ServiceError, msg=bad):
                self.client.get(bad)

    def test_a_thread_can_be_replayed_and_its_mode_changed(self):
        self.boot(home=[done("Hey sir.")])
        turn = self.client.post("/threads/home/messages", {"text": "hey"})["turn"]
        self.wait_for(lambda e: e["type"] == "turn_end" and e.get("turn") == turn)
        events = self.client.get("/threads/home/events")["events"]
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "turn_start")
        self.assertIn("answer", kinds)
        self.assertNotIn("answer_delta", kinds)               # live-only, never stored
        self.assertEqual([e["seq"] for e in events], sorted(e["seq"] for e in events))
        self.assertFalse(self.client.get("/threads/home")["thread"]["thinking"])
        self.client.post("/threads/home/mode", {"thinking": True})
        self.assertTrue(self.client.get("/threads/home")["thread"]["thinking"])


class ThreadTests(ServiceCase):
    def test_home_answers_a_message(self):
        self.boot(home=[done("Hey sir.")])
        turn = self.client.post("/threads/home/messages", {"text": "hey"})["turn"]
        self.wait_for(lambda e: e["type"] == "turn_end" and e.get("turn") == turn)
        answer = self.wait_for(lambda e: e["type"] == "answer")
        self.assertEqual((answer["thread"], answer["text"]), ("home", "Hey sir."))
        saved = self.client.get("/threads/home")["messages"]
        self.assertEqual([m["role"] for m in saved], ["user", "assistant"])

    def test_home_starts_a_task_which_works_and_reports_back(self):
        self.boot()
        project = os.path.join(self.env.root, "proj")
        os.makedirs(project)
        spawn = ("He wants a file made, that is a task.\n<spawn_task>\n"
                 "<title>Write the greeting</title>\n"
                 f"<workdir>{project}</workdir>\n"
                 "<task>\nCreate greeting.txt containing hello.\n</task>\n</spawn_task>")
        self.llm.scripts = {
            "home": [spawn, done("Started it."), done("It is done: greeting.txt exists.")],
            "task": ["Writing it.\n<write_file>\n<path>greeting.txt</path>\n"
                     "<content>\nhello\n</content>\n</write_file>",
                     done("Created greeting.txt with hello.")]}
        self.client.post("/threads/home/messages", {"text": "make me a greeting file"})
        self.wait_for(lambda e: e["type"] == "answer" and "It is done" in e["text"])

        with open(os.path.join(project, "greeting.txt")) as f:
            self.assertEqual(f.read(), "hello\n")
        task = self.client.get("/threads?kind=task")["threads"][0]
        self.assertEqual((task["status"], task["parent"], task["workdir"]),
                         ("done", "home", os.path.realpath(project)))
        self.assertEqual(task["summary"], "Created greeting.txt with hello.")
        # The task saw only its brief; home got the report as a message.
        self.assertEqual(self.llm.seen["task"][0], "Create greeting.txt containing hello.")
        stamp, report = self.llm.seen["home"][-1].split("\n", 1)
        self.assertRegex(stamp, r"^\[\w+ \d{4}-\d\d-\d\d \d\d:\d\d [\w/+-]+\]$")
        self.assertTrue(report.startswith("[task report] Task \"Write the greeting\""))
        self.assertIn("Created greeting.txt with hello.", report)
        # The folder became a project, and the report is filed in its memory.
        filed = self.env.core.store.project(task["project"])
        self.assertEqual(filed["path"], os.path.realpath(project))
        notes = os.listdir(os.path.join(self.env.core.vault.path, "projects", filed["slug"], "tasks"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(os.listdir(os.path.join(self.env.core.vault.path, "tasks")), [])

    def test_a_task_may_not_work_in_protected_places(self):
        self.boot()
        for bad in ["/", "~", "/etc", self.env.core.settings.state_dir, "~/.ssh", "/nope/x"]:
            with self.assertRaises(ServiceError, msg=bad):
                self.client.post("/threads", {"task": "do things", "workdir": bad})
        self.assertEqual(self.client.get("/threads?kind=task")["threads"], [])

    def test_a_background_task_waits_for_approval_and_obeys_it(self):
        risky = "Cleaning.\n<run_command>\n<command>rm -rf build</command>\n</run_command>"
        self.boot(task=[risky, done("left it alone")])
        os.makedirs(os.path.join(self.env.work, "build"))
        self.client.post("/threads", {"task": "clean up", "workdir": self.env.work})
        ask = self.wait_for(lambda e: e["type"] == "approval_request")
        self.assertEqual((ask["title"], ask["flag"]), ("rm -rf build", "DANGEROUS"))
        pending = self.client.get("/approvals")["approvals"]
        self.assertEqual(len(pending), 1)
        self.client.post(f"/approvals/{ask['id']}", {"allow": False})
        self.wait_for(lambda e: e["type"] == "turn_end")
        self.assertTrue(os.path.isdir(os.path.join(self.env.work, "build")))
        with self.assertRaises(ServiceError):          # cannot be answered twice
            self.client.post(f"/approvals/{ask['id']}", {"allow": True})

    def test_an_unanswered_approval_expires_as_a_no(self):
        risky = "Cleaning.\n<run_command>\n<command>rm -rf build</command>\n</run_command>"
        self.boot(task=[risky, done("left it alone")])
        self.env.core.settings.approval_timeout = 1
        os.makedirs(os.path.join(self.env.work, "build"))
        self.client.post("/threads", {"task": "clean up", "workdir": self.env.work})
        resolved = self.wait_for(lambda e: e["type"] == "approval_resolved")
        self.assertEqual((resolved["allowed"], resolved["expired"]), (False, True))
        self.assertTrue(os.path.isdir(os.path.join(self.env.work, "build")))

    def test_the_event_stream_replays_then_follows(self):
        self.boot(home=[done("one"), done("two")])
        t1 = self.client.post("/threads/home/messages", {"text": "first"})["turn"]
        self.wait_for(lambda e: e["type"] == "turn_end" and e.get("turn") == t1)
        got = []

        def read():
            for e in self.client.events(after=0):
                got.append(e)
                if e["type"] == "turn_end" and e.get("turn") == t1 + 1:
                    return
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        time.sleep(0.3)
        self.client.post("/threads/home/messages", {"text": "second"})
        reader.join(10)
        self.assertFalse(reader.is_alive())
        answers = [e["text"] for e in got if e["type"] == "answer"]
        self.assertEqual(answers, ["one", "two"])
        seqs = [e["seq"] for e in got if e.get("seq")]
        self.assertEqual(seqs, sorted(set(seqs)))       # nothing twice, in order

    def test_a_crash_inside_a_turn_does_not_kill_the_lane(self):
        def explode(messages):
            raise RuntimeError("boom")
        self.boot(home=[explode, done("still here")])
        t1 = self.client.post("/threads/home/messages", {"text": "one"})["turn"]
        self.wait_for(lambda e: e["type"] == "turn_end" and e.get("turn") == t1)
        self.client.post("/threads/home/messages", {"text": "two"})
        self.wait_for(lambda e: e["type"] == "answer" and e["text"] == "still here")


if __name__ == "__main__":
    unittest.main()


class ChatTests(ServiceCase):
    """Sir can hold more than one conversation with Claire."""

    def ended(self, tid):
        return self.wait_for(lambda e: e["type"] == "turn_end" and e["thread"] == tid)

    def test_a_new_chat_starts_clean_and_is_named_after_its_first_message(self):
        seen = []
        self.boot(home=["Noted, alpha.", lambda m: seen.extend(m) or "Hello from the new chat."])
        self.client.post("/threads/home/messages", {"text": "remember the word alpha"})
        self.ended("home")
        chat = self.client.post("/threads", {"kind": "chat"})["thread"]
        self.assertEqual((chat["kind"], chat["title"], chat["status"]), ("home", "New chat", "idle"))
        self.assertNotEqual(chat["id"], "home")
        self.client.post(f"/threads/{chat['id']}/messages", {"text": "plan my   weekend trip"})
        self.ended(chat["id"])
        self.assertIn("This is your home thread", seen[0]["content"])     # she is Claire here too
        self.assertFalse(any("alpha" in str(m["content"]) for m in seen[1:]))
        got = self.client.get(f"/threads/{chat['id']}")
        self.assertEqual(got["thread"]["title"], "plan my weekend trip")
        self.assertIn("Hello from the new chat.", str(got["messages"][-1]["content"]))
        main = self.client.get("/threads/home")
        self.assertIn("alpha", str(main["messages"]))
        self.assertNotIn("weekend", str(main["messages"]))
        kinds = [t["id"] for t in self.client.get("/threads?kind=home")["threads"]]
        self.assertEqual(sorted(kinds), sorted(["home", chat["id"]]))

    def test_a_chat_can_be_deleted_but_the_main_chat_and_tasks_cannot(self):
        self.boot(home=["Hi."])
        store = self.env.core.store
        chat = self.client.post("/threads", {"kind": "chat"})["thread"]["id"]
        self.client.post(f"/threads/{chat}/messages", {"text": "hello"})
        self.ended(chat)
        task = store.create_thread("task", "A task it started", self.env.work, parent=chat)
        for tid in ("home", task, "nope"):
            with self.assertRaises(ServiceError):
                self.client._request("DELETE", f"/threads/{tid}")
        self.assertEqual(self.client._request("DELETE", f"/threads/{chat}"), {"ok": True})
        self.assertIsNone(store.thread(chat))
        self.assertEqual(store.load_messages(chat), [])
        self.assertEqual([e["type"] for e in store.events_after(0, thread=chat)], ["thread_deleted"])
        self.assertEqual(store.thread(task)["parent"], "home")     # its report still has a home
        with self.assertRaises(ServiceError):
            self.client.get(f"/threads/{chat}")
        self.assertIsNone(self.hub.post(chat, "too late"))

    def test_clearing_the_main_chat_removes_only_its_own_conversation(self):
        self.boot(home=["Noted.", lambda m: "Fresh start." if not any(
            "alpha" in str(x["content"]) for x in m[1:-1]) else "I still remember alpha."])
        store = self.env.core.store
        task = store.create_thread("task", "Running work", self.env.work, parent="home")
        store.update_thread(task, status="running", summary="half way")
        done_task = store.create_thread("task", "Old work", self.env.work, parent="home")
        store.update_thread(done_task, status="done", summary="the report")
        chat = self.client.post("/threads", {"kind": "chat"})["thread"]["id"]
        self.client.post(f"/threads/{chat}/messages", {"text": "other chat"})
        self.ended(chat)
        approval = store.add_approval(task, "shell", "rm -rf build", "DANGEROUS")
        self.client.post("/threads/home/messages", {"text": "remember alpha"})
        self.ended("home")
        before = self.client.get("/status")["task_counts"]

        self.assertEqual(self.client.post("/threads/home/reset"), {"ok": True})
        self.assertEqual(self.client.get("/threads/home")["messages"], [])
        self.assertEqual(self.client.get("/status")["task_counts"], before)
        self.assertEqual((store.thread(task)["status"], store.thread(task)["summary"]),
                         ("running", "half way"))
        self.assertEqual(store.thread(done_task)["summary"], "the report")
        self.assertEqual([a["id"] for a in store.approvals()], [approval])
        self.assertIn("other chat", str(self.client.get(f"/threads/{chat}")["messages"]))
        self.client.post("/threads/home/messages", {"text": "what word?"})
        self.wait_for(lambda e: e["type"] == "answer" and e["thread"] == "home"
                      and e["text"] == "Fresh start.")

    def test_a_chat_cannot_be_cleared_while_claire_is_working_in_it(self):
        self.boot()
        self.env.core.store.update_thread("home", status="running")
        with self.assertRaises(ServiceError):
            self.client.post("/threads/home/reset")

    def test_a_chat_survives_a_restart_as_a_chat(self):
        self.boot(home=["First.", "Second."])
        chat = self.client.post("/threads", {"kind": "chat"})["thread"]["id"]
        self.client.post(f"/threads/{chat}/messages", {"text": "one"})
        self.ended(chat)
        with self.hub._sessions_lock:
            self.hub._sessions.clear()                   # as after a restart
        self.assertEqual(self.hub._session(chat).kind, "home")
        self.assertIn("one", str(self.hub._session(chat).messages))
