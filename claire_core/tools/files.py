"""File tools. Every path is confined to the thread's working directory."""

import os
import re
import textwrap

from ..guard import safe_path, PathEscape
from . import Tool


def read_file(ctx, args):
    """Read a file, optionally a specific line range."""
    path = args.get("path", "")
    full = safe_path(ctx.workdir, path, ctx.settings)
    if not os.path.isfile(full):
        return f"ERROR: no such file: {path}"

    cap = ctx.settings.max_file_chars
    for key in ('start', 'end'):
        value = args.get(key)
        if value is not None and str(value).strip():
            try:
                int(str(value).strip())
            except ValueError:
                return f'ERROR: {key} must be an integer line number'
    def as_int(key, default):
        try:
            return int(str(args.get(key, "")).strip())
        except (TypeError, ValueError):
            return default
    start = max(1, as_int("start", 1))
    end = as_int("end", 0) or (start + 119 if ctx.session.profile == 'worker' else None)
    if end is not None and end < start:
        return 'ERROR: end must be greater than or equal to start'
    kept, total, used, clipped = [], 0, 0, False
    with open(full, encoding='utf-8', errors='replace') as f:
        while True:
            if ctx.session.cancel.is_set():
                return 'ERROR: file read cancelled'
            raw = f.readline(cap + 1)
            if not raw:
                break
            total += 1
            prefix = raw.rstrip('\r\n')
            # Consume an oversized physical line in bounded pieces so line
            # numbers remain correct without allocating an unbounded string.
            while not raw.endswith('\n') and len(raw) == cap + 1:
                if ctx.session.cancel.is_set():
                    return 'ERROR: file read cancelled'
                raw = f.readline(cap + 1)
            if total < start or (end is not None and total > end) or clipped:
                continue
            if used + len(prefix) + 1 > cap:
                if not kept:
                    return (f'ERROR: line {total} exceeds the {cap}-character read budget; '
                            'use a targeted command to extract the relevant part of this long line')
                clipped = True
                continue
            kept.append(prefix)
            used += len(prefix) + 1
    if total == 0:
        return f'{path} (empty file, 0 lines):'
    if start > total:
        return f'ERROR: {path} has {total} lines; start={start} is past the end.'
    numbered = '\n'.join(f'{i:>5}| {line}' for i,line in enumerate(kept,start))
    shown_end = start + len(kept) - 1
    note = ''
    if clipped:
        note = f'\n... [stopped at line {shown_end} to fit; read the rest with start={shown_end+1}]'
    elif shown_end < total:
        note = f'\n... [{total-shown_end} more lines; use start={shown_end+1}]'
    return f'{path} (lines {start}-{shown_end} of {total}):\n{numbered}{note}'


def write_file(ctx, args):
    path = args.get("path", "")
    full = safe_path(ctx.workdir, path, ctx.settings, writing=True)
    if os.path.isdir(full):
        return f"ERROR: {path or '(empty path)'} is a directory, not a file"
    if 'content' not in args:
        return 'ERROR: content is required; use an explicit empty content value only to empty a file'
    content = args['content']
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    existed = os.path.isfile(full)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    verb = "Overwrote" if existed else "Created"
    return f"{verb} {path} ({len(content.splitlines())} lines)."


def flexible_replace(body, search, replace):
    """Tiered matching: exact, then whitespace-tolerant. Returns (text, how)."""
    if search in body:
        if body.count(search) > 1:
            return None, f"search text appears {body.count(search)} times; make it unique"
        return body.replace(search, replace, 1), "exact"

    # Whitespace-flexible matching must be just as unique as exact matching.
    s_lines = [line.strip() for line in search.strip('\r\n').splitlines()]
    if not any(s_lines):
        return None, 'empty search'
    b_lines = body.splitlines()
    matches = [i for i in range(len(b_lines)-len(s_lines)+1)
               if [line.strip() for line in b_lines[i:i+len(s_lines)]] == s_lines]
    if len(matches) > 1:
        return None, f'whitespace-normalized search appears {len(matches)} times; make it unique'
    if not matches:
        return None, 'search text not found'
    i = matches[0]
    indent = re.match(r'[ \t]*', b_lines[i]).group(0)
    replacement = textwrap.dedent(replace.strip('\r\n')).splitlines()
    new = [indent + line if line else '' for line in replacement]
    out = b_lines[:i] + new + b_lines[i+len(s_lines):]
    eol = '\r\n' if '\r\n' in body else '\n'
    return eol.join(out) + (eol if body.endswith(('\n','\r')) else ''), 'flexible'


