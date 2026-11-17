## make_tool -- turn working code into a tool you keep

Use this when you will need the same capability again, or when an existing tool
is broken and you have found another way to do its job. First get the approach
working with run_code; then save it here.

- <name>: lowercase letters, digits and underscores.
- <description>: one line saying what it does. This is what you will read later
  when deciding whether to use it.
- <params>: the argument names, separated by commas.
- <code>: Python defining `def run(args):`. `args` is a dict holding each
  parameter as a string. Return the result as a string.
- <test>: a Python script that imports the tool with `import tool`, calls
  `tool.run(...)`, and fails (an `assert`, or any exception) if the result is
  wrong. The tool is registered ONLY if this script exits cleanly, so test real
  behaviour, not that the function exists.
- <replaces>: optional. The name of the broken tool this stands in for.
- <network>true</network>: optional, only if it cannot work offline. The user
  is asked to approve it.

The tool runs under the same sandbox as run_code: it reads the working
directory, writes only to scratch, and has no network unless approved.

<make_tool>
<name>count_lines</name>
<description>Count the lines in a file inside the working directory</description>
<params>path</params>
<code>
import os

def run(args):
    full = os.path.join(os.environ["CLAIRE_WORKDIR"], args["path"])
    with open(full, encoding="utf-8", errors="replace") as f:
        return str(sum(1 for _ in f))
</code>
<test>
import os, tool

os.environ["CLAIRE_WORKDIR"] = os.getcwd()
with open("sample.txt", "w") as f:
    f.write("a\nb\nc\n")
assert tool.run({"path": "sample.txt"}) == "3"
</test>
</make_tool>

Once registered, call it by its own name, like any other tool.
