"""Claire's long-term memory: a folder of plain markdown.

The markdown is the source of truth -- open the folder in Obsidian and read,
edit or delete anything. The SQLite full-text index beside it is only a cache,
rebuilt from the files whenever they change, and safe to delete.

Layers:
    identity/   who Claire is and who sir is. Loaded in full in the home thread.
    projects/   one folder per project, holding that project's own memory:
                PROJECT.md (what it is, conventions, decisions), work/ (what
                was done there, by date) and tasks/ (each finished task).
                Loaded when working in that project, and only then.
    learned/    reusable facts. Titles are always visible; bodies on recall.
    tasks/      what each finished task did, written when it ends.
    sessions/   what happened, by date.
    inbox/      notes for sir to act on, such as a tool that needs repair.
"""

import hashlib
import shutil
import os
import re
import sqlite3
import time

from .tools import PROMPTS
from .worklog import WorkLog

FOLDERS = ("identity", "projects", "learned", "tasks", "sessions", "inbox",
           "skills", "work")
PROJECT_FILE = "PROJECT.md"
INDEX_FILE = "MEMORY.md"
INDEX_SECTIONS = ("about", "recent", "projects", "notes", "sessions", "inbox")


def _first_line(text, limit=150):
    """A note's first line of real content: not its header, not a heading."""
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    for line in body.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "<!--")):
            line = re.sub(r"\s+", " ", line.lstrip("-*• ").strip())
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""

# The identity notes as first seeded, when Claire called him dad. A vault still
# holding exactly these has never been edited, so it can take the new wording;
# one that has been edited is left alone.
_FIRST_SEEDS = {"claire.md": "96cad5ad50a33a5e63222cfa221eac106aa620ee", "dad.md": "b0fc5e422c1fff761b2bf04a2609189948e42a35"}

VAULT_README = """---
tags: [claire]
---
# Claire's vault

Claire's long-term memory. Plain markdown -- open this folder as an Obsidian
vault and read or edit anything here; she picks up your changes next run.

- `identity/` -- who she is and who you are to her. Hers to add to, yours to edit.
- `projects/` -- one folder per project, with that project's own memory:
  `PROJECT.md` says what the project is and how work is done there (yours to
  edit), `work/` is the dated record of what was done, `tasks/` holds each
  finished task's report. Loaded when Claire works in that project.
- `learned/` -- reusable facts that apply anywhere. Titles are always visible
  to her; she reads a note in full only when relevant.
- `tasks/` -- what each finished task did, for work outside any project.
- `work/` -- the record across everything: one line of status for each task
  in every project, and work that belongs to no project.
- `sessions/` -- what happened, by date.
- `skills/` -- instructions for particular kinds of work, written by you. She
  loads one when a task calls for it; she cannot write or change them.
- `MEMORY.md` -- the shared index every chat and task reads: one line per
  memory, with the file its detail is in. Written by Claire; edit the files it
  points to, not the index.
- `inbox/` -- notes she has left for you, such as a tool that needs fixing.
  Set `status: done` in a note's header (or delete it) once you have dealt with it.

Delete anything that goes stale. She will not resurrect it.
"""


def slug(text):
    s = re.sub(r"[^\w\s-]", "", (text or "").lower()).strip()
    return re.sub(r"[\s_]+", "-", s)[:60] or "untitled"


def _frontmatter(text):
    """The `key: value` pairs of a note's YAML header, as a dict."""
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


