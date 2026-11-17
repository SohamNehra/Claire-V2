"""Where connector credentials live.

Tokens are kept by the backend and never shown to a model, written to the
vault, put in a prompt, or returned by the API. On macOS they go in the login
keychain; elsewhere, in a private file inside the state directory. Either way
nothing Claire runs can read them: the sandbox denies both.
"""

import json
import os
import subprocess
import sys
import threading

SERVICE = "claire-connector"


class SecretStore:
    def __init__(self, settings):
        mode = (settings.secret_backend or "auto").lower()
        self.keychain = (mode in ("auto", "keychain") and sys.platform == "darwin"
                         and os.access("/usr/bin/security", os.X_OK))
        self.path = os.path.join(settings.state_dir, "secrets.json")
        self._lock = threading.Lock()

    # -- keychain ------------------------------------------------------------

    @staticmethod
    def _quote(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _security(self, command):
        """Run one `security` command, passed on stdin so that a secret never
        appears in the process list."""
        p = subprocess.run(["/usr/bin/security", "-i"], input=command + "\n",
                           capture_output=True, text=True, timeout=20)
        return p.returncode, p.stdout, p.stderr

    # -- file ----------------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)

    # -- public --------------------------------------------------------------

    def set(self, integration, name, value):
        account = f"{integration}/{name}"
        if "\n" in value or "\r" in value:
            raise ValueError("a secret cannot contain a line break")
        with self._lock:
            if self.keychain:
                code, _, err = self._security(
                    f"add-generic-password -a {self._quote(account)} -s {SERVICE} "
                    f"-w {self._quote(value)} -U")
                if code != 0:
                    raise RuntimeError(f"the keychain refused the secret: {err.strip()[:120]}")
            else:
                data = self._load()
                data[account] = value
                self._save(data)

    def get(self, integration, name):
        account = f"{integration}/{name}"
        with self._lock:
            if self.keychain:
                p = subprocess.run(
                    ["/usr/bin/security", "find-generic-password", "-a", account,
                     "-s", SERVICE, "-w"], capture_output=True, text=True, timeout=20)
                return p.stdout.rstrip("\n") if p.returncode == 0 else None
            return self._load().get(account)

    def delete(self, integration, name):
        account = f"{integration}/{name}"
        with self._lock:
            if self.keychain:
                subprocess.run(["/usr/bin/security", "delete-generic-password",
                                "-a", account, "-s", SERVICE],
                               capture_output=True, text=True, timeout=20)
            else:
                data = self._load()
                if data.pop(account, None) is not None:
                    self._save(data)
