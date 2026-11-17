## edit_file -- change part of an existing file

The <search> text must appear EXACTLY ONCE in the file, copied exactly including
indentation. Include a few surrounding lines to make it unique. If the edit fails,
re-read the file and copy the text again rather than guessing.

<edit_file>
<path>src/api/routes.py</path>
<search>
def health():
    return {"ok": True}
</search>
<replace>
def health():
    return {"ok": True, "version": VERSION}
</replace>
</edit_file>
