## write_file -- create a new file, or completely replace one

Writes <content> verbatim. Creates parent directories automatically. Use this for
NEW files. For changing part of an existing file use edit_file instead -- a full
rewrite risks silently dropping code you did not intend to touch.

<write_file>
<path>src/api/health.py</path>
<content>
def health():
    return {"ok": True}
</content>
</write_file>

The content field is required. To create an empty file, send an explicit empty
content field. Preserve Markdown fences inside content. If the literal data
contains XML tool delimiters that would close this call, use run_command with a
safely quoted heredoc instead; an ambiguous write is rejected without changing
the file.
