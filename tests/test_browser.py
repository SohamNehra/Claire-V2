"""The browser connector: a Chrome that Claire owns, and always closes.

Chrome itself is replaced by a stand-in here, and the Playwright server by the
fake MCP server, so these run anywhere. tests/ui and a real Connect & test
cover the real thing.
"""

import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from claire_core.connectors import ConnectorError, Keyring, Operation
from claire_core.connectors.browser import PACKAGE, Chrome
from claire_core.connectors.mcp import Mcp
from tests.helpers import Env, done

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = [sys.executable, os.path.join(HERE, "fake_mcp_server.py")]
ECHO = ("Looking.\n<use_connector>\n<connector>browser</connector>\n<action>echo</action>\n"
        "<input>{\"text\": \"hi\"}</input>\n</use_connector>")


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.conn = self.env.core.connectors
        self.addCleanup(self.conn.close)
        if not self.env.core.sandbox.available:
            self.skipTest("local connectors require the OS sandbox")
        self.provider = self.conn.providers["browser"]

    def fake(self):
        """Stand-ins for Chrome and for the Playwright server. Returns the
        list of every Chrome 'launched'."""
        launched = []

        def chrome(*args, **kw):
            launched.append(Mock(endpoint="http://127.0.0.1:45678"))
            return launched[-1]
        real = Mcp._open

        def open_fake(provider, integration, keys):
            return real(provider, dict(integration, config=dict(integration["config"], command=FAKE)), keys)
        for p in (patch("claire_core.connectors.browser.Chrome", side_effect=chrome),
                  patch("claire_core.connectors.mcp.Mcp._open", open_fake)):
            p.start()
            self.addCleanup(p.stop)
        return launched

    def connect(self):
        view = self.conn.test(self.conn.create("browser", "Browser", {})["id"])
        self.assertEqual(view["status"], "connected", view["error"])
        return view["id"]

    def servers(self):
        return subprocess.run(["pgrep", "-f", "fake_mcp_server.py"], capture_output=True,
                              text=True).stdout.split()


class SetupTests(BrowserCase):
    def test_setup_pins_the_server_and_accepts_no_credentials_or_approvals(self):
        item = self.conn.create("browser", "Browser", {
            "auto_allow": ["browser_click"], "command": ["bad"], "headless": False})
        config = self.conn._row(item["id"])["config"]
        self.assertEqual(config["command"], ["npx", "-y", PACKAGE])
        self.assertFalse(config["headless"])
        self.assertEqual(config["auto_allow"], [])           # every action asks
        with self.assertRaises(ConnectorError):
            self.conn.create("browser", "Unsafe", {}, {"password": "not-stored"})

    def test_the_server_is_pointed_at_the_owned_browser_and_both_are_closed(self):
        item = self.conn.create("browser", "Browser", {})
        chrome, stdio = Mock(endpoint="http://127.0.0.1:45678"), Mock()
        close_stdio = stdio.close
        with patch("claire_core.connectors.browser.Chrome", return_value=chrome), \
                patch("claire_core.connectors.mcp.Mcp._open", return_value=stdio) as opened:
            session = self.provider._open(self.conn._row(item["id"]),
                                          Keyring(self.conn.secrets, item["id"]))
            self.assertIn("--cdp-endpoint=http://127.0.0.1:45678",
                          opened.call_args.args[0]["config"]["command"])
            session.close()
        close_stdio.assert_called_once()
        chrome.close.assert_called_once()

    def test_a_server_that_fails_to_start_does_not_leave_the_browser_open(self):
        item = self.conn.create("browser", "Browser", {})
        chrome = Mock(endpoint="http://127.0.0.1:45678")
        with patch("claire_core.connectors.browser.Chrome", return_value=chrome), \
                patch("claire_core.connectors.mcp.Mcp._open", side_effect=ConnectorError("failed")):
            with self.assertRaises(ConnectorError):
                self.provider._open(self.conn._row(item["id"]), Keyring(self.conn.secrets, item["id"]))
        chrome.close.assert_called_once()