def edit_file(ctx, args):
    path = args.get("path", "")
    full = safe_path(ctx.workdir, path, ctx.settings, writing=True)
    if not os.path.isfile(full):
        return f"ERROR: no such file: {path}"
    if 'replace' not in args:
        return 'ERROR: replace is required; use an explicit empty replacement only to delete text'
    with open(full, "r", encoding="utf-8", newline='') as f:
        body = f.read()
    search, replace = args.get("search", ""), args.get("replace", "")
    if not search.strip():
        return "ERROR: <search> was empty"
    out, how = flexible_replace(body, search, replace)
    if out is None:
        return (f"ERROR: {how} in {path}. Re-read the file and copy the exact "
                f"text, including indentation.")
    with open(full, "w", encoding="utf-8", newline='') as f:
        f.write(out)
    return f"Edited {path} ({how} match)."


def list_files(ctx, args):
    full = safe_path(ctx.workdir, args.get("path", "."), ctx.settings)
    if not os.path.exists(full):
        return 'ERROR: no such file or directory'
    if os.path.isfile(full):
        return os.path.relpath(full, ctx.workdir)
    skip = ctx.settings.skip_dirs
    out = []
    for root, dirs, files in os.walk(full):
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith('.'))
        depth = root[len(full):].count(os.sep)
        if depth > 3:
            dirs[:] = []
            continue
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            try:
                safe_path(ctx.workdir, os.path.join(root, fn), ctx.settings)
            except PathEscape:
                continue
            out.append(os.path.relpath(os.path.join(root, fn), ctx.workdir))
            if len(out) >= 300:
                return '\n'.join(out + ['... [truncated; narrow the directory]'])
    return "\n".join(out) if out else "(no files)"


def search_files(ctx, args):
    """Regex search with optional surrounding context lines."""
    pattern = args.get("pattern", "")
    if not pattern:
        return 'ERROR: pattern is required'
    base = safe_path(ctx.workdir, args.get("path", "."), ctx.settings)
    try:
        around = int(str(args.get("context", "0")).strip() or 0)
    except ValueError:
        return 'ERROR: context must be an integer line count'
    around = max(0, min(around, 10))

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: bad regex: {e}"

    skip = ctx.settings.skip_dirs
    out, n_hits = [], 0
    if not os.path.exists(base):
        return 'ERROR: no such file or directory'
    walks = [(os.path.dirname(base), [], [os.path.basename(base)])] if os.path.isfile(base) else os.walk(base)
    for root, dirs, files in walks:
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith('.'))
        for fn in sorted(files):
            if fn.startswith('.') and os.path.isdir(base):
                continue
            fp = os.path.join(root, fn)
            try:
                fp = safe_path(ctx.workdir, fp, ctx.settings)
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read(2_000_000).splitlines()
            except (OSError, UnicodeDecodeError, PathEscape):
                continue
            rel = os.path.relpath(fp, ctx.workdir)
            for n, line in enumerate(lines, 1):
                if not rx.search(line):
                    continue
                n_hits += 1
                if around == 0:
                    out.append(f"{rel}:{n}: {line.strip()[:200]}")
                else:
                    lo, hi = max(1, n - around), min(len(lines), n + around)
                    out.append(f"{rel}:{n}:")
                    for i in range(lo, hi + 1):
                        mark = ">" if i == n else " "
                        out.append(f"  {mark}{i:>5}| {lines[i - 1][:200]}")
                    out.append("")
                if n_hits >= (30 if around else 100):
                    out.append("... [more matches; narrow the pattern]")
                    return "\n".join(out)
    return "\n".join(out) if out else "(no matches)"


TOOLS = [
    Tool("read_file", read_file, read_only=True),
    Tool("write_file", write_file),
    Tool("edit_file", edit_file),
    Tool("list_files", list_files, read_only=True),
    Tool("search_files", search_files, read_only=True),
]
