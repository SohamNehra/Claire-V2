"""The Mac apps connector. The accessibility script is replaced by a stand-in
here; a real app is driven by Connect & test and by hand."""

import sys
import unittest
from unittest.mock import patch

from claire_core.connectors import ConnectorError
from claire_core.connectors.mac import Mac
from tests.helpers import Env, done

WINDOW = {"title": "Untitled", "full": False, "nodes": [
    {"path": [0], "depth": 0, "role": "AXScrollArea", "sub": "", "name": "", "desc": "scroll area", "value": "", "enabled": True},
    {"path": [0, 0], "depth": 1, "role": "AXTextArea", "sub": "", "name": "", "desc": "text entry area", "value": "milk\neggs", "enabled": True},
    {"path": [0, 1], "depth": 1, "role": "AXScrollBar", "sub": "", "name": "", "desc": "scroll bar", "value": "0.2", "enabled": True},
    {"path": [1], "depth": 0, "role": "AXButton", "sub": "", "name": "Delete", "desc": "button", "value": "", "enabled": True},
    {"path": [2], "depth": 0, "role": "AXButton", "sub": "", "name": "Bold", "desc": "button", "value": "", "enabled": False},
    {"path": [3], "depth": 0, "role": "AXTextField", "sub": "", "name": "Search", "desc": "text field", "value": "", "enabled": True}]}


BOXES = {(1,): [100, 50, 40, 20], (0, 0): [100, 80, 600, 300]}     # on-screen x, y, width, height


