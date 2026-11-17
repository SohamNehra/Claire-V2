"""Projects: folders of chats and tasks, each with a memory of its own.

A project is usually a folder on disk -- a repo sir is working in. Every chat
and task that belongs to it is listed under it, and they share one memory
folder in the vault (PROJECT.md, a work log, task reports) that no other
project sees. A project can also be a plain group with no folder.

Tasks find their project by where they work, so a project appears the first
time real work is done in a folder; nobody has to set it up first.
"""

import os
import tempfile
import time
import sys

from . import host


# Folders of the home directory that hold many things rather than being one
# project. Work done directly in one of these is not filed as a project.
CONTAINERS = {"Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music",
              "Public", "Library", "Applications", "Sites", "iCloud Drive", "OneDrive"}


def scratch_base():
    """Where a task with no folder of its own is given a fresh one."""
    return os.path.join(os.path.realpath(os.path.expanduser("~")), "claire-work")


def display_name(path):
    """A readable project name from its folder: "invoice-builder" becomes
    "Invoice Builder". Words that already have capitals keep them ("iOS-app"
    becomes "iOS App"), so a name someone chose on purpose is not mangled."""
    words = os.path.basename(os.path.normpath(path)).replace("_", " ").replace("-", " ").split()
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in words) or os.path.basename(path)


class ProjectError(Exception):
    pass


