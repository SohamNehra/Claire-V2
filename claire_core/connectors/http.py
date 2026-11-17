"""HTTP for connectors: strict about where a credential may travel."""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import ConnectorError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def check_url(url, what="address"):
    """A connector address must be HTTPS, or HTTP on this machine, and carry
    no credentials, query string or fragment of its own."""
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
        host = parts.hostname or ""
        parts.port                                   # raises on a bad port
    except ValueError:
        raise ConnectorError(f"the {what} is not a valid URL")
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        raise ConnectorError(f"the {what} must use https (plain http is only "
                             f"allowed for this machine)")
    if parts.scheme not in ("http", "https") or not host:
        raise ConnectorError(f"the {what} must be an https:// URL")
    if parts.username or parts.password:
        raise ConnectorError(f"the {what} must not contain a username or password")
    if parts.query or parts.fragment:
        raise ConnectorError(f"the {what} must not contain a query string or fragment")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to wherever the server
    points, so it is refused rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectorError(f"the service tried to redirect to {newurl[:80]}; "
                             f"refused, so the credential stays where it belongs")


_opener = urllib.request.build_opener(_NoRedirect)


def request(method, url, headers=None, body=None, form=None, timeout=30,
            max_bytes=2_000_000):
    """One request. Returns (status, headers, text). Never raises for an HTTP
    error status; raises ConnectorError when the service cannot be reached."""
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", "claire-connector/2.0")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read(max_bytes).decode("utf-8", "replace")
    except ConnectorError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConnectorError(f"could not reach the service: {reason}")


def as_json(text, what="the service"):
    try:
        return json.loads(text)
    except ValueError:
        raise ConnectorError(f"{what} sent a reply that is not JSON: {text[:120]!r}")
