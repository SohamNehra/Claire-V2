"""MCP servers: the general way to reach a service Claire has no connector for.

Two transports. A local server is a program sir names, run inside the sandbox
with a folder of its own. By default it stops after each call; keep_alive
retains one session per connector until disconnect or service shutdown, for
stateful tools such as browsers. A remote server is an HTTPS address.

An MCP server describes its own tools, and nothing it says about them is
trusted: a tool is treated as able to change things, and asks sir first, unless
he has listed it under `auto_allow` when setting the connector up.
"""

from contextlib import contextmanager
from collections import deque
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import threading
import time

from .. import guard
from . import ConnectorError, Operation, Provider
from .http import as_json, check_url, request

PROTOCOL = "2025-06-18"
_TOOL = re.compile(r"^[\w.-]{1,64}$")
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
CLIENT = {"name": "claire", "version": "2.0"}


def _describe(schema):
    """One line about a parameter: its type, its allowed values, what it is."""
    kind = schema.get("type")
    kind = "/".join(str(k) for k in kind) if isinstance(kind, list) else str(kind or "")
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        kind = "one of " + ", ".join(json.dumps(v) for v in schema["enum"][:8])
    text = " ".join(str(schema.get("description") or "").split())
    return (f"[{kind}] " if kind else "") + text[:160]


class _Stdio:
    """A local MCP server process, spoken to over its stdin and stdout."""

    def __init__(self, argv, env, cwd, timeout):
        self.timeout = timeout
        self._id = 0
        self._lines = queue.Queue()
        try:
            self.proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, errors="replace",
                start_new_session=True)
        except OSError as exc:
            raise ConnectorError(f"could not start the server: {exc}")
        self._errors = deque(maxlen=200)
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._lines.put),
                         daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self._errors.append),
                         daemon=True).start()

    @staticmethod
    def _pump(stream, sink):
        try:
            for line in stream:
                sink(line)
        except (OSError, ValueError):
            pass
        sink(None)

    def _send(self, message):
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            raise ConnectorError("the server stopped: " + self._tail())

    def _tail(self):
        text = "".join(l for l in list(self._errors)[-12:] if l).strip()
        return text[-400:] or "(it printed nothing)"

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None):
        self._id += 1
        wanted = self._id
        self._send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConnectorError(f"the server did not answer within {self.timeout}s")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise ConnectorError(f"the server did not answer within {self.timeout}s")
            if line is None:
                raise ConnectorError("the server stopped: " + self._tail())
            try:
                message = json.loads(line)
            except ValueError:
                continue                         # a stray log line on stdout
            if not isinstance(message, dict):
                continue
            if message.get("id") == wanted and "method" not in message:
                if "error" in message:
                    raise ConnectorError(f"the server refused: "
                                         f"{str(message['error'].get('message', message['error']))[:200]}")
                return message.get("result") or {}
            if "method" in message and "id" in message:
                # The server is asking us for something (sampling, roots).
                # Claire offers none of it.
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not supported"}})

    def close(self):
        """Stop the server and everything it started."""
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=3)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    self.proc.wait(timeout=3)          # do not leave a zombie
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class _Http:
    """A remote MCP server over streamable HTTP."""

    def __init__(self, url, token, timeout):
        self.url, self.timeout = url, timeout
        self._id = 0
        self.headers = {"Accept": "application/json, text/event-stream"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _post(self, message):
        status, headers, text = request("POST", self.url, self.headers, body=message,
                                        timeout=self.timeout)
        if headers.get("Mcp-Session-Id"):
            self.headers["Mcp-Session-Id"] = headers["Mcp-Session-Id"]
        if status in (401, 403):
            raise ConnectorError(f"the server rejected the credentials (HTTP {status})")
        if status >= 400:
            raise ConnectorError(f"the server returned HTTP {status}: {text[:160]}")
        return headers, text

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method, params=None):
        self._id += 1
        headers, text = self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                                    "params": params or {}})
        if "text/event-stream" in (headers.get("Content-Type") or ""):
            messages = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    try:
                        messages.append(json.loads(line[5:].strip()))
                    except ValueError:
                        continue
        else:
            messages = [as_json(text, "the server")]
        for message in messages:
            if isinstance(message, dict) and message.get("id") == self._id:
                if "error" in message:
                    raise ConnectorError(f"the server refused: "
                                         f"{str(message['error'].get('message', message['error']))[:200]}")
                return message.get("result") or {}
        raise ConnectorError("the server did not answer the request")

    def close(self):
        if "Mcp-Session-Id" in self.headers:
            try:
                request("DELETE", self.url, self.headers, timeout=5)
            except ConnectorError:
                pass


