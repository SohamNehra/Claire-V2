## run_command -- run a shell command in the working directory

Use it to run code, tests, linters, git, and to verify your work. Output is
truncated at 15000 chars and commands time out after 120s. On Windows use
Windows PowerShell and `python`; on macOS/Linux use `python3`. The user may deny a command; if denied, do not retry it,
find another approach.

<run_command>
<command>python3 -m pytest tests/ -q</command>
</run_command>
