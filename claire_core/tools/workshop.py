"""The workshop: where Claire runs code she wrote, and builds tools from it.

Nothing in this module ever executes outside the strict sandbox profile: no
view of the home directory beyond the task's own folder (read-only), writes
only to a scratch folder, and no network unless a human approved it for that
tool. If the machine has no sandbox, these tools do not exist.

A workshop tool is three files under <state>/workshop/tools/<name>/:

    tool.py        defines run(args: dict) -> str
    test.py        exits 0 if the tool works
    manifest.json  description, parameters, network flag, what it replaces

A tool is registered only after its test passes inside the sandbox, and every
registration is a git commit, so any version can be inspected or rolled back.
Built-in tools cannot be replaced or shadowed from here: Claire can route
around a broken tool, but she cannot rewrite her own guards.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

from .. import protocol
from . import Tool, ToolFault

_TRUE = ("1", "true", "yes", "y")
_NAME = re.compile(r"^[a-z][a-z0-9_]{2,30}$")
_RESULT_MARK = "\x1eCLAIRE_RESULT "

RUNNER = '''"""Runs one workshop tool: JSON args on stdin, marked JSON result on stdout."""
import importlib.util, json, sys, traceback
try:
    spec = importlib.util.spec_from_file_location("tool", sys.argv[1])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.run(json.load(sys.stdin))
except BaseException:
    traceback.print_exc()
    sys.exit(1)
print("\\x1eCLAIRE_RESULT " + json.dumps("" if out is None else str(out)))
'''


def _dirs(settings):
    root = settings.workshop_dir
    return root, os.path.join(root, "tools"), os.path.join(root, "scratch")


def _ensure(settings):
    root, tools, scratch = _dirs(settings)
    for d in (tools, scratch, os.path.join(root, "staging")):
        os.makedirs(d, mode=0o700, exist_ok=True)
    runner = os.path.join(root, "_runner.py")
    # Rewritten every time, so a sandboxed process that somehow altered it
    # could not change what the next run executes.
    with open(runner, "w", encoding="utf-8") as f:
        f.write(RUNNER)
    return runner


def _scratch(ctx):
    path = os.path.join(_dirs(ctx.settings)[2], ctx.thread_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _git(root, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Claire", "-c", "user.email=claire@localhost",
         "-c", "commit.gpgsign=false"] + list(args),
        cwd=root, capture_output=True, text=True, timeout=30)


def _commit(root, message):
    """Version the workshop. Failure here must not lose a tool that passed."""
    try:
        if not os.path.isdir(os.path.join(root, ".git")):
            _git(root, "init", "-q")
            with open(os.path.join(root, ".gitignore"), "w") as f:
                f.write("scratch/\nstaging/\n__pycache__/\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", message)
    except (OSError, subprocess.SubprocessError):
        pass


def tool_doc(manifest):
    name = manifest["name"]
    lines = [f"## {name} -- {manifest['description']}", ""]
    note = "A tool you built in the workshop. Runs sandboxed"
    note += ", with network access." if manifest.get("network") else ", with no network."
    if manifest.get("replaces"):
        note += (f" Use it in place of `{manifest['replaces']}` while that tool "
                 f"is broken.")
    lines += [note, "", f"<{name}>"]
    lines += [f"<{p}>...</{p}>" for p in manifest.get("params", [])]
    lines += [f"</{name}>"]
    return "\n".join(lines)


def _make_handler(manifest, tool_dir):
    def handler(ctx, args):
        runner = _ensure(ctx.settings)
        payload = {p: args.get(p, "") for p in manifest.get("params", [])}
        result = ctx.sandbox.run(
            [sys.executable, runner, os.path.join(tool_dir, "tool.py")],
            cwd=_scratch(ctx), write_dirs=[_scratch(ctx)], strict=True,
            network=bool(manifest.get("network")),
            read_dirs=[tool_dir, runner, ctx.workdir],
            timeout=ctx.settings.code_timeout, stdin=json.dumps(payload),
            env={"CLAIRE_WORKDIR": ctx.workdir},
            max_output=ctx.settings.max_output)
        if result.timed_out:
            raise ToolFault(f"timed out after {ctx.settings.code_timeout}s")
        mark = result.output.rfind(_RESULT_MARK)
        if result.returncode != 0 or mark == -1:
            raise ToolFault(result.output.strip()[-1500:] or
                            f"exited {result.returncode} with no output")
        try:
            return json.loads(result.output[mark + len(_RESULT_MARK):].strip())
        except ValueError:
            raise ToolFault("returned a result that could not be read")
    return handler


def load_workshop(registry, settings):
    """Register every tool already in the workshop. Returns their names."""
    tools_dir = _dirs(settings)[1]
    names = []
    for name in sorted(os.listdir(tools_dir)) if os.path.isdir(tools_dir) else []:
        tool_dir = os.path.join(tools_dir, name)
        try:
            with open(os.path.join(tool_dir, "manifest.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        if manifest.get("name") != name or registry.get(name):
            continue                         # never shadow an existing tool
        registry.register(Tool(
            name, _make_handler(manifest, tool_dir),
            params=manifest.get("params", []), doc=tool_doc(manifest),
            source="workshop", needs=("sandbox",)))
        names.append(name)
    return names


def remove_tool(registry, settings, name):
    """Delete a workshop tool. Returns False if there is no such tool."""
    root, tools_dir, _ = _dirs(settings)
    tool_dir = os.path.join(tools_dir, name)
    if not _NAME.match(name) or not os.path.isdir(tool_dir):
        return False
    shutil.rmtree(tool_dir)
    registry.unregister(name)
    _commit(root, f"remove {name}")
    return True


# -- run_code -----------------------------------------------------------------

def run_code(ctx, args):
    code = args.get("code", "")
    if not code.strip():
        return "ERROR: <code> was empty"
    network = str(args.get("network", "")).strip().lower() in _TRUE
    if network and not ctx.ask("network", "run_code with network access:\n"
                               + code.strip()[:600], "NETWORK", always=True):
        return ("User DENIED network access for this code. Run it without "
                "<network>, or find another approach.")
    _ensure(ctx.settings)
    scratch = _scratch(ctx)
    script = os.path.join(scratch, "snippet.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    result = ctx.sandbox.run(
        [sys.executable, script], cwd=scratch, write_dirs=[scratch], strict=True,
        network=network, read_dirs=[ctx.workdir],
        timeout=ctx.settings.code_timeout,
        env={"CLAIRE_WORKDIR": ctx.workdir}, max_output=ctx.settings.max_output)
    if result.timed_out:
        return f"ERROR: code timed out after {ctx.settings.code_timeout}s"
    return f"exit={result.returncode}\n{result.output.strip() or '(no output)'}"


# -- make_tool ----------------------------------------------------------------

def make_tool(ctx, args):
    name = (args.get("name") or "").strip()
    description = " ".join((args.get("description") or "").split())
    code, test = args.get("code", ""), args.get("test", "")
    params = [p for p in re.split(r"[,\s]+", (args.get("params") or "").strip()) if p]
    network = str(args.get("network", "")).strip().lower() in _TRUE
    replaces = (args.get("replaces") or "").strip()

    if not _NAME.match(name):
        return ("ERROR: <name> must be 3-31 characters: lowercase letters, "
                "digits and underscores, starting with a letter.")
    existing = ctx.registry.get(name)
    if (existing and existing.source != "workshop") or name in (
            protocol.BODY_KEYS + protocol.SIMPLE_KEYS):
        return (f"ERROR: {name!r} is already the name of a built-in tool or "
                f"parameter. Choose a different name.")
    if not description:
        return "ERROR: <description> was empty"
    bad = [p for p in params if not re.match(r"^[a-z_][a-z0-9_]{0,30}$", p)]
    if bad:
        return f"ERROR: bad parameter name(s): {', '.join(bad)}"
    if not re.search(r"(?m)^def run\(", code):
        return "ERROR: <code> must define a top-level function `def run(args):`"
    if not test.strip():
        return ("ERROR: <test> was empty. A tool is only registered once its "
                "test passes, so it needs one.")
    if replaces and not ctx.registry.get(replaces):
        return f"ERROR: <replaces> names {replaces!r}, which is not a tool."

    if network and not ctx.ask(
            "network", f"new tool `{name}` wants network access: {description}",
            "NETWORK", always=True):
        return ("User DENIED network access for this tool. Build it without "
                "<network>, or find another approach.")

    _ensure(ctx.settings)
    root, tools_dir, _ = _dirs(ctx.settings)
    staging = os.path.join(root, "staging", f"{name}-{uuid.uuid4().hex[:6]}")
    os.makedirs(staging, mode=0o700)
    try:
        for fn, body in (("tool.py", code), ("test.py", test)):
            with open(os.path.join(staging, fn), "w", encoding="utf-8") as f:
                f.write(body)
        result = ctx.sandbox.run(
            [sys.executable, "test.py"], cwd=staging, write_dirs=[staging],
            strict=True, network=network, read_dirs=[ctx.workdir],
            timeout=ctx.settings.code_timeout,
            env={"CLAIRE_WORKDIR": ctx.workdir},
            max_output=ctx.settings.max_output)
        if result.timed_out or result.returncode != 0:
            why = (f"timed out after {ctx.settings.code_timeout}s"
                   if result.timed_out else f"exit={result.returncode}")
            return (f"ERROR: the test did not pass ({why}), so `{name}` was NOT "
                    f"registered.\n{result.output.strip() or '(no output)'}\n\n"
                    f"Fix the code or the test and call make_tool again.")

        manifest = {"name": name, "description": description, "params": params,
                    "network": network, "replaces": replaces,
                    "created": time.strftime("%Y-%m-%d %H:%M"),
                    "thread": ctx.thread_id}
        final = os.path.join(tools_dir, name)
        updated = os.path.isdir(final)
        if updated:
            shutil.rmtree(final)             # the old version stays in git
        # Only the tool and its test move in; anything the test left lying
        # around in staging is discarded with it.
        os.makedirs(final, mode=0o700)
        for fn in ("tool.py", "test.py"):
            shutil.copyfile(os.path.join(staging, fn), os.path.join(final, fn))
        with open(os.path.join(final, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    _commit(root, f"{'update' if updated else 'add'} {name}: {description}"
            + (f" (replaces {replaces})" if replaces else ""))
    ctx.registry.register(Tool(
        name, _make_handler(manifest, final), params=params,
        doc=tool_doc(manifest), source="workshop", needs=("sandbox",)))
    ctx.session.add_tool(name)

    if replaces:
        ctx.store.set_substitute(replaces, name)
        ctx.vault.inbox_note(
            f"tool-{replaces}", f"`{replaces}` needs repair",
            f"I built a workshop tool, `{name}`, and I am using it in place of "
            f"`{replaces}` for now. It lives in "
            f"`{os.path.join(tools_dir, name)}`. Once `{replaces}` is fixed, "
            f"run `claire2 tools reset {replaces}` so I go back to it.")
    return (f"{'Updated' if updated else 'Registered'} tool `{name}` -- its test "
            f"passed. It is available now and in future tasks. Call it like "
            f"this:\n\n{tool_doc(manifest)}")


TOOLS = [
    Tool("run_code", run_code, needs=("sandbox",)),
    Tool("make_tool", make_tool, needs=("sandbox",)),
]
