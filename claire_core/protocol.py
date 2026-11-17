"""The XML tool-call protocol: parsing what the model wrote.

XML, not JSON function calling, because this model build hangs when the OpenAI
`tools` parameter is present, and because XML degrades gracefully -- a missing
closing tag is recoverable, a truncated JSON object is not.

Every function takes the tool names it should recognise, since the set is no
longer fixed: tools Claire builds in the workshop join it at runtime.
"""

import re

# Parameters whose value is a block of text that may itself contain XML, so
# they are matched greedily to their last closing tag.
BODY_KEYS = ("content", "search", "replace", "summary", "code", "test", "task",
             "input", "text")

# Short single-value parameters used by the built-in tools.
SIMPLE_KEYS = ("path", "command", "pattern", "url", "query", "why", "kind",
               "note", "start", "end", "context", "what", "title", "workdir",
               "name", "description", "params", "network", "replaces",
               "outside", "id", "question", "paths", "cron", "timezone",
               "connector", "action", "skills")

READ_ONLY_BATCHABLE = {"read_file", "list_files", "search_files"}
MAX_BATCH = 3


def strip_reasoning(text, tools):
    """Qwen emits <think> blocks; they must not reach the XML parser.

    An orphaned </think> (reasoning with no opening tag) is only treated as a
    reasoning terminator when no tool call precedes it -- otherwise an answer
    that merely *mentions* </think> would have its opening destroyed.
    """
    text = re.sub(r"\A\s*<think>.*?</think>", "", text, flags=re.DOTALL)

    m = re.search(r"</think>", text)
    if m:
        first_tool = min(
            (text.find(f"<{t}>") for t in tools if text.find(f"<{t}>") != -1),
            default=-1)
        if first_tool == -1 or m.start() < first_tool:
            text = text[m.end():]

    # Remove an outer wrapper only; fences inside file/content bodies are data.
    wrapped = re.fullmatch(r'\s*```[a-zA-Z0-9_-]*[ \t]*\r?\n(.*)\r?\n```[ \t]*', text, re.DOTALL)
    if wrapped:
        text = wrapped.group(1)
    return text.strip()


def parse_tool_calls(text, tools, extra_params=None):
    """Find tool blocks in the reply. Returns a list of (name, args).

    Normally just one, since that's the rule for anything that writes, edits,
    or runs something. read_file/list_files/search_files are read-only and
    independent of each other, so several of those in one message all run,
    capped at MAX_BATCH. A batch that mixes in anything else collapses to its
    first call.

    extra_params maps a tool name to parameter names beyond the built-in ones,
    which is how a workshop tool declares its own arguments.
    """
    extra_params = extra_params or {}
    text = strip_reasoning(text, tools)
    found = []
    for name in tools:
        for m in re.finditer(rf"<{name}\s*>(.*?)</{name}\s*>", text, re.DOTALL):
            found.append((m.start(), name, m))
    if not found:
        # Recover an unclosed final tag, common on long outputs.
        for name in tools:
            m = re.search(rf"<{name}\s*>(.*)", text, re.DOTALL)
            if m and name in ("write_file", "done"):
                return [(name, parse_params(m.group(1), name))]
        return []

    found.sort(key=lambda t: t[0])
    # A literal closing tool tag inside file data must not turn a shortened
    # content block into an executable write. Ask for a different encoding.
    for _, name, match in found:
        if name == 'write_file' and re.search(r'<content\s*>', match.group(1)) and not re.search(r'</content\s*>', match.group(1)):
            return []
    calls = [(name, parse_params(m.group(1), name, extra_params.get(name, ())))
             for _, name, m in found]
    if not all(name in READ_ONLY_BATCHABLE for name, _ in calls):
        return calls[:1]
    return calls[:MAX_BATCH]


