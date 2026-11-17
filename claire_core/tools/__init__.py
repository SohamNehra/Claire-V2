"""Tool registry.

A tool is one object: a name, the parameters it takes, a handler, and the
markdown that teaches the model how to call it. Adding a built-in tool is one
new module in this package with a `TOOLS` list -- nothing else needs editing,
because the system prompt's tool section is generated from the registry.

Handlers return a string for the model. Three kinds of failure are kept apart,
because they call for different responses:

- an "ERROR: ..." string: the request was wrong (no such file, bad regex). The
  model should fix its request.
- ToolFault, or any unexpected exception: the tool itself is broken. Repeating
  the call will not help; the engine files a repair note and points the model
  at the workshop so it can get the result another way.
"""

import importlib
import os
import pkgutil

PROMPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "prompts")


class ToolFault(Exception):
    """Raised by a tool that knows it is broken, not merely misused."""


class Tool:
    def __init__(self, name, handler=None, params=(), read_only=False,
                 doc=None, source="builtin", needs=(), kinds=("agent",)):
        self.name = name
        self.handler = handler        # None for tools the engine handles itself
        self.params = tuple(params)   # parameter names beyond the standard set
        self.read_only = read_only
        self.source = source          # "builtin" | "workshop"
        self.needs = tuple(needs)     # capabilities: "sandbox", "hub"
        self.kinds = tuple(kinds)     # thread kinds that get it: agent, home
        self._doc = doc

    @property
    def doc(self):
        if self._doc is None:
            path = os.path.join(PROMPTS, "tools", f"{self.name}.md")
            with open(path, encoding="utf-8") as f:
                self._doc = f.read().strip()
        return self._doc


class ToolContext:
    """What a handler is given: where it is working and how to reach the rest
    of the system. Built per call, so handlers hold no state of their own."""

    def __init__(self, session):
        self.session = session
        self.settings = session.settings
        self.workdir = session.workdir
        self.vault = session.vault
        self.store = session.store
        self.registry = session.registry
        self.sandbox = session.sandbox
        self.hub = session.hub
        self.thread_id = session.thread_id
        self.images = []          # pictures a tool wants the model to see with its result

    @property
    def auto_approve(self):
        return self.session.auto_approve

    def ask(self, kind, title, flag="", always=False):
        """Ask a human. Returns True if allowed.

        always=True asks even in yolo mode -- used for anything that leaves the
        sandbox, which no mode is allowed to wave through.
        """
        if self.auto_approve and not always:
            return True
        # Human-free mode answers for sir, except for anything that deletes or
        # is dangerous. What it lets through is still recorded.
        mode = getattr(self.session.core, "human_free", None)
        if mode is not None and mode.on():
            from ..autonomy import allows
            if allows(kind, title, flag):
                store = self.session.store
                if self.session.persist:
                    store.resolve_approval(store.add_approval(self.thread_id, kind, title, flag), "auto")
                first = (title or "").strip().splitlines()[0][:160] if (title or "").strip() else kind
                self.session.emit("notice", level="info",
                                  text=f"Human-free mode: went ahead without asking: {first}")
                return True
        # Once sir has said no (or not answered) to a flagged action on a
        # service, the same kind of action on it is not put to him again in
        # this turn: seen live, a worker whose page script was not approved
        # asked for another script tool, and the task waited out a second
        # approval. A "no" is about the effect, not the tool's name.
        service = (title or "").split(":", 1)[0].strip()
        key = (kind, flag, service) if kind == "connector" and flag else None
        declined = getattr(self.session, "_declined", None)
        if key and declined is not None and key in declined:
            self.session.emit("notice", level="info",
                              text=f"not asked again: a {flag.lower()} on {service} was already "
                                   f"declined in this turn")
            return False
        allowed = self.session.ask({"kind": kind, "title": title, "flag": flag})
        if key and not allowed and declined is not None:
            declined.add(key)
        return allowed


class Registry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def unregister(self, name):
        self._tools.pop(name, None)

    def get(self, name):
        return self._tools.get(name)

    def all(self):
        return list(self._tools.values())

    def names_for(self, kind, capabilities):
        """Tools a thread of this kind may use, given what the host offers."""
        return [t.name for t in self._tools.values()
                if kind in t.kinds and all(n in capabilities for n in t.needs)]

    def extra_params(self, names):
        return {n: self._tools[n].params for n in names
                if n in self._tools and self._tools[n].params}

    def docs(self, names):
        return "\n\n".join(self._tools[n].doc for n in names if n in self._tools)

    def load_builtin(self):
        """Register every Tool listed by a module in this package."""
        package = importlib.import_module(__name__)
        for info in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{__name__}.{info.name}")
            for tool in getattr(module, "TOOLS", []):
                self.register(tool)
        return self
