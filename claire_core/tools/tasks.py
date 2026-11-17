"""Home-thread tools: starting task threads and reading how they went."""

import re
import os
import time
import sys

from . import Tool
from .. import host


def requested_workdir(session):
    """Recover an omitted workdir from an explicit existing path in this turn.

    A model mentioning the project in its brief is insufficient if the runtime
    silently puts the worker elsewhere. Only inspect actual user turn input.
    """
    text = getattr(session, '_active_request', '') or ''
    if text.startswith(('[task report]', '[integration report]')):
        return '', None
    candidates = set()
    prefix = r"(?:~/|/)" if sys.platform != "win32" else r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/)"
    quoted = re.findall(r"[\"'](" + prefix + r"[^\"'\n]+)[\"']", text)
    starts = [m.start() for m in re.finditer(r"(?<![\w])" + prefix + r"[^\s\"']+", text)]
    groups = [[p] for p in quoted]
    for start in starts:
        words = text[start:].split()[:8]
        groups.append([' '.join(words[:n]) for n in range(1, len(words)+1)])
    for group in groups:
        matches = []
        for raw in group:
            for value in (raw, raw.rstrip('.,;:!?\"\')]}')):
                path = os.path.expanduser(host.resolve_location(value) if sys.platform == "win32" else value)
                if os.path.isdir(path):
                    matches.append(path)
                elif os.path.isfile(path):
                    matches.append(os.path.dirname(path))
        if matches:
            candidates.add(os.path.realpath(max(matches, key=len)))
    # If multiple supplied paths are nested, the supplied ancestor is the
    # project. Never invent a common parent for unrelated sibling projects.
    roots = [p for p in candidates if not any(p.startswith(q + os.sep) for q in candidates if p != q)]
    if len(roots) > 1:
        return '', 'several existing project paths were supplied; set workdir explicitly'
    return (roots[0] if roots else ''), None


def _create_requested_folder(ctx, requested, title, problem):
    """A new project folder sir asked for in so many words ("in a new folder
    ~/code/app") is created when its task starts, rather than failing first.
    Only one level, only a path his own message names, and only where a task
    may work. Returns (workdir, problem, created)."""
    session = ctx.session
    text = getattr(session, "_active_request", "") or next(
        (m["content"] for m in reversed(session.messages)
         if m["role"] == "user" and isinstance(m["content"], str)), "")
    raw = (requested or "").strip().rstrip("/\\")
    if not raw or text.startswith(("[task report]", "[integration report]")):
        return None, problem, ""
    full = os.path.realpath(os.path.expanduser(raw))
    named = raw in text or full in text or full.replace(os.path.expanduser("~"), "~", 1) in text
    if not named or not os.path.isdir(os.path.dirname(full)) or os.path.exists(full):
        return None, problem, ""
    try:
        os.mkdir(full)
    except OSError as exc:
        return None, f"could not create the folder {raw}: {exc.strerror or exc}", ""
    workdir, refused = ctx.hub.resolve_workdir(full, title)
    if refused:
        os.rmdir(full)                        # not a place a task may work after all
        return None, refused, ""
    return workdir, None, full


def spawn_task(ctx, args):
    title = " ".join((args.get("title") or "").split())
    task = (args.get("task") or "").strip()
    if not task:
        return ("ERROR: <task> was empty. The task thread sees only what you "
                "write there, so it needs the full brief.")
    if not title:
        title = " ".join(task.split()[:8])
    store = ctx.session.core.skills
    wanted = [s for s in re.split(r"[,\s]+", (args.get("skills") or "").strip()) if s]
    unknown = [s for s in wanted if not store.get(s)]
    if unknown:
        return (f"ERROR: no skill named {', '.join(unknown)}. Available: "
                + (", ".join(store.names()) or "(none)"))
    requested = args.get("workdir", "")
    if not requested and ctx.session.profile == "controller":
        requested, problem = requested_workdir(ctx.session)
        if problem:
            return "ERROR: " + problem
    project = ctx.session.project()
    if not requested and project and project["path"] and os.path.isdir(project["path"]):
        requested = project["path"]          # a project chat's work happens in its folder
    workdir, problem = ctx.hub.resolve_workdir(requested, title)
    created = ""
    if problem and "not an existing folder" in problem:
        workdir, problem, created = _create_requested_folder(ctx, requested, title, problem)
    if problem:
        return f"ERROR: {problem}"
    thread = ctx.hub.spawn_task(title, task, workdir, parent=ctx.thread_id,
                                skills=wanted)
    filed = ctx.store.project(thread.get("project"))
    return (f"Started task {thread['id']}: {title}\n"
            + (f"Created the new folder {created}\n" if created else "")
            + f"Working in {workdir}\n"
            + (f"Project: {filed['name']}\n" if filed else "")
            + (f"Skills: {', '.join(wanted)}\n" if wanted else "")
            + f"Status: {thread['status']}. Its report will arrive here when it "
              f"ends; tell sir it is under way and do not wait for it.")


def list_tasks(ctx, args):
    rows = ctx.store.threads(kind="task", limit=15)
    if not rows:
        return "No tasks yet."
    names = {p["id"]: p["name"] for p in ctx.store.projects()}
    out = []
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["updated"]))
        where = f"  [{names[r['project']]}]" if names.get(r.get("project")) else ""
        out.append(f"{r['id']}  {r['status']:<9} {when}  {r['title']}{where}")
    return "\n".join(out)


def task_result(ctx, args):
    tid = (args.get("id") or "").strip()
    row = ctx.store.thread(tid)
    if not row or row["kind"] != "task":
        return f"ERROR: no task with id {tid!r}. Use list_tasks to see the ids."
    body = row["summary"] or "(no report yet)"
    project = ctx.store.project(row.get("project"))
    return (f"Task {row['id']}: {row['title']}\nStatus: {row['status']}\n"
            f"Working directory: {row['workdir']}\n"
            + (f"Project: {project['name']}\n" if project else "") + f"\n{body}")


HOME = ("home",)

TOOLS = [
    Tool("spawn_task", spawn_task, needs=("hub",), kinds=HOME),
    Tool("list_tasks", list_tasks, read_only=True, needs=("hub",), kinds=HOME),
    Tool("task_result", task_result, read_only=True, needs=("hub",), kinds=HOME),
]
