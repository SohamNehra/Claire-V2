"""Model client and role router.

A role ("brain", "scout", "clerk") maps to a server and a model in config.py.
Callers name the role and never an address, so promoting a new model is one
line of config. A role with no entry runs on the brain.

Unlike v1, nothing here prints or exits: failures are raised, and whoever
called decides what the user sees. A model error must never take the service
down with it.
"""

from contextlib import nullcontext
import json
import os
import threading
import time
import urllib.error
import urllib.request


class ModelError(Exception):
    """The server answered, but not with a usable completion."""


class ModelUnreachable(ModelError):
    """Nothing is listening at the role's address."""


class ContextOverflow(ModelError):
    """The prompt outgrew the context window."""


class Cancelled(Exception):
    """The caller's cancel flag was set mid-stream."""


class ChatResult:
    def __init__(self, content, reasoning, prompt_tokens, completion_tokens,
                 ttft=0.0, gen_seconds=0.0, finish_reason=None, cached_tokens=None):
        self.content = content
        self.reasoning = reasoning
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.ttft = ttft                  # seconds until the first token
        self.gen_seconds = gen_seconds    # seconds from first token to last
        self.finish_reason = finish_reason
        # Prompt tokens the server reused from an earlier request instead of
        # reading again; None when the server does not say.
        self.cached_tokens = cached_tokens

    @property
    def tokens_per_second(self):
        if not self.completion_tokens or self.gen_seconds <= 0:
            return 0.0
        return self.completion_tokens / self.gen_seconds


def _get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