class PromptTests(BrowserCase):
    def test_every_thread_is_told_whether_there_is_a_browser(self):
        for kind in ("home", "agent"):
            prompt = self.env.session(kind=kind).system_prompt()
            self.assertIn("No browser is connected", prompt, kind)
            self.assertIn("Integrations → Browser", prompt, kind)
        self.fake()
        iid = self.connect()
        for kind in ("home", "agent"):
            session = self.env.session(kind=kind)
            session.refresh()
            prompt = session.system_prompt()
            self.assertNotIn("No browser is connected", prompt, kind)
            self.assertIn(f"connected as `{iid}`", prompt, kind)
            self.assertIn("use_connector", session.tools, kind)


class ApprovalTests(BrowserCase):
    """Looking goes ahead; sir is asked before anything major."""

    PAGE = """```yaml
- textbox "Search" [ref=e1]
- button "Google Search" [ref=e2]
- button "Delete account" [ref=e3]
- textbox "Message" [ref=e4]
- link "Pricing" [ref=e5]
```"""

    def why(self, action, args=None, **config):
        row = {"id": "browser", "config": config}
        return self.provider.review(row, Operation("browser_" + action, ""), args or {})

    def test_looking_and_moving_around_never_asks(self):
        for action, args in [("navigate", {"url": "https://example.com"}), ("snapshot", {}),
                             ("find", {"text": "x"}), ("take_screenshot", {}), ("navigate_back", {}),
                             ("wait_for", {"time": 1}), ("hover", {"target": "e3"}), ("tabs", {"action": "list"}),
                             ("fill_form", {"fields": []}), ("select_option", {"target": "e1", "values": ["a"]})]:
            self.assertIsNone(self.why(action, args), action)

    def test_a_click_is_judged_by_what_the_page_calls_the_element(self):
        self.provider._pages["browser"] = self.PAGE
        self.assertIsNone(self.why("click", {"element": "Search button", "target": "e2"}))
        self.assertIsNone(self.why("click", {"element": "Pricing link", "target": "e5"}))
        # The model's harmless description does not hide what the page says.
        asked = self.why("click", {"element": "a link", "target": "e3"})
        self.assertIn("Delete account", asked)
        self.assertTrue(asked.startswith("MAJOR ACTION"))
        for label in ("Pay now", "Send", "Sign in", "Place order", "Confirm", "Unsubscribe", "Log out"):
            self.assertIsNotNone(self.why("click", {"element": label + " button", "target": "e99"}), label)

    def test_submitting_a_search_is_small_and_submitting_anything_else_is_not(self):
        self.provider._pages["browser"] = self.PAGE
        self.assertIsNone(self.why("type", {"target": "e1", "text": "weather", "submit": True}))
        self.assertIsNone(self.why("type", {"target": "e4", "text": "hello"}))
        self.assertIsNotNone(self.why("type", {"target": "e4", "text": "hello", "submit": True}))
        self.provider._focus["browser"] = 'textbox "Search" [ref=e1]'
        self.assertIsNone(self.why("press_key", {"key": "Enter"}))
        self.provider._focus["browser"] = 'textbox "Message" [ref=e4]'
        self.assertIsNotNone(self.why("press_key", {"key": "Enter"}))
        self.assertIsNone(self.why("press_key", {"key": "ArrowDown"}))

    def test_a_script_that_only_reads_runs_and_one_that_changes_things_asks(self):
        self.assertIsNone(self.why("evaluate", {
            "function": "() => getComputedStyle(document.querySelector('svg path')).fill"}))
        for code in ("() => fetch('/api/delete', {method: 'POST'})", "() => document.forms[0].submit()",
                     "(el) => el.click()", "() => { document.cookie = 'a=b' }", "() => localStorage.clear()"):
            self.assertIsNotNone(self.why("evaluate", {"function": code}), code)

    def test_reaching_outside_the_page_always_asks_and_sir_can_ask_for_everything(self):
        for action in ("file_upload", "drop", "run_code_unsafe", "handle_dialog", "something_new"):
            self.assertIsNotNone(self.why(action), action)
        self.assertIsNone(self.why("handle_dialog", {"accept": False}))
        self.assertIsNotNone(self.why("navigate", {"url": "https://example.com"}, ask_every=True))

    def test_the_engine_asks_only_for_the_major_call(self):
        self.fake()
        iid = self.connect()
        row = self.conn._row(iid)
        tools = [dict(t, name="browser_" + n) for t in row["config"]["tools"][:1] for n in ("navigate", "click")]
        self.env.core.store.update_integration(iid, config=dict(row["config"], tools=tools))
        self.provider._pages[iid] = self.PAGE
        asked = []
        ask = lambda title, flag: asked.append((title, flag)) or False
        with patch.object(Mcp, "call", return_value="ok"):
            self.assertIn("ok", self.conn.call(iid, "browser_navigate", {"url": "https://x.test"}, ask))
            self.assertEqual(asked, [])
            out = self.conn.call(iid, "browser_click", {"element": "button", "target": "e3"}, ask)
        self.assertIn("did not approve", out)
        self.assertEqual(asked[0][1], "MAJOR ACTION")
        self.assertIn("Delete account", asked[0][0])


