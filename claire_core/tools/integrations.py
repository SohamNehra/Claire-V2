"""build_integration: how Claire writes, checks and installs an integration.

The work happens in the task's own folder, where Claire can edit freely. What
is installed is a checked copy in her state, which only an approved submit can
replace. The steps and rules are in the building-integrations skill.
"""

import os
import time

from ..connectors import ConnectorError, builder
from . import Tool


def _folder(ctx, args):
    name = (args.get("name") or "").strip()
    if not builder.NAME.match(name):
        raise builder.BuildError("give <name>: the integration's name, lowercase_with_underscores")
    return name, os.path.join(ctx.workdir, name)


def build_integration(ctx, args):
    action = (args.get("action") or "").strip().lower()
    note = ""
    if "building-integrations" not in ctx.session.skills and ctx.session.core.skills.get("building-integrations"):
        # Building without the procedure is how a template ends up submitted.
        ctx.session.load_skill("building-integrations")
        note = ("[The building-integrations skill is now loaded; its steps and rules are in your "
                "instructions. Follow them from here.]\n\n")
    result = _run(ctx, args, action)
    # The engine classifies failures by their leading ERROR/REFUSED marker.
    # A skill-loading notice must not turn a failed build into a success.
    return result + ("\n\n" + note.strip() if note else "") if result.startswith(("ERROR:", "REFUSED:", "User DENIED", "Sir did not approve")) else note + result


def _run(ctx, args, action):
    try:
        if action == "new":
            name, folder = _folder(ctx, args)
            files = builder.scaffold(folder, name, (args.get("title") or "").strip(),
                                     (args.get("description") or "").strip(), args.get("host") or "")
            return (f"Created the starting template. Paths are relative to your working directory; "
                    f"edit exactly these files:\n  {name}/integration.json\n  {name}/server.py\n"
                    f"  {name}/test.py\n  {name}/fixtures/   (recorded answers)\n"
                    f"The template passes its own check, so any later failure is about your change. "
                    f"Replace its placeholder `ping` action with the real ones: integration.json first "
                    f"(hosts, credentials, every action and its effect), then server.py, then test.py, "
                    f"then check.")
        if action == "edit":
            name, folder = _folder(ctx, args)
            source = builder.installed_dir(ctx.session.core.settings, name)
            if not os.path.isdir(source):
                return f"ERROR: no installed integration named {name!r}. Use `new` to start one."
            if os.path.isdir(folder) and os.listdir(folder):
                return f"ERROR: {name}/ already has files; edit those, or remove the folder first."
            import shutil
            shutil.copytree(source, folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".scratch", "__pycache__"))
            return (f"Copied the installed {name} into {name}/. Change it, check, and submit: "
                    f"sir approves the update, and his credentials are kept.")
        if action == "check":
            name, folder = _folder(ctx, args)
            manifest, stage, output = builder.check(ctx.session.core, folder)
            import shutil
            shutil.rmtree(stage, ignore_errors=True)
            return (f"PASSED: {manifest['title']} is ready to submit. "
                    f"{len(manifest['actions'])} action(s), hosts {', '.join(manifest['hosts'])}.\n"
                    f"test.py said:\n{output.strip()}")
        if action == "submit":
            return _submit(ctx, args)
        if action == "status":
            return _status(ctx, args)
        return "ERROR: <action> is one of: new, edit, check, submit, status"
    except ConnectorError as exc:
        return f"ERROR: {exc}"


def _submit(ctx, args):
    import shutil
    core = ctx.session.core
    name, folder = _folder(ctx, args)
    manifest, stage, _ = builder.check(core, folder)
    if builder.is_template(manifest):
        shutil.rmtree(stage, ignore_errors=True)
        strays = builder.strays(ctx.workdir, name, folder)
        return ("ERROR: this is still the starting template (its only action is the placeholder "
                "`ping`), so there is nothing to install. "
                + (f"Your edits went elsewhere: {', '.join(strays)}. Move them into {name}/ "
                   f"(paths are relative to your working directory) and check again."
                   if strays else f"Write the real integration into {name}/ and check again."))
    try:
        previous = builder.installed_manifest(core.settings, name)
        with open(os.path.join(stage, "server.py"), encoding="utf-8") as f:
            lines = sum(1 for _ in f)
        text = builder.summary(manifest, previous, lines)
        flag = "UPDATE INTEGRATION" if previous else "NEW INTEGRATION"
        if not ctx.ask("integration", text, flag, always=True):
            return ("Sir did not approve installing this integration, so nothing was installed. "
                    "Do not resubmit it unchanged; ask him what he wants different.")
        builder.install(core.settings, stage, name)
        stage = None
    finally:
        if stage:
            shutil.rmtree(stage, ignore_errors=True)
    view = core.connectors.upsert_built(manifest)
    # A credential-free install can become usable in this very turn. Expose
    # use_connector and the new state before the next model call.
    ctx.session.refresh()
    missing = [c for c in view.get("credentials", []) if not c.get("provided")]
    if view["status"] == "connected":
        return (f"Installed and connected as `{view['id']}`. Its actions are available to every "
                f"chat and task now: {', '.join(a['name'] for a in manifest['actions'])}.")
    if missing:
        if ctx.hub is not None:
            ctx.hub.publish({"type": "notice", "thread": "home", "ts": time.time(), "level": "info",
                             "text": f"{manifest['title']} is installed and needs "
                                     + ", ".join(c["label"] for c in missing)
                                     + f": Integrations → {manifest['title']}."})
        return (f"Installed as `{view['id']}`. It now waits for sir to enter "
                + "; ".join(f"{c['label']} ({c['key']})" for c in missing)
                + f" under Integrations → {manifest['title']}. He has been told where. When he "
                  f"has, the connection is tested and its actions become available. Tell him "
                  f"exactly what to get, using the `how` steps you wrote.")
    return (f"ERROR: Installed as `{view['id']}`, but its connection test failed: {view['error']}. "
            f"Fix the code, check, and submit again.")


def _status(ctx, args):
    core = ctx.session.core
    name = (args.get("name") or "").strip()
    rows = [r for r in core.store.integrations() if r["provider"] == "built"
            and (not name or r["config"].get("integration") == name)]
    if not rows:
        return f"No installed integration{' named ' + name if name else ''}."
    out = []
    for r in rows:
        view = core.connectors.view(r)
        waiting = [c["key"] for c in view["credentials"] if not c["provided"]]
        out.append(f"`{r['id']}` ({r['config'].get('integration')}): {r['status']}"
                   + (f" -- {r['error']}" if r["error"] else "")
                   + (f"; waiting for {', '.join(waiting)}" if waiting else ""))
    return "\n".join(out)


TOOLS = [Tool("build_integration", build_integration, params=("host",), needs=("sandbox",),
              kinds=("agent",))]
