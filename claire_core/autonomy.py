"""Human-free mode: Claire works without stopping to ask, except for anything
that deletes or is dangerous.

The mode only ever removes an approval Claire would otherwise ask for. It never
widens what is possible: the OS sandbox still holds every command it held, the
forbidden list (guard.FORBIDDEN) is still refused outright, and the requests
listed below still wait for sir. Only sir can switch it on or off, from the app
or the API; there is no tool that lets a model change it.

Every request it lets through is still written to the approvals table, marked
"auto", and noted in the thread's trace, so what ran unattended can be read
back afterwards.
"""

import json
import os
import re
import threading

# Commands that delete, overwrite, publish, or hand control to something else.
# A command matching this still asks in human-free mode.
DESTRUCTIVE_COMMAND = re.compile(
    r"(\brm\b|\brmdir\b|\bunlink\b|\bshred\b|\btruncate\b|\bdd\s|\bsrm\b"
    r"|\bgit\s+push\b|\bgit\s+reset\s+--hard|\bgit\s+clean\b|\bgit\s+branch\s+-D\b"
    r"|\bgit\s+checkout\s+(--\s|\.)|\bgit\s+restore\b|\bgit\s+stash\s+(drop|clear)\b"
    r"|\bkill(all)?\s|\bpkill\b|\bchmod\s+(-R\s+)?777|\bchown\b"
    r"|curl\s+[^|]*\|\s*(ba|z|fi)?sh|wget\s+[^|]*\|\s*(ba|z|fi)?sh"
    r"|\bfind\b[^|;]*-delete|-exec\s+rm\b|os\.(remove|unlink|rmdir)|shutil\.rmtree"
    r"|\bnpm\s+(publish|unpublish)\b|\btwine\s+upload\b"
    r"|\bcrontab\b|\blaunchctl\b|\bdefaults\s+(write|delete)\b|\bdiskutil\b|\bhistory\s+-c"
    r"|\bosascript\b|\bssh\b|\bscp\b|\brsync\b[^|;]*--delete"
    r"|\bmv\b[^|;]*\s/(dev|etc|usr|bin|System|Library)/"
    r"|>\s*/dev/(?!null\b|stdout\b|stderr\b|fd/))")

# Actions on a connected service or app that remove something.
DESTRUCTIVE_ACTION = re.compile(
    r"\b(delete|remove|trash|destroy|drop|erase|purge|wipe|unlink|uninstall|revoke|"
    r"discard|clear|empty)\w*", re.I)

# Approval flags that always wait for sir: sending, paying, signing in and
# uploading ("MAJOR ACTION"), closing a tab he had open, reaching a service on
# this machine or his network, and commands with no sandbox around them.
ALWAYS_ASK_FLAGS = {"MAJOR ACTION", "CLOSE_EXISTING_TAB", "LOCAL ADDRESS",
                    "WRITES OUTSIDE PROJECT"}


def allows(kind, title, flag):
    """Whether human-free mode may answer yes to this request by itself."""
    flag = (flag or "").upper()
    title = title or ""
    if flag in ALWAYS_ASK_FLAGS:
        return False
    if kind == "command":
        return not DESTRUCTIVE_COMMAND.search(title)
    if kind in ("connector", "computer"):
        return not DESTRUCTIVE_ACTION.search(title)
    if kind in ("integration", "schedule", "network", "search", "fetch"):
        return True
    return False                     # anything new waits until it is classified


class HumanFree:
    """The switch, kept in the state directory so it survives a restart."""

    def __init__(self, settings):
        self.path = os.path.join(settings.state_dir, "mode.json")
        self._lock = threading.Lock()

    def on(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f).get("human_free") is True
        except (OSError, ValueError):
            return False

    def set(self, on):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            temporary = self.path + ".writing"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"human_free": bool(on)}, f)
            os.replace(temporary, self.path)
        return self.on()
