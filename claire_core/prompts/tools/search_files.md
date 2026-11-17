## search_files -- find text across the project by regex

Returns file:line: matched-line. Far cheaper than reading many files. Use it to
locate where something is defined or used before reading anything.

<search_files>
<pattern>def create_user</pattern>
<path>src</path>
<context>3</context>
</search_files>

<context> shows N lines either side of each match. Use 2-4 when you want to
understand the code around a hit without reading the whole file.
