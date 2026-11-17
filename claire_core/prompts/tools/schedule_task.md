## schedule_task -- do something on a recurring schedule

Use this when sir wants something to happen regularly: "every morning", "each
Friday", "make it a habit", "keep doing that". It creates a recurring job. Each
time it comes due, a task thread runs the brief and reports back here.

- <title>: a short name for the job.
- <cron>: when it runs, as five fields: minute hour day-of-month month weekday.
  Numbers, lists (1,15), ranges (1-5) and steps (*/2) only. Weekday 0 or 7 is
  Sunday, 1 is Monday.

      0 9 * * *       every day at 09:00
      30 8 * * 1-5    weekdays at 08:30
      0 18 * * 5      Fridays at 18:00
      0 */4 * * *     every four hours, on the hour
      0 10 1 * *      the 1st of each month at 10:00

- <timezone>: leave it out to use sir's own time zone, which is the usual case.
- <workdir>: the project folder, if the job works on one. Leave it out for jobs
  that need no project.
- <task>: the brief each run receives. As with spawn_task, the task thread
  knows nothing about this conversation, so the brief must stand on its own and
  must make sense every time it runs, not just today. Write "summarise the mail
  that arrived since yesterday morning", not "summarise today's mail from Sam".

<schedule_task>
<title>Morning brief</title>
<cron>0 9 * * 1-5</cron>
<task>
Check sir's calendar for today and his unread mail. Report today's meetings in
order, then the messages that need a reply from him, most urgent first. Keep it
under 200 words. If either source cannot be reached, say so rather than leaving
it out.
</task>
</schedule_task>

Sir is always asked to approve a new schedule before it starts. If he says no,
nothing is scheduled. A job cannot run more often than every 10 minutes.

If he only wants something done once, later -- "remind me tomorrow" -- this is
not the tool; tell him recurring jobs are what you can schedule.
