"""The terminal client.

    claire2                     work in this directory, as v1 did
    claire2 "task"              one-shot
    claire2 home                talk to Claire in her home thread (via the service)
    claire2 app                 open the app in your browser
    claire2 serve               run the service in the foreground
    claire2 tasks | approvals | tools | inbox | status
    claire2 stop [--unload]     stop the service (--unload: and the model, freeing its memory)
    claire2 bench [brain|scout] measure a model on Claire's own tasks
    claire2 connectors          services Claire can reach (add, connect, test, remove)
    claire2 schedules           recurring jobs (pause, resume, run, remove)
    claire2 skills              instructions Claire can load (add FILE, show, remove)
    claire2 autostart on|off    start the service when you log in
    claire2 models              the model Claire uses (add FILE.gguf, use NAME, remove NAME)

Working in a directory runs the engine inside this process and needs no
service. The home thread lives in the service, which `claire2 home` starts in
the background if it is not already running.
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time

from . import __version__, boot, prompting, tui
from .client import Client, ServiceError
from .engine import Core, Session
from .render import Renderer
from .settings import Settings
from .tools.workshop import remove_tool
from .tui import C, c

SUBCOMMANDS = ("serve", "home", "app", "tasks", "approvals", "tools", "inbox",
               "status", "stop", "bench", "connectors", "schedules", "autostart",
               "skills", "models")


# -- shared drawing -----------------------------------------------------------

def _fmt_tokens(n):
    if n < 1000:
        return str(n)
    if n < 100_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n // 1000}k"


def ctx_bar(used, limit, width=14):
    """Compact usage meter: ▰▰▰▱▱▱ 23.4k/262k 9%."""
    pct = 0 if limit <= 0 else min(1.0, used / limit)
    filled = int(pct * width)
    # Show one block as soon as anything is used, so it never reads as empty.
    if used > 0:
        filled = max(1, filled)
    bar = "▰" * filled + "▱" * (width - filled)
    color = "green" if pct < 0.5 else ("yellow" if pct < 0.8 else "red")
    return (f"{C[color]}{bar}{C['reset']} "
            f"{C['dim']}{_fmt_tokens(used)}/{_fmt_tokens(limit)} "
            f"{pct * 100:.1f}%{C['reset']}")


def print_header(subtitle, rows):
    print(c(tui.BANNER, "pink"))
    print(f"  {c('claire', 'pink')}   {c(subtitle, 'dim')}\n")
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"  {c(label.ljust(width), 'dim')}   {value}")
    print()


def _fail(text):
    print(c(f"  {text}", "red"))
    raise SystemExit(1)


# -- working in a directory ---------------------------------------------------

def local(argv):
    ap = argparse.ArgumentParser(prog="claire2", description="Claire - local coding agent")
    ap.add_argument("task", nargs="*", help="task to run (omit for interactive)")
    ap.add_argument("--thinking", action="store_true", help="enable reasoning mode")
    ap.add_argument("--yolo", action="store_true", help="skip command approval")
    ap.add_argument("--model", help="override model id")
    ap.add_argument("--worker-profile", help="configured worker: uncensored27, standard27, or fast35")
    ap.add_argument("--skills", default=None,
                    help="skills to start with, comma-separated (default: coding; "
                         "pass an empty string for none)")
    args = ap.parse_args(argv)

    settings = Settings(model=args.model, worker_profile=args.worker_profile)
    settings.thinking = settings.thinking or args.thinking
    settings.auto_approve = settings.auto_approve or args.yolo

    problem = boot.ensure_brain(settings, say=lambda t: print(f"  {t}"))
    if problem:
        _fail(problem)
    core = Core(settings)
    renderer = Renderer()
    wanted = (settings.default_skills if args.skills is None
              else [s for s in args.skills.split(",") if s.strip()])
    session = Session(core, kind="agent", workdir=os.getcwd(),
                      sink=renderer.handle, approver=renderer.ask, skills=wanted)
    core.store.create_thread("local", " ".join(args.task)[:80] or "interactive",
                             session.workdir, thread_id=session.thread_id)

    memory = core.vault.load_memory(session.workdir)
    bits = []
    if os.path.exists(core.vault.project_note(session.workdir)):
        bits.append("project note")
    if memory.count("[["):
        bits.append(f"{memory.count('[[')} learned")
    workshop = [t.name for t in core.registry.all() if t.source == "workshop"]
    print_header("local coding agent", [
        ("model", session.model["model"]),
        ("server", session.model["api_base"]),
        ("cwd", tui.tilde(session.workdir)),
        ("memory", tui.tilde(settings.vault) + (f"  ({', '.join(bits)})" if bits else "")),
        ("sandbox", "on  (commands can write only in this folder)"
         if core.sandbox.available else c("off  (pattern guards only)", "yellow")),
        ("workshop", f"{len(workshop)} tool(s)" + (f": {', '.join(workshop)}" if workshop else "")),
        ("skills", ", ".join(session.skills) or "none loaded"),
        ("mode", " · ".join(["deep" if session.thinking else "fast",
                             "yolo" if session.auto_approve else "approve",
                             prompting.prompt_name(settings)])),
    ])

    def run(task):
        text, images = tui.extract_images(task)
        if images:
            print(f"  {c('📷', 'cyan')} {c(f'{len(images)} image(s) attached', 'dim')}")
        try:
            session.run(text, images)
        except KeyboardInterrupt:
            session.cancel.set()
            renderer.close()
            print(c("\n  interrupted", "yellow"))

    if args.task:
        run(" ".join(args.task))
        return

    tui.setup_readline()
    print(c("  Type a task, or 'exit'.  Ctrl-C interrupts a running task.", "dim"))
    print(c("  Attach images: @path/to/image.png  or  [image: path.png]", "dim"))
    print(c("  /t thinking   /y yolo   reset   exit\n", "dim"))

    def status_bar():
        mode = c("thinking", "yellow") if session.thinking else c("fast", "green")
        if session.auto_approve:
            mode += c(" · ", "dim") + c("yolo", "red")
        return (f"  {mode}{c(' · ', 'dim')}{ctx_bar(session.ctx_used, session.ctx_limit, 10)}"
                f"{c('   /t thinking  /y yolo  reset  exit', 'dim')}")

    while True:
        try:
            print(status_bar())
            task = tui.read_task(f"  {c('claire', 'pink')} {c('❯', 'pink')} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = task.lower()
        if low in ("exit", "quit", "q"):
            break
        if not task:
            continue
        if low == "/t":
            session.thinking = not session.thinking
            print(c(f"  thinking {'ON (deeper, slower)' if session.thinking else 'OFF (fast)'}",
                    "yellow" if session.thinking else "green"))
        elif low == "/y":
            session.auto_approve = not session.auto_approve
            print(c(f"  approval {'OFF - yolo mode' if session.auto_approve else 'ON'}",
                    "red" if session.auto_approve else "green"))
        elif low == "reset":
            session.reset()
            print(c("  context cleared (memory kept)", "dim"))
        elif low in ("/think", "/nothink", "/think?"):
            if low != "/think?":
                session.thinking = low == "/think"
            print(c(f"  thinking: {'on (slow, deeper)' if session.thinking else 'off (fast)'}",
                    "dim"))
        else:
            run(task)


# -- the home thread ----------------------------------------------------------

class HomeUI:
    """Draws the home conversation, and a one-line trace of task threads."""

    def __init__(self, client):
        self.client = client
        self.renderer = Renderer()
        self.q = queue.Queue()
        self.titles = {}
        self.held = []          # task lines waiting for a streaming reply to end

    def listen(self, after):
        def pump():
            try:
                for event in self.client.events(after):
                    self.q.put(event)
            except Exception:
                pass
            self.q.put({"type": "_closed", "thread": None})
        threading.Thread(target=pump, daemon=True).start()

    def _title(self, tid):
        if tid not in self.titles:
            try:
                self.titles[tid] = self.client.get(f"/threads/{tid}")["thread"]["title"]
            except ServiceError:
                self.titles[tid] = tid
        return self.titles[tid][:40]

    def _approval(self, e):
        self.renderer.close()
        if e["thread"] != "home":
            print(c(f"  ┆ {self._title(e['thread'])} is asking:", "dim"))
        allow = self.renderer.ask(e)
        try:
            self.client.post(f"/approvals/{e['id']}", {"allow": allow})
        except ServiceError:
            print(c("  (that request had already expired)", "dim"))

    def _task_line(self, e):
        """A task thread's progress, one dim line per event that matters."""
        kind, tid = e["type"], e["thread"]
        if kind == "turn_start":
            self.titles[tid] = e.get("title") or tid
            print(c(f"  ┆ task started: {self._title(tid)}  ({tid})", "dim"))
        elif kind == "tool_call":
            detail = str(e.get("detail", ""))[:60]
            print(c(f"  ┆ {self._title(tid)}: {e['name']} {detail}", "dim"))
        elif kind == "tool_fault":
            print(c(f"  ┆ {self._title(tid)}: {e['name']} is broken; working around it",
                    "yellow"))
        elif kind == "notice" and e.get("level") == "error":
            print(c(f"  ┆ {self._title(tid)}: {e.get('text', '')[:120]}", "red"))
        elif kind == "turn_end":
            print(c(f"  ┆ task {e.get('status')}: {self._title(tid)}", "dim"))

    def _aside(self, e):
        """Something from outside the home conversation: a task's progress, or
        a request for approval."""
        if e["type"] == "approval_request":
            self._approval(e)
        else:
            # Wipe the spinner's line rather than stopping it; it redraws
            # itself below on its next tick.
            with tui.IO_LOCK:
                sys.stdout.write("\r\033[K")
                self._task_line(e)

    def _handle(self, e, live):
        kind, tid = e["type"], e.get("thread")
        if kind == "approval_request" or (tid and tid != "home"):
            # Her reply is being written to the screen word by word; anything
            # printed now would land in the middle of a sentence.
            if self.renderer.answer["streaming"]:
                self.held.append(e)
            else:
                self._aside(e)
        elif not tid:
            return
        elif live:
            if kind == "turn_start" and e.get("text", "").startswith("[task report]"):
                print(c("  ┆ a task reported back", "dim"))
            self.renderer.handle(e)
            if kind in ("answer", "end") and self.held:
                held, self.held = self.held, []
                for aside in held:
                    self._aside(aside)
        elif kind == "answer":
            # Something she said while sir was not watching the stream.
            self.renderer.say(e.get("text", ""))

    def catch_up(self):
        """Show what happened while the prompt was waiting for input."""
        while True:
            try:
                e = self.q.get_nowait()
            except queue.Empty:
                return True
            if e["type"] == "_closed":
                return False
            self._handle(e, live=False)

    def follow(self, turn):
        """Draw events until the given home turn ends."""
        while True:
            try:
                e = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            except KeyboardInterrupt:
                self.renderer.close()
                print(c("\n  cancelling...", "yellow"))
                try:
                    self.client.post("/threads/home/cancel")
                except ServiceError:
                    pass
                continue
            if e["type"] == "_closed":
                self.renderer.close()
                return False
            self._handle(e, live=True)
            if e["type"] == "turn_end" and e.get("thread") == "home" and e.get("turn") == turn:
                self.renderer.close()
                return True


