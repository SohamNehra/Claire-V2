"""Memory tools, and the two control tools the engine handles itself."""

from . import Tool


def remember(ctx, args):
    content = (args.get("content") or "").strip()
    if not content:
        return "ERROR: <content> was empty"
    kind = (args.get('kind') or 'project').strip().lower()
    if kind not in ('project', 'learned', 'session', 'sir', 'self', 'work'):
        return 'ERROR: kind must be project, learned, session, sir, self or work'
    project = ctx.session.project()
    return ctx.vault.remember(
        kind=kind,
        content=content, note=(args.get("note") or "").strip(),
        workdir=ctx.workdir, home=ctx.session.kind == "home",
        folder=project["slug"] if project else None,
        name=project["name"] if project else "")


def recall(ctx, args):
    query = (args.get("query") or "").strip()
    if not query:
        return "ERROR: <query> was empty"
    project = ctx.session.project()
    return ctx.vault.recall(query, prefer=project["slug"] if project else None)


BOTH = ("agent", "home")

TOOLS = [
    Tool("remember", remember, kinds=BOTH),
    Tool("recall", recall, read_only=True, kinds=BOTH),
    Tool("think_harder", None, kinds=BOTH),
    Tool("done", None, kinds=BOTH),
]
