## run_code -- run a Python script you write, in the workshop

A sandboxed place to compute something or get a result that no existing tool
gives you. The script runs with `python3` in a scratch folder of its own:

- It can READ the working directory. Its absolute path is in the environment
  variable `CLAIRE_WORKDIR`; relative paths point at the scratch folder instead.
- It can WRITE only to the scratch folder. To put a result into the project,
  print it, then use write_file or edit_file.
- It has no network. Add <network>true</network> only when the script cannot
  work without it; the user is asked to approve that each time.
- It stops after 60 seconds.

Whatever the script prints is the result, so print what you need.

<run_code>
<code>
import os, json
root = os.environ["CLAIRE_WORKDIR"]
data = json.load(open(os.path.join(root, "data/orders.json")))
print(len(data), "orders; total", sum(o["amount"] for o in data))
</code>
</run_code>

This is for work that genuinely needs code. For running the project's own
tests or commands, use run_command.
