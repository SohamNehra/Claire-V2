"""Choosing the skills a request needs, with a small model.

Skills are step-by-step instructions for kinds of work. Listing every skill's
description in every prompt costs the main model context it needs for the work,
and a long list is exactly what a busy model skims. So a small model (the
clerk) reads the catalogue and the request and names the skills that apply;
only those are loaded, and the main model sees the others by name alone.

A skill can name others it `requires` (building an integration requires the
coding skill), and those come with it. Anything that goes wrong here -- no
small model, a reply that is not JSON, a name that does not exist -- picks
nothing: routing is a convenience, never a gate.
"""

import json
import re

from . import boot

ROLE = "clerk"
MAX_PICK = 3

PROMPT = """You decide which instruction sets ("skills") an assistant must follow before it starts a piece of work.

Skills:
{catalogue}

The request:
<<<
{request}
>>>

Name every skill that covers part of this work, at most {n}. A skill fits when the request is the kind of work its description says. Name none when none fits: a wrong skill costs more than a missing one.

Answer with JSON only, nothing else: {{"skills": ["name", ...]}}"""


def _catalogue(pool):
    lines = []
    for s in pool:
        line = f"- {s['name']}: {s['description']}"
        if s.get("when"):
            line += f" Use when: {s['when']}"
        lines.append(line)
    return "\n".join(lines)


def _parse(reply, pool):
    names = {s["name"] for s in pool}
    found = []
    match = re.search(r"\{.*\}", reply or "", re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            found = [n for n in (data.get("skills") or []) if isinstance(n, str)]
        except (ValueError, AttributeError):
            found = []
    if not match:                       # a bare list of names is close enough
        found = re.findall(r"[a-z][a-z0-9-]{1,40}", reply or "")
    out = []
    for name in found:
        name = name.strip().strip("`").lower()
        if name in names and name not in out:
            out.append(name)
    return out[:MAX_PICK]


def with_requirements(store, names):
    """The names, each followed by the skills it requires, without repeats."""
    out, visiting = [], set()

    def add(name):
        skill = store.get(name)
        if not skill or name in out or name in visiting:
            return
        visiting.add(name)
        for need in skill.get("requires") or []:
            add(need)
        visiting.remove(name)
        out.append(name)
    for name in names:
        add(name)
    return out


def choose(core, request, kind="agent", exclude=()):
    """Skill names for this request, requirements included. [] when none fit
    or routing is not possible."""
    request = (request or "").strip()
    store = core.skills
    pool = [s for s in store.list() if s["name"] not in exclude and kind in s["kinds"]]
    if not request or not pool:
        return []
    role = ROLE
    if core.settings.roles.get(ROLE, {}).get("api_base") and boot.ensure_role(core.settings, ROLE):
        role = "brain"                  # the small model is down: the brain can do it
    prompt = PROMPT.format(catalogue=_catalogue(pool), request=request[:6000], n=MAX_PICK)
    try:
        reply = core.llm.chat([{"role": "user", "content": prompt}], role=role,
                              thinking=False, max_tokens=200, temperature=0).content
    except Exception:
        return []
    picked = _parse(reply, pool)
    return [n for n in with_requirements(store, picked) if n not in exclude]
