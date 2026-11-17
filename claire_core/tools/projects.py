"""The projects tool: Claire organises sir's projects when he asks her to.

Nothing here deletes a file. Removing a project takes it off the list; its
folder on disk, its memory folder in the vault, and its chats and tasks stay.
"""

from . import Tool
from ..projects import ProjectError

ACTIONS = ("list", "create", "remove", "rename", "move_chat")


def _find(store, name):
    """A project by its exact name (any case), its memory folder name or id."""
    wanted = " ".join((name or "").split()).casefold()
    if not wanted:
        return None
    for row in store.projects():
        if wanted in (row["id"].casefold(), row["name"].casefold(), row["slug"].casefold()):
            return row
    return None


def _names(store):
    rows = store.projects()
    return ", ".join(r["name"] for r in rows) if rows else "(none yet)"


def projects(ctx, args):
    action = (args.get("action") or "list").strip().lower()
    if action == "delete":
        action = "remove"
    if action not in ACTIONS:
        return f"ERROR: action must be one of {', '.join(ACTIONS)}"
    core, store, hub = ctx.session.core, ctx.store, ctx.hub
    name = " ".join((args.get("name") or "").split())

    if action == "list":
        rows = core.projects.list()
        if not rows:
            return "No projects yet."
        return "\n".join(
            f"- {r['name']}" + (f" ({r['path']})" if r["path"] else " (no folder)")
            + f": {r['chats']} chats, {r['tasks']} tasks, {r['active']} active"
            + f"; memory projects/{r['slug']}/PROJECT.md" for r in rows)

    if action == "create":
        raw = (args.get("path") or "").strip()
        path = ""
        if raw:
            path, problem = hub.resolve_workdir(raw, "project")
            if problem:
                return "ERROR: " + problem.replace("workdir", "folder")
        try:
            made = core.projects.create(name, path)
        except ProjectError as exc:
            return f"ERROR: {exc}"
        hub.projects_changed()
        return (f"Created the project {made['name']}"
                + (f" for the folder {made['path']}" if made["path"] else " (a group with no folder)")
                + f". Its memory is projects/{made['slug']}/PROJECT.md. It is in sir's sidebar now.")

    project = _find(store, name)
    if action in ("remove", "rename") and not project:
        return f"ERROR: no project named {name!r}. Projects: {_names(store)}"

    if action == "remove":
        core.projects.remove(project["id"])
        hub.projects_changed()
        return (f"Removed {project['name']} from the projects list. Nothing was deleted: "
                + (f"its folder {project['path']}, " if project["path"] else "")
                + f"its memory (projects/{project['slug']}/) and its chats and tasks are all kept; "
                "the chats and tasks are now outside any project. You cannot delete folders.")

    if action == "rename":
        try:
            renamed = core.projects.rename(project["id"], args.get("new_name") or "")
        except ProjectError as exc:
            return f"ERROR: {exc}"
        hub.projects_changed()
        return f"Renamed {project['name']} to {renamed['name']}."

    # move_chat: file the chat this conversation is in.
    row = store.thread(ctx.thread_id)
    if not row or row["kind"] != "home":
        return "ERROR: only a chat can be moved; a task belongs to the folder it works in."
    if name.casefold() in ("", "none", "no project"):
        store.file_thread(ctx.thread_id, "")
        hub.projects_changed()
        return "This chat is now outside any project."
    if not project:
        return f"ERROR: no project named {name!r}. Projects: {_names(store)}"
    store.file_thread(ctx.thread_id, project["id"])
    hub.projects_changed()
    return (f"This chat is now in {project['name']}. From your next reply you are given "
            "its memory, and its tasks work in its folder.")


TOOLS = [Tool("projects", projects, needs=("hub",), kinds=("home",))]