class LLM:
    def __init__(self, settings):
        self.s = settings
        self.power = None
        self._described = {}
        self._lock = threading.Lock()

    def endpoint(self, role="brain"):
        """(api_base, model) for a role, falling back to the brain."""
        entry = self.s.roles.get(role) or {}
        base = entry.get("api_base") or self.s.api_base
        model = entry.get("model") or ("" if entry.get("api_base")
                                       else self.s.model)
        return base.rstrip("/"), model

    def has_own_model(self, role):
        """True when the role is served by something other than the brain."""
        return bool((self.s.roles.get(role) or {}).get("api_base"))

    def _native_lmstudio(self, role, base):
        # An explicitly assigned talk model can use the same LM Studio server
        # as the brain. Its adapter requirements depend on the endpoint.
        if (self.s.roles.get(role) or {}).get("server")=="lmstudio":
            return True
        return (getattr(self.s, "brain_server", "llama") == "lmstudio"
                and (role == "brain" or not self.has_own_model(role)
                     or base.rstrip("/") == self.s.api_base.rstrip("/")))

    def _native_splash(self, role, base):
        if (self.s.roles.get(role) or {}).get("server") == "splash":
            return True
        return (getattr(self.s, "brain_server", "llama") == "splash"
                and (role == "brain" or not self.has_own_model(role)
                     or base.rstrip("/") == self.s.api_base.rstrip("/")))

    def describe(self, role="brain", refresh=False):
        """What the role's server actually loaded: model name and context.

        Asking the server removes a whole class of mistake -- a hand-set context
        size that disagrees with the loaded model means compaction never fires.
        """
        with self._lock:
            if role in self._described and not refresh:
                return self._described[role]
        base, model = self.endpoint(role)
        pinned_ctx = self.s.ctx_limit if role == "brain" else 0
        # A Splash or LM Studio role is loaded with the context its entry names,
        # so that is what to report while it is not loaded.
        entry_cfg = self.s.roles.get(role) or {}
        configured = (int(entry_cfg.get("context") or 0)
                      if entry_cfg.get("server") in ("splash", "lmstudio") else 0)
        info = {"role": role, "api_base": base, "model": model or "local-model",
                "ctx": pinned_ctx or configured or self.s.ctx_ceiling, "live": False}
        try:
            listed = _get_json(f"{base}/models").get("data") or [{}]
        except Exception:
            return info                      # not cached: it may come up later
        # LM Studio lists every model it has, loaded or not: take the one named.
        entry = next((m for m in listed if model and m.get("id") == model), listed[0])
        info["live"] = True
        meta = entry.get("meta") or {}
        if not model:
            ident = entry.get("id") or "local-model"
            info["model"] = (os.path.basename(ident).replace(".gguf", "")
                             or "local-model")
        # A pinned size is the server's total; otherwise take what it loaded,
        # bounded by what the model was trained for.
        sizes = ([pinned_ctx] if pinned_ctx else
                 [x for x in (meta.get("n_ctx"), meta.get("n_ctx_train")) if x])
        # With several slots, each conversation gets only its slot's share of
        # the window, whichever way the total was set. /props reports that.
        native_splash = self._native_splash(role, base) or entry.get("owned_by")=="splash"
        native_lmstudio = self._native_lmstudio(role, base) and not native_splash
        if native_splash and model and not any(m.get("id")==model for m in listed):
            info["live"] = False
            return info
        if native_splash:
            info["backend"] = "splash"
            info["slots"] = 1  # conservative admission; context is per request, not split
            info["vision"] = bool(entry.get("vision"))
            for key in ("context_length", "max_model_len"):
                if entry.get(key):sizes.append(int(entry[key]))
            try:
                info["live"] = _get_json(base[:-3]+"/ready").get("status")=="ready"
            except Exception:
                info["live"] = False
        if not native_lmstudio and not native_splash:
            try:
                root = base[:-3] if base.endswith("/v1") else base
                props = _get_json(f"{root}/props")
                info["slots"] = max(1, int(props.get("total_slots") or 1))
                per_slot = (props.get("default_generation_settings") or {}).get("n_ctx")
                if per_slot:
                    sizes.append(per_slot)
            except Exception:
                pass
        if native_lmstudio:
            # LM Studio's own API says what it loaded: context, parallel
            # predictions, and whether the model can see.
            info["live"] = False
            try:
                root = base[:-3] if base.endswith("/v1") else base
                for m in _get_json(f"{root}/api/v1/models").get("models") or []:
                    if m.get("key") == info["model"] and m.get("loaded_instances"):
                        info["live"] = True
                        cfg = m["loaded_instances"][0].get("config") or {}
                        if cfg.get("context_length") and not pinned_ctx:
                            sizes.append(int(cfg["context_length"]))
                        info["slots"] = max(1, int(cfg.get("parallel") or 1))
                        info["vision"] = bool((m.get("capabilities") or {}).get("vision"))
                        info["quantization"] = (m.get("quantization") or {}).get("name", "")
            except Exception:
                pass
        info["ctx"] = (min(min(sizes), self.s.ctx_ceiling) if sizes
                       else self.s.ctx_ceiling)
        with self._lock:
            self._described[role] = info
        return info

    def invalidate(self, role=None):
        """Metadata belongs to a loaded instance, not the weights on disk."""
        with self._lock:
            names=[role] if role is not None else list(self._described)
            for name in names:
                info=self._described.pop(name,None)
                if info is not None:info['live']=False

    def prime(self, messages, role="brain", cancel=None):
        """Have the server process `messages` so a longer prompt can resume there.

        Splash resumes only from the end of a prompt it has already processed,
        not from wherever two prompts stop matching (llama-server does that).
        A prompt whose last part is rewritten every time, as the conversation
        view's runtime facts are, therefore shares nothing reusable with the
        one before it and the whole history is read again. Sending the part
        before the rewritten block on its own first leaves a resume point at
        its end, and the next view starts with exactly that text.
        Returns the seconds it took; 0 where the server has no need of it.
        """
        base, _ = self.endpoint(role)
        if not self._native_splash(role, base):
            return 0.0
        started = time.time()
        try:
            self.chat(messages, role=role, thinking=False, cancel=cancel, max_tokens=1, temperature=0)
        except Cancelled:
            raise
        except ModelError:
            pass                 # the real request follows and reports any fault
        return time.time() - started

    def chat(self, messages, role="brain", thinking=False, on_delta=None,
             cancel=None, max_tokens=None, temperature=None, response_format=None):
        with self.power.activity() if self.power else nullcontext():
            if self.power and self.power.needs_wake(role):
                from . import boot
                problem = boot.ensure_role(self.s, role)
                if problem:
                    raise ModelUnreachable(problem)
                self.power.model_ready(role)
            return self._chat(messages, role, thinking, on_delta, cancel,
                              max_tokens, temperature, response_format)

    def _chat(self, messages, role="brain", thinking=False, on_delta=None,
             cancel=None, max_tokens=None, temperature=None, response_format=None):
        """Stream one completion.

        on_delta(kind, text_so_far) is called as text arrives, with kind
        "reasoning" or "content". Backends differ in what they honour for
        `thinking`: llama-server and vLLM read chat_template_kwargs per request;
        LM Studio ignores it, which is why callers strip reasoning regardless.
        """
        base, model = self.endpoint(role)
        if not model:
            model = self.describe(role)["model"]
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens or self.s.max_tokens,
            "temperature": self.s.temperature if temperature is None else temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": bool(thinking)},
            # Second lever for backends that read it off the request body.
            "enable_thinking": bool(thinking),
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if self._native_splash(role, base):
            payload["reasoning_effort"] = "medium" if thinking else "none"
        elif self._native_lmstudio(role, base):
            # LM Studio ignores enable_thinking in chat_template_kwargs. Its
            # hybrid Qwen models require reasoning_effort=none to switch off.
            lower_model = model.lower()
            qwen38_hybrid = "qwen3.8-27b" in lower_model
            qwen_boolean = any(name in lower_model for name in ("qwen3.5", "qwen3.6"))
            if qwen38_hybrid:
                payload["reasoning_effort"] = self.s.lmstudio_reasoning_effort if thinking else "none"
            elif qwen_boolean:
                payload["reasoning_effort"] = "high" if thinking else "none"
            else:
                payload["reasoning_effort"] = "high" if thinking else "low"
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})

        content, reasoning = "", ""
        prompt_tokens = completion_tokens = 0
        cached_tokens = None
        finish_reason = None
        started, first = time.time(), None
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw in resp:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    usage = chunk.get("usage") or {}
                    if usage.get("prompt_tokens"):
                        prompt_tokens = usage["prompt_tokens"]
                        completion_tokens = usage.get("completion_tokens", 0)
                        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                    if not chunk.get("choices"):
                        continue
                    finish_reason = chunk["choices"][0].get("finish_reason") or finish_reason
                    delta = chunk["choices"][0].get("delta", {})
                    if first is None and (delta.get("reasoning_content")
                                          or delta.get("content")):
                        first = time.time()
                    if delta.get("reasoning_content"):
                        reasoning += delta["reasoning_content"]
                        if on_delta:
                            on_delta("reasoning", reasoning)
                    if delta.get("content"):
                        content += delta["content"]
                        if on_delta:
                            on_delta("content", content)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if exc.code == 400 and "context" in detail.lower():
                raise ContextOverflow(detail)
            raise ModelError(f"server returned HTTP {exc.code}"
                             + (f": {detail}" if detail else ""))
        except urllib.error.URLError as exc:
            raise ModelUnreachable(f"cannot reach the model server at {base} "
                                   f"({exc.reason})")
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise ModelError(f"connection to the model server failed: {exc}")
        ended = time.time()
        return ChatResult(content, reasoning.strip(), prompt_tokens,
                          completion_tokens,
                          ttft=(first or ended) - started,
                          gen_seconds=ended - (first or ended), finish_reason=finish_reason,
                          cached_tokens=cached_tokens)
