"""Talks to the service over HTTP. Used by the terminal; the app will speak
the same API from the browser."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .service import load_token
from .settings import ROOT


class ServiceError(Exception):
    pass


class Client:
    def __init__(self, settings):
        self.s = settings
        self.base = f"http://{settings.service_host}:{settings.service_port}"
        self.token = load_token(settings)

    def healthy(self):
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure_running(self, wait=20):
        """Start the service in the background if it is not already up."""
        if self.healthy():
            self.token = load_token(self.s)
            return False
        self.s.ensure_state_dir()
        log = os.path.join(self.s.state_dir, "service.log")
        env = dict(os.environ, PYTHONPATH=ROOT)
        # Only a profile named on the command line is passed on. One from
        # config.py or the app's model choice is read by the service itself.
        if getattr(self.s, "worker_profile", "") and getattr(self.s, "worker_profile_explicit", False):
            env["CLAIRE_WORKER_PROFILE"] = self.s.worker_profile
        with open(log, "ab") as out:
            subprocess.Popen([sys.executable, "-m", "claire_core", "serve"],
                             cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.healthy():
                self.token = load_token(self.s)
                return True
            time.sleep(0.2)
        raise ServiceError(f"the service did not start. Check {log}")

    def _request(self, method, path, body=None, timeout=30):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "")
            except Exception:
                detail = ""
            raise ServiceError(detail or f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            raise ServiceError(f"cannot reach the service ({exc.reason})")

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body)

    def delete(self, path):
        return self._request("DELETE", path)

    def events(self, after=0):
        """Yield events as they happen. Ends when the connection drops."""
        req = urllib.request.Request(
            f"{self.base}/events?after={int(after)}",
            headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=None) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue
