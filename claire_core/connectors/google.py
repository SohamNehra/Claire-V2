"""Gmail and Google Calendar, read-only, signed in with OAuth.

Sir signs in once in his browser; Google hands this service a refresh token,
which goes straight into the keychain. The scopes asked for are the read-only
ones, so even a stolen token could not send mail or change an event. Sending
is deliberately not here yet.

Google requires an OAuth client of your own (a "Desktop app" client from the
Google Cloud console); its id and secret are part of setting the connector up.
One client serves every Google connection: a second one reuses the first's.

`GoogleAccount` is the one to set up: a single sign-in covering the Google
services sir picks. `Gmail` and `Calendar` remain for connections made before it.
"""

import base64
import datetime
import hashlib
import re
import secrets as _secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import ConnectorError, Operation, Provider
from .http import as_json, check_url, request

DEFAULTS = {
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_url": "https://oauth2.googleapis.com/token",
    "revoke_url": "https://oauth2.googleapis.com/revoke",
    "api_base": "https://www.googleapis.com",
}
_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,200}$")
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


class _Stay(urllib.request.HTTPRedirectHandler):
    """Report a redirect instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


API_NAMES = {"/gmail/": ("Gmail API", "gmail.googleapis.com"),
             "/calendar/": ("Google Calendar API", "calendar-json.googleapis.com")}


def refusal(path, text):
    """Why Google answered 403, in words sir can act on."""
    api, service = next((v for k, v in API_NAMES.items() if path.startswith(k)), ("API", ""))
    try:
        error = (as_json(text) or {}).get("error") or {}
    except ConnectorError:
        error = {}
    message = " ".join(str(error.get("message") or "").split())
    reasons, link = set(), ""
    for detail in error.get("details") or []:
        if isinstance(detail, dict):
            reasons.add(str(detail.get("reason") or ""))
            meta = detail.get("metadata") or {}
            link = link or str(meta.get("activationUrl") or "")
    for item in error.get("errors") or []:
        if isinstance(item, dict):
            reasons.add(str(item.get("reason") or ""))
    if "SERVICE_DISABLED" in reasons or "accessNotConfigured" in reasons or "has not been used" in message:
        where = link if link.startswith("https://console.developers.google.com/") or \
            link.startswith("https://console.cloud.google.com/") else \
            f"Google Cloud Console → APIs & Services → Library → {api}"
        return (f"the {api} is not enabled in your Google Cloud project. Enable it here: {where} "
                f"-- then wait a minute and press Test connection.")
    if "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons or "insufficientPermissions" in reasons \
            or "insufficient authentication scopes" in message:
        return (f"Google's consent screen was not given access to the {api.replace(' API', '')}. "
                f"Press Sign in with Google again and tick every box Google shows.")
    return f"Google refused this request (HTTP 403): {message[:240] or 'no reason given'}"


def auth_problem(location, redirect):
    """What is wrong, if Google answered a sign-in request by sending the
    browser to its error page. None when it went on to the sign-in page."""
    parts = urllib.parse.urlsplit(location or "")
    if "/oauth/error" not in parts.path:
        return None
    raw = urllib.parse.parse_qs(parts.query).get("authError", [""])[0]
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except ValueError:
        text = b""
    text = re.sub(rb"[^\x20-\x7e]+", b" ", text).decode().strip()
    if "redirect_uri_mismatch" in text:
        return ("Google refused Claire's sign-in address for this OAuth client. That happens when "
                "the client is a \"Web application\" client: those accept only addresses registered "
                "on them. Fix it one of two ways in Google Cloud Console → APIs & Services → "
                "Credentials: (recommended) create an OAuth client of type \"Desktop app\" and use "
                "its client id and secret here -- a Desktop client needs no address registered; or "
                "open your current client and add this exact address under \"Authorized redirect "
                f"URIs\": {redirect}")
    if "invalid_client" in text or "deleted_client" in text:
        return ("Google does not recognise this OAuth client id. It may have been deleted, or "
                "copied incompletely; copy it again from Google Cloud Console → Credentials.")
    if "access_denied" in text or "org_internal" in text or "admin_policy" in text:
        return ("Google will not let this account use this OAuth client. On the OAuth consent "
                "screen, add your Google account under Test users (or make the app available to "
                "external users).")
    return "Google refused the sign-in request: " + (text[:240] or "no reason given")


def _b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Google(Provider):
    oauth = True
    google_client = True          # shares one OAuth client with every Google connection
    scope_urls = ()
    scope_labels = ()
    fields = (("client_id", "OAuth client id (Desktop app)", False, False),
              ("client_secret", "OAuth client secret", True, False))

    def __init__(self, core):
        self.core = core
        self._tokens = {}                      # integration id -> (token, expires)
        self._lock = threading.Lock()

    # -- setup ---------------------------------------------------------------

    def validate(self, config, secrets):
        client_id = str(config.get("client_id") or "").strip()
        if not client_id or len(client_id) > 300 or " " in client_id:
            raise ConnectorError("give the OAuth client id from your Google Cloud project "
                                 "(Credentials → OAuth client of type Desktop app)")
        clean = {"client_id": client_id}
        for key, default in DEFAULTS.items():
            clean[key] = check_url(config.get(key) or default, key.replace("_", " "))
        kept = {}
        if secrets.get("client_secret"):
            kept["client_secret"] = str(secrets["client_secret"])
        return clean, kept, list(self.scope_labels)

    # -- oauth ---------------------------------------------------------------

    def scopes_for(self, integration):
        return self.scope_urls

    def preflight(self, integration, url, redirect):
        """Ask Google about the sign-in request before sending sir to it, so a
        misconfigured client is explained here instead of on Google's error
        page. Returns what is wrong, or None. Unknown answers let it through."""
        if urllib.parse.urlsplit(integration["config"]["auth_url"]).netloc != "accounts.google.com":
            return None
        try:
            urllib.request.build_opener(_Stay).open(url, timeout=10)
            return None
        except urllib.error.HTTPError as exc:
            return auth_problem(exc.headers.get("Location", ""), redirect)
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def authorize_url(self, integration, state, redirect):
        """Where to send sir to sign in. Returns (url, pkce verifier)."""
        config = integration["config"]
        verifier = _secrets.token_urlsafe(48)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        query = urllib.parse.urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect,
            "response_type": "code", "scope": " ".join(self.scopes_for(integration)),
            "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline",
            "prompt": "consent"})
        return f"{config['auth_url']}?{query}", verifier

    def _token_request(self, integration, keys, form):
        config = integration["config"]
        form = dict(form, client_id=config["client_id"])
        secret = keys.get("client_secret")
        if secret:
            form["client_secret"] = secret
        status, _, text = request("POST", config["token_url"], form=form,
                                  timeout=self.core.settings.connector_timeout)
        data = as_json(text, "the sign-in service")
        if status != 200 or "access_token" not in data:
            why = data.get("error_description") or data.get("error") or f"HTTP {status}"
            if data.get("error") == "invalid_grant":
                raise ConnectorError("the sign-in has expired or was revoked; connect again")
            raise ConnectorError(f"sign-in failed: {str(why)[:160]}")
        with self._lock:
            self._tokens[integration["id"]] = (
                data["access_token"], time.time() + int(data.get("expires_in", 3600)) - 60)
        return data

    def exchange_code(self, integration, code, verifier, redirect, keys):
        data = self._token_request(integration, keys, {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect, "code_verifier": verifier})
        if not data.get("refresh_token"):
            raise ConnectorError("the provider did not return a refresh token; "
                                 "remove Claire's access in your Google account and connect again")
        keys.set("refresh_token", data["refresh_token"])

    def _access_token(self, integration, keys, force=False):
        with self._lock:
            cached = self._tokens.get(integration["id"])
        if cached and cached[1] > time.time() and not force:
            return cached[0]
        refresh = keys.get("refresh_token")
        if not refresh:
            raise ConnectorError("not signed in; connect this account first")
        return self._token_request(integration, keys, {
            "grant_type": "refresh_token", "refresh_token": refresh})["access_token"]

    def disconnect(self, integration, keys):
        refresh = keys.get("refresh_token")
        with self._lock:
            self._tokens.pop(integration["id"], None)
        if refresh:
            request("POST", integration["config"]["revoke_url"], form={"token": refresh},
                    timeout=self.core.settings.connector_timeout)

    # -- calling the API -----------------------------------------------------

    def _get(self, integration, keys, path, query=None):
        url = integration["config"]["api_base"] + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        for attempt in (0, 1):
            token = self._access_token(integration, keys, force=attempt == 1)
            status, _, text = request("GET", url, {"Authorization": f"Bearer {token}"},
                                      timeout=self.core.settings.connector_timeout)
            if status != 401:
                break
        if status == 403:
            raise ConnectorError(refusal(path, text))
        if status == 404:
            raise ConnectorError("not found (HTTP 404)")
        if status != 200:
            raise ConnectorError(f"the service returned HTTP {status}: {text[:160]}")
        return as_json(text)


class _GmailApi:
    """Mail, read-only. Mixed into a Google provider."""

    def _gmail_test(self, integration, keys):
        return self._get(integration, keys, "/gmail/v1/users/me/profile").get("emailAddress", "")

    def _gmail_ops(self):
        return [
            Operation("search", "Find messages. Returns id, sender, subject, date and a snippet for each.",
                      {"query": "a Gmail search, e.g. `is:unread newer_than:1d` or `from:sam invoice`",
                       "max": "how many to return, 1-25 (default 10)"}),
            Operation("read", "Read one message in full.",
                      {"id": "the message id from search"}, required=("id",)),
        ]

    def _gmail_call(self, integration, name, args, keys):
        if name == "search":
            return self._search(integration, keys, args)
        return self._read(integration, keys, args)

    @staticmethod
    def _headers(message):
        return {h.get("name", "").lower(): h.get("value", "")
                for h in (message.get("payload") or {}).get("headers", [])}

    def _search(self, integration, keys, args):
        try:
            limit = max(1, min(int(args.get("max") or 10), 25))
        except (TypeError, ValueError):
            limit = 10
        query = str(args.get("query") or "").strip()
        found = self._get(integration, keys, "/gmail/v1/users/me/messages",
                          {"q": query, "maxResults": limit})
        ids = [m["id"] for m in found.get("messages", [])][:limit]
        if not ids:
            return f"No messages match {query!r}."
        out = [f"{len(ids)} message(s) for {query!r}:"]
        for mid in ids:
            message = self._get(integration, keys, f"/gmail/v1/users/me/messages/{mid}",
                                {"format": "metadata",
                                 "metadataHeaders": ["From", "Subject", "Date"]})
            h = self._headers(message)
            unread = " [unread]" if "UNREAD" in message.get("labelIds", []) else ""
            out.append(f"\nid: {mid}{unread}\nfrom: {h.get('from', '?')}\n"
                       f"date: {h.get('date', '?')}\nsubject: {h.get('subject', '(none)')}\n"
                       f"snippet: {message.get('snippet', '')[:240]}")
        return "\n".join(out)

    @classmethod
    def _body(cls, part):
        """The readable text of a message part, preferring plain text."""
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime.startswith("text/"):
            text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            if mime == "text/html":
                text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
                text = re.sub(r"(?s)<[^>]+>", " ", text)
                text = re.sub(r"[ \t]+", " ", text)
            return mime, text
        best = ("", "")
        for sub in part.get("parts", []):
            found = cls._body(sub)
            if found[1] and (found[0] == "text/plain" or not best[1]):
                best = found
                if found[0] == "text/plain":
                    break
        return best

    def _read(self, integration, keys, args):
        mid = str(args.get("id") or "").strip()
        if not _ID.match(mid):
            raise ConnectorError("give the message id exactly as search returned it")
        message = self._get(integration, keys, f"/gmail/v1/users/me/messages/{mid}",
                            {"format": "full"})
        h = self._headers(message)
        _, body = self._body(message.get("payload") or {})
        body = re.sub(r"\n\s*\n+", "\n\n", body).strip() or "(no readable text in this message)"
        if len(body) > 20000:
            body = body[:20000] + "\n... [truncated]"
        return (f"from: {h.get('from', '?')}\nto: {h.get('to', '?')}\n"
                f"date: {h.get('date', '?')}\nsubject: {h.get('subject', '(none)')}\n\n{body}")


class Gmail(_GmailApi, Google):
    id = "gmail"
    label = "Gmail"
    summary = "Search and read mail. Read-only: it cannot send, delete or change anything."
    scope_urls = (GMAIL_SCOPE,)
    scope_labels = ("Read mail", "Search mail")

    def test(self, integration, keys):
        return self._gmail_test(integration, keys), None

    def operations(self, integration):
        return self._gmail_ops()

    def call(self, integration, operation, args, keys):
        return self._gmail_call(integration, operation.name, args, keys)


class _CalendarApi:
    """Events, read-only. Mixed into a Google provider."""

    def _calendar_test(self, integration, keys):
        return self._get(integration, keys, "/calendar/v3/calendars/primary").get("id", "")

    def _calendar_ops(self):
        return [
            Operation("events", "List upcoming events in time order.",
                      {"days": "how many days ahead to look, 1-60 (default 7)",
                       "query": "optional text to match",
                       "calendar": "calendar id (default: the primary calendar)"}),
            Operation("calendars", "List the calendars on this account."),
        ]

    def _calendar_call(self, integration, name, args, keys):
        if name == "calendars":
            data = self._get(integration, keys, "/calendar/v3/users/me/calendarList")
            rows = [f"{c.get('id')}  {c.get('summary', '')}"
                    + ("  (primary)" if c.get("primary") else "")
                    for c in data.get("items", [])]
            return "\n".join(rows) or "No calendars."
        try:
            days = max(1, min(int(args.get("days") or 7), 60))
        except (TypeError, ValueError):
            days = 7
        calendar = str(args.get("calendar") or "primary").strip()
        if not _ID.match(calendar):
            raise ConnectorError("that is not a calendar id")
        now = datetime.datetime.now(datetime.timezone.utc)
        query = {"timeMin": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "timeMax": (now + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "singleEvents": "true", "orderBy": "startTime", "maxResults": 50}
        if args.get("query"):
            query["q"] = str(args["query"])
        data = self._get(integration, keys,
                         f"/calendar/v3/calendars/{urllib.parse.quote(calendar, safe='')}/events",
                         query)
        events = data.get("items", [])
        if not events:
            return f"No events in the next {days} day(s)."
        out = [f"{len(events)} event(s) in the next {days} day(s):"]
        for e in events:
            start = (e.get("start") or {})
            end = (e.get("end") or {})
            when = start.get("dateTime") or (start.get("date", "?") + " (all day)")
            until = end.get("dateTime") or ""
            line = f"\n{when}" + (f" to {until}" if until else "") + f"\n  {e.get('summary', '(no title)')}"
            if e.get("location"):
                line += f"\n  where: {e['location']}"
            if e.get("attendees"):
                line += f"\n  with: {len(e['attendees'])} attendee(s)"
            out.append(line)
        return "\n".join(out)


class Calendar(_CalendarApi, Google):
    id = "calendar"
    label = "Google Calendar"
    summary = "See upcoming events. Read-only: it cannot create or change events."
    scope_urls = (CALENDAR_SCOPE,)
    scope_labels = ("Read calendar",)

    def test(self, integration, keys):
        return self._calendar_test(integration, keys), None

    def operations(self, integration):
        return self._calendar_ops()

    def call(self, integration, operation, args, keys):
        return self._calendar_call(integration, operation.name, args, keys)


# What each service adds to one Google sign-in: its scope, what sir is shown,
# and its actions under the names Claire calls them by.
SERVICES = {
    "gmail": {"scope": GMAIL_SCOPE, "labels": ("Read and search mail",),
              "names": {"search": "gmail_search", "read": "gmail_read"}},
    "calendar": {"scope": CALENDAR_SCOPE, "labels": ("Read calendar",),
                 "names": {"events": "calendar_events", "calendars": "calendar_list"}},
}


class GoogleAccount(_GmailApi, _CalendarApi, Google):
    """One Google sign-in for every service sir picks."""

    id = "google"
    label = "Google account"
    summary = ("One sign-in for Gmail and Calendar. Read-only: it cannot send mail or "
               "change events.")
    fields = (("gmail", "Gmail: search and read mail", False, False),
              ("calendar", "Calendar: see upcoming events", False, False),
              ("client_id", "OAuth client id (Desktop app)", False, False),
              ("client_secret", "OAuth client secret", True, False))

    def validate(self, config, secrets):
        picked = {name: config.get(name) for name in SERVICES if name in config}
        services = ([name for name, on in picked.items() if on in (True, "true", "yes", "1", "on")]
                    if picked else list(SERVICES))
        if not services:
            raise ConnectorError("choose at least one Google service")
        clean, kept, _ = super().validate(config, secrets)
        clean["services"] = services
        return clean, kept, [label for name in services for label in SERVICES[name]["labels"]]

    def _services(self, integration):
        return [s for s in integration["config"].get("services") or list(SERVICES) if s in SERVICES]

    def scopes_for(self, integration):
        return tuple(SERVICES[s]["scope"] for s in self._services(integration))

    def test(self, integration, keys):
        label, problems = "", []
        for service in self._services(integration):
            try:
                found = (self._gmail_test if service == "gmail" else self._calendar_test)(integration, keys)
            except ConnectorError as exc:
                problems.append(str(exc))           # check every service, say everything at once
                continue
            label = label or found
        if problems:
            raise ConnectorError(" | ".join(problems))
        return label, None

    def operations(self, integration):
        ops = []
        for service in self._services(integration):
            names = SERVICES[service]["names"]
            for op in (self._gmail_ops() if service == "gmail" else self._calendar_ops()):
                ops.append(Operation(names[op.name], op.description, op.params,
                                     writes=op.writes, required=op.required))
        return ops

    def call(self, integration, operation, args, keys):
        for service in self._services(integration):
            for inner, outer in SERVICES[service]["names"].items():
                if outer == operation.name:
                    run = self._gmail_call if service == "gmail" else self._calendar_call
                    return run(integration, inner, args, keys)
        raise ConnectorError(f"no action {operation.name!r}")
