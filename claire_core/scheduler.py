"""Recurring jobs: the things Claire does on a schedule.

A schedule is a task brief plus a cron expression and a time zone. When it
comes due, the scheduler starts an ordinary task thread through the hub, so a
scheduled run is traced, sandboxed, approved and reported exactly like a task
sir asked for by hand, and its result stays in that task's own chat.

This is not OS cron. Jobs live in Claire's own database and run only while her
service is running (`claire2 autostart on` keeps it running across logins).
That is deliberate: it means every job is visible in one list, and nothing
here can add an entry to the system crontab.

Rules, chosen so a schedule never surprises anyone:

- A job is only taken once per due time, even across restarts or two services.
- If the service was down when a job was due, it runs once when the service
  comes back -- not once per missed time -- and only if it is less than
  SCHEDULE_GRACE_HOURS late. Otherwise that run is recorded as missed.
- If the previous run of the same job is still going, the new one is skipped.
- Times are wall-clock times in the job's own time zone. A time that does not
  exist on a daylight-saving day is skipped; a time that happens twice runs
  once.
"""

import datetime
import os
import re
import sys
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import new_id

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31),
          ("month", 1, 12), ("weekday", 0, 7))
MIN_GAP_MINUTES = 10
MAX_ACTIVE = 50
ACTIVE_THREAD = ("queued", "running", "waiting")


class ScheduleError(ValueError):
    """The schedule as given cannot be accepted. The message says why."""


def local_timezone():
    """This machine's IANA time zone name, e.g. 'Asia/Kolkata'."""
    name = os.environ.get("TZ", "")
    if not name and sys.platform == "win32":
        try:
            from tzlocal import get_localzone_name
            name = get_localzone_name()
        except (ImportError, OSError, ValueError, ZoneInfoNotFoundError):
            pass
    if not name:
        try:
            link = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in link:
                name = link.split("zoneinfo/", 1)[1]
        except OSError:
            pass
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


class Cron:
    """A five-field cron expression: minute hour day-of-month month weekday.

    Numbers, lists (1,15), ranges (1-5), steps (*/15, 8-18/2) and * are
    accepted. Weekdays run 0-7 with both 0 and 7 meaning Sunday. Names and a
    seconds field are not.
    """

    def __init__(self, expr):
        parts = (expr or "").split()
        if len(parts) != 5:
            raise ScheduleError("a cron expression has five fields: minute hour "
                                "day-of-month month weekday")
        self.expr = " ".join(parts)
        values = [self._field(part, name, lo, hi)
                  for part, (name, lo, hi) in zip(parts, FIELDS)]
        self.minutes, self.hours, self.days, self.months, weekdays = values
        self.weekdays = {d % 7 for d in weekdays}          # 7 is Sunday too
        # Standard cron: when both day fields are restricted, either may match.
        self.day_any = parts[2] == "*"
        self.weekday_any = parts[4] == "*"

    @staticmethod
    def _field(part, name, lo, hi):
        out = set()
        for piece in part.split(","):
            m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", piece)
            if not m:
                raise ScheduleError(f"cannot read the {name} field: {piece!r}")
            base, step = m.group(1), int(m.group(2) or 1)
            if step < 1:
                raise ScheduleError(f"the step in the {name} field must be at least 1")
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                start, end = (int(x) for x in base.split("-"))
            else:
                start = int(base)
                end = hi if m.group(2) else start
            if not (lo <= start <= hi and lo <= end <= hi and start <= end):
                raise ScheduleError(f"the {name} field must be between {lo} and {hi}: {piece!r}")
            out.update(range(start, end + 1, step))
        return out

    def _day_matches(self, date):
        dom = date.day in self.days
        dow = (date.weekday() + 1) % 7 in self.weekdays     # Monday=0 -> Sunday=0
        if self.day_any and self.weekday_any:
            return True
        if self.day_any:
            return dow
        if self.weekday_any:
            return dom
        return dom or dow

    def next_after(self, after_ts, tzname):
        """The first matching time strictly after after_ts, as a timestamp."""
        tz = ZoneInfo(tzname)
        start = datetime.datetime.fromtimestamp(after_ts, tz)
        date = start.date()
        for _ in range(366 * 5):                           # Feb 29 needs 4 years
            if date.month in self.months and self._day_matches(date):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        wall = datetime.datetime(date.year, date.month, date.day,
                                                 hour, minute, tzinfo=tz)
                        ts = wall.timestamp()
                        # A wall time skipped by daylight saving does not
                        # survive the round trip; it never happens, so skip it.
                        back = datetime.datetime.fromtimestamp(ts, tz)
                        if (back.hour, back.minute) != (hour, minute):
                            continue
                        if ts > after_ts:
                            return ts
            date += datetime.timedelta(days=1)
        raise ScheduleError("that cron expression never comes due")

    def describe(self):
        """A plain reading of the common shapes, for approval prompts."""
        m, h, d, mo, w = self.expr.split()
        if d == "*" and mo == "*" and m.isdigit() and h.isdigit():
            at = f"{int(h):02d}:{int(m):02d}"
            names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday", "Sunday"]
            if w == "*":
                return f"every day at {at}"
            if w == "1-5":
                return f"every weekday at {at}"
            if w in ("0,6", "6,0", "6,7"):
                return f"every weekend day at {at}"
            if w.isdigit():
                return f"every {names[int(w)]} at {at}"
        if (h, d, mo, w) == ("*", "*", "*", "*") and m.isdigit():
            return f"every hour at {int(m)} minutes past"
        if w == "*" and mo == "*" and d.isdigit() and m.isdigit() and h.isdigit():
            return f"on day {int(d)} of every month at {int(h):02d}:{int(m):02d}"
        return f"on the cron schedule `{self.expr}`"


