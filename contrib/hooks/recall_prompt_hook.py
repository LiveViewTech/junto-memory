#!/usr/bin/env python3
"""UserPromptSubmit hook: ambient associative recall (interface:recall-v0).

Posts the rolling conversation window to junto-memory's POST /recall each
user turn and injects the returned HEADERS (never bodies) as additional
context. The agent pulls the 0-1 that bite via memory_get_by_id.

FAIL-OPEN BY DESIGN: any failure (server down, timeout, bad transcript)
injects nothing and exits 0 — this hook must never block or delay a prompt
beyond its short timeout.

A2(ii) pull-through logging (Tom-approved 2026-07-27): every injection logs
{ts, session, ids, scores} to JUNTO_RECALL_LOG; the window TEXT itself is
logged only at JUNTO_RECALL_LOG_SAMPLE rate (default 0.2). Join the log
against later memory_get_by_id calls to measure follow-through.

Env knobs:
  JUNTO_RECALL_URL     default http://localhost:8080/recall
  JUNTO_PROJECT        default junto
  JUNTO_AGENT          default unset (omitted from request)
  JUNTO_RECALL_K       default 5
  JUNTO_RECALL_TIMEOUT default 4 (seconds)
  JUNTO_RECALL_LOG     default ~/junto-logs/recall-pullthrough.jsonl
  JUNTO_RECALL_LOG_SAMPLE  default 0.2 (0 disables window-text logging)
  JUNTO_RECALL_DISABLE set to any value to no-op the hook
"""

import json
import os
import random
import sys
import time
import urllib.request

WINDOW_CAP = 6000        # chars of transcript tail
PROMPT_CAP = 2000        # chars of the fresh prompt


def _texts_from_content(content):
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text") or "")
    return out


def build_window(hook_input):
    """Fresh prompt + tail of the transcript, capped. Transcript is CC's
    JSONL; unparseable lines are skipped (fail-open)."""
    parts = []
    tpath = hook_input.get("transcript_path")
    if tpath and os.path.exists(tpath):
        try:
            with open(tpath, errors="replace") as f:
                lines = f.readlines()[-40:]
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("type") not in ("user", "assistant"):
                    continue
                msg = row.get("message") or {}
                parts.extend(_texts_from_content(msg.get("content")))
        except OSError:
            pass
    tail = "\n".join(p for p in parts if p)[-WINDOW_CAP:]
    prompt = (hook_input.get("prompt") or "")[:PROMPT_CAP]
    return (tail + "\n\n" + prompt).strip()


def main():
    if os.environ.get("JUNTO_RECALL_DISABLE"):
        return
    try:
        hook_input = json.load(sys.stdin)
    except ValueError:
        return

    window = build_window(hook_input)
    if len(window) < 40:  # trivial prompts ("go", "yes") — not worth a query
        return

    body = {
        "project": os.environ.get("JUNTO_PROJECT", "junto"),
        "query": window,
        "k": int(os.environ.get("JUNTO_RECALL_K", "5")),
    }
    agent = os.environ.get("JUNTO_AGENT")
    if agent:
        body["agent"] = agent

    url = os.environ.get("JUNTO_RECALL_URL", "http://localhost:8080/recall")
    timeout = float(os.environ.get("JUNTO_RECALL_TIMEOUT", "4"))
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=timeout))

    if not resp.get("count"):
        return

    snippets = resp["snippets"]
    lines = []
    for s in snippets:
        who = s.get("authored_by") or "?"
        when = (s.get("updated") or "")[:10]
        lines.append(f"- [{s['score']:.2f}] {s['id']} ({who}, {when}): {s['one_line']}")
    context = (
        f"<junto-recall floor={resp['floor']}>\n"
        f"{len(snippets)} recorded memory header(s) matched this turn — "
        "retrieved automatically; these are recalled memories, VERIFY before "
        "relying. Pull a body ONLY if it clearly bites on the current task: "
        "memory_get_by_id(doc_id). Otherwise ignore silently.\n"
        + "\n".join(lines) + "\n</junto-recall>"
    )

    # A2(ii) pull-through log — ids always, window text sampled.
    try:
        log_path = os.path.expanduser(os.environ.get(
            "JUNTO_RECALL_LOG", "~/junto-logs/recall-pullthrough.jsonl"))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session": hook_input.get("session_id"),
            "project": body["project"],
            "ids": [s["id"] for s in snippets],
            "scores": [s["score"] for s in snippets],
            "took_ms": resp.get("took_ms"),
        }
        if random.random() < float(os.environ.get("JUNTO_RECALL_LOG_SAMPLE", "0.2")):
            entry["window"] = window
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Fail-open: never block the prompt. Nothing injected.
        pass
    sys.exit(0)
