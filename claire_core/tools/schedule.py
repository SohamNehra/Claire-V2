"""Home-thread tools for recurring jobs and for talking to a running task."""

from ..scheduler import Cron, ScheduleError
from . import Tool


def schedule_task(ctx, args):
    scheduler = ctx.hub.scheduler
    workdir = args.get('workdir', '')
    if not workdir:
        from .tasks import requested_workdir
        workdir, problem = requested_workdir(ctx.session)
        if problem:
            return 'ERROR: ' + problem
    try:
        title, task, cron, timezone, workdir = scheduler.validate(
            args.get("title", ""), args.get("task", ""), args.get("cron", ""),
            args.get("timezone", ""), workdir)
    except ScheduleError as exc:
        return f"ERROR: {exc}"
    # A job that keeps running unattended is sir's decision, every time.
    summary = (f"{title}\n{cron.describe()} ({timezone})   cron: {cron.expr}\n\n"
               f"{task[:600]}")
    if not ctx.ask("schedule", summary, "RECURRING", always=True):
        return ("Sir did not approve this schedule, so nothing was scheduled. "
                "Do not try again unless he asks.")
    try:
        made = scheduler.create(title, task, cron.expr, timezone, workdir,
                                active=True, created_by="claire")
    except ScheduleError as exc:
        return f"ERROR: {exc}"
    return (f"Scheduled {made['id']}: {title}\nRuns {cron.describe()} "
            f"({timezone}). Next run: {made['next_run_at']} UTC.\n"
            f"This is done and active; do not schedule it again. It only runs "
            f"while your service is running, and each run reports back here "
            f"like any other task. Tell sir it is set up.")


def list_schedules(ctx, args):
    rows = ctx.hub.scheduler.list()
    if not rows:
        return "No schedules yet."
    out = []
    for r in rows:
        last = r["runs"][0] if r["runs"] else None
        out.append(f"{r['id']}  {r['status']:<7} {r['description']} ({r['timezone']})  "
                   f"{r['title']}\n    next: {r['next_run_at'] or '-'}   "
                   f"last run: {(last['status'] + ' at ' + last['started_at']) if last else 'never'}")
    return "\n".join(out)


def pause_schedule(ctx, args):
    sid = (args.get("id") or "").strip()
    made = ctx.hub.scheduler.set_active(sid, False)
    if not made:
        return f"ERROR: no schedule with id {sid!r}. Use list_schedules to see the ids."
    return (f"Paused {sid}: {made['title']}. It will not run again until sir "
            f"resumes it. Nothing was deleted.")


def message_task(ctx, args):
    tid = (args.get("id") or "").strip()
    text = (args.get("task") or "").strip()
    row = ctx.store.thread(tid)
    if not row or row["kind"] != "task":
        return f"ERROR: no task with id {tid!r}. Use list_tasks to see the ids."
    if not text:
        return "ERROR: <task> was empty; say what the task should do next."
    ctx.hub.post(tid, text)
    return (f"Sent to task {tid} ({row['title']}). It will pick this up after "
            f"its current step and report back here when it ends.")


HOME = ("home",)
TOOLS = [
    Tool("schedule_task", schedule_task, needs=("hub",), kinds=HOME),
    Tool("list_schedules", list_schedules, read_only=True, needs=("hub",), kinds=HOME),
    Tool("pause_schedule", pause_schedule, needs=("hub",), kinds=HOME),
    Tool("message_task", message_task, needs=("hub",), kinds=HOME),
]