class ManualTests(BrowserCase):
    def test_help_is_a_manual_with_exact_calls_and_required_parameters(self):
        self.fake()
        iid = self.connect()
        text = self.conn.help(iid)
        self.assertIn("# How to use the browser", text)
        self.assertIn(f"connector `{iid}`", text)
        self.assertIn('<input>{"element": "Google Search button", "target": "e23"}</input>', text)
        self.assertIn("# Every action", text)
        self.assertNotIn("{id}", text)

    def test_a_failed_call_comes_back_with_how_to_make_it(self):
        self.fake()
        iid = self.connect()
        with self.assertRaises(ConnectorError) as caught:
            self.conn.call(iid, "write_outside", {"path": "/nope/x"}, ask=lambda *a: True)
        self.assertIn("How to call write_outside:", str(caught.exception))
        self.assertIn("- path", str(caught.exception))

    def test_a_snapshot_saved_to_a_file_is_shown_and_remembered(self):
        iid = "browser"
        folder = os.path.join(self.env.core.settings.state_dir, "connectors", iid, ".playwright-mcp")
        os.makedirs(folder)
        with open(os.path.join(folder, "page-1.yml"), "w") as f:
            f.write('- button "Delete account" [ref=e3]\n')
        outside = os.path.join(self.env.root, "secret.yml")
        with open(outside, "w") as f:
            f.write("secret")
        reply = ("### Page\n- [Snapshot](.playwright-mcp/page-1.yml)\n"
                 "- [Snapshot](.playwright-mcp/..-..-secret.yml)")
        with patch.object(Mcp, "call", return_value=reply):
            text = self.provider.call({"id": iid, "config": {}}, Operation("browser_navigate", ""), {}, None)
        self.assertIn('```yaml\n- button "Delete account" [ref=e3]\n```', text)
        self.assertNotIn("secret\n", text)
        self.assertEqual(os.listdir(folder), [])
        self.assertIn("Delete account", self.provider._line(iid, "e3"))


