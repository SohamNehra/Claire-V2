import os
import unittest

from claire_core.llm import ModelUnreachable
from claire_core.tools import Tool
from tests.helpers import Env, done


def call(tool_, **params):
    name = tool_
    body = "".join(f"<{k}>\n{v}\n</{k}>\n" if "\n" in str(v) or k in ("code", "test", "content")
                   else f"<{k}>{v}</{k}>\n" for k, v in params.items())
    return f"Doing the next step.\n<{name}>\n{body}</{name}>"


class EngineCase(unittest.TestCase):
    def start(self, replies, **kw):
        self.env = Env(replies)
        self.addCleanup(self.env.close)
        self.s = self.env.session(**kw)
        return self.s

    def shown(self, n=-1):
        """The n-th tool result, as the model was shown it."""
        results = [m["content"] for m in self.s.messages
                   if m["role"] == "user" and isinstance(m["content"], str)
                   and " result]\n" in m["content"][:40]]
        return results[n]

    def types(self):
        return [e["type"] for e in self.env.events]


class LoopTests(EngineCase):
    def test_tool_call_then_answer(self):
        s = self.start([call("write_file", path="hello.txt", content="hi there"),
                        call("read_file", path="hello.txt"), done("wrote it")])
        out = s.run("make hello.txt")
        self.assertEqual(out, {"status": "done", "answer": "wrote it"})
        with open(os.path.join(self.env.work, "hello.txt")) as f:
            self.assertEqual(f.read(), "hi there\n")
        self.assertIn("hello.txt (lines 1-1 of 1)", self.shown())
        kinds = self.types()
        self.assertEqual(kinds.count("tool_call"), 2)
        self.assertEqual(kinds[-2:], ["answer", "end"])
        self.assertEqual(self.env.core.store.load_messages(s.thread_id)[-1]["role"], "assistant")

    def test_plain_prose_on_the_first_step_is_the_answer(self):
        s = self.start(["Hey! All quiet here."])
        self.assertEqual(s.run("hey")["answer"], "Hey! All quiet here.")

    def test_an_announced_action_with_no_call_is_retried_then_stopped(self):
        s = self.start(["Let me read the config first."] * 3)
        self.assertEqual(s.run("fix it")["status"], "stopped")
        hints = [m["content"] for m in s.messages if "contained no tool call" in str(m["content"])]
        self.assertEqual(len(hints), 2)
        self.assertNotEqual(hints[0], hints[1])       # a retry changes the prompt

    def test_a_curly_apostrophe_does_not_hide_an_unkept_promise(self):
        from claire_core.engine import intends_to_act
        for text in ["I’m on it. I’ll start by running the tests.", "I'll fix that now.",
                     "Let’s check the config.", "I'm on it."]:
            self.assertTrue(intends_to_act(text), text)
        for text in ["Hey sir. All quiet here.", "It is 391.", "You made me; I call you sir."]:
            self.assertFalse(intends_to_act(text), text)

    def test_a_reply_that_promises_work_nothing_started_is_sent_back_once(self):
        s = self.start([done("I'm starting a task to fix the tests now."),
                        call("list_files", path="."), done("Looked: nothing to fix.")])
        out = s.run("fix the tests")
        self.assertEqual(out["answer"], "Looked: nothing to fix.")
        self.assertEqual(self.types().count("answer"), 1)          # the promise never reached him
        self.assertIn("answer_discard", self.types())
        sent_back = [m["content"] for m in s.messages if "nothing has been started" in str(m["content"])]
        self.assertEqual(len(sent_back), 1)

    def test_it_is_only_sent_back_once_and_plain_answers_pass(self):
        s = self.start([done("I'll look into it."), done("I'll look into it.")])
        self.assertEqual(s.run("hm")["answer"], "I'll look into it.")       # second time stands
        for text in ["Hey sir. Let me know what you need.", "It is 391.", "Done: the file is fixed.",
                     "I am showing it as text, not running it.", "I'll be here when you need me.",
                     "Nothing is running; checking again would not help."]:
            t = self.start([done(text)])
            self.assertEqual(t.run("hi")["answer"], text)
            self.assertEqual(len(self.env.llm.calls), 1)

    def test_work_followed_by_a_plan_is_not_challenged(self):
        s = self.start([call("list_files", path="."), done("I'll need your go-ahead before changing anything.")])
        self.assertEqual(s.run("look")["status"], "done")
        self.assertEqual(len(self.env.llm.calls), 2)

    def test_a_failed_step_turns_thinking_on_for_the_next_one(self):
        s = self.start([call("read_file", path="missing.txt"), done()])
        s.run("read it")
        self.assertEqual([c["thinking"] for c in self.env.llm.calls], [False, True])

    def test_read_only_calls_in_one_message_all_run(self):
        both = ("Reading both.\n<list_files><path>.</path></list_files>"
                "<search_files><pattern>x</pattern></search_files>")
        s = self.start([both, done()])
        s.run("look around")
        self.assertEqual([e["name"] for e in self.env.results()], ["list_files", "search_files"])

    def test_a_run_of_failures_gets_one_nudge_to_stop_and_think(self):
        s = self.start([call("read_file", path=f"missing{i}.txt") for i in range(5)] + [done()])
        s.run("read it")
        nudged = [i for i in range(5) if "steps in a row have failed" in self.shown(i)]
        self.assertEqual(nudged, [3])
        self.assertIn("report exactly what blocked you", self.shown(3))

    def test_the_step_cap_ends_a_runaway_task(self):
        s = self.start([call("list_files", path=".")] * 3, )
        s.settings.max_steps = 3
        self.assertEqual(s.run("loop")["status"], "stopped")

    def test_an_unreachable_model_is_an_error_not_a_crash(self):
        s = self.start([ModelUnreachable("cannot reach the model server")])
        self.assertEqual(s.run("hi")["status"], "error")
        self.assertIn("cannot reach", [e for e in self.env.events if e["type"] == "notice"][0]["text"])

    def test_cancel_stops_before_the_next_step(self):
        def cancel_then_reply(messages):
            self.s.cancel.set()
            return call("list_files", path=".")
        s = self.start([cancel_then_reply])
        self.assertEqual(s.run("go")["status"], "cancelled")

    def test_a_long_conversation_is_compacted(self):
        replies = [call("list_files", path=".")] * 4 + ["SUMMARY OF EARLIER WORK", done()]
        s = self.start(replies)
        s.model = dict(s.model, ctx=3000)
        with open(os.path.join(self.env.work, "big.txt"), "w") as f:
            f.write("x" * 100)
        s.messages += [{"role": "user", "content": "filler " * 300}] * 6
        s.run("go")
        self.assertIn("compacted", self.types())
        self.assertTrue(any("SUMMARY OF EARLIER WORK" in str(m["content"]) for m in s.messages))
        # With no clerk model configured, the summary is written by the brain.
        self.assertEqual({c["role"] for c in self.env.llm.calls}, {"brain"})


