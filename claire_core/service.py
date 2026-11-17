"""The local HTTP service: how every client reaches the hub.

Plain HTTP with JSON, and server-sent events for the live trace, so it needs
nothing outside the standard library and a browser can consume it directly.

This process can run shell commands, so who may talk to it matters:

- It binds to 127.0.0.1 only.
- Every request needs the token from <state>/token (mode 0600). Without that,
  any web page open in a browser could drive it by posting to localhost.
- The Host header must be this server, which stops DNS rebinding, and a
  request carrying an Origin from anywhere else is refused.

The app is served from here too. `claire2 app` opens it with the token in the
address once; the server swaps that for an HttpOnly, SameSite=Strict cookie and
redirects, so the token does not stay in the address bar or the history, and no
other site can make the browser send it. The page is built with no inline
script and a strict Content-Security-Policy, because what it displays includes
text a model wrote after reading the web.
"""

from contextlib import nullcontext
import hashlib
import hmac
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, bench, boot, models
from .connectors import ConnectorError
from .projects import ProjectError
from .scheduler import ScheduleError, local_timezone
from .skillset import SkillError
from .tools.workshop import remove_tool

MAX_BODY = 2 * 1024 * 1024
MAX_BRIEF = 40_000
# The Finder's folder chooser, brought to the front. Cancelling is error -128.
PICK_FOLDER = ('tell me to activate\n'
               'POSIX path of (choose folder with prompt "Choose the project folder" '
               'default location (path to home folder))')
PICK_MODEL = ('tell me to activate\n'
              'set start to (path to home folder)\n'
              'try\n'
              '  set start to (POSIX file ((POSIX path of (path to home folder)) & ".lmstudio/models") as alias)\n'
              'end try\n'
              'POSIX path of (choose file with prompt "Choose the model file (.gguf)" default location start)')
PICK_SECONDS = 300
_picking = threading.Lock()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Started detached by a restart: waits for this service to let go of its port,
# then becomes the new service.
RESTARTER = """import os, socket, sys, time
port = int(sys.argv[1])
end = time.time() + 60
while time.time() < end:
    s = socket.socket(); s.settimeout(0.5)
    try:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    if not busy:
        break
    time.sleep(0.3)
os.execv(sys.executable, [sys.executable, "-m", "claire_core", "serve"])
"""


def _pick(script):
    """Run a Finder chooser. Returns (status, body) for the app."""
    if sys.platform != "darwin":
        return 501, {"error": "the file chooser needs macOS; type the path instead"}
    if not _picking.acquire(blocking=False):
        return 409, {"error": "a chooser is already open"}
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=PICK_SECONDS)
    except subprocess.TimeoutExpired:          # run() has already closed the dialog
        return 408, {"error": "nothing was chosen in time"}
    except OSError as exc:
        return 501, {"error": f"the chooser could not open: {exc}"}
    finally:
        _picking.release()
    if done.returncode != 0:
        if "-128" in done.stderr:
            return 200, {"path": ""}                     # he cancelled
        return 500, {"error": (done.stderr.strip() or "the chooser failed")[:300]}
    return 200, {"path": done.stdout.strip()}
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"": ("index.html", "text/html; charset=utf-8"),
          "app.js": ("app.js", "text/javascript; charset=utf-8"),
          "app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def load_token(settings, create=False):
    path = settings.token_path
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        if not create:
            return ""
    settings.ensure_state_dir()
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    return token


# Shown to a browser that has no sign-in. Nothing here signs it in: only a
# link carrying the token does, and that link comes from the terminal.
SIGN_IN = """<p><b>{why}</b></p>
<p>Claire only opens in a browser that arrived through a sign-in link, so that a
website you visit cannot reach her through your browser. To sign in, run one of
these in Terminal:</p>
<ul>
<li><code>claire2 app</code> opens Claire in your default browser.</li>
<li><code>claire2 app --browser "Google Chrome"</code> opens it in a browser you name;
add <code>--profile you@gmail.com</code> for one Chrome profile.</li>
<li><code>claire2 app --print</code> prints a link to paste into this browser.</li>
</ul>
<p>Each browser signs in once and stays signed in.</p>"""


