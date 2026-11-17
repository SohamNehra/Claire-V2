"""Terminal pieces shared by every Claire client: colours, line editing, the
yes/no prompt, image attachments, the spinner, and prose wrapping.

Nothing here knows about the agent loop. The loop emits events; render.py turns
them into the calls below.
"""

import base64
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time

# Importing readline is what gives input() arrow keys, backspace across wrapped
# lines, and history. Without it the terminal echoes raw escape codes (^[[D).
try:
    import readline
except ImportError:                                   # pragma: no cover
    readline = None

# Serialises writes to stdout so the spinner's cursor moves and the pinned
# footer's save/restore can never interleave into each other's escape sequences.
IO_LOCK = threading.Lock()

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "nobold": "\033[22m",   # turns bold off without the full-reset that "reset" does
    # 24-bit truecolor instead of the basic 16-color codes: the old "pink" was
    # ANSI bright-magenta (95), which most terminal themes render as a dull
    # purple, not pink. Explicit RGB gives the real color regardless of theme.
    "pink": "\033[38;2;255;105;180m",    # hot pink
    "blue": "\033[38;2;78;168;255m",     # vivid sky blue
    "green": "\033[38;2;74;222;128m",    # vivid green
    "yellow": "\033[38;2;255;214;10m",   # vivid yellow
    "red": "\033[38;2;255;92;92m",       # vivid red
    "cyan": "\033[38;2;34;211;238m",     # vivid cyan
    "white": "\033[38;2;255;255;255m",
}
if not sys.stdout.isatty():
    C = {k: "" for k in C}


def c(text, color):
    return f"{C[color]}{text}{C['reset']}"

HISTORY_FILE = os.path.expanduser("~/.claire_history")


def setup_readline():
    """Enable line editing and persistent history for the prompt."""
    if readline is None:
        return
    # macOS ships libedit, which needs different bind syntax to GNU readline.
    if "libedit" in (getattr(readline, "__doc__", "") or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    try:
        readline.read_history_file(HISTORY_FILE)
    except (OSError, PermissionError):
        pass
    readline.set_history_length(1000)
    import atexit
    atexit.register(save_history)


def read_task(prompt):
    """Read one task, keeping a pasted multi-line block together.

    input() returns as soon as the first newline arrives, so a multi-line paste
    is submitted before its remaining lines are even read. We drain whatever the
    terminal left in the buffer -- which only happens for a paste, never for
    human typing -- then show the block and wait for an explicit Enter, so a
    paste is never sent on your behalf.
    """
    first = input(rl_prompt(prompt))
    if not sys.stdin.isatty():
        return first

    import select
    fd = sys.stdin.fileno()
    buf = ""
    while True:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.06)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = os.read(fd, 65536)  # non-blocking: a paste with no trailing
        except OSError:                 # newline must not hang us here
            break
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")

    if not buf.strip():
        return first

    lines = [first] + buf.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return confirm_paste(lines)


def confirm_paste(lines, _preview=14):
    """Show a pasted block and wait for the user to send it."""
    block = "\n".join(lines)
    print(c(f"\n  pasted {len(lines)} lines", "dim"))
    for i, line in enumerate(lines[:_preview], 1):
        shown = line if len(line) <= 96 else line[:93] + "..."
        print(f"  {c(f'{i:>3}', 'dim')}  {c(shown, 'dim')}")
    if len(lines) > _preview:
        print(c(f"       ... {len(lines) - _preview} more lines", "dim"))

    try:
        answer = input(rl_prompt(
            c("\n  Enter", "green") + c(" send  ·  ", "dim")
            + c("e", "yellow") + c(" edit  ·  ", "dim")
            + c("n", "red") + c(" discard   ", "dim"))).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(c("  discarded", "dim"))
        return ""

    if answer in ("n", "no", "d", "q", "discard"):
        print(c("  discarded", "dim"))
        return ""
    if answer in ("e", "edit"):
        return edit_in_editor(block)
    return block