def prose_outside(reply, tools):
    """Text the model wrote outside its XML tool call.

    Models often write the real answer as prose and then close with a bare
    <done>. That prose is the answer, so it must not be discarded.
    """
    text = strip_reasoning(reply, tools)
    for name in tools:
        text = re.sub(rf"<{name}\s*>.*?</{name}\s*>", "", text, flags=re.DOTALL)
        text = re.sub(rf"<{name}\s*>.*$", "", text, flags=re.DOTALL)
    # Drop any orphaned parameter tags left behind.
    keys = "|".join(BODY_KEYS + SIMPLE_KEYS)
    text = re.sub(rf"</?(?:{keys})\s*>", "", text)
    return text.strip()


def parse_params(block, name, extra=()):
    args = {}
    rest = block
    # Greedy for body parameters so embedded XML survives. Each body is cut out
    # before the short parameters are read, so a <path> written inside file
    # content can never be mistaken for the tool's own <path>.
    for key in BODY_KEYS:
        m = re.search(rf"<{key}\s*>\n?(.*)\n?</{key}\s*>", rest, re.DOTALL)
        if m:
            args[key] = m.group(1)
            rest = rest[:m.start()] + rest[m.end():]
    for key in tuple(SIMPLE_KEYS) + tuple(k for k in extra
                                         if k not in SIMPLE_KEYS + BODY_KEYS):
        m = re.search(rf"<{key}\s*>(.*?)</{key}\s*>", rest, re.DOTALL)
        if m:
            args[key] = m.group(1).strip()
        else:
            # Recover an unclosed tag rather than silently passing empty.
            m = re.search(rf"<{key}\s*>([^<]+)", rest, re.DOTALL)
            if m:
                args[key] = m.group(1).strip()
    # Unclosed <content> recovery.
    if name == "write_file" and "content" not in args:
        m = re.search(r"<content\s*>\n?(.*)", block, re.DOTALL)
        if m:
            args["content"] = m.group(1).rstrip()
    return args


def trim_partial_close(text):
    """Drop a `</summary>` tag, including one still arriving character by
    character, without touching a legitimate `</` inside the prose itself.
    """
    text = re.split(r"</summary\s*>?", text, maxsplit=1)[0]
    # A closing tag mid-stream: only strip if it is a prefix of "</summary".
    m = re.search(r"<(/(?:s(?:u(?:m(?:m(?:a(?:r(?:y)?)?)?)?)?)?)?)?$", text)
    if m and m.group(0):
        text = text[:m.start()]
    return text


def streaming_summary(content):
    """The <done> summary as it stands mid-stream, or None if not started."""
    s = re.search(r"<summary\s*>\n?(.*)", content, re.DOTALL)
    return trim_partial_close(s.group(1)) if s else None


def phase(content):
    """Describe what the model is currently emitting, as (label, preview).

    The label names the activity in the user's terms -- answering, writing a
    file, or preparing a tool call -- rather than leaking the fact that every
    response is technically 'content being written'.
    """
    m = re.search(r"<(\w+)\s*>", content)
    if not m:
        return "responding", content[-200:]
    name = m.group(1)
    p = re.search(r"<(?:path|command|url|pattern|query|title|name)\s*>([^<\n]*)",
                  content)
    target = p.group(1).strip() if p else ""

    # A <done> summary IS the answer, so stream it as prose.
    summary = streaming_summary(content)
    if summary is not None:
        return "answering", re.sub(r"\s+", " ", summary).strip()

    # File and code bodies get a live line counter.
    body = None
    for tag in ("content", "replace", "code", "task"):
        b = re.search(rf"<{tag}\s*>\n?(.*)", content, re.DOTALL)
        if b:
            body = b.group(1)
            break
    if body is not None:
        body = re.sub(r"</(?:content|replace|code|task)\s*>.*$", "", body,
                      flags=re.DOTALL)
        lines = body.split("\n")
        current = next((l.strip() for l in reversed(lines) if l.strip()), "")
        verb = "editing" if name == "edit_file" else "writing"
        return verb, f"{name} {target}  L{len(lines)}  {current[:70]}"

    if name == "done":
        return "answering", ""
    return f"calling {name}", target
