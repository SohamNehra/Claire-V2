"""Integrations Claire built herself, once sir approved them.

Each one is a small MCP server (written with connectors/kit/claire_kit.py)
installed under <state>/integrations/<name>/, where no task can write. It is
started for each call inside the strict sandbox: it can read only its own
folder, write only a folder of its own, and is given only the credentials its
manifest declares, as environment variables. Its manifest -- which sir approved
-- decides which actions ask him first, whatever the server itself says.
"""

import os

from . import ConnectorError, Operation
from . import builder
from .mcp import Mcp, _Stdio
from .. import guard


class Built(Mcp):
    id = "built"
    label = "Integration built by Claire"
    summary = "An integration Claire wrote and you approved. Actions that change things ask first."
    fields = ()                              # set up by Claire's build, not by a form

    def __init__(self, core):
        super().__init__(core)

    def _manifest(self, integration):
        name = integration["config"].get("integration", "")
        manifest = builder.installed_manifest(self.core.settings, name)
        if manifest is None:
            raise ConnectorError(f"the integration {name!r} is not installed (or its manifest is broken)")
        return manifest

    def validate(self, config, secrets):
        name = str(config.get("integration") or "")
        manifest = builder.installed_manifest(self.core.settings, name)
        if manifest is None:
            raise ConnectorError(f"no installed integration named {name!r}")
        allowed = {f"env:{c['key']}" for c in manifest["credentials"]}
        unknown = [k for k in secrets if k not in allowed]
        if unknown:
            raise ConnectorError("not credentials this integration declares: " + ", ".join(unknown))
        clean = {"transport": "stdio", "command": ["(installed integration)"], "keep_alive": False,
                 "auto_allow": [], "tools": [], "integration": name}
        return clean, {k: str(v) for k, v in secrets.items()}, \
            [f"Reaches {', '.join(manifest['hosts'])}", "Actions that change things ask first"]

    def shown_scopes(self, integration):
        m = self._manifest(integration)
        reach = m["hosts"] + [f"the address you enter for {c['label']}" for c in m["credentials"] if c["kind"] == "url"]
        return [f"Reaches {', '.join(reach)}", "Actions that change things ask first"]

    def credentials(self, integration):
        """What sir is asked for, and whether each has been given."""
        have = set(integration["config"].get("secret_names", []))
        return [dict(c, secret=c["kind"] != "url", provided=f"env:{c['key']}" in have)
                for c in self._manifest(integration)["credentials"]]

    def _open(self, integration, keys):
        name = integration["config"]["integration"]
        folder = builder.installed_dir(self.core.settings, name)
        home = os.path.join(self.core.settings.state_dir, "connectors", integration["id"])
        os.makedirs(home, mode=0o700, exist_ok=True)
        env = guard.clean_env()
        env.update({"HOME": home, "TMPDIR": home, "PYTHONDONTWRITEBYTECODE": "1"})
        for c in self._manifest(integration)["credentials"]:
            value = keys.get(f"env:{c['key']}")
            if value is not None:
                env[c["key"]] = value
        argv = self.core.sandbox.wrap(builder.runner(folder, "server.py"), write_dirs=[home],
                                      strict=True, network=True, read_dirs=[folder])
        return _Stdio(argv, env, home, self.core.settings.connector_timeout)

    def test(self, integration, keys):
        manifest = self._manifest(integration)
        missing = [c["key"] for c in self.credentials(integration) if not c["provided"]]
        if missing:
            raise ConnectorError("waiting for sir to enter " + ", ".join(missing)
                                 + f" (Integrations → {manifest['title']})")
        label, changes = super().test(integration, keys)
        served = {t["name"] for t in changes["tools"]}
        declared = {a["name"] for a in manifest["actions"]}
        if served != declared:
            raise ConnectorError("the installed server does not offer the actions its manifest declares")
        check = manifest.get("check")
        if check:
            # The credentials are only proven by using them.
            op = Operation(check["action"], "")
            text = self.call(integration, op, check.get("args") or {}, keys)
            if not text.strip():
                raise ConnectorError(f"{check['action']} returned nothing")
        return manifest["title"], changes

    def operations(self, integration):
        try:
            manifest = self._manifest(integration)
        except ConnectorError:
            return []
        return [Operation(a["name"], a["description"], a["params"],
                          writes=a["effect"] == "change", required=a["required"])
                for a in manifest["actions"]]

    def manual(self, integration):
        manifest = self._manifest(integration)
        return (f"# {manifest['title']} (built by Claire)\n\n{manifest['description']}\n\n"
                f"Actions marked [asks sir first] change things; the rest only read. Pass "
                f"parameters as strings.")
