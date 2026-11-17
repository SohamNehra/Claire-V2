## scout -- have a helper read files or a page and answer one question

Sends the material to a smaller, faster model that reads it and reports back, so
the text itself never enters your context. Use it when you need a fact or an
overview out of material that is long or spread across several files -- "which
of these modules touches the database", "what does this page say about rate
limits" -- and you do not need to see the lines yourself.

- <question>: one specific question. A vague question gets a vague answer.
- <paths>: files or folders in the working directory, separated by commas or
  newlines. A folder means every file under it.
- <url>: a web page to read instead of files.

<scout>
<question>Which functions write to disk, and what file does each one write?</question>
<paths>src/storage, src/export.py</paths>
</scout>

Several files are read at the same time, so this is quicker than reading them
one by one. The report cites `file:line` for what it claims.

The helper is less capable than you are and can be wrong. Treat its report as a
lead: before you edit anything or state a finding as fact, read the lines it
cites with read_file. Do not use it for a file you are about to change -- read
that one yourself.
