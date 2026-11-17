import datetime
import os
import time
import unittest
from zoneinfo import ZoneInfo

from claire_core.hub import Hub
from claire_core.scheduler import Cron, ScheduleError, local_timezone
from tests.helpers import Env, done
from tests.test_service import RoutedLLM, ServiceCase


def ts(text, zone):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=ZoneInfo(zone)).timestamp()


def at(stamp, zone):
    return datetime.datetime.fromtimestamp(stamp, ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")


class CronTests(unittest.TestCase):
    def nxt(self, expr, after, zone="Asia/Kolkata"):
        return at(Cron(expr).next_after(ts(after, zone), zone), zone)

    def test_common_shapes(self):
        self.assertEqual(self.nxt("0 9 * * *", "2026-11-11 08:59"), "2026-11-11 09:00")
        self.assertEqual(self.nxt("0 9 * * *", "2026-11-11 09:00"), "2026-11-12 09:00")
        self.assertEqual(self.nxt("30 8 * * 1-5", "2026-11-13 09:00"), "2026-11-16 08:30")  # Fri -> Mon
        self.assertEqual(self.nxt("0 18 * * 5", "2026-11-11 10:00"), "2026-11-13 18:00")
        self.assertEqual(self.nxt("*/15 * * * *", "2026-11-11 10:07"), "2026-11-11 10:15")
        self.assertEqual(self.nxt("0 */4 * * *", "2026-11-11 21:00"), "2026-11-12 00:00")
        self.assertEqual(self.nxt("0 10 1 * *", "2026-11-11 10:00"), "2026-12-01 10:00")
        self.assertEqual(self.nxt("5 0 31 12 *", "2026-11-11 10:00"), "2026-12-31 00:05")

    def test_sunday_is_both_0_and_7(self):
        self.assertEqual(self.nxt("0 9 * * 0", "2026-11-11 10:00"), "2026-11-15 09:00")
        self.assertEqual(self.nxt("0 9 * * 7", "2026-11-11 10:00"), "2026-11-15 09:00")

    def test_day_of_month_and_weekday_together_mean_either(self):
        # The 13th, or any Monday: Friday the 13th comes first.
        self.assertEqual(self.nxt("0 9 13 * 1", "2026-11-11 10:00"), "2026-11-13 09:00")
        self.assertEqual(self.nxt("0 9 13 * 1", "2026-11-13 10:00"), "2026-11-16 09:00")

    def test_the_time_is_in_the_job_s_own_zone(self):
        expr = Cron("0 9 * * *")
        india = expr.next_after(ts("2026-11-11 00:00", "UTC"), "Asia/Kolkata")
        york = expr.next_after(ts("2026-11-11 00:00", "UTC"), "America/New_York")
        self.assertEqual(at(india, "UTC"), "2026-11-11 03:30")
        self.assertEqual(at(york, "UTC"), "2026-11-11 14:00")

    def test_a_time_skipped_by_daylight_saving_does_not_run(self):
        # 02:30 does not exist in New York on 8 March 2026.
        self.assertEqual(self.nxt("30 2 * * *", "2026-03-07 03:00", "America/New_York"),
                         "2026-03-09 02:30")

    def test_a_time_that_happens_twice_runs_once(self):
        # 01:30 happens twice in New York on 1 November 2026.
        zone = "America/New_York"
        first = Cron("30 1 * * *").next_after(ts("2026-10-31 12:00", zone), zone)
        second = Cron("30 1 * * *").next_after(first, zone)
        self.assertEqual(at(first, zone), "2026-11-01 01:30")
        self.assertEqual(at(second, zone), "2026-11-02 01:30")

    def test_leap_day(self):
        self.assertEqual(self.nxt("0 0 29 2 *", "2026-11-11 10:00"), "2028-02-29 00:00")

    def test_bad_expressions_are_refused_with_a_reason(self):
        for bad in ["", "0 9 * *", "0 9 * * * *", "60 9 * * *", "0 24 * * *",
                    "0 9 0 * *", "0 9 * 13 *", "0 9 * * 8", "0 9 * * mon",
                    "5-1 9 * * *", "*/0 9 * * *", "@daily", "0 9 31 2 *"]:
            with self.assertRaises(ScheduleError, msg=bad):
                Cron(bad).next_after(time.time(), "UTC")

    def test_plain_reading(self):
        self.assertEqual(Cron("0 9 * * *").describe(), "every day at 09:00")
        self.assertEqual(Cron("30 8 * * 1-5").describe(), "every weekday at 08:30")
        self.assertEqual(Cron("0 18 * * 5").describe(), "every Friday at 18:00")
        self.assertIn("cron schedule", Cron("*/20 9-17 * * *").describe())

    def test_this_machine_s_zone_is_a_real_one(self):
        ZoneInfo(local_timezone())


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.llm = RoutedLLM([], [])
        self.env.core.llm = self.llm
        self.hub = Hub(self.env.core, start_brain=False)     # workers not started
        self.sched = self.hub.scheduler
        self.store = self.env.core.store

    def make(self, **kw):
        kw.setdefault("title", "Morning brief")
        kw.setdefault("task", "Summarise the mail.")
        kw.setdefault("cron", "0 9 * * *")
        kw.setdefault("timezone", "Asia/Kolkata")
        return self.sched.create(**kw)

    def due(self, sid, seconds_ago):
        self.store.update_schedule(sid, next_run=time.time() - seconds_ago)

    def test_a_saved_schedule_does_not_run_until_it_is_switched_on(self):
        made = self.make()
        self.assertEqual((made["status"], made["next_run_at"]), ("paused", None))
        self.assertEqual(self.sched.tick(time.time() + 86400 * 3), [])
        on = self.sched.set_active(made["id"], True)
        self.assertEqual(on["status"], "active")
        self.assertRegex(on["next_run_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_a_due_job_starts_one_task_and_moves_on(self):
        made = self.make(active=True)
        self.due(made["id"], 5)
        started = self.sched.tick()
        self.assertEqual(len(started), 1)
        self.assertEqual(self.sched.tick(), [])                # not twice
        thread = self.store.thread(started[0])
        self.assertEqual((thread["kind"], thread["parent"], thread["title"]),
                         ("task", None, "Morning brief"))
        view = self.sched.list()[0]
        self.assertEqual(view["runs"][0]["thread_id"], started[0])
        self.assertGreater(self.store.schedule(made["id"])["next_run"], time.time())
        self.assertIsNotNone(view["last_run_at"])

    def test_the_task_is_told_it_is_a_scheduled_run_and_when(self):
        made = self.make(active=True)
        self.due(made["id"], 5)
        self.sched.tick()
        tid, text, _ = self.hub._queues["task"].get_nowait()
        self.assertRegex(text, r'^\[Scheduled run of "Morning brief", \w+ \d{4}-\d\d-\d\d \d\d:\d\d Asia/Kolkata\]')
        self.assertTrue(text.endswith("Summarise the mail."))

    def test_a_job_missed_while_the_service_was_down_runs_once_not_many_times(self):
        made = self.make(cron="0 * * * *", active=True)
        self.due(made["id"], 5 * 3600)                         # five hourly runs ago
        self.assertEqual(len(self.sched.tick()), 1)
        self.assertEqual(self.sched.tick(), [])
        self.assertEqual(len(self.sched.list()[0]["runs"]), 1)

    def test_a_job_that_is_far_too_late_is_recorded_as_missed(self):
        made = self.make(active=True)
        self.due(made["id"], 13 * 3600)
        self.assertEqual(self.sched.tick(), [])
        run = self.sched.list()[0]["runs"][0]
        self.assertEqual((run["status"], run["thread_id"]), ("missed", None))
        self.assertGreater(self.store.schedule(made["id"])["next_run"], time.time())

    def test_a_job_is_skipped_while_its_last_run_is_still_going(self):
        made = self.make(active=True)
        self.due(made["id"], 5)
        first = self.sched.tick()[0]                           # stays queued
        self.due(made["id"], 5)
        self.assertEqual(self.sched.tick(), [])
        self.assertEqual(self.sched.list()[0]["runs"][0]["status"], "skipped")
        self.store.update_thread(first, status="done")
        self.due(made["id"], 5)
        self.assertEqual(len(self.sched.tick()), 1)

    def test_two_schedulers_cannot_both_take_the_same_run(self):
        made = self.make(active=True)
        due = time.time() - 5
        self.store.update_schedule(made["id"], next_run=due)
        self.assertTrue(self.store.claim_run(made["id"], due, due + 86400, time.time()))
        self.assertFalse(self.store.claim_run(made["id"], due, due + 86400, time.time()))

    def test_a_paused_job_does_not_run_and_keeps_its_history(self):
        made = self.make(active=True)
        self.due(made["id"], 5)
        self.sched.tick()
        self.sched.set_active(made["id"], False)
        self.due(made["id"], 5)                                # even if a time is set
        self.assertEqual(self.sched.tick(), [])
        self.assertEqual(len(self.sched.list()[0]["runs"]), 1)

    def test_schedules_that_would_swamp_the_machine_are_refused(self):
        for cron in ["* * * * *", "*/5 * * * *", "0,5 9 * * *"]:
            with self.assertRaises(ScheduleError, msg=cron):
                self.make(cron=cron)
        self.make(cron="*/10 * * * *")                         # the floor is allowed

    def test_bad_input_is_refused(self):
        for bad in [dict(task=""), dict(timezone="Mars/Olympus"), dict(cron="soon"),
                    dict(workdir="/etc"), dict(workdir="~")]:
            with self.assertRaises(ScheduleError, msg=str(bad)):
                self.make(**bad)
        self.assertEqual(self.sched.list(), [])

    def test_claire_cannot_make_the_same_habit_twice_in_a_row(self):
        self.make(active=True, created_by="claire")
        with self.assertRaises(ScheduleError) as twice:
            self.make(title="Daily brief", task="Reworded brief.", active=True, created_by="claire")
        self.assertIn("already a job at exactly this time", str(twice.exception))
        self.make(cron="0 18 * * *", active=True, created_by="claire")   # a different time is fine
        self.make(active=True)                                           # and he may do as he likes
        self.assertEqual(len(self.sched.list()), 3)

    def test_schedules_survive_a_restart(self):
        made = self.make(active=True)
        again = Hub(self.env.core, start_brain=False)
        self.assertEqual([s["id"] for s in again.scheduler.list()], [made["id"]])
        self.assertEqual(again.scheduler.list()[0]["status"], "active")


class ScheduleByClaireTests(ServiceCase):
    HABIT = ("He wants this every weekday.\n<schedule_task>\n<title>Morning brief</title>\n"
             "<cron>0 9 * * 1-5</cron>\n<task>\nSummarise yesterday's mail.\n</task>\n</schedule_task>")

    def test_claire_can_make_a_habit_only_with_dad_s_yes(self):
        self.boot(home=[self.HABIT, done("Not scheduled."), self.HABIT, done("Scheduled.")])
        self.client.post("/threads/home/messages", {"text": "make the mail summary a habit"})
        ask = self.wait_for(lambda e: e["type"] == "approval_request")
        self.assertEqual((ask["kind"], ask["flag"]), ("schedule", "RECURRING"))
        self.assertIn("every weekday at 09:00", ask["title"])
        self.assertIn("Summarise yesterday's mail.", ask["title"])
        self.client.post(f"/approvals/{ask['id']}", {"allow": False})
        self.wait_for(lambda e: e["type"] == "answer" and e["text"] == "Not scheduled.")
        self.assertEqual(self.client.get("/schedules")["schedules"], [])

        self.client.post("/threads/home/messages", {"text": "yes do it"})
        ask2 = self.wait_for(lambda e: e["type"] == "approval_request" and e["id"] != ask["id"])
        self.client.post(f"/approvals/{ask2['id']}", {"allow": True})
        self.wait_for(lambda e: e["type"] == "answer" and e["text"] == "Scheduled.")
        made = self.client.get("/schedules")["schedules"]
        self.assertEqual(len(made), 1)
        self.assertEqual((made[0]["status"], made[0]["cron"], made[0]["created_by"]),
                         ("active", "0 9 * * 1-5", "claire"))
        self.assertEqual(made[0]["timezone"], local_timezone())

    def test_the_schedule_api(self):
        self.boot(task=[done("Brief: nothing new.")])
        made = self.client.post("/schedules", {
            "title": "Brief", "task": "Summarise.", "cron": "0 9 * * *",
            "timezone": "Asia/Kolkata"})["schedule"]
        self.assertEqual(made["status"], "paused")             # saved, not switched on
        for key in ("id", "title", "task", "cron", "timezone", "workdir", "status",
                    "next_run_at", "last_run_at", "runs"):
            self.assertIn(key, made)
        sid = made["id"]
        self.assertEqual(self.client.post(f"/schedules/{sid}/resume")["schedule"]["status"], "active")
        ran = self.client.post(f"/schedules/{sid}/run")
        self.assertTrue(ran["started"])
        self.wait_for(lambda e: e["type"] == "turn_end" and e["thread"] == ran["thread_id"])
        self.assertEqual(self.client.get(f"/threads/{ran['thread_id']}")["thread"]["summary"], "Brief: nothing new.")
        listed = self.client.get("/schedules")["schedules"][0]
        self.assertEqual(listed["runs"][0], {"started_at": listed["runs"][0]["started_at"],
                                             "status": "done", "thread_id": ran["thread_id"],
                                             "note": ""})
        self.assertEqual(self.client.post(f"/schedules/{sid}/pause")["schedule"]["next_run_at"], None)
        with self.assertRaises(Exception):
            self.client.post("/schedules", {"title": "x", "task": "y", "cron": "* * * * *"})
        with self.assertRaises(Exception):
            self.client.post("/schedules/nope/resume")
        self.assertEqual(self.raw(f"/schedules/{sid}", method="DELETE"), 403)   # needs the token
        self.assertEqual(self.raw(f"/schedules/{sid}", {"Authorization": f"Bearer {self.service.token}"},
                                  method="DELETE"), 200)
        self.assertEqual(self.client.get("/schedules")["schedules"], [])


if __name__ == "__main__":
    unittest.main()