@unittest.skipUnless(sys.platform == "darwin", "macOS only")
class MacTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.conn = self.env.core.connectors
        self.addCleanup(self.conn.close)
        self.sent, self.overlays = [], []

        def run(provider, **request):
            self.sent.append(request)
            return {"apps": {"running": ["TextEdit", "Finder"]}, "probe": {"windows": 1},
                    "snapshot": dict(WINDOW, nodes=[dict(n, box=BOXES.get(tuple(n["path"]))) for n in WINDOW["nodes"]])
                    if request.get("geo") else WINDOW,
                    "window": {"x": 100, "y": 50, "w": 600, "h": 400, "title": "Untitled"},
                    "windows": {"windows": [{"title": "Untitled", "box": [100, 50, 600, 400], "minimized": False},
                                            {"title": "Invoices.txt", "box": [0, 0, 300, 300], "minimized": True}]},
                    "menus": {"items": ["New", "Save…"]}}.get(request["op"], {"ok": True})
        for p in (patch.object(Mac, "_run", run),
                  patch("claire_core.connectors.mac.subprocess.Popen",
                        side_effect=lambda *a, **k: self.overlays.append(_Overlay()) or self.overlays[-1])):
            p.start()
            self.addCleanup(p.stop)
        view = self.conn.test(self.conn.create("mac", "Mac apps", {"apps": "TextEdit, Notes.app"})["id"])
        self.assertEqual((view["status"], view["account_label"]), ("connected", "2 apps, 1 open now"))
        self.iid = view["id"]
        self.asked = []
        self.allow = True

    def call(self, action, owner=None, **args):
        return self.conn.call(self.iid, action, args, owner=owner,
                              ask=lambda title, flag: self.asked.append((flag, title)) or self.allow)

    def test_setup_names_the_apps_and_refuses_the_ones_that_run_the_machine(self):
        self.assertEqual(self.conn._row(self.iid)["config"]["apps"], ["TextEdit", "Notes"])
        for bad in ("", "Terminal", "System Settings", "Keychain Access", "rm -rf /; x"):
            with self.assertRaises(ConnectorError, msg=bad):
                self.conn.create("mac", "Bad", {"apps": bad})

    def test_only_the_named_apps_can_be_touched(self):
        before = len(self.sent)
        for action, args in (("snapshot", {"app": "Safari"}), ("click", {"app": "Finder", "target": "e1"}),
                             ("type", {"app": "Mail", "text": "x"}), ("menu", {"app": "Safari", "path": ["File", "Quit"]})):
            with self.assertRaises(ConnectorError) as caught:
                self.call(action, **args)
            self.assertIn("not an app you may control", str(caught.exception))
        self.assertEqual(len(self.sent), before)

    def test_a_window_is_shown_as_controls_with_references(self):
        text = self.call("snapshot", app="textedit")
        self.assertIn('TextEdit -- window “Untitled”', text)
        self.assertIn('- text area "text entry area" [ref=e1]: milk ⏎ eggs', text)
        self.assertIn('- button "Delete" [ref=e2]', text)
        self.assertIn('- button "Bold" [ref=e3] (disabled)', text)
        self.assertNotIn("scroll bar", text)

    def test_acting_uses_what_the_snapshot_saw_and_replies_with_the_window_after(self):
        self.call("snapshot", app="TextEdit")
        out = self.call("set_text", app="TextEdit", target="e1", text="bread")
        self.assertEqual(self.sent[-2], {"op": "set_value", "app": "TextEdit", "path": [0, 0],
                                         "role": "AXTextArea", "text": "bread", "expected_window": None,
                                         "expected_label": "text entry area"})
        self.assertIn("```yaml", out)                           # no extra snapshot call needed
        with self.assertRaises(ConnectorError) as caught:
            self.call("click", app="TextEdit", target="e99")
        self.assertIn("latest snapshot", str(caught.exception))
        self.call("key", app="TextEdit", key="cmd+b")
        self.assertEqual((self.sent[-2]["key"], self.sent[-2]["modifiers"]), ("b", ["cmd"]))

    def test_ordinary_work_goes_ahead_and_major_actions_ask(self):
        self.call("snapshot", app="TextEdit")
        for action, args in (("open", {}), ("type", {"text": "hello"}), ("set_text", {"target": "e1", "text": "x"}),
                             ("click", {"target": "e3"}), ("menus", {"path": ["File"]}),
                             ("menu", {"path": ["Format", "Make Plain Text"]}), ("key", {"key": "down"}),
                             ("key", {"key": "b", "modifiers": ["cmd"]})):
            self.call(action, app="TextEdit", **args)
        self.assertEqual(self.asked, [])
        self.call("click", app="TextEdit", target="e4")              # the search field
        self.call("key", app="TextEdit", key="return")               # searching is small
        self.assertEqual(self.asked, [])
        self.allow = False
        before = len(self.sent)
        for action, args in (("click", {"target": "e2"}), ("menu", {"path": ["File", "Save…"]}),
                             ("menu", {"path": "TextEdit > Quit TextEdit"}),
                             ("key", {"key": "q", "modifiers": ["cmd"]}),
                             ("key", {"key": "delete", "modifiers": ["cmd"]})):
            self.assertIn("did not approve", self.call(action, app="TextEdit", **args), action)
        self.allow = True
        self.call("click", app="TextEdit", target="e1")              # in a document, Return is a new line
        self.call("key", app="TextEdit", key="return")
        self.call("type", app="TextEdit", text="milk\neggs")
        self.assertEqual(len(self.asked), 5)
        self.allow = False
        self.call("click", app="TextEdit", target="e3")              # anywhere else it may send
        self.assertIn("did not approve", self.call("key", app="TextEdit", key="return"))
        self.assertIn("did not approve", self.call("type", app="TextEdit", text="hello\n"))
        self.assertEqual(len(self.asked), 7)
        self.assertTrue(all(flag == "MAJOR ACTION" for flag, _ in self.asked))
        self.assertIn('button "Delete"', self.asked[0][1])
        self.assertNotIn("menu", [r["op"] for r in self.sent[before:]])   # nothing refused was run

    def test_the_blue_frame_is_up_only_while_a_task_is_in_control(self):
        provider = self.conn.providers["mac"]
        self.call("snapshot", app="TextEdit")
        self.assertEqual(self.overlays, [])                          # nobody's job: no frame
        self.env.llm.replies = [
            "Reading.\n<use_connector>\n<connector>" + self.iid + "</connector>\n<action>snapshot</action>\n"
            "<input>{\"app\": \"TextEdit\"}</input>\n</use_connector>", done()]
        self.env.session().run("look at textedit")
        self.assertEqual(len(self.overlays), 1)
        self.assertTrue(self.overlays[0].stopped)
        self.assertIsNone(provider._overlay)
        self.call("snapshot", owner="a", app="TextEdit")
        self.call("snapshot", owner="b", app="TextEdit")
        self.assertEqual(len(self.overlays), 2)                      # one frame for both
        self.conn.release("a")
        self.assertFalse(self.overlays[1].stopped)
        self.conn.release("b")
        self.assertTrue(self.overlays[1].stopped)

    def test_looking_attaches_a_screenshot_and_a_click_lands_where_the_grid_says(self):
        provider = self.conn.providers["mac"]
        shots = []
        with patch.object(Mac, "_capture", lambda p, iid, win: shots.append(win) or ("QUJD", "image/jpeg")), \
                patch.object(Mac, "_can_see", lambda p: True):
            text = self.call("look", app="TextEdit")
            self.assertIn("screenshot is attached", text)
            self.assertIn("0-1000 grid", text)
            self.assertEqual(self.conn.take_images(), [("QUJD", "image/jpeg")])
            out = self.call("click_at", app="TextEdit", x=500, y=250, what="the bold button")
            sent = [r for r in self.sent if r["op"] == "click_at"][-1]
            self.assertEqual((sent["x"], sent["y"]), (400.0, 150.0))      # window at 100,50, 600x400
            self.assertIn("Clicked the bold button", out)
            self.assertEqual(len(self.conn.take_images()), 1)             # the window after the click
            for bad in ({"x": 1500, "y": 10}, {"x": "left", "y": 10}, {"x": -1, "y": 0}):
                with self.assertRaises(ConnectorError):
                    self.call("click_at", app="TextEdit", what="x", **bad)
        self.assertEqual(self.asked, [])

    def test_a_click_by_position_is_judged_by_what_is_under_it(self):
        with patch.object(Mac, "_capture", lambda p, iid, win: ("QUJD", "image/jpeg")), \
                patch.object(Mac, "_can_see", lambda p: True):
            self.allow = False
            # The Delete button's box is at 100,50 + 40x20 on screen: grid (17, 25) is inside it.
            out = self.call("click_at", app="TextEdit", x=17, y=25, what="a harmless spot")
            self.assertIn("did not approve", out)
            self.assertIn('button "Delete"', self.asked[0][1])
            out = self.call("click_at", app="TextEdit", x=900, y=900, what="the Send button")
            self.assertIn("did not approve", out)                         # nothing known there: her word counts
            self.assertEqual([r for r in self.sent if r["op"] == "click_at"], [])

    def test_without_vision_looking_is_refused_with_the_way_forward(self):
        with patch.object(Mac, "_can_see", lambda p: False):
            with self.assertRaises(ConnectorError) as caught:
                self.call("look", app="TextEdit")
        self.assertIn("Use `snapshot`", str(caught.exception))

    def test_a_screenshot_reaches_the_model_and_old_ones_are_dropped(self):
        look = ("Looking.\n<use_connector>\n<connector>" + self.iid + "</connector>\n<action>look</action>\n"
                "<input>{\"app\": \"TextEdit\"}</input>\n</use_connector>")
        self.env.llm.replies = [look, look, look, look, done()]
        with patch.object(Mac, "_capture", lambda p, iid, win: ("QUJD", "image/jpeg")), \
                patch.object(Mac, "_can_see", lambda p: True):
            session = self.env.session()
            session.run("watch textedit")
        results = [m["content"] for m in session.messages if isinstance(m["content"], list)]
        self.assertEqual(len(results), 4)
        pictures = [sum(1 for p in parts if p["type"] == "image_url") for parts in results]
        self.assertEqual(pictures, [0, 0, 1, 1])                          # only the latest two remain
        self.assertIn("no longer shown", str(results[0]))
        self.assertEqual(results[-1][1]["image_url"]["url"], "data:image/jpeg;base64,QUJD")

    def test_windows_can_be_listed_searched_and_brought_forward_without_asking(self):
        text = self.call("windows")
        self.assertIn("TextEdit:\n  1. Untitled (in front)\n  2. Invoices.txt (minimised)", text)
        self.assertNotIn("Notes:", text)                              # not open
        found = self.call("find", text="EGGS")
        self.assertIn("Found “EGGS” in:\nTextEdit, window 1 “Untitled”: textarea: …milk eggs…", found)
        self.assertEqual([r.get("win") for r in self.sent if r["op"] == "snapshot" and r.get("full")][-2:], [0, 1])
        self.assertIn("was not found", self.call("find", text="nowhere to be seen"))
        self.assertIn("“Invoices.txt” is now in front", self.call("use_window", app="TextEdit", window="invoices"))
        self.assertEqual([r for r in self.sent if r["op"] == "raise"][-1]["win"], 1)
        self.call("use_window", app="TextEdit", window=1)
        self.assertEqual([r for r in self.sent if r["op"] == "raise"][-1]["win"], 0)
        with self.assertRaises(ConnectorError) as caught:
            self.call("use_window", app="TextEdit", window="9")
        self.assertIn("1. Untitled; 2. Invoices.txt", str(caught.exception))
        self.assertEqual(self.asked, [])

    def test_scrolling_moves_the_page_and_shows_the_window_again(self):
        with patch.object(Mac, "_can_see", lambda p: False):
            out = self.call("scroll", app="TextEdit", direction="up", amount=200)
        sent = [r for r in self.sent if r["op"] == "scroll_at"][-1]
        self.assertEqual((sent["lines"], sent["x"], sent["y"]), (-60, 400.0, 250.0))   # capped, centre of the window
        self.assertIn("Scrolled up 60 lines.", out)
        self.assertIn("```yaml", out)                                 # no vision: the controls instead
        with patch.object(Mac, "_capture", lambda p, iid, win: ("QUJD", "image/jpeg")), \
                patch.object(Mac, "_can_see", lambda p: True):
            self.assertIn("screenshot is attached", self.call("scroll", app="TextEdit"))
            self.assertEqual([r for r in self.sent if r["op"] == "scroll_at"][-1]["lines"], 10)
        with self.assertRaises(ConnectorError):
            self.call("scroll", app="TextEdit", direction="sideways")
        self.assertEqual(self.asked, [])

    def test_tabs_are_listed_and_switched_by_number_or_title(self):
        tabs = [{"title": "Inbox", "selected": False}, {"title": "Docs", "selected": True}]
        real = Mac._run

        def run(provider, **request):
            if request["op"] == "tabs":
                self.sent.append(request)
                return {"tabs": tabs}
            if request["op"] == "pick_tab":
                self.sent.append(request)
                return {"title": request["title"]}
            return real(provider, **request)
        with patch.object(Mac, "_run", run):
            listed = self.call("tabs", app="TextEdit")
            self.assertIn("2 tabs in TextEdit's front window:\n  1. Inbox\n  2. Docs  (current)", listed)
            self.assertIn("Switched to tab 1, “Inbox”", self.call("use_tab", app="TextEdit", tab="inbox"))
            self.call("use_tab", app="TextEdit", tab=2)
            picked = [(r["index"], r["title"]) for r in self.sent if r["op"] == "pick_tab"]
            self.assertEqual(picked, [(0, "Inbox"), (1, "Docs")])   # the title is checked again before pressing
            with self.assertRaises(ConnectorError):
                self.call("use_tab", app="TextEdit", tab="nothing like it")
        self.assertEqual(self.asked, [])

    def test_a_long_tab_strip_is_summarised_to_the_current_tab(self):
        strip = [{"path": [5], "depth": 0, "role": "AXTabGroup", "sub": "", "name": "", "desc": "tab bar",
                  "value": "", "enabled": True}]
        strip += [{"path": [5, i], "depth": 1, "role": "AXRadioButton", "sub": "", "name": f"Tab {i}",
                   "desc": "", "value": "true" if i == 30 else "false", "enabled": True} for i in range(40)]
        window = dict(WINDOW, nodes=WINDOW["nodes"] + strip)
        real = Mac._run
        with patch.object(Mac, "_run", lambda p, **r: window if r["op"] == "snapshot" else real(p, **r)):
            text = self.call("snapshot", app="TextEdit")
        self.assertIn("(40 tabs: `tabs` lists them, `use_tab` switches)", text)
        self.assertIn('radio button "Tab 30"', text)
        self.assertNotIn('"Tab 29"', text)
        self.assertEqual(text.count("radio button"), 1)

    def test_cmd_l_in_a_browser_puts_the_cursor_where_return_only_navigates(self):
        browser = self.conn.test(self.conn.create("mac", "Web", {"apps": "Google Chrome"})["id"])["id"]
        asked = []
        go = lambda action, **a: self.conn.call(browser, action, dict(a, app="Google Chrome"),
                                                 ask=lambda t, f: asked.append(t) or False)
        self.assertIn("did not approve", go("type", text="example.com\n"))   # cursor could be anywhere
        go("key", key="l", modifiers=["cmd"])
        go("type", text="example.com\n")
        self.assertEqual(len(asked), 1)
        self.assertEqual([r["text"] for r in self.sent if r["op"] == "type"], ["example.com\n"])

    def test_threads_are_told_which_browser_is_sirs_own(self):
        prompt = self.env.session(kind="home").system_prompt()
        self.assertNotIn("Sir's own", prompt)                         # TextEdit and Notes only
        browser = self.conn.test(self.conn.create("mac", "Web", {"apps": "Google Chrome"})["id"])["id"]
        session = self.env.session(kind="home")
        session.refresh()
        prompt = session.system_prompt()
        self.assertIn(f"Sir's own Google Chrome", prompt)
        self.assertIn(f"`{browser}` with app \"Google Chrome\"", prompt)
        self.assertNotIn("No browser is connected", prompt)

    def test_a_call_that_is_clear_in_intent_is_put_right(self):
        with patch.object(Mac, "_capture", lambda p, iid, win: ("QUJD", "image/jpeg")), \
                patch.object(Mac, "_can_see", lambda p: True):
            self.call("snapshot", app="TextEdit")
            out = self.call("click", x=500, y=250)                       # no app, and the wrong click
            self.assertIn("Clicked (not described) at (500, 250)", out)
            self.assertEqual(self.sent[-2]["app"], "TextEdit")
            self.assertIn("screenshot is attached", self.call("screenshot"))
        fresh = self.conn.test(self.conn.create("mac", "Two", {"apps": "TextEdit, Notes"})["id"])["id"]
        with self.assertRaises(ConnectorError) as caught:
            self.conn.call(fresh, "snapshot", {}, ask=None)
        self.assertIn("say which app", str(caught.exception))

    def test_the_first_failed_call_brings_the_manual_once(self):
        session = self.env.session()
        first, failed, _ = session._execute("use_connector", {
            "connector": self.iid, "action": "click", "input": "{\"app\": \"TextEdit\", \"target\": \"u1\"}"})
        self.assertTrue(failed)
        self.assertIn("You had not read this connector's help", first)
        self.assertIn("# How to control a Mac app", first)
        second, _, _ = session._execute("use_connector", {
            "connector": self.iid, "action": "click", "input": "{\"app\": \"TextEdit\", \"target\": \"u1\"}"})
        self.assertNotIn("# How to control a Mac app", second)

    def test_help_is_a_manual_and_wrong_parameters_are_refused(self):
        text = self.conn.help(self.iid)
        self.assertIn("# How to control a Mac app", text)
        self.assertIn("You may control: TextEdit, Notes", text)
        self.assertNotIn("{id}", text)
        with self.assertRaises(ConnectorError) as caught:
            self.call("click", app="TextEdit", target="e1", colour="red", speed=2)
        self.assertIn("does not take `colour`, `speed`", str(caught.exception))


