"""Turns engine events into what v1 printed.

The engine no longer knows a terminal exists; this is the one place that does.
The same renderer draws a session running in this process and one running in
the service, because both arrive as the same events.
"""

import re
import sys

from . import tui
from .tui import C, IO_LOCK, c


class Renderer:
    def __init__(self):
        self.spin = None
        self.used, self.limit = 0, 0
        self._reset_answer()

    def _reset_answer(self):
        # col/bold track the cursor column and open-bold state across streamed
        # writes, so a **bold** span or a wrapped line can split across two
        # network chunks without either losing track.
        self.answer = {"streaming": False, "shown": 0, "col": 0, "bold": False}
        self.raw = ""

    # -- spinner -------------------------------------------------------------

    def _spin_start(self, label="thinking"):
        self._spin_stop()
        self.spin = tui.Spinner()
        self.spin.start(label)

    def _spin_stop(self):
        if self.spin is not None:
            self.spin.stop()
            self.spin = None

    def close(self):
        self._spin_stop()

    # -- streaming answer ----------------------------------------------------

    def _stream_write(self, segment, width, indent):
        """Write one already-safe-to-show segment of a streaming answer,
        word-wrapping at `width` and rendering **bold** as a running toggle."""
        a = self.answer
        for token in re.split(r"(\s+)", segment):
            if not token:
                continue
            if token.isspace():
                if "\n" in token:
                    sys.stdout.write("\n" + indent)
                    a["col"] = len(indent)
                else:
                    sys.stdout.write(token)
                    a["col"] += len(token)
                continue
            pieces = token.split("**")
            vis_len = sum(len(p) for p in pieces)   # width without the ** markers
            if a["col"] + vis_len > width and a["col"] > len(indent):
                sys.stdout.write("\n" + indent)
                a["col"] = len(indent)
            for i, piece in enumerate(pieces):
                if i > 0:
                    a["bold"] = not a["bold"]
                    sys.stdout.write(C["bold"] if a["bold"] else C["nobold"])
                sys.stdout.write(piece)
            a["col"] += vis_len
        sys.stdout.flush()

    def _stream_flush(self, width, indent="    "):
        """Show whatever new text is safe. Holds back the word still being
        typed, so a chunk that splits a word or a "**" marker never prints
        half of it now and the rest later."""
        raw, shown = self.raw, self.answer["shown"]
        cut = max(raw.rfind(" ", shown), raw.rfind("\n", shown), raw.rfind("\t", shown))
        end = cut + 1 if cut >= shown else shown
        if end <= shown:
            return
        self._stream_write(raw[shown:end], width, indent)
        self.answer["shown"] = end

    def _stream_finish(self, width, indent="    "):
        if len(self.raw) > self.answer["shown"]:
            self._stream_write(self.raw[self.answer["shown"]:], width, indent)
            self.answer["shown"] = len(self.raw)
        if self.answer["bold"]:
            sys.stdout.write(C["nobold"])
            self.answer["bold"] = False

    # -- blocks --------------------------------------------------------------

    def _thinking(self, text):
        if not (text or "").strip():
            return
        width = max(30, tui.term_width() - 8)
        with IO_LOCK:
            print(f"\n  {c('◇ thinking', 'dim')}")
            for line in tui._wrap(text.strip(), width):
                print(f"    {c(line, 'dim')}" if line else "")
            print()

    def _done_line(self, e):
        depth = c(" ◆", "yellow") if e.get("boosted") else ""
        return (f"  {c('✔', 'green')} {c('done', 'green')}{depth} "
                f"{c(format(e.get('elapsed', 0), '.0f') + 's', 'dim')}")

    def say(self, text):
        """Print a finished answer."""
        with IO_LOCK:
            print(f"  {c('●', 'pink')} {c('claire', 'pink')}")
            for line in tui._wrap(text, tui.term_width() - 6):
                print(f"    {c(tui._render_bold(line), 'white')}" if line else "")
            print()

    # -- events --------------------------------------------------------------

    def handle(self, e):
        handler = getattr(self, "_on_" + e["type"], None)
        if handler:
            handler(e)

    def _on_step(self, e):
        self._reset_answer()
        self._spin_start("thinking")

    def _on_usage(self, e):
        self.used, self.limit = e["used"], e["limit"]
        if self.spin:
            self.spin.update(f"thinking · ctx {e['used'] // 1000}k")

    def _on_status(self, e):
        if self.spin:
            self.spin.update(e.get("label"), e.get("preview"))

    def _on_compacted(self, e):
        self._spin_stop()
        detail = f"{e['before']} -> ~{e['after']} tokens"
        print(f"  {c('◈', 'yellow')} {c('compacted context', 'yellow')} {c(detail, 'dim')}")
        self._spin_start("thinking")

    def _on_thinking(self, e):
        self._spin_stop()
        self._thinking(e.get("text"))

    def _on_tool_call(self, e):
        self._spin_stop()
        depth = c(" ◆", "yellow") if e.get("boosted") else ""
        detail = str(e.get("detail", ""))[:tui.term_width() - 30]
        print(f"  {c('⏺', 'cyan')} {c(e['name'], 'cyan')} {c(detail, 'bold')}{depth} "
              f"{c(format(e.get('elapsed', 0), '.0f') + 's', 'dim')}")

    def _on_tool_result(self, e):
        failed = e.get("failed")
        mark = c("✗", "red") if failed else c("└", "dim")
        body = c(str(e.get("first", ""))[:tui.term_width() - 8],
                 "red" if failed else "white")
        print(f"    {mark} {body}")

    def _on_tool_fault(self, e):
        where = "  repair note left in the vault inbox" if e.get("note") else ""
        print(f"    {c('⚑', 'yellow')} {c(e['name'] + ' is broken', 'yellow')}"
              f"{c(where, 'dim')}")

    def _on_answer_start(self, e):
        self._spin_stop()
        with IO_LOCK:
            self.answer.update(streaming=True, col=4, bold=False)
            reasoning = (e.get("reasoning") or "").strip()
            if reasoning:
                sys.stdout.write(f"\n  {C['dim']}◇ thinking{C['reset']}\n")
                for line in tui._wrap(reasoning, max(30, tui.term_width() - 8)):
                    sys.stdout.write(f"    {C['dim']}{line}{C['reset']}\n")
            sys.stdout.write(f"\n  {C['pink']}● claire{C['reset']}\n    " + C["white"])
            sys.stdout.flush()

    def _on_answer_delta(self, e):
        if not self.answer["streaming"]:
            return
        self.raw += e.get("text", "")
        with IO_LOCK:
            self._stream_flush(max(30, tui.term_width() - 6))

    def _on_answer(self, e):
        self._spin_stop()
        if e.get("streamed") and self.answer["streaming"]:
            with IO_LOCK:
                # Streaming only ever showed text up to the last confirmed word
                # boundary -- flush the rest now that the stream is closed.
                self._stream_finish(max(30, tui.term_width() - 6))
                print(C["reset"] + "\n")
                print(self._done_line(e))
                print()
        else:
            print(self._done_line(e))
            print()
            self.say(e.get("text", ""))
        self._reset_answer()

    def _on_answer_discard(self, e):
        if self.answer["streaming"]:
            with IO_LOCK:
                self._stream_finish(max(30, tui.term_width() - 6))
                print(C["reset"] + c("  (withdrawn)", "dim") + "\n")
        self._reset_answer()

    def _on_notice(self, e):
        self._spin_stop()
        level, text = e.get("level"), e.get("text", "")
        if level == "warn" and text.startswith("no tool call"):
            print(f"  {c('↺', 'yellow')} {c(text, 'yellow')}")
        elif level == "warn":
            print(f"  {c('◈', 'yellow')} {c(text, 'yellow')}")
        elif level == "error":
            head, _, rest = text.partition("\n")
            print(c(f"  {head}", "red"))
            if rest:
                print(c(rest, "dim"))
        else:
            print(c(f"  {text}", "yellow"))

    def _on_end(self, e):
        self._spin_stop()
        if e.get("status") == "cancelled":
            print(c("  cancelled", "yellow"))

    # -- approvals -----------------------------------------------------------

    def ask(self, request):
        """Show a request and read yes or no from the keyboard."""
        self._spin_stop()
        word = {"command": "run:", "search": "search:", "fetch": "fetch:",
                "network": "network:"}.get(request.get("kind"), "allow:")
        flag = request.get("flag") or ""
        flag = c(f" {flag} ", "red") if flag else ""
        print(f"\n  {c(word, 'yellow')} {c(request.get('title', ''), 'bold')} {flag}")
        return tui.confirm_action()