def edit_in_editor(text):
    """Open a block in $EDITOR and return what comes back."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="claire-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.call([editor, path])
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as exc:
        print(c(f"  editor failed ({exc}); sending the block unchanged", "yellow"))
        return text
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def rl_prompt(text):
    """Wrap ANSI escapes in \\001/\\002 so readline measures the prompt width
    correctly. Without this, editing a line that wraps corrupts the display."""
    if readline is None:
        return text
    return re.sub(r"(\033\[[0-9;]*m)", "\001\\1\002", text)


def confirm_action(prompt_word="allow"):
    """Yes/No prompt with an arrow-key-selectable choice, Enter to confirm.

    Left/Right (or Up/Down, or y/n) move the highlight; Enter or y/n confirms
    immediately. Defaults to Yes. Falls back to a plain `Y/n` text prompt when
    stdin isn't a real interactive terminal (piped input, etc.), or on a
    platform with no `termios`/`tty` (native Windows without WSL) -- raw mode
    and arrow keys are a POSIX-terminal feature and don't apply there.
    """
    def plain_prompt():
        try:
            ok = input(f"  {c(f'{prompt_word}? [Y/n] ', 'yellow')}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ok = "n"
        return ok in ("", "y", "yes")

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return plain_prompt()

    import select
    try:
        import termios
        import tty
    except ImportError:
        return plain_prompt()

    options = ["Yes", "No"]
    sel = 0   # defaults to "Yes"
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def render():
        chunks = []
        for i, label in enumerate(options):
            chunks.append(f"\033[7m {label} {C['reset']}" if i == sel
                          else f"{C['dim']} {label} {C['reset']}")
        return (f"  {c(prompt_word + '?', 'yellow')} " + "".join(chunks)
                + c("   ←/→ · y/n · enter", "dim"))

    def read_key():
        ch = os.read(fd, 1)
        if ch != b"\x1b":
            return ch
        # A CSI sequence (arrow keys): drain the rest with a short timeout so
        # a bare Esc keypress doesn't hang waiting for bytes that never come.
        seq = ch
        while select.select([fd], [], [], 0.05)[0]:
            seq += os.read(fd, 1)
            if seq[-1:] in (b"A", b"B", b"C", b"D", b"~"):
                break
        return seq

    with IO_LOCK:
        sys.stdout.write("\r\033[K" + render())
        sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            key = read_key()
            if not key:                      # stdin closed
                sel = 1
                break
            if key in (b"\r", b"\n"):
                break
            if key in (b"y", b"Y"):
                sel = 0
                break
            if key in (b"n", b"N", b"\x03"):  # Ctrl-C denies rather than crashing
                sel = 1
                break
            elif key in (b"\x1b[D", b"\x1b[A"):     # left / up
                sel = 0
            elif key in (b"\x1b[C", b"\x1b[B"):     # right / down
                sel = 1
            elif key == b"\t":
                sel = 1 - sel
            with IO_LOCK:
                sys.stdout.write("\r\033[K" + render())
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        with IO_LOCK:
            sys.stdout.write("\r\033[K" + render() + "\n")
            sys.stdout.flush()

    return sel == 0


def save_history():
    if readline is None:
        return
    try:
        readline.write_history_file(HISTORY_FILE)
    except (OSError, PermissionError):
        pass


# ---------------------------------------------------------------- images

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _pasteboard_image():
    """Read an image from the macOS pasteboard. Returns (b64, mime) or None."""
    try:
        p = subprocess.run(["pbpaste", "-preference", "General"],
                           capture_output=True, timeout=2)
        _ = p  # just checking pbpaste exists
        p = subprocess.run(["osascript", "-e",
                            'the clipboard as «class PNGf»'],
                           capture_output=True, timeout=3)
        if p.returncode != 0:
            return None
        raw = p.stdout
        if not raw:
            return None
        return base64.b64encode(raw).decode(), "image/png"
    except (OSError, subprocess.TimeoutExpired):
        return None


def _file_image(path):
    """Read an image file. Returns (b64, mime) or None."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read(10 * 1024 * 1024)  # 10 MB cap
        mime = mimetypes.guess_type(path)[0] or "image/png"
        return base64.b64encode(raw).decode(), mime
    except OSError:
        return None


