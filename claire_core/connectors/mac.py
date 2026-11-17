"""Control of apps on this Mac, through macOS accessibility.

Claire reads an app's front window as a list of its controls -- the same kind
of snapshot the browser gives her -- and acts on one by its reference. Window
captures can also be inspected by the separate vision specialist.

Only the apps sir names when setting this up can be touched. While a task is
using it, a blue frame is drawn over the screen so control is never silent, and
it goes away when the task ends.
"""

from contextlib import contextmanager
import base64
import json
import os
import re
import signal
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
from pathlib import Path

from . import ConnectorError, Operation, Provider
from .browser import RISKY, SEARCH

BROWSERS = {"google chrome", "google chrome beta", "chromium", "brave browser", "microsoft edge", "arc",
            "vivaldi", "opera", "safari", "firefox"}
ADDRESS_BAR = 'text field "Address and search bar"'
BIG_TAB_STRIP = 12          # more tabs than this are summarised in a snapshot

# Words that matter in an app's menus and buttons beyond the browser's list.
APP_RISKY = re.compile(r"\b(quit|force quit|close all|revert\w*|shut ?down|restart|eject|"
                       r"move to (trash|bin)|print\w*|export\w*|overwrit\w*|replace)\b", re.I)


def _major(text):
    return bool(RISKY.search(text) or APP_RISKY.search(text))


HERE = Path(__file__).parent
CONTROL, OVERLAY = HERE / "mac_control.js", HERE / "mac_overlay.js"
# Furniture that says nothing about what the window is for.
NOISE = {"AXRulerMarker", "AXRuler", "AXScrollBar", "AXSplitter", "AXGrowArea", "AXValueIndicator",
         "AXUnknown", "AXLayoutItem"}
PLAIN = {"group", "scroll area", "button", "text", "image", "list", "table", "row", "cell", "column",
         "splitter group", "toolbar", "outline", "static text", "text field", "collection"}
PROBLEMS = {
    "NOT_RUNNING": "that app is not open. Use `open` first.",
    "NO_WINDOW": ("that app shows no window on the desktop sir is looking at. Its windows may be on "
                  "another desktop or hidden: use `open` to bring the app forward, then try again. "
                  "If it truly has none, make one with `menu` (File > New)."),
    "CHANGED": "the window changed since your snapshot. Take a new `snapshot` and use its references.",
    "BAD_KEY": ("unknown key. Use a single character, or one of: return, tab, space, delete, escape, "
                "left, right, up, down, home, end, pageup, pagedown."),
    "NO_MENU": "there is no such menu item. Use `menus` with the same path to see what is there.",
    "MENU_DISABLED": "that menu item is greyed out right now.",
    "AMBIGUOUS_APP": "multiple app instances match; choose an explicit observed window instead of guessing a browser profile.",
    "NO_TABS": "that window has no tabs.",
}
PERMISSION = ("macOS has not allowed Claire to control apps. Open System Settings → Privacy & "
              "Security → Device Control and Data Access (Accessibility on older macOS), and "
              "enable the app that starts Claire: Cursor, Codex/ChatGPT, Terminal or Python. "
              "Then Connect & test again.")


SCREEN_PERMISSION = ("macOS has not allowed Claire to see the screen. Open System Settings → Privacy "
                     "& Security → Screen & System Audio Recording, switch it on for the app that "
                     "runs Claire, then try again. Until then, use `snapshot`.")
NO_VISION = ("the model in use cannot see pictures, so a screenshot would tell you nothing. Use "
             "`snapshot` and references instead. (Sir can switch vision on with MODEL_MMPROJ in "
             "config.py.)")
GRID = ("Positions in this picture are on a 0-1000 grid: x runs from the left edge (0) to the "
        "right edge (1000), y from the top (0) to the bottom (1000).")


