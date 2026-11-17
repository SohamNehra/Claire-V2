#!/usr/bin/env python3
"""Measure a local model server's raw speed at temperature 0.

    python3 scripts/bench_speed.py --api http://127.0.0.1:8080/v1 --backend splash
    python3 scripts/bench_speed.py --api http://127.0.0.1:8081/v1 --backend llama

What it measures, each as the median of --runs:

    decode    tokens/s while generating, from the first token to the last
              (code, prose, and rewriting a file with one small change)
    prefill   tokens/s reading a long prompt the server has never seen
    cached    seconds to the first token when only the end of that prompt is new
    parallel  combined tokens/s with several requests running at once

Requests are the ones Claire sends: streaming, thinking off, temperature 0.
Nothing is loaded or unloaded here; start the server first.
"""
import argparse
import json
import statistics
import threading
import time
import urllib.request
import uuid

CODE = ("Write a Python module `inventory.py` with a dataclass `Item` (name, sku, "
        "quantity, unit_price), a class `Inventory` with add, remove, find_by_sku, "
        "total_value and low_stock(threshold) methods, and input validation. "
        "Include docstrings. Output only the code.")
PROSE = ("Explain, for a new engineer, how a hash map handles collisions, how "
         "resizing works, and why lookups are constant time on average. Write "
         "about four paragraphs of plain prose with no lists.")


def source_file(lines=120):
    """A plausible module to rewrite: most of the reply is copied from it."""
    out = ["import math", "", ""]
    i = 0
    while len(out) < lines:
        i += 1
        out += [f"def metric_{i}(values, scale={i}.0):",
                f'    """Return the scaled spread of values, variant {i}."""',
                "    if not values:",
                "        return 0.0",
                "    mean = sum(values) / len(values)",
                "    spread = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))",
                f"    return spread * scale + {i}", "", ""]
    return "\n".join(out)


def edit_prompt():
    return ("Here is a file.\n\n```python\n" + source_file() + "\n```\n\n"
            "Rename the parameter `scale` to `factor` in every function. "
            "Output the whole updated file and nothing else.")


def long_prompt(tokens):
    """A prompt of about `tokens` tokens that no cache has seen before."""
    nonce = uuid.uuid4().hex
    row = ("Entry {n} of run {nonce}: the service handled {a} requests with a median "
           "latency of {b} ms and {c} errors during the interval.\n")
    body = "".join(row.format(n=n, nonce=nonce, a=n * 37 % 9973, b=n * 13 % 487, c=n % 7)
                   for n in range(max(1, tokens // 34)))
    return "Read this log.\n\n" + body


class Server:
    def __init__(self, api, model, backend):
        self.api, self.backend = api.rstrip("/"), backend
        self.model = model or self._first_model()

    def _first_model(self):
        with urllib.request.urlopen(f"{self.api}/models", timeout=10) as r:
            return (json.load(r).get("data") or [{}])[0].get("id", "local-model")

    def chat(self, messages, max_tokens):
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": 0, "stream": True,
                   "stream_options": {"include_usage": True},
                   "chat_template_kwargs": {"enable_thinking": False},
                   "enable_thinking": False}
        if self.backend == "splash":
            payload["reasoning_effort"] = "none"
        request = urllib.request.Request(
            f"{self.api}/chat/completions", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        started, first, usage, text = time.monotonic(), None, {}, ""
        with urllib.request.urlopen(request, timeout=1800) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                chunk = json.loads(line[5:])
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = (delta.get("content") or "") + (delta.get("reasoning_content") or "")
                    if piece and first is None:
                        first = time.monotonic()
                    text += piece
        ended = time.monotonic()
        first = first or ended
        out = usage.get("completion_tokens") or 0
        return {"text": text, "ttft": first - started, "seconds": ended - started,
                "prompt_tokens": usage.get("prompt_tokens") or 0, "completion_tokens": out,
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                # The first token arrives with the prefill, so it is not decode work.
                "decode_tps": (out - 1) / (ended - first) if out > 1 and ended > first else 0.0}


def median(values):
    return statistics.median(values) if values else 0.0


def decode(server, prompt, runs, max_tokens):
    rows = [server.chat([{"role": "user", "content": prompt}], max_tokens) for _ in range(runs)]
    return {"tokens_per_second": round(median([r["decode_tps"] for r in rows]), 1),
            "completion_tokens": rows[-1]["completion_tokens"]}


def prefill(server, tokens, runs):
    cold, warm = [], []
    for _ in range(runs):
        prompt = long_prompt(tokens)
        first = server.chat([{"role": "user", "content": prompt + "\nReply with OK."}], 8)
        cold.append(first)
        # Same beginning, new ending: only the tail should need reading.
        again = server.chat([{"role": "user", "content": prompt + "\nReply with OK."},
                             {"role": "assistant", "content": "OK"},
                             {"role": "user", "content": "How many entries were there? One number."}], 8)
        warm.append(again)
    return {"prompt_tokens": cold[-1]["prompt_tokens"],
            "prefill_tokens_per_second": round(median([r["prompt_tokens"] / r["ttft"] for r in cold]), 1),
            "cold_first_token_seconds": round(median([r["ttft"] for r in cold]), 2),
            "cached_first_token_seconds": round(median([r["ttft"] for r in warm]), 2),
            "cached_tokens_reported": warm[-1]["cached_tokens"]}


def parallel(server, count, max_tokens):
    rows, errors = [], []

    def one(i):
        try:
            # Different requests, as parallel tasks would send.
            rows.append(server.chat([{"role": "user", "content": f"Task {i}. {CODE}"}], max_tokens))
        except Exception as exc:
            errors.append(repr(exc))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(count)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - started
    total = sum(r["completion_tokens"] for r in rows)
    return {"requests": count, "completed": len(rows), "errors": errors,
            "combined_tokens_per_second": round(total / wall, 1) if wall else 0.0,
            "per_request_tokens_per_second": round(median([r["decode_tps"] for r in rows]), 1)}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api", required=True, help="e.g. http://127.0.0.1:8080/v1")
    p.add_argument("--backend", choices=["splash", "llama", "lmstudio"], default="llama")
    p.add_argument("--model", default="", help="model id; default is the first the server lists")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--prefill-tokens", type=int, default=8000)
    p.add_argument("--parallel", type=int, nargs="*", default=[2, 4],
                   help="request counts to run at once; pass nothing to skip")
    p.add_argument("--only", nargs="*", choices=["decode", "prefill", "parallel"])
    p.add_argument("--label", default="")
    a = p.parse_args()
    server = Server(a.api, a.model, a.backend)
    want = set(a.only or ["decode", "prefill", "parallel"])
    result = {"label": a.label, "api": a.api, "model": server.model, "backend": a.backend,
              "temperature": 0, "runs": a.runs}
    # One short request first, so model warm-up is not counted in any figure.
    server.chat([{"role": "user", "content": "Say OK."}], 4)
    if "decode" in want:
        result["decode_code"] = decode(server, CODE, a.runs, a.max_tokens)
        result["decode_prose"] = decode(server, PROSE, a.runs, a.max_tokens)
        result["decode_file_rewrite"] = decode(server, edit_prompt(), a.runs, 1600)
    if "prefill" in want:
        result["long_prompt"] = prefill(server, a.prefill_tokens, max(1, a.runs - 1))
    if "parallel" in want:
        result["parallel"] = [parallel(server, n, a.max_tokens) for n in a.parallel]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