class SafetyTests(EngineCase):
    def test_a_denied_command_does_not_run(self):
        s = self.start([call("run_command", command="touch made.txt"), done()])
        self.env.allow = False
        s.run("go")
        self.assertIn("User DENIED", self.shown())
        self.assertFalse(os.path.exists(os.path.join(self.env.work, "made.txt")))

    def test_a_forbidden_command_is_refused_without_asking(self):
        s = self.start([call("run_command", command="rm -rf ~"), done()], auto_approve=True)
        s.run("go")
        self.assertIn("REFUSED", self.shown())
        self.assertEqual(self.env.asked, [])

    def test_yolo_skips_ordinary_approval_but_not_dangerous_commands(self):
        s = self.start([call("run_command", command="echo ok"),
                        call("run_command", command="rm -rf build"), done()],
                       auto_approve=True)
        s.run("go")
        self.assertEqual([r["title"] for r in self.env.asked], ["rm -rf build"])
        self.assertEqual(self.env.asked[0]["flag"], "DANGEROUS")

    def test_file_tools_cannot_leave_the_working_directory(self):
        s = self.start([call("write_file", path="../escape.txt", content="x"),
                        call("read_file", path="/etc/passwd"), done()])
        s.run("go")
        self.assertIn("path escapes working directory", self.shown(0))
        self.assertIn("path escapes working directory", self.shown(1))
        self.assertFalse(os.path.exists(os.path.join(self.env.root, "escape.txt")))

    def test_the_sandbox_blocks_a_write_outside_and_explains_the_way_out(self):
        s = self.start([call("run_command", command="echo x > ../escape.txt"), done()],
                       auto_approve=False)
        if not s.sandbox.available:
            self.skipTest("no OS sandbox on this machine")
        s.run("go")
        self.assertIn("Operation not permitted", self.shown())
        self.assertIn("<outside>true</outside>", self.shown())
        self.assertFalse(os.path.exists(os.path.join(self.env.root, "escape.txt")))

    def test_leaving_the_sandbox_always_asks_even_in_yolo(self):
        s = self.start([call("run_command", command="echo x > ../allowed.txt", outside="true"),
                        done()], auto_approve=True)
        if not s.sandbox.available:
            self.skipTest("no OS sandbox on this machine")
        self.env.allow = False
        s.run("go")
        self.assertEqual(self.env.asked[0]["flag"], "RUNS OUTSIDE THE SANDBOX")
        self.assertFalse(os.path.exists(os.path.join(self.env.root, "allowed.txt")))

    def test_with_no_approver_the_answer_is_no(self):
        s = self.start([call("run_command", command="touch made.txt"), done()])
        s.approver = None
        s.run("go")
        self.assertIn("User DENIED", self.shown())


