## read_file -- read a file's contents, with line numbers

Use before editing any existing file. Never guess what a file contains.

<read_file>
<path>src/api/routes.py</path>
</read_file>

For a big file, read a range instead of the whole thing. The result tells you
the total line count and where to continue from:

<read_file>
<path>src/api/routes.py</path>
<start>400</start>
<end>520</end>
</read_file>