def extract_images(text):
    """Find image references in task text.

    Supports:
      - @path/to/image.png  (file reference)
      - [image: path/to/image.png]  (explicit)

    Returns (cleaned_text, [list of (b64, mime)]).
    """
    images = []
    seen = set()

    # @path references
    for m in re.finditer(r"@([\w./~-]+\.\w{3,4})\b", text):
        path = m.group(1)
        if path not in seen and os.path.exists(path):
            img = _file_image(path)
            if img:
                images.append(img)
                seen.add(path)
                text = text.replace(m.group(0), f"[image: {path}]")

    # [image: path] references (catches ones the user typed directly)
    for m in re.finditer(r"\[image:\s*([\w./~-]+)\]", text):
        path = m.group(1)
        if path not in seen and os.path.exists(path):
            img = _file_image(path)
            if img:
                images.append(img)
                seen.add(path)

    return text, images


def build_content(text, images):
    """Build the message content, using the vision format if images present."""
    if not images:
        return text
    parts = [{"type": "text", "text": text}]
    for b64, mime in images:
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    return parts


def term_width():
    # A pty with no size set reports 0; clamp so wrapping never degenerates.
    return max(60, min(shutil.get_terminal_size((100, 24)).columns or 100, 200))


class Spinner:
    """Live status line: braille spinner + streaming reasoning preview.

    Runs on its own thread so the animation stays smooth even while the model
    is quiet during prompt processing.
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self):
        self.label = "thinking"
        self.preview = ""
        self._stop = threading.Event()
        self._thread = None
        self.t0 = time.time()
        self.enabled = sys.stdout.isatty()

    def start(self, label="thinking"):
        if not self.enabled:
            return
        self.label, self.preview = label, ""
        self.t0 = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            elapsed = time.time() - self.t0
            head = f"  {C['pink']}{frame}{C['reset']} {C['dim']}{self.label} {elapsed:.0f}s{C['reset']}"
            line = head
            if self.preview:
                room = term_width() - len(self.label) - 18
                if room > 20:
                    line += f"  {C['dim']}{self.preview[-room:]}{C['reset']}"
            with IO_LOCK:
                sys.stdout.write("\r\033[K" + line)
                sys.stdout.flush()
            i += 1
            time.sleep(0.08)

    def update(self, label=None, preview=None):
        if label:
            self.label = label
        if preview is not None:
            # Collapse whitespace so multi-line reasoning stays on one line.
            self.preview = re.sub(r"\s+", " ", preview)[-400:]

    def stop(self):
        if not self.enabled or self._thread is None:
            return time.time() - self.t0
        self._stop.set()
        self._thread.join(timeout=0.5)
        with IO_LOCK:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        return time.time() - self.t0

BANNER = r"""
   ___  __      _
  / __|| |__ _ (_) _ _  ___
 | (__ | / _` || || '_|/ -_)
  \___||_\__,_||_||_|  \___|
"""


def tilde(path):
    """Shorten $HOME to ~ so paths stay readable in the header."""
    home = os.path.expanduser("~")
    path = str(path)
    return "~" + path[len(home):] if path.startswith(home) else path

def _wrap(text, width):
    """Wrap prose to width, preserving intentional line breaks."""
    out = []
    for para in (text or "").splitlines():
        if not para.strip():
            out.append("")
            continue
        line = ""
        for word in para.split():
            if len(line) + len(word) + 1 > width and line:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        out.append(line)
    return out


def _render_bold(text):
    """Render markdown **bold** spans as ANSI bold, on an already-wrapped line.

    Applied per line (after _wrap, not before) so the escape codes it inserts
    never throw off the wrap-width arithmetic. A stray, unpaired "**" (a bold
    span that got split across two wrapped lines) is left as literal text
    rather than guessed at.
    """
    parts = text.split("**")
    if len(parts) % 2 == 0:   # odd number of ** markers -- can't pair them up
        return text
    out = [parts[0]]
    for i in range(1, len(parts), 2):
        out.append(C["bold"] + parts[i] + C["nobold"])
        if i + 1 < len(parts):
            out.append(parts[i + 1])
    return "".join(out)