GOOD_TOOL = dict(
    name="shout", description="Upper-case some text", params="text",
    code="def run(args):\n    return args['text'].upper()\n",
    test="import tool\nassert tool.run({'text': 'hi'}) == 'HI'\n")


class SelfHealingTests(EngineCase):
    def start(self, replies, **kw):
        s = super().start(replies, **kw)
        if not s.sandbox.available:
            self.skipTest("no OS sandbox on this machine")

        def broken(ctx, args):
            raise KeyError("result__a")
        self.env.core.registry.register(Tool("lookup", broken, doc="## lookup -- look something up"))
        s.add_tool("lookup")
        return s

    def test_a_broken_tool_is_reported_noted_and_routed_around(self):
        s = self.start([
            call("lookup", query="weather"),
            call("run_code", code="print('sunny, 31C')"),
            call("make_tool", replaces="lookup", **dict(GOOD_TOOL, name="lookup2")),
            call("lookup2", text="works"),
            done("lookup was broken; I built lookup2 and used that."),
        ], auto_approve=True)
        out = s.run("what is the weather")
        self.assertEqual(out["status"], "done")

        fault = self.shown(0)
        self.assertIn("TOOL FAULT: `lookup` is broken (KeyError", fault)
        self.assertIn("run_code", fault)
        self.assertIn("<replaces>lookup</replaces>", fault)
        self.assertIn("sunny, 31C", self.shown(1))
        self.assertIn("Registered tool `lookup2`", self.shown(2))
        self.assertEqual(self.shown(3), "[lookup2 result]\nWORKS")

        note = os.path.join(self.env.core.vault.path, "inbox", "tool-lookup.md")
        with open(note) as f:
            text = f.read()
        self.assertIn("KeyError", text)
        self.assertIn("Traceback", text)
        self.assertIn("- query: weather", text)
        self.assertIn("`lookup2`", text)              # the stand-in is recorded
        self.assertEqual(self.env.core.vault.inbox_open()[0][0], "tool-lookup")

        health = self.env.core.store.tool_health()["lookup"]
        self.assertEqual((health["faults"], health["substitute"]), (1, "lookup2"))
        self.assertTrue([e for e in self.env.events if e["type"] == "tool_fault"][0]["note"])

        # A later thread starts out knowing, and has the new tool.
        later = self.env.session()
        later.add_tool("lookup")
        prompt = later.system_prompt()
        self.assertIn("`lookup` is broken. Use `lookup2` in its place.", prompt)
        self.assertIn("## lookup2 -- Upper-case some text", prompt)

    def test_a_repeating_fault_leaves_one_note_not_three(self):
        s = self.start([call("lookup", query="a")] * 3 + [done()])
        s.run("go")
        with open(os.path.join(self.env.core.vault.path, "inbox", "tool-lookup.md")) as f:
            self.assertEqual(f.read().count("**Error:**"), 1)
        self.assertEqual(self.env.core.store.tool_health()["lookup"]["consecutive"], 3)

    def test_a_tool_whose_test_fails_is_not_registered(self):
        bad = dict(GOOD_TOOL, test="import tool\nassert tool.run({'text': 'hi'}) == 'nope'\n")
        s = self.start([call("make_tool", **bad), done()])
        s.run("go")
        self.assertIn("was NOT registered", self.shown())
        self.assertIn("AssertionError", self.shown())
        self.assertIsNone(self.env.core.registry.get("shout"))
        self.assertNotIn("shout", s.tools)

    def test_a_built_in_tool_cannot_be_replaced_or_shadowed(self):
        s = self.start([call("make_tool", **dict(GOOD_TOOL, name="run_command")), done()])
        s.run("go")
        self.assertIn("already the name of a built-in tool", self.shown())
        self.assertEqual(self.env.core.registry.get("run_command").source, "builtin")

    def test_workshop_code_cannot_touch_the_project_or_the_network(self):
        code = ("import os, socket\nroot = os.environ['CLAIRE_WORKDIR']\n"
                "print('read:', open(os.path.join(root, 'data.txt')).read())\n"
                "for label, fn in [('write', lambda: open(os.path.join(root, 'evil.txt'), 'w')),\n"
                "                  ('home', lambda: os.listdir(os.path.expanduser('~'))),\n"
                "                  ('net', lambda: socket.create_connection(('1.1.1.1', 53), 3))]:\n"
                "    try:\n        fn(); print(label, 'ALLOWED')\n"
                "    except Exception as e:\n        print(label, 'blocked')\n")
        s = self.start([call("run_code", code=code), done()])
        with open(os.path.join(self.env.work, "data.txt"), "w") as f:
            f.write("project data")
        s.run("go")
        out = self.shown()
        self.assertIn("read: project data", out)
        for label in ("write", "home", "net"):
            self.assertIn(f"{label} blocked", out)
        self.assertFalse(os.path.exists(os.path.join(self.env.work, "evil.txt")))

    def test_network_for_workshop_code_needs_a_yes_even_in_yolo(self):
        s = self.start([call("run_code", code="print(1)", network="true"), done()],
                       auto_approve=True)
        self.env.allow = False
        s.run("go")
        self.assertEqual(self.env.asked[0]["kind"], "network")
        self.assertIn("User DENIED network access", self.shown())

    def test_a_workshop_tool_survives_a_restart_and_is_versioned(self):
        s = self.start([call("make_tool", **GOOD_TOOL), done()])
        s.run("go")
        from claire_core.engine import Core
        again = Core(self.env.core.settings, llm=self.env.llm)
        self.addCleanup(again.store.close)
        self.assertEqual(again.registry.get("shout").source, "workshop")
        import subprocess
        log = subprocess.run(["git", "log", "--oneline"], capture_output=True, text=True,
                             cwd=self.env.core.settings.workshop_dir).stdout
        self.assertIn("add shout", log)

    def test_a_broken_workshop_tool_is_hers_to_repair(self):
        crash = dict(GOOD_TOOL, name="fragile",
                     code="def run(args):\n    return str(1 / int(args['text']))\n",
                     test="import tool\nassert tool.run({'text': '4'}) == '0.25'\n")
        s = self.start([call("make_tool", **crash), call("fragile", text="0"), done()])
        s.run("go")
        self.assertIn("TOOL FAULT: your workshop tool `fragile` failed", self.shown())
        self.assertIn("ZeroDivisionError", self.shown())
        self.assertFalse(os.path.exists(
            os.path.join(self.env.core.vault.path, "inbox", "tool-fragile.md")))