class Vault:
    def __init__(self, path, index_path=None):
        self.path = path
        self.index_path = index_path
        self.work = WorkLog(path)
        self._project_logs = {}

    def init(self):
        for sub in FOLDERS:
            os.makedirs(os.path.join(self.path, sub), exist_ok=True)
        readme = os.path.join(self.path, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w", encoding="utf-8") as f:
                f.write(VAULT_README)
        identity = os.path.join(self.path, "identity")
        legacy = os.path.join(identity, "dad.md")
        if os.path.exists(legacy):
            if self._sha(legacy) == _FIRST_SEEDS["dad.md"]:
                os.remove(legacy)                       # reseeded below as sir.md
            elif not os.path.exists(os.path.join(identity, "sir.md")):
                shutil.move(legacy, os.path.join(identity, "sir.md"))
        own = os.path.join(identity, "claire.md")
        if os.path.exists(own) and self._sha(own) == _FIRST_SEEDS["claire.md"]:
            os.remove(own)
        # Seed the identity notes once. After that they belong to the vault:
        # editing the seed files in the repo changes nothing for an existing one.
        for name in ("claire", "sir"):
            target = os.path.join(self.path, "identity", f"{name}.md")
            if not os.path.exists(target):
                with open(os.path.join(PROMPTS, f"identity_{name}.md"),
                          encoding="utf-8") as f:
                    seed = f.read()
                with open(target, "w", encoding="utf-8") as f:
                    f.write(seed)

    @staticmethod
    def _sha(path):
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()

    def _read(self, *parts):
        try:
            with open(os.path.join(self.path, *parts), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    # -- projects ------------------------------------------------------------
    # Each project has a folder of its own under projects/. Nothing outside
    # that folder is loaded as "this project's memory".

    def _adopt_flat(self, name):
        """Earlier versions kept a project as one file, projects/<name>.md.
        Move it into the project's folder the first time it is needed."""
        flat = os.path.join(self.path, "projects", f"{name}.md")
        brief = os.path.join(self.path, "projects", name, PROJECT_FILE)
        if os.path.isfile(flat) and not os.path.exists(brief):
            os.makedirs(os.path.dirname(brief), exist_ok=True)
            os.replace(flat, brief)

    def project_folder(self, workdir):
        """The name of the memory folder for a working directory.

        Named after the folder on disk. Two projects whose folders share a
        name would have shared their memory, so PROJECT.md records the path
        it belongs to, and a second project with the same name gets a
        suffixed folder.
        """
        workdir = os.path.realpath(workdir)
        base = slug(os.path.basename(workdir))
        suffixed = f"{base}-{hashlib.sha1(workdir.encode()).hexdigest()[:6]}"
        self._adopt_flat(base)
        self._adopt_flat(suffixed)
        text = self._read("projects", base, PROJECT_FILE)
        if text:
            head = _frontmatter(text)
            owner = head.get("path")
            if head.get("group") == "true" or (
                    owner and os.path.realpath(os.path.expanduser(owner)) != workdir):
                return suffixed
        return base

    def project_note(self, workdir):
        """PROJECT.md for a working directory."""
        return os.path.join(self.path, "projects", self.project_folder(workdir), PROJECT_FILE)

    def group_folder(self, name):
        """A new memory folder for a project that has no folder on disk."""
        base = slug(name)
        taken = os.path.join(self.path, "projects")
        candidate, n = base, 2
        while (os.path.exists(os.path.join(taken, candidate))
               or os.path.exists(os.path.join(taken, candidate + ".md"))):
            candidate, n = f"{base}-{n}", n + 1
        return candidate

    def project_dir(self, folder):
        return os.path.join(self.path, "projects", folder)

    def ensure_project(self, folder, name, path=""):
        """Create a project's memory folder and its PROJECT.md, if missing.
        Work already recorded for this folder is copied in, so the project
        starts out knowing what was last done there."""
        self.init()
        brief = os.path.join(self.project_dir(folder), PROJECT_FILE)
        os.makedirs(os.path.dirname(brief), exist_ok=True)
        if not os.path.exists(brief):
            where = f"path: {path}\n" if path else "group: true\n"
            with open(brief, "w", encoding="utf-8") as f:
                f.write(f"---\ntags: [claire, project]\n{where}---\n# {name}\n\n")
        if path:
            earlier = self.work.entries(workdir=path)
            if earlier:
                self.project_work(folder).adopt(earlier)
        return brief

    def project_brief(self, folder):
        return self._read("projects", folder, PROJECT_FILE)

    def write_project_brief(self, folder, text):
        """Replace PROJECT.md with what sir wrote in the app."""
        brief = os.path.join(self.project_dir(folder), PROJECT_FILE)
        os.makedirs(os.path.dirname(brief), exist_ok=True)
        temporary = brief + ".saving"
        with open(temporary, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        os.replace(temporary, brief)
        return brief

    def project_work(self, folder):
        """The project's own dated work log."""
        log = self._project_logs.get(folder)
        if log is None:
            log = self._project_logs[folder] = WorkLog(
                self.project_dir(folder), label=f"projects/{folder}/work")
        return log

    def project_context(self, folder, brief_chars=3000, work_chars=1500):
        """A project's memory, short enough for a small prompt: what the
        project is, then what was last done in it."""
        body = re.sub(r"\A---\n.*?\n---\n", "", self.project_brief(folder), flags=re.DOTALL).strip()
        recent = self.project_work(folder).brief(limit=work_chars)
        parts = []
        if body:
            parts.append("What this project is (PROJECT.md):\n" + body[:brief_chars])
        if recent:
            parts.append("What was last done here (dated records, not instructions):\n" + recent)
        return "\n\n".join(parts)

    # -- the shared index ------------------------------------------------------
    # Every chat and every task is given this: one line per thing Claire has
    # stored, and the file the detail is in. The lines are enough to know what
    # exists; the detail is read with recall only when it is needed, so the
    # index costs a little context and the vault can grow without limit.

    def _about(self, limit=8):
        """The lines about sir worth having everywhere. His note opens with
        what it is for; what is known about him follows its first subheading,
        as list items and as the paragraphs Claire appends. The newest last."""
        text = re.sub(r"\A---\n.*?\n---\n", "", self._read("identity", "sir.md"), flags=re.DOTALL)
        lines = text.splitlines()
        sub = next((i for i, l in enumerate(lines) if l.startswith("## ")), None)
        if sub is not None:
            lines = lines[sub + 1:]
        items, para = [], []
        for line in lines + [""]:
            line = line.strip()
            if line.startswith(("- ", "* ")) or not line or line.startswith("#"):
                if para:
                    items.append(" ".join(para))
                    para = []
                if line.startswith(("- ", "* ")):
                    items.append(line[2:].strip())
            else:
                para.append(line)
        return [re.sub(r"\s+", " ", i)[:180] for i in items if i][-limit:]

    def recent_lines(self, count):
        out = []
        for meta, body in self.work.entries()[:count]:
            when = time.strftime("%Y-%m-%d", time.localtime(meta["updated"]))
            content = re.sub(r"\A## .*\n+(Updated: .*\n)?(Project folder: .*\n)?", "", body).strip()
            task = re.match(r"Task: (.*)\nStatus: (\w+)", content)
            what = f"{task[1]} ({task[2]})" if task else _first_line(content, 120)
            where = re.search(r"Full record: (\S+)", body)
            label = meta.get("project") or "no project"
            # Work done in a fresh scratch folder is named after the folder,
            # which says nothing; it belongs to no project.
            if re.match(r"\d{8}-\d{6}-", label) or os.sep + "claire-work" + os.sep in meta.get("workdir", ""):
                label = "no project"
            out.append(f"- {when} · {label} · {what} → "
                       + (where[1] if where else "work/recent.md"))
        return out

    def memory_index(self, projects=(), sections=INDEX_SECTIONS, recent=6):
        """The shared index as text. `projects` are the project rows (name,
        path, slug); `sections` picks which parts to include."""
        self.init()
        home = os.path.realpath(os.path.expanduser("~"))
        parts = []
        if "about" in sections:
            about = self._about()
            parts.append("## About sir → identity/sir.md\n"
                         + ("\n".join(f"- {l}" for l in about) or "- Nothing recorded yet."))
        if "recent" in sections:
            lines = self.recent_lines(recent)
            if lines:
                parts.append("## Recent work, newest first\n" + "\n".join(lines))
        if "projects" in sections and projects:
            lines = []
            for p in list(projects)[:15]:
                where = (" (" + p["path"].replace(home, "~", 1) + ")") if p.get("path") else ""
                what = _first_line(self.project_brief(p["slug"]), 110) or "no description yet"
                lines.append(f"- {p['name']}{where}: {what} → projects/{p['slug']}/PROJECT.md")
            parts.append("## Projects\n" + "\n".join(lines))
        if "notes" in sections:
            ldir = os.path.join(self.path, "learned")
            names = sorted(fn for fn in os.listdir(ldir) if fn.endswith(".md"))[:30] if os.path.isdir(ldir) else []
            lines = [f"- {fn[:-3]}: {_first_line(self._read('learned', fn), 110)} → learned/{fn}" for fn in names]
            if lines:
                parts.append("## Things learned\n" + "\n".join(lines))
        if "sessions" in sections:
            sdir = os.path.join(self.path, "sessions")
            names = sorted((fn for fn in os.listdir(sdir) if fn.endswith(".md")), reverse=True)[:3] if os.path.isdir(sdir) else []
            if names:
                parts.append("## Recent sessions\n" + "\n".join(
                    f"- {fn[:-3]}: {_first_line(self._read('sessions', fn), 110)} → sessions/{fn}" for fn in names))
        if "inbox" in sections:
            waiting = self.inbox_open()
            if waiting:
                parts.append("## Waiting for sir\n" + "\n".join(f"- {t} → inbox/{n}.md" for n, t in waiting[:8]))
        if not parts:
            return ""
        return ("# Shared memory index\n\nOne line per memory, and the file its detail is in. "
                "These lines are all you have been given: to use a detail, read its file with "
                "recall and the path (for example recall projects/x/PROJECT.md), or search with "
                "recall and words. Do not guess what a file says.\n\n" + "\n\n".join(parts))

    def write_index(self, projects=()):
        """Keep MEMORY.md on disk in step with the vault, for sir to read.
        Returns the full index."""
        text = self.memory_index(projects)
        path = os.path.join(self.path, INDEX_FILE)
        body = ("---\ntags: [claire, index]\n---\n" + text.replace(
            "# Shared memory index\n", "# Shared memory index\n\n_Written by Claire from the vault. "
            "Edit the files it points to; this index is rewritten from them._\n", 1) + "\n")
        if self._read(INDEX_FILE) != body:
            temporary = path + ".writing"
            with open(temporary, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(temporary, path)
        return text

    # -- what gets loaded into a prompt -------------------------------------

    def _learned_titles(self):
        ldir = os.path.join(self.path, "learned")
        if not os.path.isdir(ldir):
            return []
        return [fn[:-3] for fn in sorted(os.listdir(ldir))[:60] if fn.endswith(".md")]

    def load_memory(self, workdir, folder=None):
        """The memory block for a working thread: this project, plus titles.
        `folder` names the project's memory when the thread belongs to one;
        otherwise it is found from the working directory."""
        self.init()
        parts = []
        folder = folder or self.project_folder(workdir)
        body = self.project_brief(folder)[:6000]
        if body:
            parts.append(f"## What you know about this project\n\n{body}")
        learned = self._learned_titles()
        if learned:
            parts.append("## Notes you can recall\n\n"
                         + ", ".join(f"[[{n}]]" for n in learned)
                         + "\n\nUse <recall> to read any of these in full.")
        own = self.project_work(folder)
        # Work recorded before projects had logs of their own is still found
        # by its folder in the shared log.
        parts.append(own.context() if own.entries()
                     else self.work.context(os.path.realpath(workdir)))
        return ("\n\n# Memory\n\nFrom your vault. Trust it, but verify anything that "
                "looks stale against the actual files.\n\n" + "\n\n".join(parts))

    def identity(self):
        self.init()
        notes = [re.sub(r"\A---\n.*?\n---\n", "", self._read("identity", fn),
                        flags=re.DOTALL).strip()
                 for fn in ("claire.md", "sir.md")]
        return "\n\n".join(n for n in notes if n)

    def load_home_memory(self, folder=None, name="", board=""):
        """The memory block for a conversation: titles and what is waiting.

        A chat inside a project (`folder`) gets that project's memory. A chat
        outside any project gets the record across all of them. `board` is the
        list of work under way everywhere, which every chat is shown."""
        parts = []
        learned = self._learned_titles()
        if learned:
            parts.append("## Notes you can recall\n\n"
                         + ", ".join(f"[[{n}]]" for n in learned)
                         + "\n\nUse <recall> to read any of these in full.")
        waiting = self.inbox_open()
        if waiting:
            parts.append("## Waiting in sir's inbox\n\n"
                         + "\n".join(f"- [[{name}]]: {title}" for name, title in waiting)
                         + "\n\nMention these when it is relevant; he may not have seen them.")
        if folder:
            body = self.project_brief(folder)[:6000]
            parts.append(f"## This chat belongs to the project: {name or folder}\n\n"
                         + (body or "Nothing is written about this project yet."))
            parts.append(self.project_work(folder).context())
        else:
            parts.append(self.work.context())
        if board:
            parts.append(board)
        return "\n\n# Memory\n\nFrom your vault.\n\n" + "\n\n".join(parts)

    # -- writing -------------------------------------------------------------

    def remember(self, kind, content, note="", workdir=None, home=False,
                 folder=None, name=""):
        """Append a durable note. Returns a one-line result for the model.

        `folder` is the memory folder of the project the thread belongs to,
        and `name` that project's name. A working thread with no project is
        filed by its working directory, as before."""
        self.init()
        if not folder and not home and workdir:
            folder = self.project_folder(workdir)
        if kind == "work":
            if folder:
                return self.project_work(folder).record(
                    note or name or (os.path.basename(workdir) if workdir and not home else folder),
                    content, workdir=os.path.realpath(workdir) if workdir and not home else "",
                    source="Claire work note")
            return self.work.record(note or "Project work", content, source="Claire work note")
        if kind not in ("project", "learned", "session", "sir", "self"):
            kind = "project"
        if kind in ("sir", "self") and not home:
            return ("ERROR: notes about sir or about yourself are kept by the "
                    "home thread. Save this as kind `learned` or `project`.")
        if kind == "project" and not folder:
            kind = "learned"       # a chat outside any project has none to file it under

        header = None
        if kind == "project":
            path = os.path.join(self.project_dir(folder), PROJECT_FILE)
            title = name or (os.path.basename(os.path.realpath(workdir)) if workdir and not home else folder)
            where = (f"path: {os.path.realpath(workdir)}\n" if workdir and not home else "group: true\n")
            header = f"---\ntags: [claire, project]\n{where}---\n# {title}\n\n"
            os.makedirs(os.path.dirname(path), exist_ok=True)
        elif kind == "session":
            title = time.strftime("%Y-%m-%d")
            path = os.path.join(self.path, "sessions", f"{title}.md")
        elif kind in ("sir", "self"):
            path = os.path.join(self.path, "identity",
                                "sir.md" if kind == "sir" else "claire.md")
            title = kind
        else:
            # A short topic title keeps notes findable; fall back to the first
            # few words rather than a 40-character sentence fragment.
            note = note or " ".join(content.split()[:4])
            path = os.path.join(self.path, "learned", f"{slug(note)}.md")
            title = note

        new = not os.path.exists(path)
        if not new:
            with open(path, encoding="utf-8") as f:
                if content in f.read():
                    return f"Already in memory: {os.path.relpath(path, self.path)}"
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(header or f"---\ntags: [claire, {kind}]\n---\n# {title}\n\n")
            f.write(f"{content}\n\n")
        rel = os.path.relpath(path, self.path)
        if kind == "project":
            self.project_work(folder).record(
                title, content, workdir=os.path.realpath(workdir) if workdir and not home else "",
                source="Project note")
        return f"{'Created' if new else 'Updated'} memory: {rel}"

    def write_task(self, title, body, status, workdir, thread_id, folder=None):
        """Record what a finished task did. Returns the note's path. A task
        in a project is filed in that project's folder."""
        self.init()
        name = f"{time.strftime('%Y-%m-%d')}-{slug(title)[:40]}-{thread_id[-4:]}.md"
        where = os.path.join(self.project_dir(folder), "tasks") if folder else os.path.join(self.path, "tasks")
        os.makedirs(where, exist_ok=True)
        path = os.path.join(where, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\ntags: [claire, task]\nstatus: {status}\n"
                    f"workdir: {workdir}\nthread: {thread_id}\n---\n# {title}\n\n"
                    f"{body.strip()}\n")
        return path

    # -- inbox ---------------------------------------------------------------

    def inbox_note(self, name, title, entry):
        """Add an entry to a note for sir, creating or reopening it."""
        self.init()
        path = os.path.join(self.path, "inbox", f"{slug(name)}.md")
        stamp = time.strftime("%Y-%m-%d %H:%M")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            text = re.sub(r"(?m)^status:.*$", "status: open", text, count=1)
        else:
            text = f"---\ntags: [claire, inbox]\nstatus: open\n---\n# {title}\n"
        text = text.rstrip() + f"\n\n## {stamp}\n\n{entry.strip()}\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def inbox_open(self):
        """(name, title) for every inbox note not yet marked done."""
        idir = os.path.join(self.path, "inbox")
        out = []
        for fn in sorted(os.listdir(idir)) if os.path.isdir(idir) else []:
            if not fn.endswith(".md"):
                continue
            text = self._read("inbox", fn)
            if _frontmatter(text).get("status", "open").lower() in ("done", "closed"):
                continue
            m = re.search(r"(?m)^# (.+)$", text)
            out.append((fn[:-3], m.group(1) if m else fn[:-3]))
        return out

    # -- browsing ------------------------------------------------------------

    def tree(self):
        """Every note, for the app's memory browser."""
        self.init()
        out = []
        for fp in self._notes(index=True):
            rel = os.path.relpath(fp, self.path).replace(os.sep, "/")
            folder = os.path.dirname(rel) or "."
            text = self._read(rel)
            m = re.search(r"(?m)^# (.+)$", text)
            out.append({"path": rel, "folder": folder,
                        "title": m.group(1) if m else os.path.basename(rel)[:-3],
                        "status": _frontmatter(text).get("status", ""),
                        "modified": os.path.getmtime(fp), "size": len(text)})
        return out

    def read_note(self, rel):
        """A note's text, or None if rel is not a note inside the vault."""
        root = os.path.realpath(self.path)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not full.endswith(".md"):
            return None
        if not os.path.isfile(full):
            return None
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read()

    # -- recall --------------------------------------------------------------

    def _notes(self, index=False):
        """Every note. The index is left out of searches, which should find
        the notes it points to, not the pointer."""
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in sorted(files):
                if fn.endswith(".md") and fn != "README.md" and (
                        index or not (fn == INDEX_FILE and root == self.path)):
                    yield os.path.join(root, fn)

    def recall(self, query, prefer=None):
        """Search the vault and return the best-matching notes. Notes from
        the project folder `prefer` come first: a thread asking about its own
        project should not be answered from another one."""
        self.init()
        wanted = query.strip().strip("`'\"").lstrip("/")
        if wanted.endswith(".md") and os.path.basename(wanted) != INDEX_FILE:
            text = self.read_note(wanted)
            if text is not None:
                return f"### {wanted}\n" + text[:12000] + ("\n…(truncated)" if len(text) > 12000 else "")
            return (f"No note at {wanted}. Search with recall and words, or check the "
                    "path in the shared memory index.")
        hits = None
        if self.index_path:
            try:
                hits = self._recall_indexed(query)
            except sqlite3.Error:
                hits = None                 # no FTS5, or a damaged cache
        if hits is None:
            hits = self._recall_scan(query)
        if prefer:
            own = f"projects/{prefer}/"
            hits = sorted(hits, key=lambda hit: not hit[0].replace(os.sep, "/").startswith(own))
        if not hits:
            return (f"No notes matching {query!r}. Nothing remembered about this "
                    f"yet -- explore, then use <remember> to save what you learn.")
        return "\n\n".join("### " + rel.replace(os.sep, "/") + "\n" + self._excerpt(body, query)
                           for rel, body in hits[:5])

    @staticmethod
    def _excerpt(body, query, limit=2500):
        if len(body)<=limit:
            return body
        words=sorted(set(re.findall(r"\w+",query.lower())),key=len,reverse=True)
        lower=body.lower()
        index=lower.find(query.lower())
        if index<0:
            index=next((lower.find(word) for word in words if lower.find(word)>=0),0)
        start=max(0,index-250)
        return ("…\n" if start else "")+body[start:start+limit]

    def _recall_indexed(self, query):
        words = [w for w in re.split(r"[^\w]+", query.lower()) if len(w) >= 2]
        if not words:
            return []
        db = sqlite3.connect(self.index_path, timeout=10)
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes "
                       "USING fts5(path UNINDEXED, title, body)")
            db.execute("CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, mtime REAL)")
            # Bring the cache in line with the files: they are the truth.
            seen = dict(db.execute("SELECT path, mtime FROM seen"))
            current = {}
            for fp in self._notes():
                rel = os.path.relpath(fp, self.path)
                current[rel] = os.path.getmtime(fp)
                if seen.get(rel) != current[rel]:
                    with open(fp, encoding="utf-8", errors="ignore") as f:
                        body = f.read()
                    db.execute("DELETE FROM notes WHERE path = ?", (rel,))
                    title = os.path.basename(rel)[:-3]
                    if os.path.basename(rel) == PROJECT_FILE:   # found by the project's name
                        title = os.path.basename(os.path.dirname(rel))
                    db.execute("INSERT INTO notes VALUES (?, ?, ?)", (rel, title, body))
                    db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?)",
                               (rel, current[rel]))
            for rel in set(seen) - set(current):
                db.execute("DELETE FROM notes WHERE path = ?", (rel,))
                db.execute("DELETE FROM seen WHERE path = ?", (rel,))
            db.commit()
            # Prefix terms, so "llama-cpp" still finds a note on llama-server.
            terms = set()
            for w in words:
                terms.add(f'"{w}"*')
                if len(w) >= 6:
                    terms.add(f'"{w[:5]}"*')
            rows = db.execute(
                "SELECT path, body FROM notes WHERE notes MATCH ? "
                "ORDER BY bm25(notes, 0.0, 4.0, 1.0) LIMIT 12",
                (" OR ".join(sorted(terms)),)).fetchall()
            return [(rel, body) for rel, body in rows]
        finally:
            db.close()

    def _recall_scan(self, query):
        """v1's word scoring, used when the index is unavailable."""
        words = [w for w in re.split(r"[^\w]+", query.lower()) if len(w) >= 3]
        phrase = query.lower()
        scored = []
        for fp in self._notes():
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    body = f.read()
            except OSError:
                continue
            hay = (body + " " + os.path.basename(fp)).lower()
            score = 10 if phrase in hay else 0
            score += sum(2 for w in words if w in hay)
            score += sum(1 for w in words if len(w) >= 5 and w[:5] in hay)
            if score:
                scored.append((score, os.path.relpath(fp, self.path), body))
        scored.sort(key=lambda t: -t[0])
        return [(rel, body) for _, rel, body in scored]
