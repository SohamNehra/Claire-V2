"""claire_kit: what every integration Claire builds is written with.

An integration is a folder:

    integration.json   what it is: hosts it reaches, credentials it needs, and
                       each action with what it does and whether it changes
                       anything. Sir approves this before it is installed.
    server.py          the actions, written with this kit.
    test.py            proves the actions work, against recorded responses.
    fixtures/          (optional) recorded responses, as JSON files.

server.py:

    from claire_kit import action, http, secret, Fail, run

    @action("search_videos")
    def search_videos(query, count="5"):
        data = http("GET", "https://www.googleapis.com/youtube/v3/search",
                    params={"q": query, "maxResults": count, "part": "snippet",
                            "key": secret("YOUTUBE_API_KEY")}).json()
        return [item["snippet"]["title"] for item in data.get("items", [])]

    if __name__ == "__main__":
        run()

test.py:

    from claire_kit import testing, call
    import server

    with testing.fixtures({"GET https://www.googleapis.com/youtube/v3/search":
                           {"items": [{"snippet": {"title": "A video"}}]}},
                          secrets={"YOUTUBE_API_KEY": "test-key"}) as sent:
        assert call("search_videos", query="cats") == ["A video"]
        assert sent[0].params["q"] == "cats"

This file is replaced with Claire's own copy when an integration is checked
and installed, so changing it changes nothing.
"""

import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["action", "http", "secret", "Fail", "run", "call", "manifest", "testing"]

_ACTIONS = {}
_FIXTURES = None          # set by testing.fixtures(); None means the real network


class Fail(Exception):
    """The action could not be done. Its message is what Claire is shown, so
    say what went wrong and what would fix it."""


def action(name):
    """Register a function as the action `name` (declared in integration.json)."""
    def register(fn):
        _ACTIONS[name] = fn
        return fn
    return register


def _folder():
    if os.environ.get("CLAIRE_INTEGRATION_DIR"):
        return os.environ["CLAIRE_INTEGRATION_DIR"]
    main = sys.modules.get("server") or sys.modules.get("__main__")
    path = getattr(main, "__file__", None) or os.path.join(os.getcwd(), "server.py")
    return os.path.dirname(os.path.abspath(path))


_MANIFEST = None


def manifest():
    global _MANIFEST
    if _MANIFEST is None:
        with open(os.path.join(_folder(), "integration.json"), encoding="utf-8") as f:
            _MANIFEST = json.load(f)
    return _MANIFEST


def secret(key):
    """A credential sir provided, by the key integration.json declares -- a
    token, or (kind "url") the address of his own instance, without a
    trailing slash."""
    declared = [c.get("key") for c in manifest().get("credentials", [])]
    if key not in declared:
        raise Fail(f"{key} is not a credential this integration declares in integration.json")
    value = os.environ.get(key)
    if not value:
        raise Fail(f"the credential {key} has not been provided yet")
    return value.rstrip("/") if any(c.get("key") == key and c.get("kind") == "url"
                                    for c in manifest().get("credentials", [])) else value


class Response:
    def __init__(self, status, headers, text):
        self.status, self.headers, self.text = status, headers, text

    def json(self):
        try:
            return json.loads(self.text) if self.text else None
        except ValueError:
            raise Fail(f"the service sent something that is not JSON: {self.text[:200]!r}")


class Sent:
    """One request, as a test sees it."""

    def __init__(self, method, url, params, headers, body):
        self.method, self.url, self.params, self.headers, self.body = method, url, params, headers, body


