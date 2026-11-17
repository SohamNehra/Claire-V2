"""Scouts: small-model helpers that read material so the brain does not have to.

A scout is not an agent. It gets one bundle of text and one question, makes one
model call, and returns one short answer. Small models are unreliable in long
tool-calling loops and good at exactly this, and it keeps thousands of lines
out of the brain's context. Bundles are read concurrently, which is where a
small model with several slots gives real parallel speed.

The scout role is set in config.py. With no model of its own it runs on the
brain, which still buys the context isolation, just not the speed.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor

from .. import boot, protocol
from ..guard import safe_path
from ..llm import ModelError
from . import PROMPTS, Tool, ToolFault
from .web import fetch_url

MAX_FILES = 40
MAX_FILE_CHARS = 400_000
REPORT_NOTE = ("[scout report -- written by a smaller model from the material "
               "listed below. It can be wrong: read the lines it cites before "
               "you act on them.]")


def _numbered(name, text):
    lines = text.splitlines()
    body = "\n".join(f"{i:>5}| {l}" for i, l in enumerate(lines, 1))
    return f"=== {name} ({len(lines)} lines) ===\n{body}\n"


def gather_files(workdir, raw_paths, skip_dirs, settings=None):
    """Resolve <paths> into [(relative name, text)]. Returns (sources, problems)."""
    sources, problems, seen = [], [], set()
    paths = raw_paths if isinstance(raw_paths, (list, tuple)) else re.split(r'[,\n]+', raw_paths or '')
    for raw in [p.strip() for p in paths if p.strip()]:
        full = safe_path(workdir, raw, settings)
        if os.path.isdir(full):
            def walk():
                for root, dirs, files in os.walk(full):
                    dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith('.'))
                    for name in sorted(files):
                        if not name.startswith('.'):
                            yield os.path.join(root, name)
            found = walk()
        elif os.path.isfile(full):
            found = [full]
        else:
            problems.append(f"no such file or folder: {raw}")
            continue
        for fp in found:
            try:
                fp = safe_path(workdir, fp, settings)
            except ValueError:
                problems.append('skipped protected/outside path: ' + os.path.relpath(fp, workdir))
                continue
            if fp in seen:
                continue
            seen.add(fp)
            if len(sources) >= MAX_FILES:
                raise ValueError(f'more than {MAX_FILES} readable files; narrow paths before scouting')
            try:
                with open(fp, encoding="utf-8") as f:
                    text = f.read(MAX_FILE_CHARS + 1)
            except (OSError, UnicodeDecodeError):
                continue                      # binary or unreadable: nothing to scout
            if len(text) > MAX_FILE_CHARS:
                problems.append(f'{os.path.relpath(fp,workdir)}: tail omitted after {MAX_FILE_CHARS} characters')
                text = text[:MAX_FILE_CHARS]
            sources.append((os.path.relpath(fp, workdir).replace(os.sep, "/"), text))
    return sources, problems


def bundle(sources, budget):
    """Pack sources into bundles of at most `budget` characters, splitting a
    file that is too large on its own while keeping its real line numbers."""
    bundles, current, size = [], [], 0
    for name, text in sources:
        block = _numbered(name, text)
        if len(block) <= budget:
            pieces = [(name, block)]
        else:
            pieces, lines, start = [], text.splitlines(), 0
            while start < len(lines):
                end, used = start, 0
                while end < len(lines) and used + len(lines[end]) + 8 <= budget - 200:
                    used += len(lines[end]) + 8
                    end += 1
                if end == start:
                    raise ValueError(f'{name}: line {start+1} exceeds the scout context budget; extract a smaller part with a targeted command')
                body = "\n".join(f"{i:>5}| {l}" for i, l in
                                 enumerate(lines[start:end], start + 1))
                pieces.append((name, f"=== {name} (lines {start + 1}-{end} of "
                                     f"{len(lines)}) ===\n{body}\n"))
                start = end
        for piece_name, block in pieces:
            if current and size + len(block) > budget:
                bundles.append(current)
                current, size = [], 0
            current.append((piece_name, block))
            size += len(block)
    if current:
        bundles.append(current)
    return bundles


def run_scouts(settings, llm, question, sources, cancel=None):
    """Answer one question over the sources. Returns (report text, stats)."""
    role = "scout"
    if getattr(llm, "has_own_model", lambda r: False)(role):
        problem = boot.ensure_role(settings, role)
        if problem:
            raise ToolFault(problem)
    info = llm.describe(role)
    # Leave room for the instructions and the answer; ~3.6 characters a token.
    budget = int(max(4000, min(info["ctx"] * 0.6 * 3.6, 120_000)))
    bundles = bundle(sources, budget)
    with open(os.path.join(PROMPTS, "scout.md"), encoding="utf-8") as f:
        system = f.read()

    def one(parts):
        material = "\n".join(block for _, block in parts)
        result = llm.chat(
            [{"role": "system", "content": system},
             # Material first, question last: a second question about the
             # same files then reuses the server's cached read of them.
             {"role": "user", "content": f"MATERIAL:\n\n{material}\n\nQUESTION: {question}"}],
            role=role, thinking=False, cancel=cancel, max_tokens=900, temperature=0)
        return result

    workers = max(1, min(settings.scout_parallel, len(bundles)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, bundles))
    except ModelError as exc:
        raise ToolFault(f"the scout model failed: {exc}")

    answers = []
    for parts, result in zip(bundles, results):
        text = protocol.strip_reasoning(result.content, []).strip() or "(no answer)"
        if len(bundles) > 1:
            names = ", ".join(dict.fromkeys(n for n, _ in parts))
            text = f"--- from {names} ---\n{text}"
        answers.append(text)
    stats = {"calls": len(results), "model": info["model"],
             "completion_tokens": sum(r.completion_tokens for r in results),
             "gen_seconds": sum(getattr(r, "gen_seconds", 0.0) for r in results)}
    return "\n\n".join(answers), stats


def scout(ctx, args):
    question = " ".join((args.get("question") or "").split())
    if not question:
        return "ERROR: <question> was empty"
    url = (args.get("url") or "").strip()
    if url:
        page = fetch_url(ctx, {"url": url})
        if page.startswith(("ERROR", "User DENIED")):
            return page
        sources, problems, listing = [(url, page)], [], url
    else:
        # Every other tool takes <path>, so a model reaches for that here too.
        paths = (args.get('paths') or '').strip()
        if not paths and args.get('path'):
            paths = [args['path'].strip()]
        if not paths:
            return "ERROR: give <paths> (files or folders) or a <url> to read"
        try:
            sources, problems = gather_files(ctx.workdir, paths, ctx.settings.skip_dirs, ctx.settings)
        except ValueError as exc:
            return f'ERROR: {exc}'
        if len(sources) > MAX_FILES:
            return (f"ERROR: that is {len(sources)} files; a scout reads at most "
                    f"{MAX_FILES} at once. Narrow <paths>, or use search_files "
                    f"to find the relevant ones first.")
        if not sources:
            return "ERROR: nothing readable there. " + "; ".join(problems)
        listing = ", ".join(f"{n} ({len(t.splitlines())} lines)" for n, t in sources)
    if problems:
        question += '\nSource limitations (do not infer missing tails): ' + '; '.join(problems)
    try:
        report, stats = run_scouts(ctx.settings, ctx.session.llm, question, sources, ctx.session.cancel)
    except ValueError as exc:
        return f'ERROR: {exc}'
    ctx.session.emit("model_call", role="scout", prompt_tokens=0,
                     completion_tokens=stats["completion_tokens"], ttft=0,
                     gen_seconds=round(stats["gen_seconds"], 3))
    out = f"{REPORT_NOTE}\nRead: {listing}\n\n{report}"
    if problems:
        out += "\n\nNot read: " + "; ".join(problems)
    return out


TOOLS = [Tool("scout", scout, read_only=True)]
