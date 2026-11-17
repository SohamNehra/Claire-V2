"""Loading a skill into the current thread."""

from . import Tool


def use_skill(ctx, args):
    """Load a skill by name -- or, given a description of the work, the
    skills the router picks for it."""
    from ..skillroute import choose, with_requirements
    name = (args.get("name") or "").strip()
    session = ctx.session
    skills = session.core.skills
    if name in session.skills:
        return f"The `{name}` skill is already loaded; its instructions are in effect."
    if skills.get(name):
        wanted = with_requirements(skills, [name])
    else:
        wanted = (choose(session.core, name, session.kind, exclude=session.skills)
                  if session.settings.skill_router else [])
        if not wanted:
            listing = ", ".join(f"`{n}`" for n in skills.names()) or "(none)"
            return f"ERROR: no skill matches {name!r}. Available: {listing}"
    added = [n for n in wanted if n not in session.skills]
    for n in added:
        session.load_skill(n)
    return (f"Loaded {', '.join(f'`{n}`' for n in added)}. Their instructions have been "
            f"added to your own and apply from here on; follow them for the rest of "
            f"this work.")


TOOLS = [Tool("use_skill", use_skill, read_only=True, kinds=("agent", "home"))]