class Mac(Provider):
    id = "mac"
    label = "Mac apps"
    summary = ("Work inside apps on this Mac: read a window, click, type, use menus. Only the apps "
               "you name. Anything major asks first.")
    fields = (("apps", "Apps Claire may control, comma-separated (e.g. TextEdit, Notes)", False, True),
              ("ask_every", "Ask before every action (otherwise only major ones)", False, False),
              ("notes", "What Claire should use these apps for", False, False))
    strict = True
    aliases = {"application": "app", "name": "app", "ref": "target", "element": "target",
               "value": "text", "content": "text", "keys": "key", "modifier": "modifiers",
               "items": "path", "menu": "path", "description": "what", "label": "what",
               "double_click": "double", "lines": "amount", "query": "text", "title": "window",
               "index": "window", "number": "window"}

    def __init__(self, core):
        self.core = core
        self._lock = threading.Lock()
        self._interaction_lock = threading.RLock()
        self._control_scope = threading.local()
        self._enhanced_pids = set()
        self._ax_apps = {}
        self._owners = set()
        self._overlay = None
        self._snapshots = {}
        self._seen = {}            # (integration id, app) -> {ref: node}
        self._focus = {}           # integration id -> what was last clicked or filled
        self._vision = (0.0, True)  # when it was last checked, and the answer
        self._last = {}            # integration id -> the app she last worked in

    # -- setup ---------------------------------------------------------------

    def validate(self, config, secrets):
        if sys.platform != "darwin":
            raise ConnectorError("this connector controls macOS apps and needs a Mac")
        if secrets:
            raise ConnectorError("controlling apps needs no password or token")
        raw = config.get("apps") or ""
        names = raw if isinstance(raw, list) else re.split(r"[,\n]+", str(raw))
        apps = []
        for name in names:
            name = " ".join(str(name).split())
            if name.lower().endswith(".app"):
                name = name[:-4]
            if not name:
                continue
            if not re.fullmatch(r"[\w .+&'()-]{1,60}", name):
                raise ConnectorError(f"{name!r} does not look like an app name")
            if name.lower() in ("system events", "terminal", "iterm", "iterm2", "keychain access",
                                "system settings", "system preferences", "passwords"):
                raise ConnectorError(f"{name} cannot be handed to Claire: it controls the Mac "
                                     f"itself or holds your passwords")
            if name.lower() not in [a.lower() for a in apps]:
                apps.append(name)
        if not apps:
            raise ConnectorError("name at least one app Claire may control")
        clean = {"apps": apps[:12], "ask_every": config.get("ask_every") is True}
        return clean, {}, [f"Read and operate: {', '.join(clean['apps'])}",
                           "Major actions ask each time"]

    @contextmanager
    def control_scope(self, process_id=None, window_id=None):
        previous=getattr(self._control_scope,'process_id',None)
        previous_window=getattr(self._control_scope,'window_id',None)
        self._control_scope.process_id=process_id
        self._control_scope.window_id=window_id
        try:yield
        finally:
            self._control_scope.process_id=previous
            self._control_scope.window_id=previous_window

    def _prepare_native(self, app, process_id=None):
        from .. import mac_ax
        if not mac_ax.trusted():raise ConnectorError(PERMISSION)
        if process_id is not None and process_id in self._ax_apps:
            return process_id,self._ax_apps[process_id]
        candidates=self._run(op='instances',app=app)['instances']
        if not candidates and process_id is None:
            try:subprocess.run(['/usr/bin/open','-a',app],check=True,capture_output=True,timeout=10)
            except (OSError,subprocess.SubprocessError):raise ConnectorError('macOS could not open the allowed app '+app+'. Check that it is installed.')
            for _ in range(20):
                time.sleep(.1)
                candidates=self._run(op='instances',app=app)['instances']
                if candidates:break
        if process_id is not None:candidates=[x for x in candidates if x['pid']==process_id]
        for candidate in candidates:
            pid=candidate['pid']
            if pid not in self._ax_apps:self._ax_apps[pid]=mac_ax.application(pid)
        if len(candidates)>1:
            hosting=[x for x in candidates if any(re.match(r'^Claire(?:$|\s|[-–—])',w.get('AXTitle') or '') for w in mac_ax.windows(self._ax_apps[x['pid']]))]
            active=[x for x in candidates if x['active']]
            if len(hosting)==1:candidates=hosting
            elif len(active)==1:candidates=active
            else:
                regular=[]
                for candidate in candidates:
                    command=subprocess.run(['ps','-p',str(candidate['pid']),'-o','command='],capture_output=True,text=True,timeout=3).stdout
                    if not re.search(r'--user-data-dir|--remote-debugging|--headless',command):regular.append(candidate)
                if len(regular)==1:candidates=regular
        if len(candidates)!=1:raise ConnectorError(PROBLEMS['AMBIGUOUS_APP'] if candidates else PROBLEMS['NOT_RUNNING'])
        pid=candidates[0]['pid']
        return pid,self._ax_apps[pid]

    def _browser_native(self, ax, app, pid, request):
        op=request['op'];expected=request.get('expected_window')
        def key(value,window=None,text=None):
            app.set('AXFrontmost',True)
            if window is not None:window.press('AXRaise')
            meta=ax.info(window,pid) if window is not None else {}
            if window is not None and meta.get('id') is None:raise RuntimeError('CLAIRE:CHANGED')
            payload={'app':request['app'],'process_id':pid,'expected_window':meta.get('id'),
                     'expected_size':[meta.get('w',0),meta.get('h',0)]}
            self._run(op='browser_shortcut',key=value,**payload)
            if text is not None:
                deadline=time.monotonic()+1
                while True:
                    field=app.get('AXFocusedUIElement')
                    label=(str(field.get('AXTitle') or '')+' '+str(field.get('AXDescription') or '')) if field else ''
                    if field and field.get('AXRole') in ('AXTextField','AXComboBox') and re.search(r'address|search or enter|search the web',label,re.I):break
                    if time.monotonic()>deadline:raise RuntimeError('CLAIRE:CHANGED')
                    time.sleep(.05)
                self._run(op='browser_shortcut',key='url',url=text,**payload)
        def read():return ax.browser_state(app,pid,expected)
        def guard(state):
            if request.get('expected_tab_ids') is not None:
                if [t.get('identity') for t in state['tabs']]!=request['expected_tab_ids']:raise RuntimeError('CLAIRE:CHANGED')
            elif request.get('expected_tabs') is not None and [t['title'] for t in state['tabs']]!=request['expected_tabs']:
                raise RuntimeError('CLAIRE:CHANGED')
            index=request.get('expected_index')
            if index is not None and not 0<=index<len(state['tabs']):raise RuntimeError('CLAIRE:CHANGED')
        if op=='browser_new':
            if not ax.windows(app):
                if ax.has_window_candidates(pid):raise RuntimeError('CLAIRE:NO_WINDOW')
                before={'tabs':[],'window':None,'url':''};key('n')
            else:
                before,_,window=read();expected=before['window']['id'];key('t',window)
            deadline=time.monotonic()+3;after=None
            while time.monotonic()<deadline:
                try:
                    after,_,_=read()
                    index=next((i for i,t in enumerate(after['tabs']) if t['selected']),-1)
                    if (len(after['tabs'])==len(before['tabs'])+1 and index>=0 and
                        [t['identity'] for i,t in enumerate(after['tabs']) if i!=index]==[t['identity'] for t in before['tabs']]):break
                except RuntimeError:pass
                time.sleep(.05)
            if after is None:raise RuntimeError('CLAIRE:CHANGED')
            index=next((i for i,t in enumerate(after['tabs']) if t['selected']),-1)
            if index<0 or [t['identity'] for i,t in enumerate(after['tabs']) if i!=index]!=[t['identity'] for t in before['tabs']]:raise RuntimeError('CLAIRE:CHANGED')
            return {'before':before,'after':after,'index':index,'verified':True}
        before,tabs,window=read();guard(before)
        if op=='browser_state':return before
        index=request.get('expected_index')
        if index is None:raise RuntimeError('CLAIRE:CHANGED')
        if op in ('browser_select','browser_close_owned') and not before['tabs'][index]['selected']:
            tabs[index].press();time.sleep(.05)
            before,tabs,window=read();guard(before)
        if not before['tabs'][index]['selected']:raise RuntimeError('CLAIRE:CHANGED')
        if op=='browser_select':return {'after':before,'performed':True,'verified':True}
        if op=='browser_navigate':
            key('l',window,text=request['url'])
            normalize=lambda v:re.sub(r'^www\.','',re.sub(r'^https?://','',str(v).lower())).rstrip('/')
            deadline=time.monotonic()+4;after=before
            while time.monotonic()<deadline:
                after,_,_=read()
                if normalize(after['url'])==normalize(request['url']):break
                time.sleep(.05)
            return {'before':before,'after':after,'performed':True,'verified':normalize(after['url'])==normalize(request['url'])}
        if op=='browser_close_owned':
            key('w',window)
            remaining=[t['identity'] for i,t in enumerate(before['tabs']) if i!=index]
            deadline=time.monotonic()+3;after=None
            while time.monotonic()<deadline:
                if not remaining and not any(ax.info(w,pid)['id']==before['window']['id'] for w in ax.windows(app)):
                    return {'after':{'tabs':[],'window':None,'url':''},'performed':True,'verified':True}
                try:
                    after,_,_=read()
                    if [t['identity'] for t in after['tabs']]==remaining:break
                except RuntimeError:pass
                time.sleep(.05)
            verified=after is not None and [t['identity'] for t in after['tabs']]==remaining
            return {'after':after or before,'performed':True,'verified':verified}
        raise RuntimeError('Unknown browser action')

    def _run(self, **request):
        request.setdefault('ax_ready_pids',list(self._enhanced_pids))
        process_id=getattr(self._control_scope,'process_id',None)
        if process_id is not None and request.get('app'):
            request.setdefault('process_id',process_id)
        window_id=getattr(self._control_scope,'window_id',None)
        if window_id is not None and request.get('app'):
            request.setdefault('expected_window',window_id)
        if request.get('app') and request.get('op') not in ('instances','apps','probe','permissions') and sys.platform=='darwin':
            from .. import mac_ax
            try:
                pid,axapp=self._prepare_native(request['app'],request.get('process_id'))
                request['process_id']=pid
                if request.get('op','').startswith('browser_') and request['op']!='browser_shortcut':
                    return self._browser_native(mac_ax,axapp,pid,request)
                if request.get('op') in ('windows','window','snapshot','click','set_value'):
                    return mac_ax.run(axapp,pid,request)
            except RuntimeError as exc:
                for code,text in PROBLEMS.items():
                    if 'CLAIRE:'+code in str(exc):raise ConnectorError(text)
                raise ConnectorError('Native accessibility failed: '+str(exc))
        try:
            done = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", str(CONTROL),
                                   json.dumps(request)], capture_output=True, text=True,
                                  timeout=self.core.settings.connector_timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise ConnectorError("the app did not respond in time. If macOS is showing a "
                                 "permission question, answer it.")
        except OSError as exc:
            raise ConnectorError(f"could not reach the app: {exc}")
        if done.returncode != 0:
            error = done.stderr.strip()
            for code, text in PROBLEMS.items():
                if "CLAIRE:" + code in error:
                    raise ConnectorError(text)
            if "CLAIRE:NO_ACCESSIBILITY" in error or "-25211" in error or "assistive access" in error or "-1743" in error \
                    or "Not authorized" in error:
                raise ConnectorError(PERMISSION)
            if "-1719" in error or "-1728" in error or "Invalid index" in error or "Can't get" in error:
                raise ConnectorError(PROBLEMS["CHANGED"])
            raise ConnectorError("the app reported a problem: " + error[-300:])
        try:
            result=json.loads(done.stdout)
            window=result.get('window') or result.get('after',{}).get('window') or (result if request.get('op')=='window' else {})
            if window and window.get('process_id') is not None:self._enhanced_pids.add(window['process_id'])
            return result
        except ValueError:
            raise ConnectorError("the app gave an answer that could not be read")

    def test(self, integration, keys):
        running = {n.lower() for n in self._run(op="apps")["running"]}
        self._run(op="probe")                       # fails here if Accessibility is off
        apps = integration["config"]["apps"]
        open_now = [a for a in apps if a.lower() in running]
        return f"{len(apps)} app{'s' if len(apps) != 1 else ''}, {len(open_now)} open now", {}

    # -- the blue frame, held only while a job is in control ------------------

    def claim(self, integration, owner):
        if owner is None:
            return
        with self._lock:
            self._owners.add(owner)
            if self._overlay is None or self._overlay.poll() is not None:
                try:
                    self._overlay = subprocess.Popen(
                        ["/usr/bin/osascript", "-l", "JavaScript", str(OVERLAY),
                         "Claire is controlling this Mac"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True)
                except OSError:
                    self._overlay = None          # the frame is a courtesy, not a requirement

    def release(self, owner):
        with self._lock:
            self._owners.discard(owner)
            if self._owners:
                return
            overlay, self._overlay = self._overlay, None
        self._stop(overlay)

    def abort(self, owner):
        self.release(owner)

    def close(self):
        with self._lock:
            self._owners.clear()
            self._ax_apps.clear()
            overlay, self._overlay = self._overlay, None
        self._stop(overlay)

    @staticmethod
    def _stop(overlay):
        if overlay is None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(overlay.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                overlay.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                continue

    def disconnect(self, integration, keys):
        for key in [k for k in self._seen if k[0] == integration["id"]]:
            self._seen.pop(key, None)
        self._focus.pop(integration["id"], None)

    # -- using ---------------------------------------------------------------

    def operations(self, integration):
        apps = ", ".join(integration["config"]["apps"])
        app = f"Which app: one of {apps}. May be left out to stay in the app you last used"
        target = "A reference from the latest snapshot of that app, like e12"
        every = bool(integration["config"].get("ask_every"))
        return [
            Operation("apps", "List the apps you may control and which are open.", writes=every),
            Operation("open", "Open an app, or bring it to the front.", {"app": app},
                      writes=every, required=()),
            Operation("snapshot", "Read the app's front window: every control, with a reference "
                                  "to act on it.", {"app": app}, writes=every, required=()),
            Operation("click", "Click a control.", {"app": app, "target": target},
                      writes=every, required=("target",)),
            Operation("set_text", "Replace the whole contents of a text field or text area.",
                      {"app": app, "target": target, "text": "The new contents"},
                      writes=every, required=("target", "text")),
            Operation("type", "Type text at the cursor, as if on the keyboard. Click the field first.",
                      {"app": app, "text": "What to type"}, writes=every, required=("text",)),
            Operation("key", "Press one key, optionally with modifiers.",
                      {"app": app, "key": "A character, or: return, tab, space, delete, escape, left, "
                                          "right, up, down, home, end, pageup, pagedown",
                       "modifiers": "A list from: cmd, shift, alt, ctrl"},
                      writes=every, required=("key",)),
            Operation("look", "Take a screenshot of the app's front window and look at it. Use this "
                              "when `snapshot` does not show what you need.", {"app": app},
                      writes=every, required=()),
            Operation("click_at", "Click a point you can see in the latest screenshot.",
                      {"app": app, "x": "Across, 0 (left edge) to 1000 (right edge)",
                       "y": "Down, 0 (top edge) to 1000 (bottom edge)",
                       "what": "What you are clicking, in a few words",
                       "double": "true for a double click"},
                      writes=every, required=("x", "y", "what")),
            Operation("scroll", "Scroll the window, then show it again.",
                      {"app": app, "direction": "down, up, right or left (default down)",
                       "amount": "How many lines, default 10 (about a third of a page)",
                       "x": "Where the pointer should be, 0-1000 across (default 500)",
                       "y": "Where the pointer should be, 0-1000 down (default 500)"},
                      writes=every),
            Operation("windows", "List the open windows of the apps you may control.",
                      {"app": "Only this app (optional)"}, writes=every),
            Operation("use_window", "Bring one window to the front so your next actions apply to it.",
                      {"app": app, "window": "Its number in `windows`, or part of its title"},
                      writes=every, required=("window",)),
            Operation("tabs", "List the tabs of the app's front window (a browser's tabs, say).",
                      {"app": app}, writes=every),
            Operation("use_tab", "Switch to one tab of the app's front window.",
                      {"app": app, "tab": "Its number in `tabs`, or part of its title"},
                      writes=every, required=("tab",)),
            Operation("find", "Search every open window of your apps for some text, to locate "
                              "which window holds what you are looking for.",
                      {"text": "The words to look for", "app": "Only this app (optional)"},
                      writes=every, required=("text",)),
            Operation("menus", "List the app's menus, or the items inside one.",
                      {"app": app, "path": "Empty for the menu bar, or e.g. [\"File\"]"},
                      writes=every, required=()),
            Operation("menu", "Choose a menu item.",
                      {"app": app, "path": "The menu and item, e.g. [\"File\", \"Save…\"]"},
                      writes=every, required=("path",)),
        ]

    def manual(self, integration):
        return MANUAL.replace("{apps}", ", ".join(integration["config"]["apps"]))

    def adapt(self, integration, action, args):
        """Put right a call whose meaning is plain."""
        if action in ("click", "double_click") and "target" not in args \
                and "ref" not in args and "x" in args and "y" in args:
            args.setdefault("what", "(not described)")
            if action == "double_click":
                args["double"] = True
            action = "click_at"                      # she is pointing at the screenshot
        if action in ("screenshot", "see", "view", "screen"):
            action = "look"
        if action == "press_key":
            action = "key"
        if action == "look":
            args = {k: v for k, v in args.items() if k == "app"}     # a look takes no question
        return action, args

    def _app(self, integration, args):
        apps = integration["config"]["apps"]
        if not str(args.get("app") or "").strip():
            # Left out: the app she is already working in, or the only one there is.
            last = self._last.get(integration["id"])
            if last in apps:
                return last
            if len(apps) == 1:
                return apps[0]
            raise ConnectorError("say which app with `app`. You may control: " + ", ".join(apps))
        wanted = " ".join(str(args.get("app") or "").split()).lower()
        if wanted.endswith(".app"):
            wanted = wanted[:-4]
        for name in apps:
            if name.lower() == wanted:
                self._last[integration["id"]] = name
                return name
        raise ConnectorError(f"{args.get('app')!r} is not an app you may control. You may control: "
                             + ", ".join(integration["config"]["apps"]))

    def _node(self, integration, app, target):
        target = str(target or "").strip().strip("[]").replace("ref=", "")
        node = self._seen.get((integration["id"], app), {}).get(target)
        if not node:
            raise ConnectorError(f"there is no {target!r} in your latest snapshot of {app}. "
                                 f"Take a `snapshot` and use a reference from it.")
        return node

    @staticmethod
    def _path(args):
        path = args.get("path") or []
        if isinstance(path, str):
            path = [p.strip() for p in re.split(r"\s*(?:>|→|/)\s*", path) if p.strip()]
        if not isinstance(path, list) or not all(isinstance(p, str) and p for p in path):
            raise ConnectorError("`path` is a list of menu names, e.g. [\"File\", \"Save…\"]")
        return path[:6]

    def review(self, integration, op, args):
        """Why this call needs sir's approval, or None when it does not."""
        if integration["config"].get("ask_every"):
            return "CHANGES DATA"
        name = op.name
        if name in ("apps", "open", "snapshot", "look", "menus", "set_text", "scroll", "windows",
                    "use_window", "find", "tabs", "use_tab"):
            return None
        app = self._app(integration, args)
        if name == "click_at":
            # Judged by what macOS says is under the point where it can tell,
            # and by her own description of it where it cannot.
            x, y = self._point(args)
            under = self._under(app, x, y)
            self._judged = ((app, x, y), under)        # the call that follows reuses it
            line = " ".join(s for s in (under, str(args.get("what") or "")) if s)
            return f"MAJOR ACTION: clicks “{(under or line)[:90]}” in {app}" if _major(line) else None
        # Return is a new line in a document and a search in a search field.
        # Anywhere else -- a message box, a form, a dialog -- it sends or confirms.
        focus = self._focus.get(integration["id"], "")
        harmless_return = focus.startswith("text area") or bool(SEARCH.search(focus))
        if name == "type":
            if "\n" in str(args.get("text") or "") and not harmless_return:
                return f"MAJOR ACTION: types text ending lines with Return in {app}, which may send or confirm"
            return None
        if name == "click":
            node = self._seen.get((integration["id"], app), {}).get(str(args.get("target") or ""))
            line = node["line"] if node else str(args.get("target") or "")
            return f"MAJOR ACTION: clicks “{line[:90]}” in {app}" if _major(line) else None
        if name == "menu":
            path = " > ".join(self._path(args))
            return f"MAJOR ACTION: chooses {path} in {app}" if _major(path) else None
        if name == "key":
            key = str(args.get("key") or "").lower()
            mods = [str(m).lower() for m in (args.get("modifiers") or [])]
            if key in ("return", "enter") and not harmless_return:
                return f"MAJOR ACTION: presses Return in {app}, which may send or confirm"
            if key in ("delete", "backspace", "forwarddelete") and mods:
                return f"MAJOR ACTION: presses {'+'.join(mods)}+{key} in {app}, which may delete"
            if mods and key in ("q", "w", "s", "p", "d") and any(m in ("cmd", "command") for m in mods):
                return f"MAJOR ACTION: presses cmd+{key} in {app}"
            return None
        return "MAJOR ACTION: an action there is no rule for yet"

    def call(self, integration, operation, args, keys):
        name, iid = operation.name, integration["id"]
        if name == "apps":
            running = {n.lower() for n in self._run(op="apps")["running"]}
            return "\n".join(f"{a}  ({'open' if a.lower() in running else 'not open'})"
                             for a in integration["config"]["apps"])
        if name in ("windows", "find") and not str(args.get("app") or "").strip():
            running = {n.lower() for n in self._run(op="apps")["running"]}
            targets = [a for a in integration["config"]["apps"] if a.lower() in running]
        elif name in ("windows", "find"):
            targets = [self._app(integration, args)]
        if name == "windows":
            lines = []
            for target in targets:
                lines.append(f"{target}:")
                wins = self._windows(target)
                lines += [f"  {i + 1}. {w['title'] or '(untitled)'}"
                          + (" (in front)" if i == 0 and not w["minimized"] else "")
                          + (" (minimised)" if w["minimized"] else "") for i, w in enumerate(wins)]
                if not wins:
                    lines.append("  (none on this desktop -- they may be on another desktop or "
                                 "hidden; `open` the app to bring them forward)")
            return "\n".join(lines) or "None of your apps is open. Use `open`."
        if name == "find":
            return self._find(targets, " ".join(str(args.get("text") or "").split()))
        app = self._app(integration, args)
        if name == "tabs":
            tabs = self._run(op="tabs", app=app)["tabs"]
            return f"{len(tabs)} tabs in {app}'s front window:\n" + "\n".join(
                f"  {i + 1}. {t['title'] or '(untitled)'}" + ("  (current)" if t["selected"] else "")
                for i, t in enumerate(tabs))
        if name == "use_tab":
            tabs = self._run(op="tabs", app=app)["tabs"]
            wanted = str(args.get("tab") or "").strip()
            index = int(wanted) - 1 if wanted.isdigit() else next(
                (i for i, t in enumerate(tabs) if wanted.lower() in t["title"].lower()), -1)
            if not 0 <= index < len(tabs):
                raise ConnectorError(f"{app} has no tab {wanted!r}. Use `tabs` to see them.")
            self._run(op="pick_tab", app=app, index=index, title=tabs[index]["title"])
            return f"Switched to tab {index + 1}, “{tabs[index]['title']}”.\n\n" + self._snapshot(iid, app)
        if name == "use_window":
            wins = self._windows(app)
            wanted = str(args.get("window") or "").strip()
            index = int(wanted) - 1 if wanted.isdigit() else next(
                (i for i, w in enumerate(wins) if wanted.lower() in w["title"].lower()), -1)
            if not 0 <= index < len(wins):
                raise ConnectorError(f"{app} has no window {wanted!r}. Its windows: "
                                     + "; ".join(f"{i + 1}. {w['title']}" for i, w in enumerate(wins)))
            self._run(op="raise", app=app, win=index)
            return f"“{wins[index]['title']}” is now in front.\n\n" + self._snapshot(iid, app)
        if name == "scroll":
            direction = str(args.get("direction") or "down").lower()
            if direction not in ("down", "up", "left", "right"):
                raise ConnectorError("direction is down, up, left or right")
            try:
                amount = max(1, min(int(float(args.get("amount") or 10)), 60))
            except (TypeError, ValueError):
                raise ConnectorError("amount is a number of lines, like 10")
            x, y = self._point({"x": args.get("x", 500), "y": args.get("y", 500)})
            if direction in ("left", "right"):
                # A sideways wheel is rare; the arrow keys do the same without it.
                self._run(op="key", app=app, key=direction, modifiers=[])
            else:
                win = self._run(op="window", app=app)
                self._run(op="scroll_at", app=app, lines=amount if direction == "down" else -amount,
                          x=round(win["x"] + x / 1000 * win["w"], 1),
                          y=round(win["y"] + y / 1000 * win["h"], 1))
            done = f"Scrolled {direction} {amount} lines."
            if self._can_see():
                try:
                    return done + "\n\n" + self._look(iid, app)
                except ConnectorError:
                    pass
            return done + "\n\n" + self._snapshot(iid, app)
        if name == "open":
            self._run(op="open", app=app)
            try:
                return f"{app} is open and in front.\n\n" + self._snapshot(iid, app)
            except ConnectorError as exc:
                return f"{app} is open and in front, but: {exc}"
        if name == "snapshot":
            return self._snapshot(iid, app)
        if name == "look":
            return self._look(iid, app)
        if name == "click_at":
            x, y = self._point(args)
            win = self._run(op="window", app=app)
            key, under = getattr(self, "_judged", (None, ""))
            if key != (app, x, y):
                under = self._under(app, x, y, win)
            self._focus[iid] = under or str(args.get("what") or "")
            self._run(op="click_at", app=app, double=args.get("double") in (True, "true", "yes", 1),
                      x=round(win["x"] + x / 1000 * win["w"], 1), y=round(win["y"] + y / 1000 * win["h"], 1))
            what = " ".join(str(args.get("what") or "").split())[:80]
            return f"Clicked {what} at ({x:.0f}, {y:.0f}).\n\n" + self._look(iid, app)
        if name in ("click", "set_text"):
            node = self._node(integration, app, args.get("target"))
            self._focus[iid] = node["line"]
            if name == "click":
                self._run(op="click", app=app, path=node["path"], role=node["role"],
                          expected_window=node.get("window"),expected_label=node.get("label"))
                done = f"Clicked {node['line']}."
            else:
                self._run(op="set_value", app=app, path=node["path"], role=node["role"],
                          expected_window=node.get("window"),expected_label=node.get("label"),text=str(args.get("text") or ""))
                done = f"Set the text of {node['line']}."
        elif name == "type":
            self._run(op="type", app=app, text=str(args.get("text") or ""))
            done = "Typed it."
        elif name == "key":
            mods = args.get("modifiers") or []
            if isinstance(mods, str):
                mods = [m for m in re.split(r"[+,\s]+", mods) if m]
            key = str(args.get("key") or "")
            if "+" in key and len(key) > 1:                 # "cmd+s" written as one key
                *more, key = key.split("+")
                mods = list(mods) + more
            self._run(op="key", app=app, key=key if len(key) == 1 else key.lower(),
                      modifiers=[str(m).lower() for m in mods][:4])
            if app.lower() in BROWSERS and key.lower() in ("l", "t") \
                    and any(str(m).lower() in ("cmd", "command") for m in mods):
                self._focus[iid] = ADDRESS_BAR         # every browser puts the cursor there
            done = f"Pressed {'+'.join([str(m) for m in mods] + [key])}."
        elif name == "menus":
            items = self._run(op="menus", app=app, path=self._path(args))["items"]
            return "\n".join(items) or "(empty)"
        elif name == "menu":
            path = self._path(args)
            if len(path) < 2:
                raise ConnectorError("give the menu and the item, e.g. [\"File\", \"Save…\"]")
            self._run(op="menu", app=app, path=path)
            done = f"Chose {' > '.join(path)}."
        else:
            raise ConnectorError(f"no action {name!r}")
        # What the window looks like now, so the next step needs no extra call.
        try:
            return done + "\n\n" + self._snapshot(iid, app)
        except ConnectorError as exc:
            return f"{done}\n\n(Could not read the window afterwards: {exc})"

    def _windows(self, app):
        try:
            return self._run(op="windows", app=app)["windows"]
        except ConnectorError as exc:
            if str(exc) == PROBLEMS["NOT_RUNNING"]:
                return []
            raise

    def _find(self, apps, text):
        if len(text) < 2:
            raise ConnectorError("give at least two characters to look for")
        hits, needle = [], text.lower()
        for app in apps:
            for index, win in enumerate(self._windows(app)):
                try:
                    nodes = self._run(op="snapshot", app=app, win=index, max=400, full=True)["nodes"]
                except ConnectorError:
                    continue
                for node in nodes:
                    for field in ("name", "desc", "value"):
                        hay = node[field]
                        at = hay.lower().find(needle)
                        if at < 0 or node["role"] in NOISE:
                            continue
                        around = " ".join(hay[max(0, at - 60):at + len(text) + 60].split())
                        hits.append(f"{app}, window {index + 1} “{win['title']}”: "
                                    f"{node['role'][2:].lower()}: …{around}…")
                        break
                    if len(hits) >= 20:
                        break
        if not hits:
            return (f"“{text}” was not found in any open window of: {', '.join(apps) or 'your apps'}. "
                    f"It may be further down a page (scroll), in a closed document, or in a "
                    f"part of the window that has no readable text (look).")
        return (f"Found “{text}” in:\n" + "\n".join(hits)
                + "\n\nTo work there: use_window with that app and window number.")

    @staticmethod
    def _point(args):
        try:
            x, y = float(args.get("x")), float(args.get("y"))
        except (TypeError, ValueError):
            raise ConnectorError("x and y are numbers from 0 to 1000")
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ConnectorError("x and y are positions on the 0-1000 grid of the screenshot, "
                                 "not pixels: 0 is the left or top edge, 1000 the right or bottom")
        return x, y

    def _under(self, app, x, y, win=None):
        """What macOS says the control at a point of the window is, or ''."""
        try:
            win = win or self._run(op="window", app=app)
            sx, sy = win["x"] + x / 1000 * win["w"], win["y"] + y / 1000 * win["h"]
            best = None
            for node in self._run(op="snapshot", app=app, max=400, geo=True)["nodes"]:
                box = node.get("box")
                label = node["name"] or node["desc"]
                if not box or not label or node["role"] in NOISE:
                    continue
                if box[0] <= sx <= box[0] + box[2] and box[1] <= sy <= box[1] + box[3]:
                    if best is None or box[2] * box[3] < best[0]:
                        best = (box[2] * box[3], f'{node["role"][2:].lower()} "{label[:80]}"')
            return best[1] if best else ""
        except ConnectorError:
            return ""

    def _can_see(self):
        """Whether the model can take a picture. Asked of the server, briefly cached."""
        checked, answer = self._vision
        if time.monotonic() - checked < 30 and checked:
            return answer
        try:
            described = self.core.llm.describe("brain", refresh=True)
            if "vision" in described:
                answer = bool(described["vision"])        # LM Studio says
            else:
                base = self.core.settings.api_base
                root = base[:-3] if base.endswith("/v1") else base
                with urllib.request.urlopen(root.rstrip("/") + "/props", timeout=5) as r:
                    answer = bool((json.load(r).get("modalities") or {}).get("vision"))
        except Exception:
            answer = True                 # cannot tell: let the attempt speak for itself
        self._vision = (time.monotonic(), answer)
        return answer

    def _capture(self, iid, win):
        """A picture of one region of the screen, as (base64, mime)."""
        folder = os.path.join(self.core.settings.state_dir, "connectors", iid)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd,path = tempfile.mkstemp(prefix="screen-",suffix=".jpg",dir=folder)
        os.close(fd)
        region = ",".join(str(int(win[k])) for k in ("x", "y", "w", "h"))
        try:
            subprocess.run(["/usr/sbin/screencapture", "-x"] +
                           (["-o", "-l", str(win["id"])] if win.get("id") is not None else ["-R", region]) + ["-t", "jpg", path],
                           capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise ConnectorError(SCREEN_PERMISSION)
            subprocess.run(["/usr/bin/sips", "-Z", "1280", path], capture_output=True, timeout=20)
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode(), "image/jpeg"
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConnectorError(f"could not take the screenshot: {exc}")
        finally:
            try:
                os.unlink(path)               # a picture of sir's screen is not left on disk
            except OSError:
                pass

    def _look(self, iid, app):
        if not self._can_see():
            raise ConnectorError(NO_VISION)
        win = self._run(op="window", app=app)
        self.core.connectors.attach(*self._capture(iid, win))
        title = f" “{win['title']}”" if win.get("title") else ""
        return f"{app} -- window{title}: the screenshot is attached. {GRID}"

    def _snapshot(self, iid, app):
        found = self._run(op="snapshot", app=app, max=400)
        return self._format_snapshot(iid, app, found)

    def _format_snapshot(self, iid, app, found):
        seen, lines, n = {}, [], 0
        shown = {}                                  # path -> indent it was printed at
        # A browser with many tabs would fill the snapshot with them: show the
        # current tab and say how many others there are.
        tabs_in = {}
        for node in found["nodes"]:
            if node["role"] == "AXRadioButton":
                parent = tuple(node["path"][:-1])
                tabs_in[parent] = tabs_in.get(parent, 0) + 1
        big = {p for p, count in tabs_in.items() if count > BIG_TAB_STRIP}
        summarised = set()
        for node in found["nodes"]:
            if node["role"] in NOISE:
                continue
            parent = tuple(node["path"][:-1])
            if node["role"] == "AXRadioButton" and parent in big:
                if parent not in summarised:
                    summarised.add(parent)
                    indent = shown.get(parent, -1) + 1
                    lines.append("  " * indent + f"- ({tabs_in[parent]} tabs: `tabs` lists them, "
                                                 f"`use_tab` switches)")
                if node["value"] not in ("true", "1"):
                    continue
            role = re.sub(r"(?<!^)(?=[A-Z])", " ", node["role"][2:]).lower() or "element"
            label = node["name"] or (node["desc"] if node["desc"].lower() not in PLAIN | {role} else "")
            value = " ⏎ ".join(" ".join(part.split()) for part in node["value"].strip().split("\n"))
            if role in ("group", "scroll area", "split group", "unknown", "layout area") and not label:
                continue                            # an unnamed box: show what is in it, not the box
            parents = [shown[tuple(node["path"][:i])] for i in range(len(node["path"]) - 1, 0, -1)
                       if tuple(node["path"][:i]) in shown]
            indent = (parents[0] + 1) if parents else 0
            shown[tuple(node["path"])] = indent
            n += 1
            ref = f"e{n}"
            line = role + (f' "{label[:80]}"' if label else "")
            seen[ref] = {"path": node["path"], "role": node["role"], "line": line, "label": label, "window": (found.get("window") or {}).get("id")}
            extra = (f": {value[:300]}" if value else "") + ("" if node["enabled"] else " (disabled)")
            lines.append("  " * indent + f"- {line} [ref={ref}]{extra}")
        self._seen[(iid, app)] = seen
        self._snapshots[(iid,app)] = found.get("window") or {"title":found.get("title")}
        head = f"{app} -- window “{found['title']}”" if found["title"] else f"{app} -- front window"
        tail = "\n(The window has more than is shown.)" if found["full"] else ""
        return head + "\n```yaml\n" + ("\n".join(lines) or "(nothing readable)") + "\n```" + tail


MANUAL = """# How to control a Mac app

You may control: {apps}. Nothing else. Each action is one use_connector call
with connector `{id}`, and every action names the app.

1. Bring the app up. The reply shows its front window, so you do not need a
   snapshot straight after.

       <action>open</action>
       <input>{"app": "TextEdit"}</input>

2. Read the window. Each control is one line ending in a reference:

       - text area [ref=e2]: Shopping list
       - button "Save" [ref=e9]

3. Act by reference. Pass only the reference, like "e9".

       <action>click</action>
       <input>{"app": "TextEdit", "target": "e9"}</input>

       <action>set_text</action>
       <input>{"app": "TextEdit", "target": "e2", "text": "milk\\neggs"}</input>

   `set_text` replaces everything in a field, and is the simplest way to write
   several lines at once (use \\n between lines). To add at the cursor instead,
   click the field and then:

       <action>type</action>
       <input>{"app": "TextEdit", "text": " and bread\\nbutter"}</input>

4. Every action replies with the window as it is afterwards. References are
   only good for the latest snapshot; use the new ones.

Keys and menus:

       <action>key</action>
       <input>{"app": "TextEdit", "key": "s", "modifiers": ["cmd"]}</input>

       <action>menus</action>
       <input>{"app": "TextEdit", "path": ["File"]}</input>

       <action>menu</action>
       <input>{"app": "TextEdit", "path": ["File", "New"]}</input>

A menu is the dependable way to do what the app can do: list it with `menus`
before guessing an item's name (names are exact, including "…").

# Seeing the window

`snapshot` is exact and should be your first choice. When it does not show what
you need -- a drawing, a web page inside the app, a control with no name -- look:

       <action>look</action>
       <input>{"app": "TextEdit"}</input>

The reply carries a screenshot of the window. Click something in it by where it
is on a 0-1000 grid across and down the picture -- not pixels. The middle of the
picture is x 500, y 500; a button a quarter of the way across near the top is
about x 250, y 80.

       <action>click_at</action>
       <input>{"app": "TextEdit", "x": 250, "y": 80, "what": "the bold button"}</input>

The reply is a new screenshot: check it to see that the click did what you
meant before going on. If it missed, look again and adjust; do not repeat the
same position.

# More than one window, and finding things

Your actions apply to the app's front window. To see what else is open:

       <action>windows</action>

To find which window holds something, search them all at once instead of
opening each:

       <action>find</action>
       <input>{"text": "invoice 2041"}</input>

Then bring that window forward and carry on in it:

       <action>use_window</action>
       <input>{"app": "TextEdit", "window": 2}</input>

A long page shows only part of itself. Scroll, and you are shown the window
again:

       <action>scroll</action>
       <input>{"app": "TextEdit", "direction": "down", "amount": 15}</input>

If you are looking for something on a long page, `find` first: it reads the
whole text, not only what is on screen.

# Browsers

A browser window reads like any other: its address bar, its buttons, and the
page inside a "web area". To go somewhere, put the cursor in the address bar
with cmd+L (or open a new tab with cmd+T), type the address with a line break
at the end, and the page loads:

       <action>key</action>
       <input>{"app": "Google Chrome", "key": "l", "modifiers": ["cmd"]}</input>

       <action>type</action>
       <input>{"app": "Google Chrome", "text": "example.com\\n"}</input>

Tabs:

       <action>tabs</action>
       <input>{"app": "Google Chrome"}</input>

       <action>use_tab</action>
       <input>{"app": "Google Chrome", "tab": "Inbox"}</input>

These are sir's own tabs and accounts, signed in as him. Read and move around
freely; anything that sends, posts, buys or deletes asks him first. Closing a
tab (cmd+W) asks too -- close only a tab you opened yourself.

# When something fails

Read the error and fix that one thing. "window changed" means take a new
snapshot. If a control you need is not in the snapshot, look for it in the
menus. Do not reach for another tool or a script: this is how the app is used.

# What asks sir first

Reading, opening, typing and ordinary clicks go ahead. Sir is asked before a
click or menu item that deletes, sends, pays, saves over, quits or confirms,
before Return outside a search field, and before closing or quitting with a
shortcut. Do what he asked and stop; do not tidy up, save or close things he
did not mention. A blue frame is on his screen while you work."""