def _iso(ts):
    if not ts:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class Scheduler:
    def __init__(self, hub, tick_seconds=20):
        self.hub = hub
        self.store = hub.store
        self.settings = hub.settings
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._thread = None

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="claire-scheduler",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:          # the clock must keep running
                self.hub.publish({"type": "notice", "thread": "home", "ts": time.time(),
                                  "level": "error",
                                  "text": f"scheduler error: {type(exc).__name__}: {exc}"})
            self._stop.wait(self.tick_seconds)

    # -- creating and changing -----------------------------------------------

    def validate(self, title, task, cron, timezone, workdir=""):
        """Check a schedule and return its normalised parts."""
        title = " ".join((title or "").split())[:80]
        task = (task or "").strip()
        if not task:
            raise ScheduleError("the schedule needs a task: what should be done each time")
        if not title:
            title = " ".join(task.split()[:8])
        timezone = (timezone or "").strip() or local_timezone()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ScheduleError(f"unknown time zone {timezone!r}; use a name like Asia/Kolkata")
        parsed = Cron(cron)
        # Each run is a model task. Something that fires every few minutes
        # would keep the machine busy forever and bury the home thread.
        at, gaps = time.time(), []
        for _ in range(6):
            nxt = parsed.next_after(at, timezone)
            gaps.append(nxt - at)
            at = nxt
        if min(gaps[1:]) < MIN_GAP_MINUTES * 60:
            raise ScheduleError(f"that runs more often than every {MIN_GAP_MINUTES} "
                                f"minutes, which is too frequent for a scheduled task")
        workdir = (workdir or "").strip()
        if workdir:
            workdir, problem = self.hub.resolve_workdir(workdir)
            if problem:
                raise ScheduleError(problem)
        return title, task, parsed, timezone, workdir

    def create(self, title, task, cron, timezone="", workdir="", active=False,
               created_by="user"):
        title, task, parsed, timezone, workdir = self.validate(
            title, task, cron, timezone, workdir)
        existing = self.store.schedules()
        if active and sum(1 for s in existing if s["status"] == "active") >= MAX_ACTIVE:
            raise ScheduleError(f"there are already {MAX_ACTIVE} active schedules")
        if created_by != "user":
            # A model that schedules, has second thoughts, and schedules again
            # leaves two jobs doing the same thing at the same minute.
            twin = next((s for s in existing if s["status"] == "active"
                         and s["cron"] == parsed.expr and s["timezone"] == timezone
                         and (s["workdir"] or "") == workdir
                         and time.time() - s["created"] < 3600), None)
            if twin:
                raise ScheduleError(
                    f"there is already a job at exactly this time, made a moment ago: "
                    f"{twin['id']} \"{twin['title']}\". It is active and nothing more "
                    f"is needed. To replace it, pause that one first, then schedule again")
        sid = new_id("s")
        self.store.add_schedule(
            sid, title, task, parsed.expr, timezone, workdir,
            "active" if active else "paused", created_by,
            parsed.next_after(time.time(), timezone) if active else None)
        self._changed(sid)
        return self.view(self.store.schedule(sid))

    def set_active(self, sid, active):
        row = self.store.schedule(sid)
        if not row:
            return None
        if active:
            nxt = Cron(row["cron"]).next_after(time.time(), row["timezone"])
            self.store.update_schedule(sid, status="active", next_run=nxt)
        else:
            self.store.update_schedule(sid, status="paused", next_run=None)
        self._changed(sid)
        return self.view(self.store.schedule(sid))

    def delete(self, sid):
        ok = self.store.delete_schedule(sid)
        if ok:
            self._changed(sid)
        return ok

    def _changed(self, sid):
        self.hub.publish({"type": "schedule_changed", "thread": "home",
                          "ts": time.time(), "schedule": sid})

    # -- running -------------------------------------------------------------

    def tick(self, now=None):
        """Start every job that has come due. Returns the runs it started."""
        now = time.time() if now is None else now
        grace = float(getattr(self.settings, "schedule_grace_hours", 12)) * 3600
        started = []
        for row in self.store.schedules():
            due = row["next_run"]
            if row["status"] != "active" or not due or due > now:
                continue
            nxt = Cron(row["cron"]).next_after(now, row["timezone"])
            if not self.store.claim_run(row["id"], due, nxt, now):
                continue                      # someone else took it
            if now - due > grace:
                self.store.add_run(row["id"], "missed",
                                   note="the service was not running at the scheduled time")
            else:
                thread = self._dispatch(row, due)
                if thread:
                    started.append(thread)
            self._changed(row["id"])
        return started

    def run_now(self, sid):
        """Run a job immediately, outside its schedule. Returns the thread id."""
        row = self.store.schedule(sid)
        if not row:
            return None
        thread = self._dispatch(row, time.time(), manual=True)
        self._changed(sid)
        return thread

    def _dispatch(self, row, due, manual=False):
        for run in self.store.runs(row["id"], limit=3):
            if run["thread_id"] and run["thread_status"] in ACTIVE_THREAD:
                self.store.add_run(row["id"], "skipped",
                                   note="the previous run was still going")
                return None
        when = datetime.datetime.fromtimestamp(due, ZoneInfo(row["timezone"]))
        workdir = row["workdir"]
        if not workdir:
            workdir, _ = self.hub.resolve_workdir("", row["title"])
        kind = "Run on request" if manual else "Scheduled run"
        brief = (f"[{kind} of \"{row['title']}\", "
                 f"{when.strftime('%A %Y-%m-%d %H:%M')} {row['timezone']}]\n\n{row['task']}")
        # A run keeps its result in its own task chat, listed with its project
        # and on the command center; there is no main chat to relay it to.
        thread = self.hub.spawn_task(row["title"], brief, workdir, parent=None)
        self.store.add_run(row["id"], "started", thread_id=thread["id"])
        return thread["id"]

    # -- reporting -----------------------------------------------------------

    def view(self, row):
        """A schedule as clients see it."""
        runs = []
        for r in self.store.runs(row["id"]):
            status = r["status"]
            if r["thread_id"]:
                status = r["thread_status"] or "unknown"
            runs.append({"started_at": _iso(r["started"]), "status": status,
                         "thread_id": r["thread_id"], "note": r["note"] or ""})
        return {"id": row["id"], "title": row["title"], "task": row["task"],
                "cron": row["cron"], "timezone": row["timezone"],
                "workdir": row["workdir"] or "", "status": row["status"],
                "description": Cron(row["cron"]).describe(),
                "created_by": row["created_by"],
                "next_run_at": _iso(row["next_run"]) if row["status"] == "active" else None,
                "last_run_at": _iso(row["last_run"]), "runs": runs}

    def list(self):
        return [self.view(row) for row in self.store.schedules()]
