"""Shell and git tools."""

import subprocess

from .. import guard
from . import Tool

_TRUE = ("1", "true", "yes", "y")


def run_command(ctx, args):
    cmd = args.get("command", "").strip()
    if not cmd:
        return "ERROR: empty command"

    verdict = guard.classify(cmd)
    if verdict.level == "deny":
        return (f"REFUSED: this command {verdict.reason}. It is never run, with "
                f"or without approval. Do not retry it or rephrase it; find "
                f"another approach, or tell the user what you needed it for.")

    outside = str(args.get("outside", "")).strip().lower() in _TRUE
    sandbox = ctx.sandbox
    timeout = ctx.settings.cmd_timeout

    if not sandbox.available:
        # No OS sandbox on this machine: fall back to v1's rules, where the
        # pattern guards are all there is.
        if verdict.escapes and ctx.auto_approve:
            return ("REFUSED: this command writes outside the working directory, "
                    "which is not allowed unattended. Work inside the project "
                    "directory, or ask the user to run it themselves.")
        if verdict.level == "ask" or not ctx.auto_approve:
            flag = ("WRITES OUTSIDE PROJECT" if verdict.escapes
                    else "DANGEROUS" if verdict.level == "ask" else "")
            if not ctx.ask("command", cmd, flag, always=True):
                return "User DENIED this command. Do not retry it; choose another approach."
        result = sandbox.run(cmd, cwd=ctx.workdir, timeout=timeout,
                             sandboxed=False, max_output=ctx.settings.max_output)
    elif outside:
        # Leaving the sandbox is never automatic, in any mode.
        if not ctx.ask("command", cmd, "RUNS OUTSIDE THE SANDBOX", always=True):
            return ("User DENIED running this outside the sandbox. Do not retry "
                    "it; choose another approach.")
        result = sandbox.run(cmd, cwd=ctx.workdir, timeout=timeout,
                             sandboxed=False, max_output=ctx.settings.max_output)
    else:
        if verdict.level == "ask":
            if not ctx.ask("command", cmd, "DANGEROUS", always=True):
                return "User DENIED this command. Do not retry it; choose another approach."
        elif not ctx.ask("command", cmd):
            return "User DENIED this command. Do not retry it; choose another approach."
        result = sandbox.run(cmd, cwd=ctx.workdir, timeout=timeout,
                             max_output=ctx.settings.max_output)

    if result.timed_out:
        return f"ERROR: command timed out after {timeout}s"
    text = f"exit={result.returncode}\n{result.output.strip() or '(no output)'}"
    if (sandbox.available and not outside and result.returncode != 0
            and "Operation not permitted" in result.output):
        text += ("\n\n[sandbox] Commands can only write inside the working "
                 "directory and cannot read credentials. If this command "
                 "genuinely has to reach outside, call run_command again with "
                 "<outside>true</outside> and the user will be asked to approve "
                 "it. Otherwise keep the work inside the project.")
    return text


GIT_READONLY = {
    "status": ["status", "--short", "--branch"],
    "diff": ["diff"],
    "staged": ["diff", "--cached"],
    "log": ["log", "--oneline", "-20"],
    "branch": ["branch", "-vv"],
    "show": ["show", "--stat"],
}


def git(ctx, args):
    """Read-only git inspection. Mutating commands go through run_command so
    they still hit the approval gate."""
    what = (args.get("what") or "status").strip().lower()
    if what not in GIT_READONLY:
        return (f"ERROR: unknown git query {what!r}. Available: "
                f"{', '.join(sorted(GIT_READONLY))}. For anything that changes "
                f"the repo (commit, checkout, add), use run_command.")

    cmd = ["git"] + GIT_READONLY[what]
    target = (args.get("path") or "").strip()
    if target:
        if target.startswith('-'):
            return 'ERROR: path/reference must not be a Git option; use ./ for a filename beginning with a dash'
        guard.safe_path(ctx.workdir, target, ctx.settings)
        if what == 'branch':
            # A positional branch name creates a branch; --list is inspection.
            cmd += ['--list', target]
        else:
            cmd += ["--", target] if what in ("status", "diff", "staged", "log") else [target]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           cwd=ctx.workdir, env=guard.clean_env())
    except FileNotFoundError:
        return "ERROR: git is not installed"
    except subprocess.TimeoutExpired:
        return "ERROR: git timed out"

    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0 and "not a git repository" in out.lower():
        return "ERROR: not a git repository"
    if len(out) > ctx.settings.max_output:
        out = out[:ctx.settings.max_output] + "\n... [truncated]"
    if p.returncode != 0:
        return f'ERROR: git {what} exited {p.returncode}\n' + (out or '(no output)')
    return out or f"(git {what}: no output -- nothing to report)"


TOOLS = [
    Tool("run_command", run_command),
    Tool("git", git, read_only=True),
]