if __name__ == "__main__":
    unittest.main()


class WebApprovalTests(unittest.TestCase):
    """Looking something up does not ask; reaching into this machine does."""

    def test_only_local_addresses_are_treated_as_local(self):
        from claire_core.tools.web import _local
        for url in ("https://example.com/a", "https://www.google.com", "http://8.8.8.8/x"):
            self.assertFalse(_local(url), url)
        for url in ("http://localhost:8765/shutdown", "http://127.0.0.1:8765/", "http://10.0.0.5/",
                    "http://192.168.1.1/admin", "http://[::1]/", "http://router/", "http://nas.local/",
                    "http://169.254.169.254/latest/meta-data"):
            self.assertTrue(_local(url), url)

    def test_a_search_or_a_public_page_does_not_ask_but_a_local_address_does(self):
        from unittest.mock import patch
        from claire_core.tools import web
        env = Env()
        self.addCleanup(env.close)
        env.allow = False
        session = env.session(kind="home")

        class Reply:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return b"<html><body>public page</body></html>"
        with patch.object(web.urllib.request, "urlopen", return_value=Reply()), \
                patch.object(web.urllib.request.OpenerDirector, "open", return_value=Reply()):
            session._execute("web_search", {"query": "weather in pune"})
            text, failed, _ = session._execute("fetch_url", {"url": "https://example.com/"})
            self.assertIn("public page", text)
            self.assertEqual(env.asked, [])
            text, _, _ = session._execute("fetch_url", {"url": "http://127.0.0.1:8765/status"})
        self.assertIn("DENIED", text)
        self.assertEqual([a["kind"] for a in env.asked], ["fetch"])

    def test_a_public_page_cannot_redirect_a_fetch_to_this_machine(self):
        import urllib.error
        import urllib.request
        from claire_core.tools.web import _NoLocalRedirect
        handler = _NoLocalRedirect()
        req = urllib.request.Request("https://example.com/")
        with self.assertRaises(urllib.error.URLError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8765/shutdown")
        self.assertIsNotNone(handler.redirect_request(req, None, 302, "Found", {}, "https://example.org/"))


class RepeatedFailureTests(unittest.TestCase):
    def test_the_same_failing_call_is_flagged_then_stops_the_turn_and_says_why(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        same = call("read_file", path="missing.txt")
        env.llm.replies = [same] * 6
        outcome = env.session().run("Read missing.txt and summarise it.")
        self.assertEqual(outcome["status"], "stopped")
        self.assertIn("4 times with nothing changing and it failed the same way", outcome["answer"])
        self.assertEqual(len(env.llm.calls), 4)          # no fifth, sixth... attempt
        # The third time, the model was told that repeating it changes nothing.
        self.assertIn("Running it again will not change anything", env.llm.calls[3]["messages"][-1]["content"])
        self.assertTrue(any(e["type"] == "answer" and "stopped" in e["text"] for e in env.events))

    def test_different_failing_calls_are_left_to_the_usual_nudge(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        env.llm.replies = [call("read_file", path=f"missing{i}.txt") for i in range(4)] + [done("Gave up.")]
        self.assertEqual(env.session().run("Find the notes file.")["status"], "done")

    def test_the_same_successful_call_over_and_over_also_stops(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        with open(os.path.join(env.work, "notes.txt"), "w") as f:
            f.write("hello")
        same = call("read_file", path="notes.txt")
        env.llm.replies = [same] * 6
        outcome = env.session().run("Summarise notes.txt.")
        self.assertEqual(outcome["status"], "stopped")
        self.assertIn("getting the same result each time", outcome["answer"])
        self.assertEqual(len(env.llm.calls), 4)

    def test_a_nudged_thread_that_moves_on_finishes_normally(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        with open(os.path.join(env.work, "notes.txt"), "w") as f:
            f.write("hello")
        same = call("read_file", path="notes.txt")
        env.llm.replies = [same, same, same, done("The notes say hello.")]
        self.assertEqual(env.session().run("Summarise notes.txt.")["status"], "done")


class StuckCycleTests(unittest.TestCase):
    """Found live: a worker cycled through four commands, unchanged, to its step cap."""

    def test_a_repeating_cycle_with_nothing_changing_is_stopped_with_a_report(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        for name in ("a.txt", "b.txt"):
            with open(os.path.join(env.work, name), "w") as f:
                f.write(name)
        cycle = [call("read_file", path="a.txt"), call("read_file", path="b.txt")]
        env.llm.replies = cycle * 6
        outcome = env.session().run("Check both files.")
        self.assertEqual(outcome["status"], "stopped")
        self.assertIn("4 times with nothing changing", outcome["answer"])
        self.assertLessEqual(len(env.llm.calls), 8)

    def test_running_tests_after_each_edit_is_progress_not_a_loop(self):
        from tests.helpers import Env
        env = Env()
        self.addCleanup(env.close)
        with open(os.path.join(env.work, "notes.txt"), "w") as f:
            f.write("v0")
        check = call("read_file", path="notes.txt")
        replies = []
        for i in range(1, 6):
            replies += [check, call("write_file", path=f"out{i}.txt", content=f"step {i}")]
        env.llm.replies = replies + [done("All steps written.")]
        self.assertEqual(env.session().run("Do five steps.")["status"], "done")

    def test_running_out_of_steps_still_reports_what_was_done(self):
        from tests.helpers import Env
        env = Env(max_steps=4)
        self.addCleanup(env.close)
        env.llm.replies = [call("write_file", path=f"f{i}.txt", content=str(i)) for i in range(6)]
        outcome = env.session().run("Write files.")
        self.assertEqual(outcome["status"], "stopped")
        self.assertIn("ran out of steps", outcome["answer"])
        self.assertIn("Files changed: f0.txt, f1.txt, f2.txt, f3.txt", outcome["answer"])
        warned = [m["content"] for m in env.llm.calls[-1]["messages"] if isinstance(m["content"], str)]
        self.assertTrue(any("3 steps left in this turn" in m for m in warned))


class StallTests(unittest.TestCase):
    """Found live: a worker wrote two files, then probed for 57 steps, each
    command slightly different, so no repeat was ever exact."""

    def test_steps_without_any_change_after_writing_are_nudged_then_stopped(self):
        from tests.helpers import Env
        from claire_core.engine import STALL_NUDGE, STALL_STOP
        env = Env(max_steps=60)
        self.addCleanup(env.close)
        probes = [call("run_command", command=f"python3 -c 'print({i})'") for i in range(40)]
        env.llm.replies = [call("write_file", path="hello.py", content="print('hi')")] + probes
        outcome = env.session().run("Write hello.py and check it.")
        self.assertEqual(outcome["status"], "stopped")
        self.assertIn(f"{STALL_STOP} steps passed without any further change", outcome["answer"])
        self.assertIn("Files changed: hello.py", outcome["answer"])
        self.assertEqual(len(env.llm.calls), 1 + STALL_STOP)
        told = [m["content"] for m in env.llm.calls[STALL_NUDGE + 1]["messages"] if isinstance(m["content"], str)]
        self.assertTrue(any("since you last changed a file" in m for m in told))

    def test_exploring_before_any_change_is_not_counted(self):
        from tests.helpers import Env
        from claire_core.engine import STALL_STOP
        env = Env(max_steps=60)
        self.addCleanup(env.close)
        reads = [call("run_command", command=f"python3 -c 'print({i})'") for i in range(STALL_STOP + 4)]
        env.llm.replies = reads + [call("write_file", path="fix.py", content="x = 1"), done("Fixed.")]
        self.assertEqual(env.session().run("Find and fix it.")["status"], "done")
