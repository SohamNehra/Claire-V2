## remember -- save something worth knowing next time

Your chat history is persisted, but context can be compacted. Save durable facts: project
conventions, decisions and why they were made, gotchas, commands that work here.
Do NOT save transient detail or anything already obvious from reading the code.
Link related notes with [[wikilinks]].

kind is one of:
  project  -- specific to this directory (default)
  learned  -- a reusable fact that applies anywhere; also give a <note> title
  work     -- recent project activity, goals, decisions, or next steps; give a
              <note> project name. Updates older than 30 days stay searchable in the archive.
  session  -- what happened today

<remember>
<kind>project</kind>
<content>
Tests run with `python3 -m pytest -q`; there is no `python` on this machine.
Config lives in settings/local.py, which is gitignored.
</content>
</remember>
