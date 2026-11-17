"""The agent loop.

A Session is one conversation: its messages, its working directory, its tools
and its modes. `run()` takes one task to completion and reports everything that
happens as events to a sink. It never prints and never prompts -- the terminal,
the service and the tests are all just different sinks and approvers, which is
what lets several sessions run side by side.

Events (each a dict with "type", "thread", "ts", plus the fields shown):

    step          n, thinking            a model turn is starting
    status        label, preview         live: what the model is emitting
    usage         used, limit            context after the latest step
    compacted     before, after
    thinking      text                   reasoning + rationale behind a step
    tool_call     name, detail, elapsed, boosted
    tool_result   name, first, failed, fault
    tool_fault    name, error, note      a tool is broken; repair note filed
    answer_start  reasoning              the answer has begun streaming
    answer_delta  text                   live: next piece of the answer
    answer        text, elapsed, boosted, streamed
    answer_discard                       the streamed answer was sent back; drop it
    model_call    role, prompt_tokens, completion_tokens, cached_tokens, ttft, prime_seconds, gen_seconds
    notice        level, text            retries, the step cap, errors
    end           status, answer         the task is over
"""

import os
import json
import re
import threading
import time
import traceback

from . import boot, controller, prompting, protocol
from .connectors import Connectors
from .guard import PathEscape, Sandbox
from .llm import LLM, Cancelled, ContextOverflow, ModelError
from .power import ModelPower
from .settings import Settings
from .skillset import Skills
from .skillroute import with_requirements
from .store import Store, new_id
from .tools import Registry, ToolContext, ToolFault
from .tools.workshop import load_workshop
from .vault import Vault

# Too frequent to be worth storing; they are only ever shown live.
EPHEMERAL = {"status", "answer_delta", "answer_discard"}

# Models write the apostrophe both straight and curly; "I’ll start" is as
# much an announcement as "I'll start".
_ACTION_INTENT = re.compile(
    r"\b(i['’]?ll|i will|let me|i['’]?m (going to|on it)|i am going to|first,? i|"
    r"i need to|i should|let['’]?s (start|begin|look|check|read|run)|"
    r"starting|reading|checking|running|searching|looking at)\b", re.I)


# Narrower than the above, for judging a finished reply: only a first-person
# promise counts. "It is not running" is a statement; "I'll run it" is a promise.
_PROMISE = re.compile(
    r"\b(i['’]?ll|i will|i['’]?m (going to|about to|on it|starting|beginning)|"
    r"i am (going to|about to|starting|beginning)|let me (?!know)|"
    r"starting (a|the) task)\b", re.I)


def intends_to_act(text):
    """True when prose reads as a preamble to work rather than a finished reply.

    "Hey! What can I do for you?" is an answer. "Let me read the config first"
    is an announcement of a tool call that never came, and must be retried
    rather than accepted as the result.
    """
    head = " ".join(text.split())[:400]
    return bool(_ACTION_INTENT.search(head))


def _content(text, images):
    """A user message, in the vision format when images are attached."""
    if not images:
        return text
    parts = [{"type": "text", "text": text}]
    for b64, mime in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{b64}"}})
    return parts


# A worker that has changed files and then runs this many steps without
# changing anything is told to wrap up, and at the second number is stopped.
STALL_NUDGE = 12
STALL_STOP = 24
# At temperature 0 the same context gives the same next step, so a model that
# has repeated a call twice tends to copy itself again even after being told
# not to (seen live: it ignored the nudge and ran the same check a fourth
# time). The step after a repeat nudge is sampled with some freedom instead.
UNSTICK_TEMPERATURE = 0.7

_ERROR_LINE = re.compile(r"error|exception|traceback|fail|denied|not found|no such", re.I)


def _headline(output, failed, limit=300):
    """The line of a result that says what happened. For a failure, the last
    line that reads like an error: the last line is often a footer (Node
    ends a crash with its version), not the error."""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    if failed:
        for line in reversed(lines):
            if _ERROR_LINE.search(line):
                return line[:limit]
    return (lines[-1] if lines else "")[:limit]

class Core:
    """The shared parts every session is built from. One per process."""

    def __init__(self, settings=None, llm=None):
        self.settings = settings or Settings()
        self.settings.ensure_state_dir()
        self.llm = llm or LLM(self.settings)
        self.power = ModelPower(self.settings, on_sleep=getattr(self.llm, "invalidate", None))
        if isinstance(self.llm, LLM):
            self.llm.power = self.power
        self.sandbox = Sandbox(self.settings)
        self.store = Store(self.settings.db_path)
        self.vault = Vault(self.settings.vault,
                           os.path.join(self.settings.state_dir, "index.db"))
        self.registry = Registry().load_builtin()
        if self.sandbox.available:
            load_workshop(self.registry, self.settings)
        self.connectors = Connectors(self)
        from .computer import Computer
        self.computer = Computer(self)
        self.skills = Skills(self.settings.vault)
        from .projects import Projects
        from .autonomy import HumanFree
        self.projects = Projects(self)
        self.human_free = HumanFree(self.settings)
        self.hub = None

    def capabilities(self):
        caps = set()
        if self.sandbox.available:
            caps.add("sandbox")
        if self.hub is not None:
            caps.add("hub")
        if self.connectors.connected() or self.connectors.failing():
            caps.add("connectors")
        return caps