class _Overlay:
    pid = 2 ** 22 + 12345                    # no such process: stopping it must not raise
    stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def wait(self, timeout=None):
        self.stopped = True
        return 0


if __name__ == "__main__":
    unittest.main()


class PermissionGateTests(unittest.TestCase):
    def test_native_permission_error_is_not_misreported_as_a_timeout(self):
        from subprocess import CompletedProcess
        from claire_core.connectors.mac import PERMISSION
        env=Env()
        self.addCleanup(env.close)
        mac=Mac(env.core)
        response=CompletedProcess([],1,stdout='',stderr='Error: CLAIRE:NO_ACCESSIBILITY (-2700)')
        with patch('claire_core.connectors.mac.subprocess.run',return_value=response):
            with self.assertRaisesRegex(ConnectorError,'macOS has not allowed Claire') as error:
                mac._run(op='probe')
        self.assertEqual(str(error.exception),PERMISSION)
        self.assertIn('Codex/ChatGPT',str(error.exception))

    def test_no_apple_event_is_sent_before_accessibility_is_granted(self):
        import json, shutil, subprocess
        from pathlib import Path
        node=shutil.which('node')
        if not node:self.skipTest('Node is needed to exercise the JavaScript permission boundary')
        script=Path(__file__).resolve().parents[1]/'claire_core/connectors/mac_control.js'
        harness=r"""
const fs=require('fs'),vm=require('vm');let calls=0;
const ctx={ObjC:{import(){},unwrap:v=>v},$:{AXIsProcessTrusted:()=>false,
  NSWorkspace:{sharedWorkspace:{runningApplications:{js:[{activationPolicy:0,localizedName:'Google Chrome'},{activationPolicy:2,localizedName:'Background'}]}}}},
  Application(){calls++;throw Error('An Apple event must not be sent without permission');}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const permission=JSON.parse(ctx.run([JSON.stringify({op:'permissions'})]));
const apps=JSON.parse(ctx.run([JSON.stringify({op:'apps'})]));
let error='';try{ctx.run([JSON.stringify({op:'probe'})]);}catch(e){error=e.message;}
process.stdout.write(JSON.stringify({permission,apps,error,calls}));
"""
        run=subprocess.run([node,'-e',harness,str(script)],capture_output=True,text=True,timeout=5,check=True)
        result=json.loads(run.stdout)
        self.assertFalse(result['permission']['accessibility'])
        self.assertEqual(result['apps']['running'],['Google Chrome'])
        self.assertEqual(result['error'],'CLAIRE:NO_ACCESSIBILITY')
        self.assertEqual(result['calls'],0)