class Projects:
    def __init__(self, core):
        self.core = core
        self.store, self.vault, self.settings = core.store, core.vault, core.settings

    # -- finding -------------------------------------------------------------

    def get(self, pid):
        return self.store.project(pid)

    def of_thread(self, tid):
        """The project a thread belongs to, or None."""
        row = self.store.thread(tid) if tid else None
        return self.store.project(row["project"]) if row and row.get("project") else None

    def root_for(self, workdir):
        """The folder a new project for `workdir` should be: the repository
        it sits in when there is one, otherwise the folder itself."""
        home = os.path.realpath(os.path.expanduser("~"))
        at = workdir
        while (at != os.path.dirname(at) if sys.platform == "win32" else at.startswith(home + os.sep)):
            if os.path.exists(os.path.join(at, ".git")):
                return at
            at = os.path.dirname(at)
        return workdir

    def _own_folder(self, path):
        """Whether a folder is one a project should be made for. Scratch
        folders, temporary folders and Claire's own state are not."""
        home = os.path.realpath(os.path.expanduser("~"))
        if not os.path.isdir(path):
            return False
        if sys.platform == "win32":
            if path == os.path.dirname(path) or os.path.normcase(path) == os.path.normcase(home):
                return False
            if any(os.path.normcase(path) == os.path.normcase(p) for p in host.locations().values()):
                return False
        elif not path.startswith(home + os.sep):
            return False
        if os.path.dirname(path) == home and os.path.basename(path) in CONTAINERS:
            return False
        outside = [scratch_base(), os.path.realpath(tempfile.gettempdir()),
                   os.path.realpath(self.settings.state_dir),
                   os.path.realpath(self.settings.vault)]
        return not any(path == p or path.startswith(p + os.sep) for p in outside)

    def for_task(self, workdir, parent=None):
        """The project a task working in `workdir` belongs to.

        An existing project that holds the folder wins. A fresh scratch folder
        belongs with the chat that asked for the work. Any other folder of
        sir's becomes a project of its own.
        """
        workdir = os.path.realpath(workdir)
        found = self.store.project_for_path(workdir)
        if found:
            return found
        scratch = scratch_base()
        if workdir == scratch or workdir.startswith(scratch + os.sep):
            return self.of_thread(parent)
        if not self._own_folder(workdir):
            return None
        root = self.root_for(workdir)
        if not self._own_folder(root):
            root = workdir
        return self.store.project_for_path(root) or self._add(display_name(root), root)

    # -- changing ------------------------------------------------------------

    def _add(self, name, path):
        slug = self.vault.project_folder(path) if path else self.vault.group_folder(name)
        self.vault.ensure_project(slug, name, path)
        pid = self.store.create_project(name, path, slug)
        return self.store.project(pid)

    def create(self, name, path=""):
        """Add a project by hand. `path` must already have been checked."""
        name = " ".join((name or "").split())[:80]
        path = os.path.realpath(path) if path else ""
        if path:
            for row in self.store.projects():
                if row["path"] == path:
                    raise ProjectError(f"the project “{row['name']}” already uses that folder")
        name = name or (display_name(path) if path else "")
        if not name:
            raise ProjectError("a project needs a name or a folder")
        return self._add(name, path)

    def rename(self, pid, name):
        name = " ".join((name or "").split())[:80]
        if not name:
            raise ProjectError("a project needs a name")
        self.store.update_project(pid, name=name)
        return self.store.project(pid)

    def touch(self, pid):
        if pid:
            self.store.update_project(pid, updated=time.time())

    def remove(self, pid):
        """Take a project off the list. Its chats and tasks stay, unfiled, and
        its memory folder is left in the vault for sir to keep or delete."""
        return self.store.delete_project(pid)

    def adopt_existing(self):
        """File tasks that ran before projects existed under the folders they
        worked in. Only tasks with no project are touched, and only once a
        folder still exists."""
        cache = {}
        for row in self.store.threads(kind="task", limit=500):
            if row.get("project") or not row.get("workdir"):
                continue
            workdir = os.path.realpath(row["workdir"])
            if workdir not in cache:
                try:
                    cache[workdir] = self.for_task(workdir) if os.path.isdir(workdir) else None
                except OSError:
                    cache[workdir] = None
            project = cache[workdir]
            if project:
                self.store.file_thread(row["id"], project["id"])

    # -- reading -------------------------------------------------------------

    def summary(self, row):
        """A project as the app lists it."""
        threads = self.store.project_threads(row["id"])
        active = [t for t in threads if t["kind"] == "task"
                  and t["status"] in ("queued", "running", "waiting")]
        latest = threads[0] if threads else None
        return dict(row, chats=sum(1 for t in threads if t["kind"] == "home"),
                    tasks=sum(1 for t in threads if t["kind"] == "task"),
                    active=len(active),
                    last_activity=max([row["updated"]] + [t["updated"] for t in threads[:1]]),
                    last_title=latest["title"] if latest else "",
                    last_status=latest["status"] if latest else "",
                    memory=f"projects/{row['slug']}")

    def list(self):
        rows = [self.summary(r) for r in self.store.projects()]
        return sorted(rows, key=lambda r: r["last_activity"], reverse=True)

    def detail(self, pid):
        row = self.store.project(pid)
        if not row:
            return None
        log = self.vault.project_work(row["slug"])
        return dict(self.summary(row), brief=self.vault.project_brief(row["slug"]),
                    work=[{"updated": meta["updated"], "source": meta.get("source", ""),
                           "text": body} for meta, body in log.entries()[:12]])

    def memory_index(self, sections=None):
        """The shared index of everything stored, for any chat or task. Also
        keeps MEMORY.md in the vault up to date."""
        rows = sorted(self.store.projects(), key=lambda r: r["updated"], reverse=True)
        full = self.vault.write_index(rows)
        if sections is None:
            return full
        return self.vault.memory_index(rows, sections=sections)

    def active_work(self):
        """Every task under way, in every project, with where it belongs."""
        return [dict(r, project_name=r["project_name"] or "") for r in self.store.active_work()]

    def board(self, exclude=None):
        """The work under way everywhere and the projects that exist, as text
        for a conversation's memory. `exclude` is the project the chat is
        already in, whose detail it has been given separately."""
        active = self.active_work()
        lines = ["## Work across all projects", ""]
        if active:
            lines.append("Active now (live from the task list, not from memory):")
            for r in active:
                lines.append(f"- {r['id']} [{r['status']}] {r['title']}"
                             f" -- project: {r['project_name'] or 'none'}")
        else:
            lines.append("No task is queued or running in any project right now.")
        rows = [r for r in self.list() if r["id"] != exclude][:12]
        if rows:
            lines += ["", "Projects (each keeps its own memory; use recall with a "
                          "project's name to read it):"]
            for r in rows:
                when = time.strftime("%Y-%m-%d", time.localtime(r["last_activity"]))
                last = f"; last: {r['last_title']} ({r['last_status']})" if r["last_title"] else ""
                where = f" at {r['path']}" if r["path"] else ""
                lines.append(f"- {r['name']}{where} -- last worked {when}{last}")
        return "\n".join(lines)
