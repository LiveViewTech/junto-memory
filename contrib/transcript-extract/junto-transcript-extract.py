#!/usr/bin/env python3
"""Nightly transcript extraction (Memento #4, design:transcript-extraction-v0).

Reads new Claude Code session transcripts (~/.claude/projects/<dir>/*.jsonl)
for OPTED-IN project dirs only, sanitizes and redacts them, asks `claude -p`
(no tools, JSON only) for decision / explanation / resolution records, and
writes each record as a junto learning with provenance. Every record lands
UNREVIEWED (interface:verification-state-v0); the server's redaction, write-
time gate and facets pipeline all apply because writes go through MCP.

Opt-in config (JSON, default ~/.config/junto/transcript-extract.json):
    {"projects": {"-home-tlemmons-HA": "ha", "-home-tlemmons-sharedUtils-junto-junto-memory": "junto"},
     "model": "sonnet", "max_records_per_chunk": 5}
No config, or an empty "projects" map, means a no-op run.

State (watermarks): ~/.local/state/junto-transcript-extract/state.json
    {"<transcript path>": <lines already processed>}

Usage:
    junto-transcript-extract.py               # extract + write
    junto-transcript-extract.py --dry-run     # extract + print, no writes, no watermark move
    junto-transcript-extract.py --limit 3     # at most 3 transcripts this run
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Cron's PATH has no ~/.local/bin, where the claude CLI installs (the 10-31
# extractor run failed on every transcript with "No such file: 'claude'").
CLAUDE_BIN = (os.environ.get("CLAUDE_BIN") or shutil.which("claude")
              or str(Path("~/.local/bin/claude").expanduser()))

URL = os.environ.get("JUNTO_MCP_URL", "http://localhost:8080/mcp")
API_KEY = os.environ.get("JUNTO_API_KEY")
IDENTITY = "extractor"
CONFIG = Path(os.environ.get("JUNTO_EXTRACT_CONFIG", "~/.config/junto/transcript-extract.json")).expanduser()
STATE = Path(os.environ.get("JUNTO_EXTRACT_STATE",
                            "~/.local/state/junto-transcript-extract/state.json")).expanduser()
PROJECTS_DIR = Path("~/.claude/projects").expanduser()
PROMPT_FILE = Path(__file__).with_name("extract_prompt.txt")

IDLE_SECONDS = 2 * 3600      # only sessions untouched for 2h (a live session is still deciding)
CHUNK_CHARS = 60_000         # per claude -p call
MAX_CHUNKS_PER_SESSION = 3   # per-session input cap = 180K chars
MAX_TOOL_INPUT_CHARS = 200   # tool_use rows keep a short hint, not the payload
DUPLICATE_RELEVANCE = 85     # skip a record when memory_query's top hit is >= this %
RUN_DEADLINE_SECONDS = int(os.environ.get("JUNTO_EXTRACT_DEADLINE", "3000"))
KINDS = ("decision", "explanation", "resolution")

# The server's redactor is the source of truth; import it from the repo when
# present so the prompt never even sees a secret. Fall back to server-side
# redaction only (writes are redacted there regardless).
_REPO_SRC = Path(__file__).resolve().parents[2] / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))
try:
    from shared_memory.redact import redact_secrets
except Exception:  # pragma: no cover - box without the repo
    def redact_secrets(text):
        return text, {}


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _text_of(content):
    """User/assistant message content -> list of (kind, text)."""
    if isinstance(content, str):
        return [("text", content)]
    out = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and block.get("text"):
            out.append(("text", block["text"]))
        elif t == "tool_use":
            inp = block.get("input") or {}
            name = block.get("name") or ""
            if "memory_record_learning" in name or "memory_store" in name or "memory_define_spec" in name:
                # What the session ALREADY saved: the extractor must not re-record it.
                hint = "ALREADY SAVED: " + str(inp.get("title") or inp.get("name") or "")
            else:
                hint = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("query") or ""
            out.append(("tool", f"[tool {name}] {str(hint)[:MAX_TOOL_INPUT_CHARS]}"))
        # tool_result and thinking blocks are dropped: results are bulk output,
        # thinking is not the agent's stated position.
    return out


def sanitize(path, start_line):
    """Transcript lines after start_line -> (rendered text with L<n> markers,
    session_id, total_lines, skip_reason)."""
    rows, session_id, n = [], None, 0
    with open(path, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            session_id = session_id or d.get("sessionId")
            if d.get("entrypoint") == "sdk-cli":
                return "", session_id, n, "headless claude -p session (librarian/extractor runs)"
            if n <= start_line or d.get("type") not in ("user", "assistant"):
                continue
            if d.get("isMeta") or d.get("isSidechain"):
                continue
            who = "USER" if d["type"] == "user" else "CLAUDE"
            for kind, text in _text_of((d.get("message") or {}).get("content")):
                text = text.strip()
                if not text or text.startswith("<command-") or text.startswith("<local-command"):
                    continue
                if kind == "text" and who == "USER" and "<system-reminder>" in text:
                    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL).strip()
                    if not text:
                        continue
                rows.append(f"L{n} {who}: {text}")
    rendered, _ = redact_secrets("\n".join(rows))
    return rendered, session_id, n, None


def chunks(text):
    out, cur = [], []
    size = 0
    for line in text.splitlines():
        if size + len(line) > CHUNK_CHARS and cur:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line[:CHUNK_CHARS])
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out[-MAX_CHUNKS_PER_SESSION:]   # newest content wins when over the cap


def extract(chunk, model, max_records):
    prompt = PROMPT_FILE.read_text().replace("{MAX_RECORDS}", str(max_records))
    # No tools, no MCP servers, no saved transcript (so the extractor never
    # extracts itself), instructions as the system prompt, and a neutral cwd so
    # no project CLAUDE.md is discovered. --bare would be cleaner but needs an
    # API key; this keeps the run on the subscription.
    cmd = [CLAUDE_BIN, "-p", "Extract records from the transcript excerpt on stdin.",
           "--system-prompt", prompt, "--output-format", "json", "--max-turns", "1",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    proc = subprocess.run(cmd, input=chunk, capture_output=True, text=True, timeout=600,
                          cwd=tempfile.gettempdir())
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p exit {proc.returncode}: {proc.stderr[:300]}")
    body = json.loads(proc.stdout).get("result", "")
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        return []
    records = json.loads(m.group(0)).get("records") or []
    clean = []
    for r in records[:max_records]:
        if r.get("kind") in KINDS and r.get("title") and r.get("details"):
            clean.append(r)
    return clean


def _text(result):
    try:
        return result.content[0].text
    except Exception:
        return ""


async def server_features():
    """The server's feature switches (memory_health `features`), read before any
    model call so a disabled server costs nothing. None = the server predates
    the switch (< 1.43.1)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else None
    async with streamablehttp_client(URL, headers=headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            health = json.loads(_text(await s.call_tool("memory_health", {"include_storage": False})) or "{}")
    return health.get("features")


async def write_records(batch, dry_run):
    """batch: list of (junto_project, transcript_path, session_id, record)."""
    if dry_run or not batch:
        for project, path, sid, r in batch:
            print(json.dumps({"project": project, "session": sid, **r}, indent=2))
        return {"written": 0, "duplicates": 0}
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else None
    written = dupes = disabled = 0
    off_projects = set()
    async with streamablehttp_client(URL, headers=headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            start = json.loads(_text(await s.call_tool("memory_start_session", {
                "project": "junto", "claude_instance": IDENTITY,
                "role_description": "Nightly transcript extractor (Memento #4). Writes unreviewed learnings "
                                    "with provenance from opted-in Claude Code transcripts.",
                **({"api_key": API_KEY} if API_KEY else {}),
            })))
            sid = start.get("session_id")
            if not sid:
                raise RuntimeError(f"start_session failed: {start}")
            for project, path, cc_sid, r in batch:
                if project in off_projects:
                    disabled += 1
                    continue
                probe = json.loads(_text(await s.call_tool("memory_query", {
                    "session_id": sid, "query": f"{r['title']}\n{r['details'][:300]}",
                    "project": project, "limit": 1, "include_shared": False,
                })) or "{}")
                top = (probe.get("results") or [{}])[0]
                if int(str(top.get("relevance", "0")).rstrip("%") or 0) >= DUPLICATE_RELEVANCE:
                    dupes += 1
                    continue
                details = (f"{r['details']}\n\n---\nProvenance: transcript-extract · kind={r['kind']} · "
                           f"cc_session={cc_sid} · lines {r.get('lines', '?')} · {path}")
                res = json.loads(_text(await s.call_tool("memory_record_learning", {
                    "session_id": sid, "project": project, "title": r["title"], "details": details,
                    "tags": ["transcript-extract", f"kind:{r['kind']}", f"cc-session:{cc_sid}"],
                })) or "{}")
                if res.get("code") == "transcript_extract_disabled":
                    # Switched off for this project (query_config override).
                    print(f"transcript extraction disabled on the server for {project}", file=sys.stderr)
                    off_projects.add(project)
                    disabled += 1
                    continue
                written += 1
            await s.call_tool("memory_end_session", {
                "session_id": sid, "summary": f"transcript extract: {written} written, {dupes} duplicates skipped",
            })
    out = {"written": written, "duplicates": dupes}
    if disabled:
        out["disabled"] = disabled
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="max transcripts per run")
    args = ap.parse_args()

    cfg = load_json(CONFIG, {})
    projects = cfg.get("projects") or {}
    if not projects:
        print(f"no opted-in projects in {CONFIG}; nothing to do")
        return
    try:
        features = asyncio.run(server_features())
    except Exception as e:
        print(f"cannot reach {URL} to check the server switch: {e}", file=sys.stderr)
        sys.exit(2)
    if features is None:
        print("server predates the transcript_extract switch (< 1.43.1); proceeding", file=sys.stderr)
    elif not features.get("transcript_extract"):
        print("transcript extraction is disabled on the server (query_config transcript_extract); nothing to do")
        return
    state = load_json(STATE, {})
    model = cfg.get("model", "sonnet")
    max_records = int(cfg.get("max_records_per_chunk", 5))
    now = time.time()

    todo = []
    for dirname, junto_project in projects.items():
        for path in sorted((PROJECTS_DIR / dirname).glob("*.jsonl")):
            if now - path.stat().st_mtime < IDLE_SECONDS:
                continue
            todo.append((path, junto_project))
    batch, new_state, done = [], dict(state), 0
    for path, junto_project in todo:
        if done >= args.limit:
            break
        start_line = int(state.get(str(path), 0))
        text, cc_sid, total, skip = sanitize(path, start_line)
        if skip or total <= start_line:
            new_state[str(path)] = total
            continue
        done += 1
        try:
            for chunk in chunks(text):
                for r in extract(chunk, model, max_records):
                    batch.append((junto_project, str(path), cc_sid, r))
            new_state[str(path)] = total
        except Exception as e:  # leave the watermark so the next run retries
            print(f"extract failed for {path}: {e}", file=sys.stderr)
    result = asyncio.run(write_records(batch, args.dry_run))
    print(f"transcripts processed: {done}, records: {len(batch)}, {result}")
    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(new_state, indent=1))


def _arm_deadline(seconds):
    """Hard ceiling on one run. signal.alarm is Unix-only, so a daemon timer
    thread that hard-exits works on Windows (Task Scheduler) too."""
    import threading
    t = threading.Timer(seconds, lambda: (print(f"deadline {seconds}s hit; exiting", file=sys.stderr),
                                          os._exit(3)))
    t.daemon = True
    t.start()


if __name__ == "__main__":
    _arm_deadline(RUN_DEADLINE_SECONDS)
    main()