class _SameHost(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed(urllib.parse.urlsplit(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _allowed(host):
    hosts = [h.lower() for h in manifest().get("hosts", [])]
    # An address sir entered for his own instance is his to allow.
    for c in manifest().get("credentials", []):
        if c.get("kind") == "url" and os.environ.get(c.get("key", "")):
            hosts.append((urllib.parse.urlsplit(os.environ[c["key"]]).hostname or "").lower())
    if host.lower() not in hosts:
        raise Fail(f"{host} is not one of this integration's hosts ({', '.join(hosts) or 'none'}); "
                   f"add it to \"hosts\" in integration.json")


def http(method, url, params=None, headers=None, json_body=None, form=None, timeout=30, allow=()):
    """One request to a host integration.json declares. Returns a Response.
    An answer of 400 or more raises Fail unless its status is in `allow`."""
    method = method.upper()
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "https" and host not in ("127.0.0.1", "localhost"):
        raise Fail(f"only https addresses can be called, not {url}")
    _allowed(host)
    query = {k: str(v) for k, v in (params or {}).items() if v is not None}
    full = url + (("&" if parts.query else "?") + urllib.parse.urlencode(query) if query else "")
    headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    headers.setdefault("User-Agent", "claire-integration/1")
    if _FIXTURES is not None:
        response = _FIXTURES.answer(method, url.split("?")[0], query, headers,
                                    json_body if json_body is not None else form)
        if response.status >= 400 and response.status not in allow:
            raise Fail(f"{host} answered HTTP {response.status}: {response.text[:400]}")
        return response
    req = urllib.request.Request(full, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_SameHost).open(req, timeout=timeout) as r:
            response = Response(r.status, dict(r.headers), r.read(5_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        response = Response(exc.code, dict(exc.headers or {}), exc.read(200_000).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as exc:
        raise Fail(f"could not reach {host}: {getattr(exc, 'reason', exc)}")
    if response.status >= 400 and response.status not in allow:
        raise Fail(f"{host} answered HTTP {response.status}: {response.text[:400]}")
    return response


def call(name, **args):
    """Run one action as Claire would: by name, with its arguments checked
    against integration.json."""
    spec = next((a for a in manifest().get("actions", []) if a.get("name") == name), None)
    if spec is None:
        raise Fail(f"no action {name!r} in integration.json")
    fn = _ACTIONS.get(name)
    if fn is None:
        raise Fail(f"integration.json declares {name!r} but server.py has no @action({name!r})")
    params = spec.get("params") or {}
    unknown = sorted(k for k in args if k not in params)
    if unknown:
        raise Fail(f"{name} does not take {', '.join(unknown)}; it takes {', '.join(params) or 'nothing'}")
    missing = [k for k in spec.get("required") or [] if args.get(k) in (None, "")]
    if missing:
        raise Fail(f"{name} needs {', '.join(missing)}")
    return fn(**args)


def _text(result):
    if result is None:
        return "(done)"
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=1, ensure_ascii=False, default=str)


def run():
    """Serve the actions over MCP on stdin/stdout. Claire starts this."""
    info = manifest()
    tools = [{"name": a["name"], "description": a.get("description", ""),
              "inputSchema": {"type": "object",
                              "properties": {k: {"type": "string", "description": v}
                                             for k, v in (a.get("params") or {}).items()},
                              "required": list(a.get("required") or [])}}
             for a in info.get("actions", [])]
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            continue                                   # a notification
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": info.get("name", "integration"),
                                     "version": str(info.get("version", 1))}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            try:
                out = call(params.get("name", ""), **(params.get("arguments") or {}))
                result = {"content": [{"type": "text", "text": _text(out)}]}
            except Fail as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                result = {"content": [{"type": "text", "text": f"the integration failed: "
                                       f"{type(exc).__name__}: {exc}"}], "isError": True}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid,
                              "error": {"code": -32601, "message": "method not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)


class _Fixtures:
    def __init__(self, answers):
        self.answers, self.sent = dict(answers), []

    def answer(self, method, url, params, headers, body):
        self.sent.append(Sent(method, url, params, headers, body))
        key = f"{method} {url}"
        if key not in self.answers:
            raise Fail(f"no recorded answer for {key}; the test's fixtures have: "
                       + ", ".join(sorted(self.answers)))
        value = self.answers[key]
        if isinstance(value, testing.Reply):
            status, body = value.status, value.body
        else:
            status, body = 200, value
        text = body if isinstance(body, str) else json.dumps(body)
        return Response(status, {}, text)


class testing:
    """For test.py: answer requests from recordings, never the network."""

    class Reply:
        def __init__(self, status, body):
            self.status, self.body = status, body

    class fixtures:
        def __init__(self, answers, secrets=None):
            self.state, self.secrets, self.saved = _Fixtures(answers), dict(secrets or {}), {}

        def __enter__(self):
            global _FIXTURES
            self.previous = _FIXTURES
            _FIXTURES = self.state
            for k, v in self.secrets.items():
                self.saved[k] = os.environ.get(k)
                os.environ[k] = v
            return self.state.sent

        def __exit__(self, *exc):
            global _FIXTURES
            _FIXTURES = self.previous
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return False

    @staticmethod
    def load(name):
        """A recorded response saved as fixtures/<name>.json."""
        with open(os.path.join(_folder(), "fixtures", name + ".json"), encoding="utf-8") as f:
            return json.load(f)
