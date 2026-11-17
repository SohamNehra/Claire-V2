"""Skills: instructions for one kind of work, loaded when a task calls for it.

A skill is a markdown file with a name and a one-line description. Claire is
shown the list; when a task is the kind a skill covers, the skill's text is
added to that thread's instructions -- either up front, because whoever started
the task named it, or by Claire herself with use_skill. A thread that never
needs a skill never pays for it in context.

Built-in skills ship in this package. Yours live in the vault's `skills/`
folder, where you can edit them like any other note, and one of yours with the
same name replaces a built-in. Skills are instructions, so only you can write
them: there is no tool that lets a model create or change one.
"""

import os
import re

BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
MAX_CHARS = 40_000


class SkillError(ValueError):
    """A skill could not be saved. The message says why."""


def _parse(raw):
    """(frontmatter dict, body) of a skill file."""
    raw = raw.replace("\r\n", "\n")
    meta = {}
    m = re.match(r"\A---\n(.*?)\n---\n?", raw, re.DOTALL)
    body = raw
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip().strip("\"'")
        body = raw[m.end():]
    return meta, body.strip()


def clean_name(text):
    name = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:41]
    return name if _NAME.match(name) else ""


class Skills:
    def __init__(self, vault_path):
        self.user_dir = os.path.join(vault_path, "skills")

    def _files(self):
        """name -> (path, source), with yours taking the place of a built-in."""
        found = {}
        for folder, source in ((BUILTIN, "builtin"), (self.user_dir, "user")):
            for fn in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                if fn.endswith(".md") and _NAME.match(fn[:-3]):
                    found[fn[:-3]] = (os.path.join(folder, fn), source)
        return found

    def get(self, name):
        entry = self._files().get(name)
        if not entry:
            return None
        try:
            with open(entry[0], encoding="utf-8") as f:
                meta, body = _parse(f.read(MAX_CHARS * 2))
        except OSError:
            return None
        first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
        split = lambda v: [x for x in re.split(r"[,\s]+", v or "") if x]
        return {"name": name, "source": entry[1], "text": body,
                "description": meta.get("description") or first[:160], "size": len(body),
                # When to use it, in words the skill router reads.
                "when": meta.get("when", ""),
                # Skills that must be loaded with this one (building an
                # integration needs the coding skill).
                "requires": [n for n in split(meta.get("requires")) if _NAME.match(n) and n != name],
                # Which threads it is for: "agent" (task threads), "home" (the
                # chats with Claire). Most work happens in tasks.
                "kinds": [k for k in split(meta.get("kinds")) if k in ("agent", "home")] or ["agent"],
                # Steps a task needs for this work, when more than the usual
                # cap (MAX_STEPS). Capped, so a skill cannot ask for a runaway.
                "steps": min(max(int(meta["steps"]), 0), 150) if str(meta.get("steps", "")).isdigit() else 0,
                # A skill marked `always: true` starts every task with it loaded,
                # for instructions that should not depend on anyone's judgement.
                "always": meta.get("always", "").lower() in ("true", "yes", "1")}

    def list(self):
        out = []
        for name in sorted(self._files()):
            skill = self.get(name)
            if skill:
                out.append({k: skill[k] for k in ("name", "description", "source", "size",
                                                  "always", "when", "requires", "kinds")})
        return out

    def names(self):
        return sorted(self._files())

    def always(self):
        """Names of the skills every task starts with."""
        return [s["name"] for s in self.list() if s["always"]]

    def save(self, name, description, text, always=None, *, when=None, requires=None,
             kinds=None, steps=None):
        """Write one of your skills. Returns it as get() would."""
        name = (name or "").strip()
        if not _NAME.match(name):
            raise SkillError("a skill's name is 2-41 characters: lowercase letters, "
                             "digits and hyphens, starting with a letter")
        description = " ".join((description or "").split())[:300]
        if not description:
            raise SkillError("give the skill a one-line description: it is what Claire "
                             "reads when deciding whether a task needs it")
        text = (text or "").strip()
        if not text:
            raise SkillError("the skill has no instructions in it")
        if len(text) > MAX_CHARS:
            raise SkillError(f"the skill is {len(text):,} characters; the limit is "
                             f"{MAX_CHARS:,}, because all of it goes into the model's context")
        # Editing the body in the app must not discard the uploaded procedure's
        # routing, prerequisites or budget. An explicit value can still clear it.
        previous = self.get(name) or {}
        when = " ".join(str(previous.get("when", "") if when is None else when).split())[:1000]
        def names(value, fallback):
            if value is None:
                return previous.get(fallback, [])
            if isinstance(value, str):
                return re.split(r"[,\s]+", value.strip())
            if not isinstance(value, (list, tuple)) or not all(isinstance(n, str) for n in value):
                raise SkillError(f"{fallback} must be a list of names or comma-separated text")
            return list(value)
        requires = list(dict.fromkeys(n for n in names(requires, "requires") if n))
        kinds = list(dict.fromkeys(k for k in names(kinds, "kinds") if k)) or ["agent"]
        if any(not _NAME.fullmatch(n) for n in requires):
            raise SkillError("requires must contain skill names")
        if any(k not in ("agent", "home") for k in kinds):
            raise SkillError("kinds must contain agent or home")
        steps = previous.get("steps", 0) if steps is None else steps
        if not str(steps).isdigit() or not 0 <= int(steps) <= 150:
            raise SkillError("steps must be a number from 0 to 150")
        always = previous.get("always", False) if always is None else bool(always)
        os.makedirs(self.user_dir, exist_ok=True)
        with open(os.path.join(self.user_dir, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(f"---\nname: {name}\ndescription: {description}\n"
                    + f"when: {when}\nrequires: {', '.join(requires)}\nkinds: {', '.join(kinds)}\nsteps: {int(steps)}\n"
                    + ("always: true\n" if always else "") + f"---\n{text}\n")
        return self.get(name)

    def save_upload(self, filename, raw, name="", description="", always=None):
        """Save a skill from a file's contents. Its own header, if it has one,
        supplies the name and description unless they are given."""
        meta, body = _parse(raw or "")
        name = clean_name(name or meta.get("name") or os.path.splitext(
            os.path.basename(filename or ""))[0])
        first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
        if always is None:
            always = meta.get("always", "").lower() in ("true", "yes", "1")
        return self.save(name, description or meta.get("description") or first, body, always,
                         **{k: meta[k] for k in ("when", "requires", "kinds", "steps") if k in meta})

    def delete(self, name):
        """Remove one of yours. Built-in skills stay; returns False for those."""
        entry = self._files().get(name)
        if not entry or entry[1] != "user":
            return False
        os.remove(entry[0])
        return True
