"""A plain HTTP API, reached with a token sir provides."""

import json
import urllib.parse

from . import ConnectorError, Operation, Provider
from .http import as_json, check_url, request

WRITE_METHODS = ("post", "put", "patch", "delete")


class Api(Provider):
    id = "api"
    label = "API service"
    summary = "Any HTTPS API that takes a token. Reads only, unless you allow changes."
    fields = (("base_url", "Base address (https://...)", False, True),
              ("auth", "How the token is sent: bearer, header or none", False, False),
              ("header_name", "Header name, when auth is 'header'", False, False),
              ("allow_write", "Allow POST/PUT/PATCH/DELETE (each one still asks)", False, False),
              ("test_path", "A path to GET when testing, e.g. /me", False, False),
              ("notes", "What it is for and its useful paths, for Claire to read", False, False),
              ("token", "API token", True, False))

    def __init__(self, core):
        self.core = core

    def validate(self, config, secrets):
        base = check_url(config.get("base_url"), "base address")
        auth = str(config.get("auth") or ("bearer" if secrets.get("token") else "none")).lower()
        if auth not in ("bearer", "header", "none"):
            raise ConnectorError("auth must be bearer, header or none")
        header = str(config.get("header_name") or "").strip()
        if auth == "header" and not header.replace("-", "").isalnum():
            raise ConnectorError("give the header name the token is sent in, e.g. X-API-Key")
        if auth != "none" and not secrets.get("token"):
            raise ConnectorError("this connector needs its API token")
        test_path = str(config.get("test_path") or "").strip()
        if test_path and not test_path.startswith("/"):
            raise ConnectorError("the test path must start with /")
        clean = {"base_url": base, "auth": auth, "header_name": header,
                 "allow_write": config.get("allow_write") in (True, "true", "yes", "1"),
                 "test_path": test_path}
        kept = {"token": secrets["token"]} if auth != "none" else {}
        scopes = ["Read"] + (["Change data (asks each time)"] if clean["allow_write"] else [])
        return clean, kept, scopes

    def _headers(self, integration, keys):
        config = integration["config"]
        if config["auth"] == "none":
            return {}
        token = keys.get("token")
        if not token:
            raise ConnectorError("the token for this connector is missing; reconnect it")
        if config["auth"] == "bearer":
            return {"Authorization": f"Bearer {token}"}
        return {config["header_name"]: token}

    def _url(self, integration, path, query=None):
        """base + path, refusing anything that would leave the configured host."""
        base = integration["config"]["base_url"]
        path = str(path or "")
        if not path.startswith("/") or "://" in path or path.startswith("//") or "\\" in path:
            raise ConnectorError("path must start with a single / and stay on this service")
        if any(part == ".." for part in path.split("?")[0].split("/")):
            raise ConnectorError("path must not contain ..")
        url = base + path
        if query:
            if not isinstance(query, dict):
                raise ConnectorError("query must be a JSON object")
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query, doseq=True)
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(base).netloc:
            raise ConnectorError("that address is outside this connector's service")
        return url

    def test(self, integration, keys):
        config = integration["config"]
        url = self._url(integration, config.get("test_path") or "/")
        status, _, _ = request("GET", url, self._headers(integration, keys),
                               timeout=self.core.settings.connector_timeout)
        if status in (401, 403):
            raise ConnectorError(f"the service rejected the token (HTTP {status})")
        if status >= 500:
            raise ConnectorError(f"the service is failing (HTTP {status})")
        return urllib.parse.urlsplit(config["base_url"]).netloc, None

    def operations(self, integration):
        ops = [Operation("get", "GET a path on this service.",
                         {"path": "the path, starting with /",
                          "query": "optional JSON object of query parameters"})]
        if integration["config"].get("allow_write"):
            for method in WRITE_METHODS:
                ops.append(Operation(
                    method, f"{method.upper()} to a path on this service.",
                    {"path": "the path, starting with /",
                     "body": "optional JSON body"}, writes=True))
        return ops

    def call(self, integration, operation, args, keys):
        url = self._url(integration, args.get("path"), args.get("query"))
        status, headers, text = request(
            operation.name, url, self._headers(integration, keys),
            body=args.get("body") if operation.name != "get" else None,
            timeout=self.core.settings.connector_timeout)
        if "json" in (headers.get("Content-Type") or ""):
            try:
                text = json.dumps(as_json(text), indent=1, ensure_ascii=False)
            except ConnectorError:
                pass
        if status >= 400:
            # Said plainly, so a wrong path is not mistaken for an answer and
            # tried again in ten variations.
            raise ConnectorError(f"the service answered HTTP {status} for "
                                 f"{operation.name.upper()} {args.get('path')}: {text[:300]}")
        return f"HTTP {status}\n{text}"