def _ui_build():
    """A fingerprint of the app's files as they are on disk now. A page that
    loaded different files reloads itself, so it never runs older code than
    the service it talks to."""
    parts = []
    for filename, _ in STATIC.values():
        try:
            st = os.stat(os.path.join(WEB, filename))
            parts.append(f"{filename}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(filename)
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claire/" + __version__

    def log_message(self, fmt, *args):       # the event trace is the log
        pass

    # -- plumbing ------------------------------------------------------------

    @property
    def hub(self):
        return self.server.hub

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    def _allowed(self, query):
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        given = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given = auth[7:].strip()
        elif "token" in query:
            given = query["token"][0]
        else:                                # the app, signed in by cookie
            given = self._cookie()
        return bool(given) and hmac.compare_digest(given, self.server.token)

    def _cookie_name(self):
        return f"claire_{self.server.server_address[1]}"

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self._cookie_name():
                return value
        return ""

    def _static(self, name, query):
        """Serve the app. Arriving with ?token= signs the browser in."""
        if name == "" and "token" in query:
            if not self._allowed(query):
                return self._page(403, SIGN_IN.replace("{why}", "That sign-in link is no longer valid."))
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie",
                             f"{self._cookie_name()}={self.server.token}; "
                             f"HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if not self._allowed(query):
            return self._page(403, SIGN_IN.replace("{why}", "This browser is not signed in to Claire."))
        filename, content_type = STATIC[name]
        with open(os.path.join(WEB, filename), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, code, text):
        body = text if text.lstrip().startswith("<") else f"<p>{text}</p>"
        data = (f"<!doctype html><meta charset=utf-8><title>Claire</title>"
                f"<style>body{{font:15px/1.6 -apple-system,sans-serif;max-width:640px;margin:48px auto;"
                f"padding:0 16px}}code{{background:#eee;padding:2px 6px;border-radius:4px}}</style>"
                f"{body}").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        if method == "GET" and parts == ["health"]:
            return self._json(200, {"ok": True, "version": __version__})
        if method == "GET" and parts == ["oauth", "callback"]:
            return self._oauth_callback(query)
        if method == "GET" and len(parts) <= 1 and (parts[0] if parts else "") in STATIC:
            try:
                return self._static(parts[0] if parts else "", query)
            except (BrokenPipeError, ConnectionResetError):
                return None
        if not self._allowed(query):
            return self._json(403, {"error": "forbidden"})
        if method in ("POST", "DELETE"):
            self.hub.core.power.touch()
        try:
            handler = getattr(self, f"_{method.lower()}_{parts[0] if parts else ''}", None)
            if handler is None:
                return self._json(404, {"error": "not found"})
            work = self.hub.core.power.activity() if method in ("POST", "DELETE") and parts != ["activity"] else nullcontext()
            with work:
                return handler(parts[1:], query)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as exc:              # never let a request kill the server
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    # -- GET -----------------------------------------------------------------

    def _context_usage(self, tid):
        # Report retained context, never sum token usage across unrelated chats.
        with self.hub._sessions_lock:
            session = self.hub._sessions.get(tid)
        if session:
            return {"used": session.est_tokens(), "limit": session.ctx_limit, "estimated": True}
        row = self.hub.store.thread(tid)
        if self.hub.settings.controller_enabled and row and row["kind"] == "home":
            session = self.hub._session(tid)
            return {"used": session.est_tokens(), "limit": session.ctx_limit, "estimated": True}
        messages = self.hub.store.load_messages(tid)
        used = int(sum(len(str(m.get('content', ''))) for m in messages)/3.6)
        return {"used": used, "limit": self.hub.core.llm.describe('brain')['ctx'], "estimated": True}

    def _post_activity(self, rest, query):
        # Browser sends real, throttled interactions, never periodic heartbeats.
        self._body()
        self.hub.core.power.touch()
        self._json(200, {"ok": True})

    def _get_status(self, rest, query):
        core = self.hub.core
        self._json(200, {
            "version": __version__,
            "platform": sys.platform,
            "power": core.power.status(),
            "model": core.llm.describe("brain", refresh=True),
            "controller": (core.llm.describe(core.settings.controller_role)
                           if core.settings.controller_enabled else None),
            "sandbox": core.sandbox.available,
            "vault": core.settings.vault,
            "home": os.path.expanduser("~"),
            "inbox": [{"name": n, "title": t} for n, t in core.vault.inbox_open()],
            "pending_approvals": len(core.store.approvals()),
            "last_event": core.store.last_event_seq(),
            "task_counts": core.store.task_counts(),
            "active_work": core.projects.active_work(),
            "human_free": core.human_free.on(),
            "model_choice": (lambda view: {"running": view["running"], "selected": view["selected"]})(
                models.catalogue(core.settings, running=models.current(core.settings))),
            "ui": _ui_build(),
            "capacity": self.hub.capacity(),
            "context": self._context_usage("home"),
        })

    def _get_threads(self, rest, query):
        store = self.hub.store
        if not rest:
            kind = (query.get("kind") or [None])[0]
            try:
                limit = max(1, min(int((query.get("limit") or ["50"])[0]), 500))
            except ValueError:
                limit = 50
            return self._json(200, {"threads": store.threads(kind=kind, limit=limit)})
        row = store.thread(rest[0])
        if not row:
            return self._json(404, {"error": "no such thread"})
        if len(rest) > 1 and rest[1] == "events":
            return self._json(200, {"events": store.thread_events(rest[0])})
        messages = [m for m in store.load_messages(rest[0]) if m["role"] != "system"]
        self._json(200, {"thread": dict(row, thinking=self.hub.thinking(rest[0]), usage=self._context_usage(rest[0])),
                         "messages": messages})

    def _get_vault(self, rest, query):
        vault = self.hub.vault
        if not rest or rest == ["MEMORY.md"]:
            self.hub.core.projects.memory_index()     # the index on disk, current
        if not rest:
            return self._json(200, {"notes": vault.tree()})
        text = vault.read_note("/".join(rest))
        if text is None:
            return self._json(404, {"error": "no such note"})
        self._json(200, {"path": "/".join(rest), "text": text})

    # -- projects ------------------------------------------------------------

    def _get_projects(self, rest, query):
        projects = self.hub.core.projects
        if not rest:
            return self._json(200, {"projects": projects.list(),
                                    "active": projects.active_work()})
        found = projects.detail(rest[0])
        if not found:
            return self._json(404, {"error": "no such project"})
        threads = self.hub.store.project_threads(rest[0])
        self._json(200, {"project": found, "threads": threads})

    def _post_projects(self, rest, query):
        """Create a project ({name, path}); or /projects/<id>/rename {name},
        /projects/<id>/brief {text} to replace its PROJECT.md."""
        hub = self.hub
        projects = hub.core.projects
        body = self._body()
        try:
            if not rest:
                raw = str(body.get("path") or "").strip()
                path = ""
                if raw:
                    path, problem = hub.resolve_workdir(raw, "project")
                    if problem:
                        return self._json(400, {"error": problem.replace("workdir", "folder")})
                made = projects.create(str(body.get("name") or ""), path)
                hub.projects_changed()
                return self._json(200, {"project": projects.detail(made["id"])})
            row = projects.get(rest[0])
            if not row:
                return self._json(404, {"error": "no such project"})
            action = rest[1] if len(rest) > 1 else ""
            if action == "rename":
                projects.rename(row["id"], str(body.get("name") or ""))
            elif action == "brief":
                text = str(body.get("text") or "")
                if len(text) > MAX_BRIEF:
                    return self._json(400, {"error": f"PROJECT.md is limited to {MAX_BRIEF:,} characters"})
                hub.vault.write_project_brief(row["slug"], text)
                projects.touch(row["id"])
            else:
                return self._json(404, {"error": "not found"})
        except ProjectError as exc:
            return self._json(400, {"error": str(exc)})
        hub.projects_changed()
        hub.refresh_sessions("projects_changed")
        self._json(200, {"project": projects.detail(rest[0])})

    def _get_mode(self, rest, query):
        self._json(200, {"human_free": self.hub.core.human_free.on()})

    def _post_mode(self, rest, query):
        """Switch human-free mode. Only sir can: no tool reaches this."""
        body = self._body()
        if not isinstance(body.get("human_free"), bool):
            return self._json(400, {"error": "human_free must be true or false"})
        on = self.hub.core.human_free.set(body["human_free"])
        self.hub.publish({"type": "mode_changed", "thread": "home", "ts": time.time(), "human_free": on})
        self._json(200, {"human_free": on})

    def _post_folders(self, rest, query):
        """/folders/pick: let sir choose a folder in the Finder's own dialog.
        Only the app can ask, and only what he picks comes back. One dialog
        at a time; the dialog is closed if he leaves it open too long."""
        if rest != ["pick"]:
            return self._json(404, {"error": "not found"})
        code, body = _pick(PICK_FOLDER)
        if code != 200 or not body["path"]:
            return self._json(code, body)
        path, problem = self.hub.resolve_workdir(body["path"], "project")
        if problem:
            return self._json(400, {"error": problem.replace("workdir", "folder")})
        self._json(200, {"path": path})

    # -- models --------------------------------------------------------------

    def _models_view(self):
        settings = self.hub.settings
        view = models.catalogue(settings, running=models.current(settings))
        view["llama_server"] = bool(boot.shutil.which("llama-server"))
        return view

    def _get_models(self, rest, query):
        if rest == ["lmstudio"]:
            library = models.lmstudio_library(self.hub.settings)
            return self._json(200, {"running": library is not None, "models": library or []})
        self._json(200, self._models_view())

    def _post_models(self, rest, query):
        """Add a model ({server, path|model, label, context}); or
        /models/inspect {path}, /models/browse, /models/use {name},
        /models/restart."""
        settings = self.hub.settings
        body = self._body()
        try:
            if not rest:
                made = models.add(settings, str(body.get("server") or ""), str(body.get("path") or ""),
                                  str(body.get("model") or ""), str(body.get("label") or ""),
                                  int(body.get("context") or 0))
                return self._json(200, {"model": made, **self._models_view()})
            if rest == ["inspect"] or rest == ["browse"]:
                if rest == ["browse"]:
                    code, picked = _pick(PICK_MODEL)
                    if code != 200 or not picked["path"]:
                        return self._json(code, picked)
                    target = picked["path"]
                else:
                    target = os.path.realpath(os.path.expanduser(str(body.get("path") or "")))
                info = models.gguf_info(target)
                info.update(path=target, suggested_context=min(info["context_length"] or models.DEFAULT_CONTEXT,
                                                             models.DEFAULT_CONTEXT))
                return self._json(200, info)
            if rest == ["use"]:
                models.choose(settings, str(body.get("name") or ""))
                return self._json(200, self._models_view())
            if rest == ["restart"]:
                busy = [t["title"] for t in self.hub.store.threads(limit=500)
                        if t["status"] in ("queued", "running", "waiting")]
                if busy:
                    return self._json(409, {"error": "Claire is still working on: " + ", ".join(busy[:3])
                                            + ". Restart when she is done."})
                self._json(200, {"restarting": True})
                threading.Thread(target=self.server.restart, daemon=True).start()
                return None
        except (models.ModelError, ValueError) as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    def _delete_models(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        try:
            models.remove(self.hub.settings, rest[0])
        except models.ModelError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(200, self._models_view())

    def _delete_projects(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        if not self.hub.core.projects.remove(rest[0]):
            return self._json(404, {"error": "no such project"})
        self.hub.projects_changed()
        self.hub.refresh_sessions("projects_changed")
        self._json(200, {"ok": True})

    # -- integrations --------------------------------------------------------

    def _oauth_callback(self, query):
        """Where a provider sends the browser back after sign-in.

        The browser arrives from another site, so it carries no cookie; the
        one-time `state` value issued when the sign-in began is what proves
        this request belongs to it.
        """
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self._page(403, "Forbidden.")
        first = lambda key: (query.get(key) or [""])[0]
        ok, message = self.hub.core.connectors.oauth_callback(
            first("state"), first("code"), first("error"))
        import html
        self._page(200 if ok else 400, html.escape(message))

    def _get_integrations(self, rest, query):
        connectors = self.hub.core.connectors
        if rest == ["providers"]:
            port = self.server.server_address[1]
            return self._json(200, {"providers": connectors.catalogue(),
                                    "redirect_uri": f"http://127.0.0.1:{port}/oauth/callback"})
        self._json(200, {"integrations": connectors.list(),
                         "installed": connectors.installed_unconnected()})

    def _post_integrations(self, rest, query):
        """Create a connector, or /integrations/<id>/connect|test|disconnect."""
        connectors = self.hub.core.connectors
        body = self._body()
        try:
            if not rest:
                made = connectors.create(
                    str(body.get("provider") or ""), str(body.get("name") or ""),
                    body.get("config") if isinstance(body.get("config"), dict) else {},
                    body.get("secrets") if isinstance(body.get("secrets"), dict) else {})
                return self._json(200, {"integration": made})
            action = rest[1] if len(rest) > 1 else ""
            if action == "connect":
                port = self.server.server_address[1]
                return self._json(200, connectors.connect(rest[0], f"http://127.0.0.1:{port}"))
            if action == "test":
                return self._json(200, {"integration": connectors.test(rest[0])})
            if action == "disconnect":
                return self._json(200, {"integration": connectors.disconnect(rest[0])})
            if action == "credentials":
                values = body.get("credentials") if isinstance(body.get("credentials"), dict) else {}
                return self._json(200, {"integration": connectors.set_credentials(rest[0], values)})
        except ConnectorError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    def _delete_integrations(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        try:
            self.hub.core.connectors.disconnect(rest[0], delete=True)
        except ConnectorError as exc:
            return self._json(404, {"error": str(exc)})
        self._json(200, {"ok": True})

    # -- skills --------------------------------------------------------------

    def _get_skills(self, rest, query):
        skills = self.hub.core.skills
        if not rest:
            return self._json(200, {"skills": skills.list()})
        skill = skills.get(rest[0])
        if not skill:
            return self._json(404, {"error": "no such skill"})
        self._json(200, {"skill": skill})

    def _post_skills(self, rest, query):
        """Create or replace one of your skills. Send {name, description, text},
        or {filename, text} with a file's contents to take both from its header."""
        body = self._body()
        skills = self.hub.core.skills
        try:
            if body.get("filename") is not None:
                made = skills.save_upload(str(body.get("filename") or ""),
                                          str(body.get("text") or ""),
                                          str(body.get("name") or ""),
                                          str(body.get("description") or ""),
                                          body.get("always") if isinstance(body.get("always"), bool) else None)
            else:
                made = skills.save(str(body.get("name") or ""),
                                   str(body.get("description") or ""),
                                   str(body.get("text") or ""),
                                   always=body.get("always") if isinstance(body.get("always"), bool) else None,
                                   **{k: body[k] for k in ("when", "requires", "kinds", "steps") if k in body})
        except SkillError as exc:
            return self._json(400, {"error": str(exc)})
        self.hub.refresh_sessions("skills_changed")
        self._json(200, {"skill": made})

    def _delete_skills(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        skills = self.hub.core.skills
        existing = skills.get(rest[0])
        if not existing:
            return self._json(404, {"error": "no such skill"})
        if not skills.delete(rest[0]):
            return self._json(400, {"error": "built-in skills cannot be removed; save "
                                             "one of your own with the same name to replace it"})
        self.hub.refresh_sessions("skills_changed")
        self._json(200, {"ok": True})

    def _backend(self, role):
        """Which kind of server a role is configured to run on."""
        entry = self.hub.core.settings.roles.get(role) or {}
        if entry.get("server"):
            return entry["server"]
        if entry.get("gguf") and entry.get("port"):
            return "llama"                   # its own llama-server
        return self.hub.core.settings.brain_server

    def _get_config(self, rest, query):
        """What is actually in effect right now, for the controller page."""
        core = self.hub.core
        s = core.settings
        roles = {}
        for role in dict.fromkeys(["brain"] + list(s.roles)):
            info = core.llm.describe(role, refresh=True)
            roles[role] = {"model": info["model"], "api_base": info["api_base"],
                           "context": info["ctx"], "live": info["live"],
                           "own_model": role == "brain" or core.llm.has_own_model(role),
                           "backend": info.get("backend") or self._backend(role)}
        self._json(200, {
            "roles": roles, "worker_profile": s.worker_profile,
            "worker_profiles": {name: {"model": entry["model"], "server": entry["server"]}
                                for name, entry in s.worker_profiles.items()},
            "task_workers": s.task_workers, "brain_slots": s.brain_slots,
            "scout_parallel": s.scout_parallel, "sandbox": core.sandbox.available,
            "max_steps": s.max_steps, "timezone": local_timezone(),
            "note": "Read-only. Roles and limits are set in config.py and take "
                    "effect when the service restarts."})

    def _get_schedules(self, rest, query):
        self._json(200, {"schedules": self.hub.scheduler.list(),
                         "timezone": local_timezone()})

    def _post_schedules(self, rest, query):
        """Create a schedule, or act on one: /schedules/<id>/pause|resume|run.

        A schedule created here is saved paused unless the request says
        `"active": true`: saving and switching on are separate steps.
        """
        scheduler = self.hub.scheduler
        body = self._body()
        try:
            if not rest:
                made = scheduler.create(
                    str(body.get("title") or ""), str(body.get("task") or ""),
                    str(body.get("cron") or ""), str(body.get("timezone") or ""),
                    str(body.get("workdir") or ""), active=body.get("active") is True)
                return self._json(200, {"schedule": made})
            action = rest[1] if len(rest) > 1 else ""
            if action in ("pause", "resume"):
                made = scheduler.set_active(rest[0], action == "resume")
                if not made:
                    return self._json(404, {"error": "no such schedule"})
                return self._json(200, {"schedule": made})
            if action == "run":
                if not self.hub.store.schedule(rest[0]):
                    return self._json(404, {"error": "no such schedule"})
                thread = scheduler.run_now(rest[0])
                return self._json(200, {"thread_id": thread, "started": bool(thread)})
        except ScheduleError as exc:
            return self._json(400, {"error": str(exc)})
        self._json(404, {"error": "not found"})

    def _delete_schedules(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        ok = self.hub.scheduler.delete(rest[0])
        self._json(200 if ok else 404, {"ok": ok})

    def _get_bench(self, rest, query):
        reports = [{k: v for k, v in r.items()} for r in bench.history()]
        self._json(200, {"reports": reports})

    def _get_approvals(self, rest, query):
        self._json(200, {"approvals": self.hub.store.approvals()})

    def _get_tools(self, rest, query):
        core = self.hub.core
        health = core.store.tool_health()
        self._json(200, {"tools": [
            {"name": t.name, "source": t.source, "kinds": list(t.kinds),
             "health": health.get(t.name)} for t in core.registry.all()]})

    def _get_events(self, rest, query):
        """Server-sent events: stored events after `after`, then live ones."""
        try:
            # A browser reconnecting says where it got to; pick up from there.
            after = int(self.headers.get("Last-Event-ID")
                        or (query.get("after") or ["0"])[0])
        except ValueError:
            after = 0
        only = (query.get("thread") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(event):
            if only and event.get("thread") != only:
                return
            head = f"id: {event['seq']}\n" if event.get("seq") else ""
            self.wfile.write(f"{head}data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        # Subscribe before replaying, so nothing published in between is lost;
        # the live loop then skips whatever the replay already sent.
        q = self.hub.subscribe()
        try:
            last = after
            for event in self.hub.store.events_after(after, thread=only):
                send(event)
                last = event["seq"]
            while not self.server.stopping.is_set():
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event.get("seq") and event["seq"] <= last:
                    continue
                send(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    # -- POST ----------------------------------------------------------------

    def _post_threads(self, rest, query):
        body = self._body()
        hub = self.hub
        project = str(body.get("project") or "")
        owner = hub.store.project(project) if project else None
        if project and not owner:
            return self._json(400, {"error": "no such project"})
        if not rest and body.get("kind") == "chat":      # another chat with Claire
            return self._json(200, {"thread": hub.new_chat(project=project or None)})
        if not rest:                          # start a task directly
            task = str(body.get("task") or "").strip()
            if not task:
                return self._json(400, {"error": "task is required"})
            title = str(body.get("title") or " ".join(task.split()[:8]))
            raw = str(body.get("workdir") or "")
            if not raw.strip() and owner and owner["path"]:
                raw = owner["path"]           # a project's task works in its folder
            workdir, problem = hub.resolve_workdir(raw, title)
            if problem:
                return self._json(400, {"error": problem})
            wanted = body.get("skills") if isinstance(body.get("skills"), list) else []
            unknown = [str(s) for s in wanted if not hub.core.skills.get(str(s))]
            if unknown:
                return self._json(400, {"error": f"no skill named {', '.join(unknown)}"})
            # A task started inside a project with no folder of its own (or a
            # scratch folder) is filed there; one aimed at another project's
            # folder goes with that folder.
            filed = hub.store.project_for_path(os.path.realpath(workdir)) or owner
            return self._json(200, {"thread": hub.spawn_task(
                title, task, workdir, skills=[str(s) for s in wanted],
                project=filed["id"] if filed else None)})
        tid, action = rest[0], (rest[1] if len(rest) > 1 else "")
        if not hub.store.thread(tid):
            return self._json(404, {"error": "no such thread"})
        if action == "messages":
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text is required"})
            return self._json(200, {"turn": hub.post(tid, text)})
        if action == "cancel":
            return self._json(200, {"cancelled": hub.cancel(tid)})
        if action == "reset":
            problem = hub.reset(tid)
            if problem:
                return self._json(409, {"error": problem})
            return self._json(200, {"ok": True})
        if action == "mode":
            hub.set_thinking(tid, bool(body.get("thinking")))
            return self._json(200, {"thinking": hub.thinking(tid)})
        if action == "project":
            problem = hub.move_chat(tid, project)
            if problem:
                return self._json(409 if "still replying" in problem else 400, {"error": problem})
            return self._json(200, {"thread": hub.store.thread(tid)})
        self._json(404, {"error": "not found"})

    def _delete_threads(self, rest, query):
        if len(rest) != 1:
            return self._json(404, {"error": "not found"})
        problem = self.hub.delete_chat(rest[0])
        if problem == "no such chat":
            return self._json(404, {"error": problem})
        if problem:
            return self._json(409, {"error": problem})
        self._json(200, {"ok": True})

    def _post_approvals(self, rest, query):
        if not rest:
            return self._json(404, {"error": "not found"})
        ok = self.hub.resolve_approval(rest[0], bool(self._body().get("allow")))
        self._json(200 if ok else 409, {"ok": ok})

    def _post_tools(self, rest, query):
        """/tools/<name>/reset clears a fault record; /remove deletes a
        workshop tool."""
        if len(rest) != 2:
            return self._json(404, {"error": "not found"})
        core = self.hub.core
        if rest[1] == "reset":
            core.store.clear_tool(rest[0])
            return self._json(200, {"ok": True})
        if rest[1] == "remove":
            return self._json(200, {"ok": remove_tool(core.registry, core.settings, rest[0])})
        self._json(404, {"error": "not found"})

    def _post_shutdown(self, rest, query):
        self._json(200, {"ok": True})
        threading.Thread(target=self.server.shutdown_all, daemon=True).start()


class Service(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, hub):
        settings = hub.settings
        super().__init__((settings.service_host, settings.service_port), _Handler)
        self.hub = hub
        self.token = load_token(settings, create=True)
        self.stopping = threading.Event()

    def shutdown_all(self):
        self.stopping.set()
        self.hub.stop()
        self.shutdown()

    def restart(self):
        """Become a fresh service, so a new model choice takes effect. The
        model this one started is unloaded first, freeing its memory."""
        settings = self.hub.settings
        try:
            result = boot.unload_models(settings)
            if result["errors"]:
                print("  restart: " + "; ".join(result["errors"]), flush=True)
        except Exception as exc:                 # the restart matters more
            print(f"  restart: models not unloaded: {exc}", flush=True)
        env = dict(os.environ, PYTHONPATH=ROOT)
        env.pop("CLAIRE_WORKER_PROFILE", None)   # the app's choice decides now
        settings.ensure_state_dir()
        with open(os.path.join(settings.state_dir, "service.log"), "ab") as out:
            subprocess.Popen([sys.executable, "-c", RESTARTER, str(self.server_address[1])],
                             cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        self.shutdown_all()


def serve(core):
    """Run the service until it is told to stop."""
    from .hub import Hub
    hub = Hub(core).start()
    service = Service(hub)
    try:
        service.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        service.stopping.set()
        hub.stop()
        service.server_close()
        time.sleep(0.1)
