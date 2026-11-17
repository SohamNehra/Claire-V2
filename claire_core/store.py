"""Durable state: threads, their messages, the event trace, approvals, and
tool health. One SQLite file, so the terminal and the service can both use it
and a restart loses nothing.

The vault holds what Claire knows; this holds what she is in the middle of.
"""

import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, kind TEXT, title TEXT, workdir TEXT, parent TEXT,
    status TEXT, summary TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS messages (
    thread_id TEXT, seq INTEGER, role TEXT, content TEXT,
    PRIMARY KEY (thread_id, seq));
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, ts REAL,
    type TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_thread ON events (thread_id, seq);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, thread_id TEXT, kind TEXT, title TEXT, flag TEXT,
    status TEXT, created REAL, resolved REAL);
CREATE TABLE IF NOT EXISTS tool_health (
    tool TEXT PRIMARY KEY, faults INTEGER, consecutive INTEGER,
    last_error TEXT, last_ts REAL, substitute TEXT);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY, title TEXT, task TEXT, cron TEXT, timezone TEXT,
    workdir TEXT, status TEXT, created_by TEXT, created REAL,
    next_run REAL, last_run REAL);
CREATE TABLE IF NOT EXISTS schedule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id TEXT, started REAL,
    status TEXT, thread_id TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS runs_schedule ON schedule_runs (schedule_id, id);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT, path TEXT, slug TEXT, created REAL,
    updated REAL);
CREATE TABLE IF NOT EXISTS integrations (
    id TEXT PRIMARY KEY, provider TEXT, name TEXT, config TEXT, status TEXT,
    account_label TEXT, scopes TEXT, error TEXT, created REAL, updated REAL);
