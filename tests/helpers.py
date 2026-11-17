"""Shared test fixtures: a scripted model, and a Core on throwaway folders."""

import os
import shutil
import tempfile

from claire_core import boot
from claire_core.engine import Core, Session
from claire_core.llm import ChatResult
from claire_core.settings import Settings


def symlink_or_skip(source, target):
    try:
        os.symlink(source, target, target_is_directory=os.path.isdir(source))
    except OSError as exc:
        if getattr(exc, 'winerror', None) == 1314:
            import unittest
            raise unittest.SkipTest('Windows account lacks permission to create symlinks') from exc
        raise


# No test may launch a real model server, whatever code path it reaches.
managed_server_spec = boot.server_spec
boot.server_spec = lambda settings, role: None


class FakeLLM:
    """Replays a list of replies. A callable entry is called with the messages
    so a test can react to what the engine sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def describe(self, role="brain", refresh=False):
        return {"role": role, "api_base": "http://fake/v1", "model": "fake",
                "ctx": 100000, "live": True}

    def chat(self, messages, role="brain", thinking=False, on_delta=None,
             cancel=None, max_tokens=None, temperature=None, response_format=None):
        self.calls.append({"role": role, "thinking": thinking, "temperature": temperature,
                           "messages": [dict(m) for m in messages]})
        if not self.replies:
            raise AssertionError("the model was called more times than scripted")
        reply = self.replies.pop(0)
        if callable(reply):
            reply = reply(messages)
        if isinstance(reply, Exception):
            raise reply
        if on_delta:
            on_delta("content", reply)
        return ChatResult(reply, "", 0, 0)


class Env:
    """A Core whose state, vault and working directory are all temporary.

    They live under the home directory rather than the system temp folder,
    because the sandbox treats temp as writable and the tests need a folder
    that is genuinely outside what a task may touch.
    """

    def __init__(self, replies=(), **overrides):
        base = os.path.join(os.path.expanduser("~"), ".claire-test-tmp")
        os.makedirs(base, exist_ok=True)
        self.root = tempfile.mkdtemp(dir=base)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        overrides.setdefault("auto_parallel", False)   # deterministic tests, no host RAM dependence
        overrides.setdefault("secret_backend", "file")     # never the real keychain
        overrides.setdefault("model_idle_seconds", 1800.0) # not config.py's choice
        overrides.setdefault("preload_model", False)
        settings = Settings(state_dir=os.path.join(self.root, "state"),
                            vault=os.path.join(self.root, "vault"), **overrides)
        settings.prompt_override = ""
        settings.skill_router = False     # tests script every model call; routing is tested on its own
        settings.controller_enabled = False  # opt in explicitly in controller tests
        settings.roles = {}            # tests must not depend on config.py's models
        settings.auto_approve = False
        settings.thinking = False
        self.llm = FakeLLM(replies)
        self.core = Core(settings, llm=self.llm)
        self.events = []
        self.asked = []
        self.allow = True

    def approver(self, request):
        self.asked.append(request)
        return self.allow

    def session(self, kind="agent", **kw):
        kw.setdefault("workdir", self.work)
        return Session(self.core, kind=kind, sink=self.events.append,
                       approver=self.approver, **kw)

    def results(self):
        return [e for e in self.events if e["type"] == "tool_result"]

    def last_result_text(self, n=-1):
        """The text of the n-th tool result as the model was shown it."""
        sent = [m["content"] for m in self.session_messages
                if m["role"] == "user" and isinstance(m["content"], str)
                and m["content"].startswith("[")]
        return sent[n]

    def close(self):
        self.core.store.close()
        shutil.rmtree(self.root, ignore_errors=True)
        base = os.path.dirname(self.root)
        try:
            os.rmdir(base)
        except OSError:
            pass


def done(text="finished"):
    return f"All set.\n<done>\n<summary>\n{text}\n</summary>\n</done>"