def home(argv):
    settings = Settings()
    client = Client(settings)
    try:
        if client.ensure_running():
            print(c("  started the service in the background", "dim"))
        status = client.get("/status")
    except ServiceError as exc:
        _fail(str(exc))

    model = status["model"]
    inbox = status.get("inbox") or []
    print_header("home", [
        ("model", model["model"] + ("" if model["live"] else c("  (starts on first message)", "dim"))),
        ("service", client.base),
        ("memory", tui.tilde(status["vault"])),
        ("sandbox", "on" if status["sandbox"] else c("off", "yellow")),
        ("inbox", f"{len(inbox)} note(s) waiting" + (
            ": " + ", ".join(n["name"] for n in inbox) if inbox else "")),
    ])
    ui = HomeUI(client)
    ui.listen(status["last_event"])

    def send(text):
        try:
            turn = client.post("/threads/home/messages", {"text": text})["turn"]
        except ServiceError as exc:
            print(c(f"  {exc}", "red"))
            return True
        return ui.follow(turn)

    if len(argv) > 1:
        send(" ".join(argv[1:]))
        return

    tui.setup_readline()
    print(c("  Talk to Claire. She starts tasks herself and reports back here.", "dim"))
    print(c("  /tasks   /inbox   reset   exit      Ctrl-C cancels her current reply\n", "dim"))
    alive = True
    while alive:
        if not ui.catch_up():
            break
        try:
            text = tui.read_task(f"  {c('you', 'blue')} {c('❯', 'blue')} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = text.lower()
        if low in ("exit", "quit", "q"):
            break
        if not text:
            continue
        if low == "/tasks":
            tasks([])
        elif low == "/inbox":
            inbox_cmd([])
        elif low == "reset":
            client.post("/threads/home/reset")
            print(c("  conversation cleared (memory kept)", "dim"))
        else:
            alive = send(text)
    if not alive:
        print(c("  lost the connection to the service", "red"))
    print(c("  The service keeps running so tasks can finish. Stop it with: claire2 stop",
            "dim"))


# -- small commands -----------------------------------------------------------

def app(argv):
    """Open the app. The link carries the token once; the page trades it for a
    cookie and drops it from the address."""
    import webbrowser
    client = Client(Settings())
    if client.ensure_running():
        print(c("  started the service in the background", "dim"))
    url = f"{client.base}/?token={client.token}"
    if "--print" in argv:
        print(url)
        return
    if "--browser" in argv:
        # A browser other than the default, e.g. --browser "Google Chrome".
        at = argv.index("--browser")
        name = argv[at + 1] if at + 1 < len(argv) else ""
        if not name or sys.platform != "darwin":
            print(c('  usage: claire2 app --browser "Google Chrome"  (macOS)', "yellow"))
            return
        command = ["/usr/bin/open", "-a", name, url]
        where = name
        if "--profile" in argv:
            # One profile of a Chromium browser, by its account email or its
            # folder name ("Profile 2"). Each profile keeps its own sign-in.
            at = argv.index("--profile")
            profile = argv[at + 1] if at + 1 < len(argv) else ""
            if not profile:
                print(c('  usage: claire2 app --browser "Google Chrome" --profile you@gmail.com', "yellow"))
                return
            switch = "--profile-email" if "@" in profile else "--profile-directory"
            command = ["/usr/bin/open", "-na", name, "--args", f"{switch}={profile}", url]
            where = f"{name} ({profile})"
        print(f"  opening {client.base} in {where}")
        if subprocess.run(command, capture_output=True).returncode != 0:
            print(c(f"  could not open {name}. Open this link in it yourself:", "yellow"))
            print(f"  {url}")
        return
    print(f"  opening {client.base}")
    if not webbrowser.open(url):
        print(c("  could not open a browser. Open this link yourself:", "yellow"))
        print(f"  {url}")


def serve(argv):
    from .service import serve as run_service
    parser = argparse.ArgumentParser(prog="claire2 serve")
    parser.add_argument("--worker-profile", help="configured Splash worker profile")
    args = parser.parse_args(argv[1:])
    settings = Settings(worker_profile=args.worker_profile)
    if Client(settings).healthy():
        # Exit cleanly: launchd restarts a service that fails, and a second
        # copy fighting over the port would restart forever.
        print("  the service is already running")
        return
    core = Core(settings)
    print(f"  claire service {__version__} on http://{core.settings.service_host}:"
          f"{core.settings.service_port}  (sandbox "
          f"{'on' if core.sandbox.available else 'OFF'})", flush=True)
    run_service(core)


def tasks(argv):
    core = Core(Settings())
    rows = core.store.threads(kind="task", limit=20)
    if not rows:
        print(c("  no tasks yet", "dim"))
    for r in rows:
        color = {"done": "green", "running": "cyan", "waiting": "yellow",
                 "error": "red", "stopped": "yellow"}.get(r["status"], "dim")
        when = time.strftime("%m-%d %H:%M", time.localtime(r["updated"]))
        print(f"  {c(r['id'], 'dim')}  {c(format(r['status'], '<11'), color)} "
              f"{c(when, 'dim')}  {r['title']}")


def approvals(argv):
    client = Client(Settings())
    if not client.healthy():
        print(c("  the service is not running, so nothing is waiting", "dim"))
        return
    pending = client.get("/approvals")["approvals"]
    if not pending:
        print(c("  nothing waiting for approval", "dim"))
    renderer = Renderer()
    for a in pending:
        print(c(f"  thread {a['thread_id']}", "dim"))
        allow = renderer.ask(a)
        try:
            client.post(f"/approvals/{a['id']}", {"allow": allow})
        except ServiceError as exc:
            print(c(f"  {exc}", "red"))


def tools(argv):
    settings = Settings()
    core = Core(settings)
    client = Client(settings)
    if len(argv) >= 3 and argv[1] in ("reset", "remove"):
        name = argv[2]
        if argv[1] == "reset":
            core.store.clear_tool(name)
            print(f"  cleared the fault record for {name}")
        else:
            ok = remove_tool(core.registry, settings, name)
            print(f"  removed {name}" if ok else c(f"  no workshop tool named {name}", "red"))
        if client.healthy():                 # keep a running service in step
            try:
                client.post(f"/tools/{name}/{argv[1]}")
            except ServiceError:
                pass
        return
    health = core.store.tool_health()
    for t in core.registry.all():
        h = health.get(t.name) or {}
        state = c("ok", "green")
        if h.get("substitute"):
            state = c(f"broken -> using {h['substitute']}", "yellow")
        elif (h.get("consecutive") or 0) >= 1:
            state = c(f"failing ({h['consecutive']} in a row)", "red")
        elif h.get("faults"):
            state = c(f"ok ({h['faults']} past fault(s))", "dim")
        source = c("workshop", "cyan") if t.source == "workshop" else c("built-in", "dim")
        print(f"  {t.name:<14} {source}  {state}")
        if h.get("last_error") and (h.get("consecutive") or h.get("substitute")):
            print(c(f"      {h['last_error'].splitlines()[0][:100]}", "dim"))
    print(c("\n  claire2 tools reset NAME    after you repair a tool"
            "\n  claire2 tools remove NAME   delete a workshop tool", "dim"))


def inbox_cmd(argv):
    vault = Core(Settings()).vault
    vault.init()
    notes = vault.inbox_open()
    if not notes:
        print(c("  inbox is empty", "dim"))
    for name, title in notes:
        print(f"  {c('⚑', 'yellow')} {title}")
        print(c(f"      {tui.tilde(os.path.join(vault.path, 'inbox', name + '.md'))}", "dim"))


def status(argv):
    client = Client(Settings())
    if not client.healthy():
        print(c("  the service is not running", "dim"))
        return
    s = client.get("/status")
    m = s["model"]
    print(f"  service   {client.base}  ({s['version']})")
    print(f"  model     {m['model']}  {'live' if m['live'] else 'not loaded'}  "
          f"ctx {m['ctx']:,}")
    print(f"  sandbox   {'on' if s['sandbox'] else 'off'}")
    print(f"  waiting   {s['pending_approvals']} approval(s), {len(s['inbox'])} inbox note(s)")


def stop(argv):
    """claire2 stop [--unload]. The model server stays loaded so the next start
    is quick; --unload stops the ones Claire started too, freeing their memory."""
    settings = Settings()
    client = Client(settings)
    if client.healthy():
        client.post("/shutdown")
        print("  service stopped")
    else:
        print(c("  the service is not running", "dim"))
    if "--unload" in argv[1:]:
        result = boot.unload_models(settings)
        print("  model servers Claire started are stopped; their memory is free")
        for problem in result["errors"]:
            print(c(f"  left running: {problem}", "yellow"))


def _service():
    client = Client(Settings())
    if client.ensure_running():
        print(c("  started the service in the background", "dim"))
    return client


def _integration_line(i):
    color = {"connected": "green", "error": "red", "connecting": "yellow"}.get(i["status"], "dim")
    who = f"  {i['account_label']}" if i["account_label"] else ""
    print(f"  {i['id']:<22} {i['provider']:<9} {c(format(i['status'], '<13'), color)}{i['name']}{who}")
    if i.get("error"):
        print(c(f"      {i['error']}", "red"))


def connectors(argv):
    """claire2 connectors [add KIND | connect ID | test ID | disconnect ID | remove ID]

    Secrets are typed here, hidden, and go straight to the keychain. They are
    never put in a prompt, a note, or the browser's storage.
    """
    import getpass
    import webbrowser
    client = _service()
    action = argv[1] if len(argv) > 1 else "list"
    target = argv[2] if len(argv) > 2 else ""

    if action == "list":
        rows = client.get("/integrations")["integrations"]
        if not rows:
            print(c("  nothing connected yet", "dim"))
        for row in rows:
            _integration_line(row)
        kinds = ", ".join(p["provider"] for p in client.get("/integrations/providers")["providers"])
        print(c(f"\n  claire2 connectors add KIND     ({kinds})"
                "\n  claire2 connectors connect|test|disconnect|remove ID", "dim"))
        return

    if action == "add":
        catalogue = {p["provider"]: p for p in client.get("/integrations/providers")["providers"]}
        if target not in catalogue:
            _fail(f"choose a kind: {', '.join(catalogue)}")
        kind = catalogue[target]
        print(f"  {c(kind['label'], 'bold')}  {c(kind['summary'], 'dim')}")
        name = input("  Name for this connector: ").strip()
        config, secrets = {}, {}
        for field in kind["fields"]:
            prompt = f"  {field['label']}{'' if field['required'] else ' (optional)'}: "
            value = (getpass.getpass(prompt) if field["secret"] else input(prompt)).strip()
            if value:
                (secrets if field["secret"] else config)[field["key"]] = value
        if target == "mcp" and config.get("transport") == "stdio":
            print(c("  Secrets the server needs, as environment variables. Blank name to finish.", "dim"))
            while True:
                key = input("  Variable name: ").strip()
                if not key:
                    break
                secrets[f"env:{key}"] = getpass.getpass(f"  Value for {key}: ")
        made = client.post("/integrations", {"provider": target, "name": name,
                                             "config": config, "secrets": secrets})["integration"]
        print(f"  saved as {c(made['id'], 'bold')}")
        action, target = "connect", made["id"]

    if action == "connect":
        result = client.post(f"/integrations/{target}/connect")
        if result.get("auth_url"):
            print("  opening your browser to sign in...")
            if not webbrowser.open(result["auth_url"]):
                print(f"  open this link to sign in:\n  {result['auth_url']}")
            deadline = time.time() + 300
            row = result["integration"]
            while time.time() < deadline and row["status"] == "connecting":
                time.sleep(2)
                row = next(i for i in client.get("/integrations")["integrations"]
                           if i["id"] == target)
            if row["status"] == "connecting":
                print(c("  still waiting for the sign-in; check with: claire2 connectors", "yellow"))
                return
        else:
            row = result["integration"]
        _integration_line(row)
    elif action in ("test", "disconnect"):
        _integration_line(client.post(f"/integrations/{target}/{action}")["integration"])
    elif action == "remove":
        client._request("DELETE", f"/integrations/{target}")
        print(f"  removed {target} and its stored credentials")
    else:
        _fail("usage: claire2 connectors [add KIND | connect ID | test ID | disconnect ID | remove ID]")


def schedules(argv):
    """claire2 schedules [pause ID | resume ID | run ID | remove ID]"""
    client = _service()
    action = argv[1] if len(argv) > 1 else "list"
    target = argv[2] if len(argv) > 2 else ""
    if action in ("pause", "resume"):
        made = client.post(f"/schedules/{target}/{action}")["schedule"]
        print(f"  {made['title']}: {made['status']}")
    elif action == "run":
        ran = client.post(f"/schedules/{target}/run")
        print(f"  started task {ran['thread_id']}" if ran["started"]
              else c("  not started: its previous run is still going", "yellow"))
    elif action == "remove":
        client._request("DELETE", f"/schedules/{target}")
        print(f"  removed {target}")
    elif action != "list":
        _fail("usage: claire2 schedules [pause ID | resume ID | run ID | remove ID]")
    if action != "list":
        return
    rows = client.get("/schedules")["schedules"]
    if not rows:
        print(c("  no schedules yet. Ask Claire to make something a habit.", "dim"))
    for r in rows:
        color = "green" if r["status"] == "active" else "dim"
        last = r["runs"][0] if r["runs"] else None
        print(f"  {r['id']}  {c(format(r['status'], '<7'), color)} {r['title']}")
        print(c(f"      {r['description']} ({r['timezone']})   next: {r['next_run_at'] or '-'}"
                f"   last: {(last['status'] + ' ' + last['started_at']) if last else 'never'}", "dim"))
    print(c("\n  claire2 schedules pause|resume|run|remove ID", "dim"))


def skills(argv):
    """claire2 skills [add FILE [NAME] | show NAME | remove NAME]"""
    client = _service()
    action = argv[1] if len(argv) > 1 else "list"
    target = argv[2] if len(argv) > 2 else ""
    if action == "add":
        try:
            with open(os.path.expanduser(target), encoding="utf-8") as f:
                raw = f.read()
        except OSError as exc:
            _fail(f"cannot read {target}: {exc}")
        made = client.post("/skills", {"filename": target, "text": raw,
                                       "name": argv[3] if len(argv) > 3 else ""})["skill"]
        print(f"  saved skill {c(made['name'], 'bold')}: {made['description']}")
    elif action == "show":
        skill = client.get(f"/skills/{target}")["skill"]
        print(c(f"  {skill['name']}  ({skill['source']})  {skill['description']}\n", "dim"))
        print(skill["text"])
    elif action == "remove":
        client._request("DELETE", f"/skills/{target}")
        print(f"  removed {target}")
    elif action == "list":
        for s in client.get("/skills")["skills"]:
            source = c("built-in", "dim") if s["source"] == "builtin" else c("yours   ", "cyan")
            print(f"  {s['name']:<18} {source}  {s['description'][:90]}")
        print(c("\n  claire2 skills add FILE.md [NAME]   show NAME   remove NAME", "dim"))
    else:
        _fail("usage: claire2 skills [add FILE [NAME] | show NAME | remove NAME]")


def models(argv):
    """claire2 models [add FILE.gguf | add-lmstudio KEY | use NAME | remove NAME]

    Works without the service: the choice is saved and takes effect when
    Claire next starts."""
    from . import models as registry
    ap = argparse.ArgumentParser(prog="claire2 models")
    ap.add_argument("action", nargs="?", default="list",
                    choices=["list", "add", "add-lmstudio", "use", "remove"])
    ap.add_argument("target", nargs="?", default="")
    ap.add_argument("--name", default="", help="what to call it")
    ap.add_argument("--context", type=int, default=0, help="context window in tokens")
    args = ap.parse_args(argv[1:])
    settings = Settings()
    try:
        if args.action in ("add", "add-lmstudio"):
            if not args.target:
                _fail("usage: claire2 models add PATH/TO/model.gguf   (or add-lmstudio MODEL_KEY)")
            made = registry.add(settings, "llama" if args.action == "add" else "lmstudio",
                                file_path=args.target if args.action == "add" else "",
                                model=args.target if args.action == "add-lmstudio" else "",
                                label=args.name, context=args.context)
            print(f"  added {c(made['name'], 'bold')}  ({made['context']:,}-token context)")
            print(c(f"  use it with: claire2 models use {made['name']}", "dim"))
            return
        if args.action == "use":
            registry.choose(settings, args.target)
            print(f"  Claire will use {c(args.target or 'config', 'bold')} from her next start.")
            if Client(settings).healthy():
                print(c("  restart her now: claire2 stop, then claire2 app", "dim"))
            return
        if args.action == "remove":
            registry.remove(settings, args.target)
            print(f"  removed {args.target}")
            return
    except registry.ModelError as exc:
        _fail(str(exc))
    view = registry.catalogue(settings, running=None)
    kinds = {"llama": "llama.cpp", "lmstudio": "LM Studio", "splash": "Splash"}
    for m in view["models"]:
        mark = c("●", "green") if m["selected"] else " "
        context = f"{m['context']:,}" if m["context"] else "model's own"
        problem = c("  " + m["problem"], "yellow") if m["problem"] else ""
        print(f"  {mark} {m['name']:<20} {kinds.get(m['server'], m['server']):<10} {context:>9}  "
              f"{m['where']}{problem}")
    print(c("\n  ● = what Claire uses from her next start\n"
            "  claire2 models add FILE.gguf [--name N] [--context N]   add-lmstudio KEY   use NAME   remove NAME", "dim"))


def launch_agent(settings):
    """The login item that keeps the service running, as a plist dict."""
    from .settings import ROOT
    import shutil
    paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    llama = shutil.which("llama-server")
    if llama and os.path.dirname(llama) not in paths:
        paths.insert(0, os.path.dirname(llama))
    log = os.path.join(settings.state_dir, "service.log")
    return {
        "Label": "com.claire.service",
        "ProgramArguments": [sys.executable, "-m", "claire_core", "serve"],
        "WorkingDirectory": ROOT,
        "EnvironmentVariables": {"PYTHONPATH": ROOT, "PATH": ":".join(paths)},
        "RunAtLoad": True,
        # Restart after a crash, but stay stopped after `claire2 stop`.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "StandardOutPath": log, "StandardErrorPath": log,
    }


def autostart(argv):
    """claire2 autostart on|off -- start the service at login, so scheduled
    jobs run without you opening Claire first. macOS only."""
    import plistlib
    import subprocess
    if sys.platform != "darwin":
        _fail("autostart uses launchd and is only available on macOS")
    settings = Settings()
    settings.ensure_state_dir()
    path = os.path.expanduser("~/Library/LaunchAgents/com.claire.service.plist")
    domain = f"gui/{os.getuid()}"
    action = argv[1] if len(argv) > 1 else "status"
    if action == "on":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump(launch_agent(settings), f)
        client = Client(settings)
        if client.healthy():                 # hand the running one over to launchd
            client.post("/shutdown")
            time.sleep(1.5)
        subprocess.run(["launchctl", "bootout", domain, path], capture_output=True)
        p = subprocess.run(["launchctl", "bootstrap", domain, path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            _fail(f"launchd would not load it: {p.stderr.strip()}")
        print(f"  on: the service now starts when you log in\n  {tui.tilde(path)}")
    elif action == "off":
        subprocess.run(["launchctl", "bootout", domain, path], capture_output=True)
        if os.path.exists(path):
            os.remove(path)
        print("  off: the service only runs when you start it")
    else:
        print(f"  autostart is {'on' if os.path.exists(path) else 'off'}"
              + c("   (claire2 autostart on|off)", "dim"))


def _report_line(r):
    rate = f"{r['passed']}/{r['total']}"
    color = "green" if r["passed"] == r["total"] else ("yellow" if r["passed"] else "red")
    partial = "" if r.get("complete", True) else c("  (partial run)", "dim")
    return (f"  {c(r['when'], 'dim')}  {r['suite']:<6} {c(format(rate, '<6'), color)} "
            f"{r['tok_s']:>6.1f} tok/s  {r['seconds']:>6.0f}s  "
            f"{r['retries']} bad repl{'y' if r['retries'] == 1 else 'ies'}  "
            f"{r['model']}{partial}")


def bench(argv):
    from . import bench as report_card
    ap = argparse.ArgumentParser(
        prog="claire2 bench",
        description="Run a model through Claire's own tasks and record how it did.")
    ap.add_argument("suite", nargs="?", default="brain", choices=["brain", "scout"])
    ap.add_argument("--api", help="test the model served at this URL (…/v1) "
                                  "instead of the one in config.py")
    ap.add_argument("--model", help="model id to send, if the server hosts several")
    ap.add_argument("--only", action="append", help="run just this task (repeatable)")
    ap.add_argument("--keep", action="store_true", help="keep the work folders")
    ap.add_argument("--history", action="store_true", help="show past results")
    args = ap.parse_args(argv[1:])

    if args.history:
        reports = report_card.history()
        if not reports:
            print(c("  no results yet. Run: claire2 bench", "dim"))
        for r in reports:
            print(_report_line(r))
        return

    def say(row):
        if "note" in row:
            print(c(f"  {row['note']}", "dim"))
            return
        mark = c("✔", "green") if row["passed"] else c("✗", "red")
        extra = f"{row['steps']} steps" + (f", {row['retries']} bad" if row["retries"] else "")
        print(f"  {mark} {row['task']:<20} {row['seconds']:>6.1f}s  {row['tok_s']:>6.1f} tok/s  "
              f"{c(format(extra, '<16'), 'dim')} {c(row['why'][:70], 'dim')}", flush=True)

    def on_start(info):
        print(f"\n  {c('report card', 'pink')}  {args.suite}  {c(info['model'], 'bold')}  "
              f"{c(info['api_base'] + '  ctx ' + format(info['ctx'], ','), 'dim')}\n",
              flush=True)

    try:
        report = report_card.run(args.suite, api=args.api, model=args.model,
                                 only=args.only, keep=args.keep, say=say,
                                 on_start=on_start)
    except RuntimeError as exc:
        _fail(str(exc))
    print()
    print(_report_line(report))
    print(c("\n  Saved. Compare runs with: claire2 bench --history", "dim"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        handler = {"inbox": inbox_cmd}.get(argv[0]) or globals()[argv[0]]
        try:
            handler(argv)
        except ServiceError as exc:
            _fail(str(exc))
        return
    local(argv)