"""


def new_id(prefix="t"):
    return f"{prefix}{time.strftime('%m%d')}-{uuid.uuid4().hex[:6]}"


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            # Columns added after the first release.
            have = {r[1] for r in self._db.execute("PRAGMA table_info(threads)")}
            if "skills" not in have:
                self._db.execute("ALTER TABLE threads ADD COLUMN skills TEXT DEFAULT ''")
            if "project" not in have:
                self._db.execute("ALTER TABLE threads ADD COLUMN project TEXT DEFAULT ''")
            self._db.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _run(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def close(self):
        with self._lock:
            self._db.close()

    def recent_requests(self, thread_id, limit=6):
        """Actual turn inputs, separate from internal user-role tool feedback."""
        rows = self._all("SELECT data FROM events WHERE thread_id=? AND type='turn_start' "
                         "ORDER BY seq DESC LIMIT ?", (thread_id, limit))
        return [json.loads(row['data']).get('text', '') for row in rows]

    # -- threads -------------------------------------------------------------

    def create_thread(self, kind, title, workdir, parent=None, thread_id=None,
                      skills=(), project=""):
        tid = thread_id or new_id()
        now = time.time()
        self._run("INSERT INTO threads (id, kind, title, workdir, parent, status, "
                  "summary, created, updated, skills, project) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, kind, title, workdir, parent, "idle", "", now, now,
                   ",".join(skills), project or ""))
        return tid

    def thread(self, tid):
        rows = self._all("SELECT * FROM threads WHERE id = ?", (tid,))
        return rows[0] if rows else None

    def threads(self, kind=None, limit=50):
        if kind:
            return self._all("SELECT * FROM threads WHERE kind = ? "
                             "ORDER BY updated DESC LIMIT ?", (kind, limit))
        return self._all("SELECT * FROM threads ORDER BY updated DESC LIMIT ?",
                         (limit,))

    def delete_thread(self, tid, heir=None):
        """Remove a thread and everything recorded for it. Threads it started
        are handed to `heir`, so their reports still have somewhere to go."""
        with self._lock:
            self._db.execute("UPDATE threads SET parent = ? WHERE parent = ?", (heir, tid))
            for table, column in (("messages", "thread_id"), ("events", "thread_id"),
                                  ("approvals", "thread_id"), ("threads", "id")):
                self._db.execute(f"DELETE FROM {table} WHERE {column} = ?", (tid,))
            self._db.commit()

    def task_counts(self):
        groups = self._all("SELECT status, COUNT(*) AS n FROM threads WHERE kind = 'task' GROUP BY status")
        counts = {row['status']: row['n'] for row in groups}
        return {"total": sum(counts.values()),
                "active": sum(counts.get(s, 0) for s in ('idle', 'queued', 'running', 'waiting')),
                "completed": counts.get('done', 0),
                "failed": sum(counts.get(s, 0) for s in ('error', 'stopped', 'interrupted')),
                "cancelled": counts.get('cancelled', 0)}

    def update_thread(self, tid, **fields):
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE threads SET {cols} WHERE id = ?",
                  tuple(fields.values()) + (tid,))

    # -- projects ------------------------------------------------------------
    # A project is a named folder of chats and tasks with a memory of its own.
    # `path` is the folder on disk it works in (empty for a project that is
    # only a group); `slug` is the name of its memory folder in the vault.

    def create_project(self, name, path, slug):
        pid = new_id("p")
        now = time.time()
        self._run("INSERT INTO projects VALUES (?,?,?,?,?,?)",
                  (pid, name, path or "", slug, now, now))
        return pid

    def project(self, pid):
        if not pid:
            return None
        rows = self._all("SELECT * FROM projects WHERE id = ?", (pid,))
        return rows[0] if rows else None

    def projects(self):
        return self._all("SELECT * FROM projects ORDER BY updated DESC")

    def project_for_path(self, path):
        """The project whose folder is `path` or contains it; the deepest one
        when projects are nested."""
        best = None
        for row in self._all("SELECT * FROM projects WHERE path != ''"):
            root = row["path"]
            if os.path.normcase(path) == os.path.normcase(root) or os.path.normcase(path).startswith(os.path.normcase(root.rstrip(os.sep) + os.sep)):
                if best is None or len(root) > len(best["path"]):
                    best = row
        return best

    def update_project(self, pid, **fields):
        fields.setdefault("updated", time.time())
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE projects SET {cols} WHERE id = ?",
                  tuple(fields.values()) + (pid,))

    def delete_project(self, pid):
        """Forget a project. Its chats and tasks are kept, unfiled."""
        with self._lock:
            self._db.execute("UPDATE threads SET project = '' WHERE project = ?", (pid,))
            cur = self._db.execute("DELETE FROM projects WHERE id = ?", (pid,))
            self._db.commit()
            return cur.rowcount > 0

    def file_thread(self, tid, pid):
        """Put a thread in a project (or none). Filing is not activity, so the
        thread keeps its place in the list."""
        self._run("UPDATE threads SET project = ? WHERE id = ?", (pid or "", tid))

    def project_threads(self, pid, limit=200):
        return self._all("SELECT * FROM threads WHERE project = ? "
                         "ORDER BY updated DESC LIMIT ?", (pid, limit))

    def active_work(self):
        """Every task that is queued, running or waiting, in any project."""
        return self._all(
            "SELECT t.id, t.title, t.status, t.workdir, t.updated, t.parent, "
            "t.project, p.name AS project_name FROM threads t "
            "LEFT JOIN projects p ON p.id = t.project "
            "WHERE t.kind = 'task' AND t.status IN ('queued', 'running', 'waiting') "
            "ORDER BY t.updated DESC")

    # -- messages ------------------------------------------------------------

    def save_messages(self, tid, messages):
        """Replace a thread's conversation. Compaction rewrites history, so an
        append-only log could not represent what the model is actually sent."""
        with self._lock:
            self._db.execute("DELETE FROM messages WHERE thread_id = ?", (tid,))
            self._db.executemany(
                "INSERT INTO messages VALUES (?,?,?,?)",
                [(tid, i, m["role"], json.dumps(m["content"]))
                 for i, m in enumerate(messages)])
            self._db.commit()

    def load_messages(self, tid):
        rows = self._all("SELECT role, content FROM messages WHERE thread_id = ? "
                         "ORDER BY seq", (tid,))
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    # -- events --------------------------------------------------------------

    def add_event(self, event):
        cur = self._run("INSERT INTO events (thread_id, ts, type, data) VALUES (?,?,?,?)",
                        (event.get("thread"), event.get("ts", time.time()),
                         event["type"], json.dumps(event)))
        return cur.lastrowid

    def events_after(self, seq, thread=None, limit=2000):
        if thread:
            rows = self._all("SELECT seq, data FROM events WHERE seq > ? AND "
                             "thread_id = ? ORDER BY seq LIMIT ?", (seq, thread, limit))
        else:
            rows = self._all("SELECT seq, data FROM events WHERE seq > ? "
                             "ORDER BY seq LIMIT ?", (seq, limit))
        out = []
        for r in rows:
            e = json.loads(r["data"])
            e["seq"] = r["seq"]
            out.append(e)
        return out

    def thread_events(self, thread, limit=4000):
        """The most recent events of one thread, oldest first."""
        rows = self._all("SELECT seq, data FROM events WHERE thread_id = ? "
                         "ORDER BY seq DESC LIMIT ?", (thread, limit))
        out = []
        for r in reversed(rows):
            e = json.loads(r["data"])
            e["seq"] = r["seq"]
            out.append(e)
        return out

    def last_event_seq(self):
        row = self._all("SELECT MAX(seq) AS m FROM events")[0]
        return row["m"] or 0

    # -- approvals -----------------------------------------------------------

    def add_approval(self, thread_id, kind, title, flag):
        aid = new_id("a")
        self._run("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?)",
                  (aid, thread_id, kind, title, flag, "pending", time.time(), None))
        return aid

    def resolve_approval(self, aid, status):
        cur = self._run("UPDATE approvals SET status = ?, resolved = ? "
                        "WHERE id = ? AND status = 'pending'",
                        (status, time.time(), aid))
        return cur.rowcount > 0

    def approvals(self, status="pending"):
        return self._all("SELECT * FROM approvals WHERE status = ? ORDER BY created",
                         (status,))

    def expire_pending_approvals(self):
        """A pending approval belongs to a process that is gone after a restart."""
        self._run("UPDATE approvals SET status = 'expired', resolved = ? "
                  "WHERE status = 'pending'", (time.time(),))

    # -- schedules -----------------------------------------------------------

    def add_schedule(self, sid, title, task, cron, timezone, workdir, status,
                     created_by, next_run):
        self._run("INSERT INTO schedules VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, title, task, cron, timezone, workdir, status, created_by,
                   time.time(), next_run, None))

    def schedule(self, sid):
        rows = self._all("SELECT * FROM schedules WHERE id = ?", (sid,))
        return rows[0] if rows else None

    def schedules(self):
        return self._all("SELECT * FROM schedules ORDER BY created")

    def update_schedule(self, sid, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE schedules SET {cols} WHERE id = ?",
                  tuple(fields.values()) + (sid,))

    def delete_schedule(self, sid):
        self._run("DELETE FROM schedule_runs WHERE schedule_id = ?", (sid,))
        return self._run("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount > 0

    def claim_run(self, sid, expected_next, new_next, now):
        """Take a due run, moving the schedule on in the same statement.

        Only succeeds if the schedule is still active and still waiting on the
        time we saw, so a run can never be taken twice -- not by two ticks, and
        not by a second service started by mistake.
        """
        cur = self._run(
            "UPDATE schedules SET next_run = ?, last_run = ? WHERE id = ? "
            "AND status = 'active' AND next_run = ?", (new_next, now, sid, expected_next))
        return cur.rowcount == 1

    def add_run(self, sid, status, thread_id=None, note=""):
        self._run("INSERT INTO schedule_runs (schedule_id, started, status, thread_id, note) "
                  "VALUES (?,?,?,?,?)", (sid, time.time(), status, thread_id, note))

    def runs(self, sid, limit=10):
        """Latest runs, newest first, each with its thread's current status."""
        return self._all(
            "SELECT r.started, r.status, r.thread_id, r.note, t.status AS thread_status "
            "FROM schedule_runs r LEFT JOIN threads t ON t.id = r.thread_id "
            "WHERE r.schedule_id = ? ORDER BY r.id DESC LIMIT ?", (sid, limit))

    # -- integrations --------------------------------------------------------

    def save_integration(self, iid, provider, name, config, status="disconnected",
                         account_label="", scopes=(), error=""):
        now = time.time()
        self._run(
            "INSERT INTO integrations VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET name = excluded.name, config = excluded.config, status = excluded.status, "
            "account_label = excluded.account_label, scopes = excluded.scopes, "
            "error = excluded.error, updated = excluded.updated",
            (iid, provider, name, json.dumps(config), status, account_label,
             json.dumps(list(scopes)), error, now, now))

    def integration(self, iid):
        rows = self._all("SELECT * FROM integrations WHERE id = ?", (iid,))
        return self._integration_row(rows[0]) if rows else None

    def integrations(self):
        return [self._integration_row(r)
                for r in self._all("SELECT * FROM integrations ORDER BY created")]

    @staticmethod
    def _integration_row(row):
        row["config"] = json.loads(row["config"] or "{}")
        row["scopes"] = json.loads(row["scopes"] or "[]")
        return row

    def update_integration(self, iid, **fields):
        for key in ("config", "scopes"):
            if key in fields:
                fields[key] = json.dumps(fields[key])
        fields["updated"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE integrations SET {cols} WHERE id = ?",
                  tuple(fields.values()) + (iid,))

    def delete_integration(self, iid):
        return self._run("DELETE FROM integrations WHERE id = ?", (iid,)).rowcount > 0

    # -- tool health ---------------------------------------------------------

    def tool_fault(self, tool, error):
        """Record a fault. Returns how many have happened in a row."""
        row = self._all("SELECT * FROM tool_health WHERE tool = ?", (tool,))
        if row:
            self._run("UPDATE tool_health SET faults = faults + 1, "
                      "consecutive = consecutive + 1, last_error = ?, last_ts = ? "
                      "WHERE tool = ?", (error, time.time(), tool))
            return row[0]["consecutive"] + 1
        self._run("INSERT INTO tool_health VALUES (?,?,?,?,?,?)",
                  (tool, 1, 1, error, time.time(), None))
        return 1

    def tool_ok(self, tool):
        """A clean run ends a streak: the tool has recovered on its own."""
        self._run("UPDATE tool_health SET consecutive = 0 WHERE tool = ? "
                  "AND consecutive > 0", (tool,))

    def set_substitute(self, tool, substitute):
        self._run("INSERT INTO tool_health VALUES (?,0,0,'',?,?) ON CONFLICT(tool) "
                  "DO UPDATE SET substitute = excluded.substitute",
                  (tool, time.time(), substitute))

    def clear_tool(self, tool):
        self._run("DELETE FROM tool_health WHERE tool = ?", (tool,))

    def tool_health(self):
        return {r["tool"]: r for r in self._all("SELECT * FROM tool_health")}