class LifetimeTests(BrowserCase):
    """Chrome runs only while a job is using it."""

    def test_connect_and_test_does_not_leave_a_browser_running(self):
        launched = self.fake()
        iid = self.connect()
        self.assertEqual(len(launched), 1)
        launched[0].close.assert_called_once()
        self.assertNotIn(iid, self.provider._sessions)
        time.sleep(0.2)
        self.assertEqual(self.servers(), [])

    def test_a_task_keeps_one_browser_for_its_actions_and_closes_it_when_it_ends(self):
        launched = self.fake()
        self.connect()
        self.env.llm.replies = [ECHO, ECHO, done()]
        session = self.env.session()
        self.assertEqual(session.run("look at the page")["status"], "done")
        self.assertEqual(len(self.env.results()), 2)
        self.assertEqual(len(launched), 2)                   # the check, then one for the task
        launched[1].close.assert_called_once()
        self.assertEqual(self.provider._sessions, {})
        time.sleep(0.2)
        self.assertEqual(self.servers(), [])

    def test_a_task_that_crashes_still_closes_its_browser(self):
        launched = self.fake()
        self.connect()
        self.env.llm.replies = [ECHO, RuntimeError("the model server died")]
        try:
            self.env.session().run("look at the page")
        except RuntimeError:
            pass
        launched[1].close.assert_called_once()
        self.assertEqual(self.provider._sessions, {})

    def test_a_browser_two_tasks_share_closes_with_the_last_of_them(self):
        launched = self.fake()
        iid = self.connect()
        for owner in ("first", "second"):
            self.conn.call(iid, "echo", {"text": "x"}, ask=lambda *a: True, owner=owner)
        self.assertEqual(len(launched), 2)
        self.conn.release("first")
        launched[1].close.assert_not_called()
        self.conn.abort("first")                             # nor may a cancel kill it
        launched[1].close.assert_not_called()
        self.conn.release("second")
        launched[1].close.assert_called_once()

    def test_cancelling_a_stuck_call_kills_that_task_s_browser_at_once(self):
        launched = self.fake()
        iid = self.connect()
        row, keys = self.conn._row(iid), Keyring(self.conn.secrets, iid)
        self.provider.claim(row, "job")
        outcome = []

        def stuck():
            try:
                self.provider.call(row, Operation("hang", ""), {}, keys)
            except ConnectorError as exc:
                outcome.append(str(exc))
        thread = threading.Thread(target=stuck, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while iid not in self.provider._sessions and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        started = time.monotonic()
        self.conn.abort("job")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 5)       # not the 60s call timeout
        self.assertIn("the server stopped", outcome[0])
        self.assertTrue(launched[1].close.called)
        self.assertNotIn(iid, self.provider._sessions)
        self.conn.release("job")
        time.sleep(0.2)
        self.assertEqual(self.servers(), [])

    def test_a_cancelled_task_does_not_open_a_browser(self):
        launched = self.fake()
        iid = self.connect()
        session = self.env.session()
        session.cancel.set()
        text, failed, _ = session._execute("use_connector", {
            "connector": iid, "action": "echo", "input": "{\"text\": \"x\"}"})
        self.assertTrue(failed, text)
        self.assertEqual(len(launched), 1)                   # only the connection check


@unittest.skipUnless(os.name == 'posix', 'POSIX Chrome process groups; the sandboxed local browser is unavailable on Windows')
class ChromeTests(unittest.TestCase):
    def chrome(self, proc, folder):
        chrome = Chrome.__new__(Chrome)
        chrome.proc, chrome._closed = proc, False
        chrome._pid_file = folder / "chrome.pid"
        return chrome

    def setUp(self):
        import pathlib
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = pathlib.Path(self.dir.name)

    def test_close_kills_the_whole_process_group_and_can_be_called_twice(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                start_new_session=True)
        chrome = self.chrome(proc, self.folder)
        chrome.close()
        chrome.close()
        self.assertIsNotNone(proc.poll())

    def test_closing_a_browser_that_already_died_does_not_raise(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        self.chrome(proc, self.folder).close()

    def test_a_browser_left_by_a_killed_service_is_reaped_only_if_it_is_ours(self):
        profile = str(self.folder / "browser-profile")
        ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                 "--user-data-dir=" + profile], start_new_session=True)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 start_new_session=True)
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in (ours, other) if p.poll() is None])
        chrome = self.chrome(None, self.folder)
        for proc, dies in ((other, False), (ours, True)):
            chrome._pid_file.write_text(str(proc.pid))
            chrome._reap(profile)
            time.sleep(0.3)
            self.assertEqual(proc.poll() is not None, dies)
            self.assertFalse(chrome._pid_file.exists())


if __name__ == "__main__":
    unittest.main()
