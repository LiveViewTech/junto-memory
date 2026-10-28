#!/usr/bin/env python3
"""li.py — one-shot local inference on an Ollama box (stdlib only; Linux/Windows).

Endpoint: --url or env JUNTO_OLLAMA_URL (Ollama base URL, e.g. http://<host>:11434).
There is no default; the script exits if neither is set.

  python li.py -i "Classify each line as error|warn|noise" -f app.log --per-line \
      --schema '{"type":"object","properties":{"label":{"enum":["error","warn","noise"]}},"required":["label"]}'
  some-cmd | python li.py -i "Summarize the failures in 3 bullets"

Prints JSON (or writes it to -o FILE). --per-line: one model call per non-empty
input line, output {"items":[{"input":..., "output":...}], "count":N, "failed":K}.
Otherwise one call over the whole text, output {"output":..., "chars":N}.
Use the model for labels and names; COUNT with grep/sort/uniq — model counts are wrong.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

MAX_CHARS = 60_000       # whole-text cap (~15K tokens; fits the 16K context)
MAX_LINES = 500          # --per-line cap


def ctx_for(text):
    # Fixed sizes: a different num_ctx forces Ollama to reload the model, so
    # callers that all pick from the same two sizes share the loaded copy.
    return 8192 if len(text) < 20_000 else 16384


def think_for(model, think):
    """Ollama `think` value, or None to omit (models without reasoning reject it)."""
    if think == "auto":
        if model.startswith("gpt-oss"):
            return "low"   # default "medium" runs away on long inputs: 153s vs 7.5s on 30KB
        if model.startswith("qwen3"):
            return False
        return None
    if think == "off":
        return False
    return think


def call(url, model, instructions, text, schema, timeout, think="auto"):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": text},
        ],
        "stream": False,
        # Ollama's default context is 4096 tokens and it TRUNCATES silently.
        "options": {"temperature": 0, "num_ctx": ctx_for(text)},
    }
    if schema is not None:
        body["format"] = schema
    t = think_for(model, think)
    if t is not None:
        body["think"] = t
    if urllib.parse.urlsplit(url).scheme.lower() not in ("http", "https"):
        raise ValueError(f"Ollama URL must be http:// or https://, got {url!r}")
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = json.loads(resp.read())["message"]["content"]
    if schema is not None:
        return json.loads(content)
    return content.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--instructions", required=True)
    ap.add_argument("-f", "--file", help="input file (default: stdin)")
    ap.add_argument("--schema", help="JSON schema string or @path for structured output")
    ap.add_argument("--per-line", action="store_true", help="one call per non-empty line")
    ap.add_argument("-m", "--model", default="gpt-oss:20b")
    ap.add_argument("--url", default=os.environ.get("JUNTO_OLLAMA_URL"),
                    help="Ollama base URL (default: env JUNTO_OLLAMA_URL; required)")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--think", default="auto", choices=["auto", "off", "low", "medium", "high"],
                    help="reasoning effort; auto = low for gpt-oss, off for qwen3")
    ap.add_argument("-o", "--out", help="write JSON here instead of stdout")
    a = ap.parse_args()
    if not a.url:
        sys.exit("no Ollama URL: pass --url http://<host>:11434 or set JUNTO_OLLAMA_URL")

    if a.file:
        with open(a.file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        text = sys.stdin.read()
    schema = None
    if a.schema:
        raw = a.schema
        if raw.startswith("@"):
            with open(raw[1:], encoding="utf-8") as fh:
                raw = fh.read()
        schema = json.loads(raw)

    t0 = time.time()
    if a.per_line:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) > MAX_LINES:
            sys.exit(f"{len(lines)} lines > {MAX_LINES}; split the input or use memory_delegate")
        items, failed = [], 0
        for ln in lines:
            try:
                items.append({"input": ln, "output": call(a.url, a.model, a.instructions, ln, schema,
                                                          a.timeout, a.think)})
            except Exception as e:  # keep going; report per item
                failed += 1
                items.append({"input": ln, "error": str(e)[:200]})
        out = {"items": items, "count": len(lines), "failed": failed}
    else:
        if len(text) > MAX_CHARS:
            sys.exit(f"{len(text)} chars > {MAX_CHARS}; dedupe/trim the input or use --per-line")
        try:
            output = call(a.url, a.model, a.instructions, text, schema, a.timeout, a.think)
        except TimeoutError:
            sys.exit(f"timed out after {a.timeout}s: the Ollama box is busy or the input is too big; "
                     "try -m phi4:14b, a smaller input, or --timeout")
        out = {"output": output, "chars": len(text)}
    out.update({"model": a.model, "seconds": round(time.time() - t0, 1)})
    data = json.dumps(out, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(data)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(data)


if __name__ == "__main__":
    main()