class Mcp(Provider):
    id = "mcp"
    label = "MCP server"
    summary = ("Any Model Context Protocol server, local or remote. Every tool "
               "asks first unless you allow it.")
    fields = (("transport", "stdio (a local program) or http (a remote address)", False, True),
              ("command", "For stdio: the program and its arguments", False, False),
              ("url", "For http: the server address (https://...)", False, False),
              ("keep_alive", "Keep the server session open between calls", False, False),
              ("auto_allow", "Tools that may run without asking, comma-separated", False, False),
              ("notes", "What this server is for, for Claire to read", False, False),
              ("token", "For http: bearer token, if the server needs one", True, False))

    # True: a kept session belongs to the jobs using it and is closed when the
    # last of them ends, instead of living until disconnect.
    job_scoped = False

    def __init__(self, core):
        self.core = core
        self._sessions = {}
        self._owners = {}                  # integration id -> jobs using its session
        self._locks = {}
        self._lock = threading.Lock()
        self._closed = False

    def _session_lock(self, iid):
        with self._lock:
            return self._locks.setdefault(iid, threading.RLock())

    @contextmanager
    def _lease(self, integration, keys):
        """Serialize calls on a stateful connection; never replay a failed action."""
        iid = integration["id"]
        keep = integration["config"].get("keep_alive", False)
        with self._session_lock(iid):
            if self._closed:
                raise ConnectorError("the connection service is shutting down")
            entry = self._sessions.get(iid)
            if entry is None:
                entry = self._session(integration, keys)
                if keep:
                    self._sessions[iid] = entry
            try:
                yield entry
            except BaseException:
                self._sessions.pop(iid, None)
                entry[0].close()
                raise
            finally:
                if not keep:
                    entry[0].close()

    def disconnect(self, integration, keys):
        iid = integration["id"]
        with self._session_lock(iid):
            with self._lock:
                self._owners.pop(iid, None)
            entry = self._sessions.pop(iid, None)
            if entry:
                entry[0].close()

    def claim(self, integration, owner):
        """Note that a job is about to use this connector's session."""
        if self.job_scoped and owner is not None:
            with self._lock:
                self._owners.setdefault(integration["id"], set()).add(owner)

    def release(self, owner):
        """A job ended: close every session no other job is still using."""
        with self._lock:
            mine = [iid for iid, owners in self._owners.items() if owner in owners]
        for iid in mine:
            with self._session_lock(iid):
                with self._lock:
                    owners = self._owners.get(iid, set())
                    owners.discard(owner)
                    shared = bool(owners)
                if not shared:
                    self.disconnect({"id": iid}, None)

    def abort(self, owner):
        """A job was cancelled mid-call: kill its session's processes now,
        without waiting for the call to time out. A session another job is
        also using is left alone."""
        with self._lock:
            mine = [iid for iid, owners in self._owners.items() if owners == {owner}]
        for iid in mine:
            entry = self._sessions.get(iid)
            if entry:
                entry[0].close()

    def close(self):
        with self._lock:
            self._closed = True
            ids = list(self._locks)
        for iid in ids:
            self.disconnect({"id": iid}, None)

    def validate(self, config, secrets):
        transport = str(config.get("transport") or "").lower()
        allow = config.get("auto_allow") or []
        if isinstance(allow, str):
            allow = [a for a in re.split(r"[,\s]+", allow) if a]
        if not all(isinstance(a, str) and _TOOL.match(a) for a in allow):
            raise ConnectorError("auto_allow must be a list of tool names")
        clean = {"transport": transport, "auto_allow": sorted(set(allow)), "tools": [],
                 "keep_alive": config.get("keep_alive") is True}
        kept = {}
        if transport == "stdio":
            if not self.core.sandbox.available:
                raise ConnectorError("a local MCP server runs inside the sandbox, "
                                     "and this machine has none")
            command = config.get("command")
            if isinstance(command, str):
                try:
                    command = shlex.split(command)
                except ValueError:
                    raise ConnectorError("could not read the command")
            if (not isinstance(command, list) or not command
                    or not all(isinstance(c, str) and c for c in command)):
                raise ConnectorError("give the program to run and its arguments")
            clean["command"] = command
            for key, value in secrets.items():
                if not key.startswith("env:") or not _ENV.match(key[4:]):
                    raise ConnectorError("a local server's secrets are environment "
                                         "variables, named like env:API_KEY")
                kept[key] = str(value)
        elif transport == "http":
            clean["url"] = check_url(config.get("url"), "server address")
            if secrets.get("token"):
                kept["token"] = str(secrets["token"])
        else:
            raise ConnectorError("transport must be stdio or http")
        return clean, kept, ["Use this server's tools (each asks first unless allowed)"]

    def _open(self, integration, keys):
        config = integration["config"]
        timeout = self.core.settings.connector_timeout
        if config["transport"] == "http":
            return _Http(config["url"], keys.get("token"), timeout)
        home = os.path.join(self.core.settings.state_dir, "connectors", integration["id"])
        cache = os.path.join(home, "cache")
        os.makedirs(cache, mode=0o700, exist_ok=True)
        extra = {"HOME": home, "npm_config_cache": os.path.join(cache, "npm"), "XDG_CACHE_HOME": cache,
                 "UV_CACHE_DIR": os.path.join(cache, "uv"),
                 "PIP_CACHE_DIR": os.path.join(cache, "pip")}
        for name in config.get("secret_names", []):
            if name.startswith("env:"):
                value = keys.get(name)
                if value is not None:
                    extra[name[4:]] = value
        argv = self.core.sandbox.wrap(config["command"], write_dirs=[home])
        # clean_env drops anything secret-looking from our own environment; the
        # server's own secrets are then added back by name.
        env = guard.clean_env()
        env.update(extra)
        return _Stdio(argv, env, home, timeout)

    def _session(self, integration, keys):
        session = self._open(integration, keys)
        try:
            info = session.request("initialize", {
                "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT})
            if hasattr(session, "headers"):
                session.headers["MCP-Protocol-Version"] = info.get("protocolVersion", PROTOCOL)
            session.notify("notifications/initialized")
        except BaseException:
            session.close()
            raise
        return session, info

    def test(self, integration, keys):
        with self._lease(integration, keys) as (session, info):
            tools, cursor = [], None
            for _ in range(20):                       # paged listings
                page = session.request("tools/list", {"cursor": cursor} if cursor else {})
                tools += page.get("tools") or []
                cursor = page.get("nextCursor")
                if not cursor:
                    break
        kept = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if not _TOOL.match(name):
                continue                              # a name we could not call safely
            schema = tool.get("inputSchema") or {}
            props = schema.get("properties") or {}
            kept.append({"name": name,
                         "required": [str(r)[:60] for r in (schema.get("required") or [])[:20]
                                      if isinstance(r, str)],
                         "description": " ".join(str(tool.get("description") or "").split())[:300],
                         "params": {str(k)[:60]: _describe(v or {})
                                    for k, v in list(props.items())[:20]}})
        server = info.get("serverInfo") or {}
        label = " ".join(str(x) for x in (server.get("name"), server.get("version")) if x)
        return label[:80], {"tools": kept[:100]}

    def operations(self, integration):
        allow = set(integration["config"].get("auto_allow") or [])
        return [Operation(t["name"], t["description"] or "(no description)", t["params"],
                          writes=t["name"] not in allow, required=t.get("required") or ())
                for t in integration["config"].get("tools") or []]

    def call(self, integration, operation, args, keys):
        with self._lease(integration, keys) as (session, _):
            result = session.request("tools/call", {"name": operation.name,
                                                    "arguments": args or {}})
        parts = []
        for item in result.get("content") or []:
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get('type')=='image' and item.get('data'):
                self.core.connectors.attach(item['data'],item.get('mimeType') or 'image/png')
                parts.append('[screenshot attached for visual inspection]')
            else:
                parts.append(f"[{item.get('type', 'content')} not shown]")
        text = "\n".join(parts).strip() or "(the tool returned nothing)"
        if result.get("isError"):
            raise ConnectorError("The tool reported an error:\n" + text)
        return text