class Session:
    def __init__(self, core, kind="agent", workdir=None, thread_id=None,
                 sink=None, approver=None, thinking=None, auto_approve=None,
                 messages=None, persist=True, skills=(), role="brain", profile=""):
        self.core = core
        self.settings = core.settings
        self.llm, self.store, self.vault = core.llm, core.store, core.vault
        self.registry, self.sandbox, self.hub = core.registry, core.sandbox, core.hub
        self.kind = kind
        self.role, self.profile = role, profile
        self.workdir = os.path.realpath(workdir or os.getcwd())
        self.thread_id = thread_id or new_id()
        self.sink = sink or (lambda event: None)
        self.approver = approver            # callable(request dict) -> bool
        self.thinking = self.settings.thinking if thinking is None else thinking
        self.auto_approve = (self.settings.auto_approve if auto_approve is None
                             else auto_approve)
        self.persist = persist
        self.cancel = threading.Event()
        self.tools = self.registry.names_for(kind, core.capabilities())
        self.tools = self._profile_tools(self.tools)
        # Skills this thread starts with. Unknown names are dropped rather than
        # failing the thread: a skill may have been removed since it was named.
        wanted = list(skills) + (core.skills.always() if kind == "agent" else [])
        self.skills = with_requirements(core.skills, wanted)
        self.model = self.llm.describe(self.role)
        self.ctx_used = 0
        self._last_prompt_tokens = 0
        self._noted_faults = set()
        self.messages = messages or [{"role": "system", "content": self.system_prompt()}]

    # -- plumbing ------------------------------------------------------------

    def system_prompt(self):
        return prompting.build(self, self.model)

    def project(self):
        """The project this thread belongs to, or None. Read each time, so a
        chat moved to another project picks up that project's memory."""
        row = self.store.thread(self.thread_id)
        if row and row.get("project"):
            return self.store.project(row["project"])
        if self.kind == "agent":
            return self.store.project_for_path(self.workdir)
        return None

    def _profile_tools(self, names):
        if self.profile == "controller":
            return [n for n in names if n in controller.TOOLS]
        if self.profile == "worker":
            return [n for n in names if n in controller.WORKER_TOOLS]
        return [n for n in names if n not in ("local_files", "tool_help", "conversation_history")]

    def reset(self):
        """Clear the conversation; long-term memory is reloaded from the vault."""
        self.model = self.llm.describe(self.role, refresh=True)
        self._active_request = None
        if hasattr(self.core,'computer'):self.core.computer.forget(self.thread_id)
        self._local_locations = []
        self._turn_outcomes = []
        self._local_read_chars = 0
        self._local_last_read = None
        self.messages = [{"role": "system", "content": self.system_prompt()}]
        self._last_prompt_tokens = 0
        self.ctx_used = 0
        self._save()

    def emit(self, type_, **data):
        event = {"type": type_, "thread": self.thread_id, "ts": time.time()}
        event.update(data)
        self.sink(event)

    def ask(self, request):
        """Put a question to a human. No approver means no."""
        if self.approver is None:
            return False
        return bool(self.approver(dict(request, thread=self.thread_id)))

    def add_tool(self, name):
        if name not in self.tools:
            self.tools.append(name)

    def step_budget(self):
        """Steps this turn may take: the configured cap, or more when a loaded
        skill's work needs it (building an integration takes research, files
        and several check-and-fix rounds)."""
        if self.profile == "controller":
            using_computer=any(n=="computer" for n,_,_,_ in getattr(self,"_turn_outcomes",[]))
            return max(self.settings.controller_max_steps,self.settings.computer_max_steps) if using_computer else self.settings.controller_max_steps
        needs = [s.get("steps") or 0 for s in (self.core.skills.get(n) for n in self.skills) if s]
        return max([self.settings.max_steps] + needs)

    def load_skill(self, name):
        """Add a skill's instructions to this thread from now on, with the
        skills it requires."""
        added = [n for n in with_requirements(self.core.skills, [name]) if n not in self.skills]
        if added:
            self.skills.extend(added)
            self.messages[0] = {"role": "system", "content": self.system_prompt()}
            if self.persist:
                self.store.update_thread(self.thread_id, skills=",".join(self.skills))

    def refresh(self):
        """Pick up tools and connected services that changed since this
        session began, without disturbing the conversation."""
        current = self.registry.names_for(self.kind, self.core.capabilities())
        extra = [n for n in self.tools if n not in current and self.registry.get(n)
                 and self.kind in self.registry.get(n).kinds
                 and "connectors" not in self.registry.get(n).needs]
        self.tools = self._profile_tools(current + extra)
        self.messages[0] = {"role": "system", "content": self.system_prompt()}

    def _save(self):
        if self.persist:
            self.store.save_messages(self.thread_id, self.messages)

    @property
    def ctx_limit(self):
        return self.model["ctx"]

    def est_tokens(self):
        """Token count for the conversation: the server's own prompt_tokens
        when we have them, else ~3.6 characters per token."""
        chars = 0
        for m in (controller.messages(self) if self.profile == "controller" else self.messages):
            body = m.get("content") or ""
            if isinstance(body, list):
                for part in body:
                    if part.get("type") == "text":
                        chars += len(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        chars += 3600          # ~1000 tokens
            else:
                chars += len(body)
        # The real count lags by one turn, so take whichever is larger.
        return max(int(chars / 3.6), self._last_prompt_tokens)

    # -- model calls ---------------------------------------------------------

    def _call(self, thinking, stream_state, temperature=None):
        last_reasoning = [""]
        streamed = {"content": "", "reasoning": "", "reported": 0}
        base_used = self.est_tokens()

        def on_delta(kind, text):
            streamed[kind] = text
            chars = len(streamed["content"]) + len(streamed["reasoning"])
            if chars - streamed["reported"] >= 512:
                streamed["reported"] = chars
                self.emit("usage", used=base_used+int(chars/3.6), limit=self.ctx_limit, estimated=True)
            if kind == "reasoning":
                last_reasoning[0] = text
                self.emit("status", label="thinking", preview=text[-400:])
                return
            label, preview = protocol.phase(text)
            if label == "answering":
                raw = protocol.streaming_summary(text) or ""
                if raw:
                    if not stream_state["streaming"]:
                        stream_state["streaming"] = True
                        self.emit("answer_start", reasoning=last_reasoning[0].strip())
                    if len(raw) > stream_state["sent"]:
                        self.emit("answer_delta", text=raw[stream_state["sent"]:])
                        stream_state["sent"] = len(raw)
                    return
            self.emit("status", label=label, preview=preview[-400:])

        request = controller.messages(self) if self.profile == "controller" else self.messages
        if self.profile == "controller":
            thinking = bool(self.thinking and self.role == "brain")
        self.emit("status", label="processing context", preview="")
        self.emit("inference_start", role=self.role, model=self.model["model"],
                  estimated_tokens=self.est_tokens())
        extra = ({"response_format": controller.response_format(self.tools), "temperature": 0}
                 if self.profile == "controller" else {})
        if temperature is not None:
            extra["temperature"] = temperature
        primed = 0.0
        prime = getattr(self.llm, "prime", None)
        if self.profile == "controller" and prime and len(request) > 2:
            # Everything but the runtime facts, which are rewritten every call.
            primed = prime(request[:-1], role=self.role, cancel=self.cancel)
        result = self.llm.chat(request, role=self.role, thinking=thinking,
                               max_tokens=700 if self.profile == "controller" and not thinking else None,
                               on_delta=on_delta, cancel=self.cancel, **extra)
        if result.prompt_tokens:
            self._last_prompt_tokens = result.prompt_tokens
        self.emit("model_call", role=self.role, model=self.model["model"], prompt_tokens=result.prompt_tokens,
                  completion_tokens=result.completion_tokens,
                  cached_tokens=getattr(result, "cached_tokens", None),
                  finish_reason=getattr(result, "finish_reason", None),
                  ttft=round(getattr(result, "ttft", 0.0), 3),
                  prime_seconds=round(primed, 3),
                  gen_seconds=round(getattr(result, "gen_seconds", 0.0), 3))
        return result

    def compact(self):
        """Summarise older turns so a long session never overruns the context.

        Keeps the system prompt and the most recent exchanges verbatim;
        everything older is replaced by a single summary message. Returns True
        if anything changed.
        """
        messages = self.messages
        if len(messages) < 8:
            return False
        system, keep, older = messages[0], messages[-6:], messages[1:-6]
        if not older:
            return False

        transcript = []
        for m in older:
            body = m.get("content") or ""
            if isinstance(body, list):
                text = " ".join(p.get("text", "") for p in body if p.get("type") == "text")
                n_img = sum(1 for p in body if p.get("type") == "image_url")
                body = text + (f" [+{n_img} image(s)]" if n_img else "")
            transcript.append(f"{m['role'].upper()}: {body[:1500]}")
        joined = "\n\n".join(transcript)[:30000]

        if self.kind == "home":
            brief = ("Summarise this conversation between Claire and the man she calls sir. "
                     "Preserve: what he asked for and decided, tasks started and "
                     "how each turned out, anything promised or still open, and "
                     "anything he said about how he wants things done. Be dense "
                     "and factual.")
        else:
            brief = ("Summarise this coding-session transcript. Preserve: files "
                     "created or modified and their purpose, decisions made, "
                     "commands that worked or failed, and anything still "
                     "outstanding. Be dense and factual.")
        self.emit("status", label="compacting context", preview="")
        # The clerk's own model if it has one and it is up; otherwise the brain,
        # which is already running or we would not be here.
        role = "brain"
        if self.settings.roles.get("clerk", {}).get("api_base"):
            if not boot.ensure_role(self.settings, "clerk"):
                role = "clerk"
        try:
            summary = self.llm.chat(
                [{"role": "system", "content": brief},
                 {"role": "user", "content": joined}],
                role=role, thinking=False, cancel=self.cancel).content
        except ModelError:
            summary = ""
        summary = protocol.strip_reasoning(summary, self.tools)

        self._last_prompt_tokens = 0         # stale: it measured the old history
        notes = []
        if summary.strip():
            notes.append({"role": "user", "content": f"[earlier context, compacted]\n{summary.strip()}"})
        # The summarizer can omit the goal, constraints or last acceptance
        # criterion. Keep the actual current request, not its interpretation.
        request = getattr(self, "_active_request", None)
        if request is not None:
            notes.append({"role": "user", "content": "[current request, preserved verbatim]\n" + request})
        self.messages = [system] + notes + keep
        return True

    # -- tools ---------------------------------------------------------------

    def _copied_example(self, name, args):
        """True when a call repeats, word for word, a long example from the
        tool's own documentation -- a small model echoing its instructions,
        not something sir asked for."""
        tool = self.registry.get(name)
        cache = self.__dict__.setdefault("_examples", {})
        if name not in cache:
            examples = []
            try:
                doc = tool.doc or ""
            except OSError:
                doc = ""
            for m in re.finditer(rf"<{name}\s*>(.*?)</{name}\s*>", doc, re.DOTALL):
                ex = protocol.parse_params(m.group(1), name, tool.params)
                if any(len(str(v)) >= 80 for v in ex.values()):
                    examples.append({k: " ".join(str(v).split()) for k, v in ex.items()})
            cache[name] = examples
        given = {k: " ".join(str(v).split()) for k, v in (args or {}).items()}
        return any(given == ex for ex in cache[name])

    def _execute(self, name, args):
        """Run one tool. Returns (result_text, failed, fault)."""
        tool = self.registry.get(name)
        if tool is None or tool.handler is None:
            return f'ERROR: tool {name!r} is not available; choose an available tool', True, None
        if self._copied_example(name, args):
            return (f"ERROR: that is the example from {name}'s documentation, copied word for "
                    f"word -- not something sir asked for. Nothing was done. Do only what the "
                    f"request in front of you asks."), True, None
        ctx = ToolContext(self)
        self._result_images = ctx.images
        fault = trace = None
        try:
            result = tool.handler(ctx, args)
            if not isinstance(result, str):
                result = "" if result is None else str(result)
        except PathEscape as exc:
            result = f"ERROR: {exc}"
        except ToolFault as exc:
            fault = str(exc)
        except Cancelled:
            result = 'ERROR: operation cancelled'
        except OSError as exc:
            result = f'ERROR: {type(exc).__name__}: {exc}'
        except Exception as exc:              # a bug in the tool, not the request
            fault = f"{type(exc).__name__}: {exc}"
            trace = traceback.format_exc()

        if fault is None:
            self.store.tool_ok(name)
            first = result.splitlines()[0] if result else ""
            exit_status = re.match(r'^exit=(-?\d+)\b', first)
            failed = (result.startswith(("ERROR", "REFUSED", "User DENIED", "Sir did not approve"))
                      or (exit_status is not None and int(exit_status.group(1)) != 0))
            return result, failed, None
        return self._on_fault(tool, args, fault, trace), True, fault

    def _on_fault(self, tool, args, fault, trace):
        """A tool is broken. Record it, leave sir a repair note, and tell the
        model how to get the result another way instead of retrying."""
        name = tool.name
        self.store.tool_fault(name, fault)
        can_build = "run_code" in self.tools
        note_path = None

        if tool.source == "workshop":
            text = (f"TOOL FAULT: your workshop tool `{name}` failed:\n{fault}\n\n"
                    f"The fault is in the tool's code, not in your request, so "
                    f"the same call will fail the same way. Either repair it "
                    f"(call make_tool again with the same name and corrected "
                    f"code) or get the result another way.")
        else:
            # One repair note per distinct error per session: enough for sir to
            # act on, without a page of duplicates when a tool fails in a loop.
            signature = (name, fault.splitlines()[0][:160])
            if signature not in self._noted_faults:
                self._noted_faults.add(signature)
                shown_args = "\n".join(f"- {k}: {str(v)[:300]}" for k, v in args.items())
                entry = (f"`{name}` failed while I was working in `{self.workdir}` "
                         f"(thread `{self.thread_id}`).\n\n**Error:** {fault}\n\n"
                         f"**Called with:**\n{shown_args or '- (no arguments)'}")
                if trace:
                    entry += f"\n\n```\n{trace.strip()[-3000:]}\n```"
                note_path = self.vault.inbox_note(
                    f"tool-{name}", f"`{name}` needs repair", entry)
            text = (f"TOOL FAULT: `{name}` is broken ({fault}).\n\n"
                    f"This is a defect in the tool itself, not a mistake in your "
                    f"request, so repeating or rephrasing the call will not help. "
                    f"A repair note has been left for the user.")
            if can_build:
                text += (f"\n\nGet the result another way: write the code that "
                         f"does this job and run it with run_code. If it works "
                         f"and you will need it again, keep it with make_tool, "
                         f"passing <replaces>{name}</replaces>. Mention in your "
                         f"final summary that `{name}` was broken and what you "
                         f"used instead.")
            else:
                text += ("\n\nContinue with the tools that work, and say in "
                         "your final summary that this tool was broken and what "
                         "you could not do because of it.")
        self.emit("tool_fault", name=name, error=fault, note=note_path)
        return text

    # -- the loop ------------------------------------------------------------

    def _drop_old_images(self, keep):
        """A screenshot is only useful until the next one. Keep the latest
        few and replace the rest with a note, so watching the screen for many
        steps does not fill the context with stale pictures."""
        seen = 0
        for message in reversed(self.messages):
            parts = message.get("content")
            if not isinstance(parts, list):
                continue
            for i in range(len(parts) - 1, -1, -1):
                if parts[i].get("type") == "image_url":
                    seen += 1
                    if seen > keep:
                        parts[i] = {"type": "text", "text": "[an earlier screenshot, no longer shown]"}

    def _end(self, status, answer=""):
        self.ctx_used = self.est_tokens()
        self._save()
        self.emit("end", status=status, answer=answer)
        return {"status": status, "answer": answer}

    def run(self, task, images=None, preface=None, reset_cancel=True):
        """Run one task to completion. Returns {"status", "answer"}.

        `preface` is a line shown to the model ahead of the message but not
        part of it -- the current time, say -- so it never appears as
        something sir typed.
        """
        original_role, original_model = self.role, self.model
        try:
            return self._run(task, images, preface, reset_cancel)
        finally:
            if self.profile == 'controller':
                self.role, self.model = original_role, original_model
            # Done, failed or cancelled: nothing opened for this job (a
            # browser, say) outlives it.
            self.core.connectors.release(self)

    def _run(self, task, images, preface, reset_cancel):
        if reset_cancel:
            self.cancel.clear()
        self.refresh()       # current shared project work, including other task reports
        text = f"{preface}\n{task}" if preface else task
        self._active_request = text
        self._turn_message_start = len(self.messages)
        self.messages.append({"role": "user", "content": _content(text, images)})
        bad_parses = 0
        failures_in_a_row = 0
        # Each exact call and its result, and how often it has come back the
        # same since a file last changed (an edit resets it: re-running tests
        # after each edit is progress, re-running them with nothing changed is not).
        seen_since_change = {}
        # Steps since the work last changed, counted once something has been
        # changed: a worker that has written its files and then probes on and
        # on (each probe slightly different) is stalled, not verifying.
        steps_since_change = None
        outcomes = []             # actual receipts, never model descriptions of them
        self._turn_outcomes = outcomes
        self._turn_touched = []   # files that commands changed, from the folder itself
        self._declined = set()    # flagged service actions sir said no to (or left unanswered)
        self._local_read_chars = 0
        self._local_last_read = None
        grounded_retry = False
        acted = False              # has any tool actually run in this turn?
        challenged = False         # an empty promise is sent back once, not forever
        # Thinking escalates on trouble and decays back to the default when
        # things go smoothly, so simple work stays fast and hard patches get
        # more depth.
        boost_left = 0
        loosen = False             # sample the next step more freely (after a repeat nudge)
        step = 0
        # The budget is read every step: a skill loaded partway through (the
        # build tool loads its own) can need more room than the default.
        while step < self.step_budget():
            step += 1
            if self.cancel.is_set():
                return self._end("cancelled")
            # Worker reasoning stays stable for the turn: toggling it after
            # every tool error changes Qwen's template prefix and discards a
            # long cached prompt. Explicit worker thinking is still honored.
            active = (self.thinking or (boost_left > 0 and self.profile != "worker")) and self.profile != "controller"
            if self.profile == "controller":
                active = bool(self.thinking and self.role == "brain")
            boosted = active and not self.thinking
            stream = {"streaming": False, "sent": 0}
            self.emit("step", n=step, thinking=active)

            # Keep the conversation inside the server's context window.
            used = self.ctx_used = self.est_tokens()
            self.emit("usage", used=used, limit=self.ctx_limit)
            if self.profile != "controller" and used > self.ctx_limit * self.settings.compact_at and self.compact():
                self.emit("compacted", before=used, after=self.est_tokens())

            t0 = time.time()
            temperature = UNSTICK_TEMPERATURE if loosen else None
            loosen = False
            try:
                try:
                    result = self._call(active, stream, temperature)
                except ContextOverflow:
                    # Emergency compaction, then retry this step once.
                    self.emit("notice", level="warn", text="context full - compacting")
                    self.compact()
                    result = self._call(active, stream, temperature)
            except Cancelled:
                return self._end("cancelled")
            except ContextOverflow:
                self.emit("notice", level="error",
                          text="context still too large after compacting. Raise "
                               f"it: CLAIRE_CTX and llama-server -c (currently "
                               f"{self.ctx_limit})")
                return self._end("error")
            except ModelError as exc:
                self.emit("notice", level="error", text=str(exc))
                return self._end("error")
            reply, deep = result.content, result.reasoning
            elapsed = time.time() - t0
            if boost_left > 0:
                boost_left -= 1

            if getattr(result, "finish_reason", None) == "length":
                # The XML parser can recover a missing closing tag, but it
                # cannot know whether a cut-off program is complete. Never
                # execute a tool or publish an answer known to be truncated.
                bad_parses += 1
                if stream["streaming"]:
                    self.emit("answer_discard")
                self.emit("notice", level="warn", text="model output reached its token cap; nothing executed")
                if bad_parses > 2:
                    return self._end("stopped")
                self.messages.append({"role": "assistant", "content": reply[:2000]})
                self.messages.append({"role": "user", "content":
                    "Your output reached the model token limit before completion. Nothing was executed. "
                    "Return one smaller complete tool call, split long file changes across edits, "
                    "and keep the explanation brief. Do not repeat the same oversized output."})
                continue

            if not reply.strip():
                bad_parses += 1
                if bad_parses > 2:
                    self.emit("notice", level="error",
                              text="model returned nothing three times; stopping.")
                    return self._end("stopped")
                format_hint = ("JSON decision" if self.profile == "controller" else "XML tool call")
                self.messages.append({"role": "user", "content":
                                      "You returned an empty message. Respond with "
                                      f"exactly one {format_hint} and nothing else."})
                continue

            calls = (controller.parse(reply, self.tools) if self.profile == "controller" else
                     protocol.parse_tool_calls(reply, self.tools, self.registry.extra_params(self.tools)))
            name, args = calls[0] if calls else (None, None)
            rationale = "" if self.profile == "controller" else protocol.prose_outside(reply, self.tools).strip()

            def thinking_text(include_prose=True):
                parts = [deep] if deep else []
                # Don't repeat the rationale if it is already in the reasoning.
                if include_prose and rationale and rationale not in deep:
                    parts.append(rationale)
                return "\n".join(parts)

            if name is None:
                if self.profile == "controller":
                    bad_parses += 1
                    if bad_parses > 2:
                        return self._end("stopped")
                    self.messages.append({"role": "assistant", "content": reply[:1000]})
                    self.messages.append({"role": "user", "content":
                        'Return exactly one JSON object with tool and arguments. Use an available tool; '
                        'to answer, use {"tool":"done","arguments":{"summary":"actual answer"}}.'})
                    continue
                # Conversation, not a task. "hey bud" gets answered in prose
                # with no tool call, which is the right reply -- so take it as
                # the answer instead of nagging for XML. Only on the first step:
                # later on, prose with no tool call means it lost the thread.
                if step == 1 and rationale and not intends_to_act(rationale):
                    if deep:
                        self.emit("thinking", text=deep)
                    self.messages.append({"role": "assistant", "content": reply})
                    self.ctx_used = self.est_tokens()
                    self.emit("usage", used=self.ctx_used, limit=self.ctx_limit, estimated=True)
                    self.emit("answer", text=rationale, elapsed=elapsed,
                              boosted=boosted, streamed=False)
                    return self._end("done", rationale)

                bad_parses += 1
                if bad_parses > 2:
                    self.emit("notice", level="error",
                              text="no valid tool call after 3 tries; stopping.\n"
                                   + reply[:400])
                    return self._end("stopped")
                self.emit("notice", level="warn",
                          text=f"no tool call - retrying ({bad_parses}/2)")
                # Retries must CHANGE the prompt, not repeat it.
                if "read_file" in self.tools:
                    hint = ("Your reply contained no tool call. Reply with ONLY one "
                            "XML block, e.g.:\n<read_file>\n<path>file.py</path>\n</read_file>")
                else:
                    # Saying what you are about to do is not doing it, and in
                    # the home thread the doing is a tool call too.
                    hint = ("Your reply said what you were about to do, but contained "
                            "no tool call, so nothing has happened. If there is work "
                            "to do, send the tool call that does it now (real work "
                            "is started with spawn_task). If you only meant to "
                            "answer, put the answer in <done><summary>...</summary></done>.")
                if bad_parses == 2:
                    hint += "\nDo not explain. Output the XML block alone."
                    # Only now is it worth paying for deeper reasoning. A first
                    # formatting slip is not a thinking problem.
                    boost_left = max(boost_left, 2)
                self.messages.append({"role": "assistant", "content": reply[:2000]})
                self.messages.append({"role": "user", "content": hint})
                continue

            bad_parses = 0
            self.messages.append({"role": "assistant", "content": reply})

            if name == "think_harder":
                self.emit("thinking", text=thinking_text())
                boost_left = 3
                self.emit("notice", level="info",
                          text="think_harder " + args.get("why", "").strip()[:60])
                self.messages.append({"role": "user", "content":
                                      "[think_harder] Deep reasoning enabled for the "
                                      "next few steps. Continue with the task."})
                continue

            if name == "done":
                # The rationale before <done> is the only reasoning fast mode
                # ever produces, so it shows here too -- unless the answer is
                # already on screen, where it would land after the fact.
                if not stream["streaming"]:
                    self.emit("thinking", text=thinking_text())
                summary = args.get("summary", "").strip()
                if not summary:
                    # The answer was written as prose before the tag; keep it.
                    summary = rationale
                if not summary:
                    summary = "(no summary provided)"
                # "I'm starting a task" with no task started is a promise that
                # nothing will keep: the turn ends here and the work never
                # begins. Send it back once so the words and the facts agree.
                head = " ".join(summary.split())[:240]
                if (self.profile != "controller" and not acted and not challenged and _PROMISE.search(head)
                        and not re.search(r"i['’]?ll (be|let you know)", head, re.I)):
                    challenged = True
                    if stream["streaming"]:
                        self.emit("answer_discard")
                    self.emit("notice", level="warn",
                              text="that reply promised work that had not been started; asking again")
                    start = ("Start it now with spawn_task" if "spawn_task" in self.tools
                             else "Do it now with a tool call")
                    self.messages.append({"role": "user", "content":
                        "Your reply says you are starting or about to do something, "
                        "but no tool has run in this turn, so nothing has been "
                        f"started and nothing will happen after this message. {start}, "
                        "and reply once it is under way. If there is nothing to do, "
                        "reply again without saying that you are doing it."})
                    continue
                if self.profile == "controller":
                    issue = controller.completion_issue(self, summary, outcomes)
                    denied = bool(outcomes and outcomes[-1][3] and outcomes[-1][2].startswith(
                        ('REFUSED', 'User DENIED', 'Sir did not approve')))
                    if issue and not denied and not grounded_retry:
                        grounded_retry = True
                        if self.role != 'brain' and controller.unverified_directory_denial(self, summary, outcomes):
                            self.role = 'brain'
                            self.model = self.llm.describe('brain', refresh=True)
                            self.refresh()
                            self.emit('notice', level='info', text='checking an unsupported access claim with the specialist')
                        self.emit("notice", level="warn", text="checking the reply against actual execution state")
                        self.messages.append({"role": "user", "content":
                            "[Execution check] " + issue + " Only perform actions authorized by the original "
                            "user request. For coding changes or testing use spawn_task; otherwise use "
                            "the appropriate tool or explain honestly. Never invent pending work or success."})
                        continue
                    if issue or (outcomes and outcomes[-1][3]):
                        summary = controller.grounded_fallback(self, outcomes)
                        # Persist the words actually shown, not the rejected status claim.
                        self.messages[-1] = {"role": "assistant", "content": json.dumps(
                            {"tool": "done", "arguments": {"summary": summary}})}
                        if issue and not denied:
                            # The correction still ended in an unkept plan.
                            # Let the hub recover the original request once;
                            # if it does not, it shows this explanation.
                            if getattr(self, "defer_failure_answer", False):
                                self._deferred_answer = summary
                            return self._end('stopped', summary)
                if self.profile == 'controller' and controller.recoverable_failure(outcomes):
                    return self._end('stopped', summary)
                self.ctx_used = self.est_tokens()
                self.emit("usage", used=self.ctx_used, limit=self.ctx_limit, estimated=True)
                self.emit("answer", text=summary, elapsed=elapsed,
                          boosted=boosted, streamed=stream["streaming"])
                return self._end("done", summary)

            self.emit("thinking", text=thinking_text())

            # Normally just one call. When the model batches several
            # independent read-only lookups in one message, run every one and
            # feed back every result before the next model turn.
            for name, args in calls:
                if self.cancel.is_set():
                    return self._end("cancelled")
                detail = (args.get("path") or args.get("command")
                          or args.get("pattern") or args.get("url")
                          or args.get("query") or args.get("title")
                          or args.get("name") or args.get("id") or "")
                if name=="computer":
                    detail=f"{args.get('app','')} · {args.get('action','')}".strip()
                if args.get("connector"):
                    detail = f"{args['connector']} {args.get('action', '')}".strip()
                self.emit("tool_call", name=name, detail=str(detail)[:300],
                          elapsed=elapsed, boosted=boosted)
                # A command can write files too (a heredoc, a generator), so
                # whether it changed the work is read from the folder itself.
                tracked = name == "run_command" and self.profile != "controller"
                before = self._folder_snapshot() if tracked else None
                result_text, failed, fault = self._execute(name, args)
                acted = True
                outcomes.append((name, args, result_text, failed))
                if name in ("edit_file", "write_file") and not failed:
                    changed = True
                elif tracked and before is not None:
                    after = self._folder_snapshot()
                    changed = None if after is None else after != before
                    if changed:      # the receipts name what a command changed, too
                        self._turn_touched.extend(sorted(
                            p for p in before.keys() | after.keys() if before.get(p) != after.get(p)))
                else:
                    changed = None if tracked else False   # too big to tell: neither way
                # The exact same call with the exact same result, again and
                # again: at temperature 0 the same context gives the same next
                # step, so a stuck thread stays stuck. The third time, say so
                # in the result (which also changes what the model sees); the
                # fourth time, stop and report honestly.
                key = json.dumps([name, args, result_text], sort_keys=True, default=str)
                if changed:
                    seen_since_change = {}
                    steps_since_change = 0
                elif changed is False and steps_since_change is not None and self.profile != "controller":
                    steps_since_change += 1
                    if steps_since_change == STALL_NUDGE:
                        result_text += (f"\n\n[{STALL_NUDGE} steps have passed since you last changed "
                                        f"a file. If the work is done, finish now and report what you "
                                        f"verified. If a result surprises you, say what you think the "
                                        f"cause is and act on that, instead of probing further.]")
                    if steps_since_change >= STALL_STOP:
                        self.messages.append({"role": "user", "content": f"[{name} result]\n{result_text}"})
                        self.emit("tool_result", name=name, first=(result_text.splitlines() or [""])[0][:300],
                                  failed=failed, fault=bool(fault))
                        self.emit("notice", level="warn", text="stopped: no progress since the last change")
                        return self._stop_with_report(
                            f"I stopped: {STALL_STOP} steps passed without any further change "
                            f"to the work, so I was no longer making progress.",
                            "many steps have passed with no change to the work", outcomes)
                repeats = 0
                if not getattr(self, "_result_images", None):    # a fresh screenshot each time: watching
                    repeats = seen_since_change[key] = seen_since_change.get(key, 0) + 1
                if repeats == 3:
                    if len(calls) == 1:
                        self._collapse_repeats(self.messages[-1]["content"], f"[{name} result]\n{outcomes[-1][2]}")
                    result_text += (f"\n\n[You have now made exactly this call {repeats} times since "
                                    f"anything changed, and got exactly this result each time. Running it "
                                    f"again will not change anything. Use what it showed you and take the "
                                    f"next step, or finish and report what you did, what you verified and "
                                    f"what remains.]")
                    loosen = True
                    self.emit("notice", level="info",
                              text=f"the same {name} step came back 3 times; varying the next step")
                if repeats >= 4:
                    first = _headline(outcomes[-1][2], failed)
                    lead = (f"I stopped: I ran the same `{name}` step {repeats} times with nothing changing"
                            + (f" and it failed the same way each time ({first})." if failed else
                               ", getting the same result each time."))
                    self.messages.append({"role": "user", "content": f"[{name} result]\n{result_text}"})
                    self.emit("tool_result", name=name, first=(result_text.splitlines() or [""])[0][:300],
                              failed=failed, fault=bool(fault))
                    self.emit("notice", level="warn", text=f"stopped repeating the same {name} step")
                    if self.profile != "controller":
                        return self._stop_with_report(
                            lead, "the same call kept returning the same result", outcomes,
                            fallback="Repeating it will not help; this needs a different approach.")
                    answer = (lead + " Repeating it will not help; this needs a different approach. "
                              + self._progress_note(outcomes))
                    self.messages.append({"role": "assistant", "content": answer})
                    if getattr(self, "defer_failure_answer", False):
                        self._deferred_answer = answer
                    else:
                        self.emit("answer", text=answer)
                    return self._end("stopped", answer)
                left = self.step_budget() - step
                if left == 3 and self.profile != "controller":
                    result_text += ("\n\n[3 steps left in this turn. Finish now: report with done "
                                    "what is complete, what you verified, and what remains.]")
                elif left == 2 and self.profile == "controller" and "spawn_task" in self.tools:
                    # Seen live: the controller spent its whole budget reading a
                    # project's files for a bug report and was cut off.
                    result_text += ("\n\n[2 steps left in this turn. Answer now with what you have; "
                                    "or, if the request needs files changed or more digging, start "
                                    "a task with spawn_task now and put what you found in its brief.]")
                if failed:
                    boost_left = max(boost_left, 2)   # a failing step earns more thought
                    failures_in_a_row += 1
                    if failures_in_a_row % 4 == 0:
                        # Trying variations of a failing call can eat the whole
                        # step budget. Stop and think instead.
                        result_text += (
                            f"\n\n[{failures_in_a_row} steps in a row have failed. "
                            f"Stop trying variations. Re-read the task and these "
                            f"errors: is there something you have not checked that "
                            f"would tell you the right way, such as a tool's help? "
                            f"If it cannot be done with what you have, finish now "
                            f"and report exactly what blocked you.]")
                else:
                    failures_in_a_row = 0
                first = result_text.splitlines()[0] if result_text else ""
                self.emit("tool_result", name=name, first=first[:300],
                          failed=failed, fault=bool(fault))
                images = [] if failed else list(getattr(self, "_result_images", []))
                if images:
                    self._drop_old_images(keep=1)      # with this one: the latest two
                self.messages.append({"role": "user", "content": _content(
                    f"[{name} result]\n{result_text}", images)})
                if self.profile == "controller" and failed and failures_in_a_row >= 2:
                    answer=controller.grounded_fallback(self,outcomes)
                    self.messages.append({'role':'assistant','content':answer})
                    if getattr(self, "defer_failure_answer", False):
                        # The hub may still hand the request to a worker; it shows
                        # this only if it does not, so sir is not told it failed
                        # just before being told it is under way.
                        self._deferred_answer = answer
                    else:
                        self.emit('answer',text=answer)
                    return self._end("stopped",answer)
            self._save()

        if self.profile == "controller" and getattr(self, "defer_failure_answer", False):
            # The hub may still hand the request to a worker; it shows this
            # only if it does not, so the chat is not told the work may be
            # incomplete just before being told it is under way.
            self._deferred_answer = (f"I ran out of steps ({self.step_budget()}) before I could "
                                     f"finish this, so I stopped.")
            return self._end("stopped")
        self.emit("notice", level="warn",
                  text=f"hit the {self.step_budget()}-step cap. Task may be incomplete.")
        if self.profile == "controller":
            return self._end("stopped")
        # Never end a task silently: say what actually happened.
        return self._stop_with_report(f"I ran out of steps ({self.step_budget()}).",
                                      "the step limit is reached", outcomes,
                                      fallback="I did not get to write my report.")

    def _stop_with_report(self, lead, why, outcomes, fallback=""):
        """End a worker turn that a guard cut short. The model is asked once
        for its own report (no more tools), so the turn still says what was
        done and checked; the guard's reason and the receipts go with it, so
        the words never outrun the facts. The status stays stopped."""
        report = self._closing_report(why)
        if report:
            answer = f"{lead}\n\n{report}\n\n{self._progress_note(outcomes)}"
        else:
            answer = f"{lead} {fallback + ' ' if fallback else ''}{self._progress_note(outcomes)}"
        self.messages.append({"role": "assistant", "content": answer})
        self.emit("answer", text=answer)
        return self._end("stopped", answer)

    def _closing_report(self, why):
        """The model's own final report, asked for once, with no tool allowed.
        "" when it gives none: another tool call, a cut-off or empty reply, a
        failed call, or a cancelled job."""
        if self.cancel.is_set():
            return ""
        note = (f"[This turn is ending now: {why}. No more tools will run. Reply with your "
                "final report as plain text, with no tool call: what you changed, what you "
                "checked and what it showed, and anything still unfinished or unverified. "
                "Claim only what the results above show: a check passed only if its output "
                "says so, and checks after a failure in the same run did not run. If you are "
                "unsure why something failed, say so rather than guess.]")
        last = self.messages[-1] if self.messages else None
        if last and last["role"] == "user":
            # Into the last result, not a second user message in a row: some
            # chat templates refuse turns that do not alternate.
            content = last["content"]
            last["content"] = (content + "\n\n" + note if isinstance(content, str)
                               else list(content) + [{"type": "text", "text": note}])
        else:
            self.messages.append({"role": "user", "content": note})
        stream = {"streaming": False, "sent": 0}
        try:
            result = self._call(bool(self.thinking), stream)
        except Exception:                   # best effort: the guard's own answer stands
            return ""
        if stream["streaming"]:
            self.emit("answer_discard")     # the final answer carries the report
        reply = (result.content or "").strip()
        if not reply or getattr(result, "finish_reason", None) == "length":
            return ""
        calls = protocol.parse_tool_calls(reply, self.tools, self.registry.extra_params(self.tools))
        if calls:
            name, args = calls[0]
            if name != "done":
                return ""
            return (args.get("summary", "").strip()
                    or protocol.prose_outside(reply, self.tools).strip())
        if re.search(r"<(%s)\s*>" % "|".join(map(re.escape, self.tools)), reply):
            return ""                       # a malformed tool call, not a report
        return reply

    def _folder_snapshot(self, limit=4000):
        """A cheap picture of the working folder (each file's size and time),
        to tell whether a command changed the work, and which files. Caches
        and compiled files do not count. None when the folder is too big to
        check quickly."""
        skip = set(self.settings.skip_dirs) | {".pytest_cache", ".mypy_cache", ".ruff_cache"}
        files_seen = {}
        for root, dirs, files in os.walk(self.workdir):
            dirs[:] = sorted(d for d in dirs if d not in skip)
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files_seen[os.path.relpath(path, self.workdir)] = (st.st_size, st.st_mtime_ns)
                if len(files_seen) > limit:
                    return None
        return files_seen

    def _collapse_repeats(self, call_text, result_head):
        """Drop this turn's earlier copies of a repeated call and its identical
        result, keeping the latest. With three identical copies in view the
        model copies the call again however it is told: replayed live on the
        27B, it did so every time at temperature 0 and in 1 of 3 samples at
        0.7; with one copy left, 0 of 3 at 0.7."""
        start, last = getattr(self, "_turn_message_start", 0), len(self.messages) - 1
        drop = set()
        for i in range(start, last):
            m, reply = self.messages[i], self.messages[i + 1]
            if (m["role"] == "assistant" and m["content"] == call_text and reply["role"] == "user"
                    and isinstance(reply["content"], str)
                    and (reply["content"] == result_head or reply["content"].startswith(result_head + "\n\n["))):
                drop.update((i, i + 1))
        if drop:
            self.messages = [m for i, m in enumerate(self.messages) if i not in drop]

    def _progress_note(self, outcomes):
        """What a stopped turn had done, from its receipts: the files it
        changed (by edits or by commands) and the last results, so the report
        is never empty."""
        edited = [args.get("path", "") for name, args, _, failed in outcomes
                  if name in ("edit_file", "write_file") and not failed and args.get("path")]
        workdir = getattr(self, "workdir", None) or "."

        def shown(path):
            try:
                return os.path.relpath(os.path.join(workdir, os.path.expanduser(path)), workdir)
            except ValueError:       # Windows: another drive has no relative path
                return path
        changed = list(dict.fromkeys(shown(p) for p in edited + list(getattr(self, "_turn_touched", []))))
        ran = [(args.get("command", ""), result, failed) for name, args, result, failed in outcomes
               if name == "run_command"]
        note = (("Files changed: " + ", ".join(changed) + ". ") if changed
                else "No files were changed. ")
        if ran:
            command, result, failed = ran[-1]
            note += (f"Last command: `{command[:120]}` ({'failed' if failed else 'succeeded'}: "
                     f"{_headline(result, failed, 200)}). ")
        return note + "Check the working folder before relying on this."
