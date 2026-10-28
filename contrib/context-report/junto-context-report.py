#!/usr/bin/env python3
"""junto context-report — push every LOCAL junto agent's context fill to the central
store over MCP, so memory_get_agent_status shows all agents on ALL machines.

Box-agnostic: run one copy per machine via a systemd --user timer. Enumerates local
`claude` processes (/proc/<pid>/environ JUNTO_AGENT + /proc/<pid>/cwd), reads each
agent's <cwd>/.claude/context-metrics.json (written by statusline.sh), and calls
memory_report_context over MCP at JUNTO_SHARED_MEMORY_URL.

Uses MCP (not direct Mongo) because the Mongo port is localhost-only on the server box
while :8080/mcp is reachable fleet-wide — so this same script works on madrox, HA, etc.
On the server box the default localhost URL is correct; off-box set
JUNTO_SHARED_MEMORY_URL=http://sage:8080/mcp (or 192.168.15.240) in the service env.

(Filename kept as sage-context-report.py so the existing sage service/timer are unchanged;
the script itself is box-agnostic.) interface:context-tokens-sideband-v0.

Idle-park auto-reset (design:idle-park-autoreset-v0): the report response lists pending
resets for local agents that parked after an idle nudge and are opted in. For each, find
the agent's tmux pane, run the SAFE-INJECT GATE (learning_69b6fe0d5771747a), type /clear,
verify, type go, and report the outcome back in a second memory_report_context call.
"""
import os
import glob
import json
import sys
import socket
import asyncio
import re
import signal
import subprocess
import time

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
except ImportError:
    print("mcp sdk not available", file=sys.stderr)
    sys.exit(1)

URL = os.environ.get("JUNTO_SHARED_MEMORY_URL") or "http://localhost:8080/mcp"
PROJECT = os.environ.get("JUNTO_PROJECT") or "cerebro"
REPORTER = "ctx-reporter-" + socket.gethostname().split(".")[0]

PRESENCE_SECONDS = 600          # any tmux keypress on the session this recently = human here
BANNER = "I AM PARKED"
# The server's idle-park nudge as rendered in the pane. A reset doc exists only
# when end_session answered that nudge, so the notice anchors the park even
# when the agent paraphrases away the banner (nimbusSupport, msg_03714e33f488).
NUDGE_MARKER = "[SYSTEM NOTICE] Idle"
PROMPT = "\u276f"               # ❯ — CC input-line / submitted-prompt glyph
BUSY_MARKERS = ("esc to interrupt", "queued messages")
DIALOG_MARKERS = ("Do you want", "No, exit")
SURVEY_MARKER = "0: Dismiss"
OPTION_RE = re.compile(r"^\s*\u276f\s*\d+\.")
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
GAP_RE = re.compile(r"\s{8,}")  # right-aligned hint text sharing the input row


_pids = {}  # agent -> every local pid carrying JUNTO_AGENT (for the tmux pane lookup)


def running_agents():
    for environ_path in glob.glob("/proc/*/environ"):
        pid = environ_path.split("/")[2]
        if not pid.isdigit():  # /proc/self, /proc/thread-self
            continue
        try:
            with open(environ_path, "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        env = dict(p.split("=", 1) for p in raw.split("\0") if "=" in p)
        agent = env.get("JUNTO_AGENT")
        if not agent:
            continue
        try:
            cwd = os.readlink("/proc/%s/cwd" % pid)
        except OSError:
            continue
        _pids.setdefault(agent, []).append(int(pid))
        yield agent, cwd


def collect():
    freshest = {}
    for agent, cwd in running_agents():
        path = os.path.join(cwd, ".claude", "context-metrics.json")
        try:
            with open(path) as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            continue
        if agent not in freshest or m.get("written_at", "") > freshest[agent].get("written_at", ""):
            freshest[agent] = m
    reports = []
    for agent, m in freshest.items():
        tokens = m.get("context_tokens")
        if not isinstance(tokens, int):
            continue
        reports.append({
            "instance": agent,
            "context_tokens": tokens,
            "context_window": m.get("context_window"),
            "context_pct": m.get("context_pct"),
            "written_at": m.get("written_at"),
        })
    return reports


# --- idle-park auto-reset ---------------------------------------------------------

def _tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)


def _ppid(pid):
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def find_pane(pid):
    """Return (pane_id, session_name) of the tmux pane whose process tree contains pid."""
    out = _tmux("list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{session_name}")
    if out.returncode != 0:
        return None
    panes = {}
    for line in out.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            panes[int(parts[0])] = (parts[1], parts[2])
    while pid and pid > 1:
        if pid in panes:
            return panes[pid]
        pid = _ppid(pid)
    return None


def capture(pane):
    # -e keeps SGR styling so _after_prompt can tell grey ghost text from typed input.
    return _tmux("capture-pane", "-p", "-e", "-t", pane, "-S", "-80").stdout.splitlines()


def _plain(line):
    return ANSI_RE.sub("", line)


def _grey(params, grey):
    """Fold one SGR parameter list into the running is-grey state."""
    codes = [int(c) if c.isdigit() else 0 for c in params.split(";")] if params else [0]
    i = 0
    while i < len(codes):
        c = codes[i]
        if c in (0, 22, 39):
            grey = False
        elif c in (2, 90):
            grey = True
        elif c == 38 and i + 2 < len(codes) and codes[i + 1] == 5:
            n = codes[i + 2]
            grey = n == 8 or 232 <= n <= 255
            i += 2
        elif c == 38 and i + 4 < len(codes) and codes[i + 1] == 2:
            grey = codes[i + 2] == codes[i + 3] == codes[i + 4]
            i += 4
        elif 30 <= c <= 37 or 91 <= c <= 97:
            grey = False
        i += 1
    return grey


def _fold(text, grey=False):
    """Split styled text into (non-grey text, grey state at its end). CC draws prompt
    suggestions and hints grey; tmux -e carries SGR state across lines."""
    out, pos = [], 0
    for m in SGR_RE.finditer(text):
        if not grey:
            out.append(text[pos:m.start()])
        grey = _grey(m.group(1), grey)
        pos = m.end()
    if not grey:
        out.append(text[pos:])
    return _plain("".join(out)), grey


def _line_states(raw):
    """Grey state at the start of each captured line."""
    states, grey = [], False
    for line in raw:
        states.append(grey)
        grey = _fold(line, grey)[1]
    return states


def _after_prompt(line, grey=False):
    """Text a human typed after the ❯ glyph on the live input row. Typed text renders in
    the default colour (checked 2026-10-15, CC 2.1.281); grey ghost text and anything after
    a wide gap (right-aligned hints sharing the row) don't count."""
    head, rest = line.split(PROMPT, 1)
    typed = _fold(rest, _fold(head, grey)[1])[0]
    return GAP_RE.split(typed.replace("\u00a0", " "), 1)[0].strip()


def _is_anchor(line):
    return BANNER in line or NUDGE_MARKER in line


def analyze(lines):
    """Gate facts from a pane capture (SGR escapes allowed). Returns (ok, reason)."""
    raw = lines
    lines = [_plain(l) for l in raw]
    grey = _line_states(raw)
    prompt_idx = [i for i, l in enumerate(lines) if l.lstrip().startswith(PROMPT)]
    if not prompt_idx:
        return False, "no input line"
    input_idx = prompt_idx[-1]
    if _after_prompt(raw[input_idx], grey[input_idx]):
        return False, "input not empty"
    # Submitted prompts are drawn grey on a grey background, so judge them as plain text.
    last_user = max([i for i in prompt_idx[:-1] if _after_prompt(lines[i])] or [-1])
    anchors = [i for i, l in enumerate(lines[:input_idx]) if _is_anchor(l)]
    if not anchors or anchors[-1] < last_user:
        return False, "no park banner or nudge after last prompt"
    tail = lines[anchors[-1]:]
    text = "\n".join(tail)
    if any(m in text for m in BUSY_MARKERS):
        return False, "busy"
    if any(m in text for m in DIALOG_MARKERS) or any(OPTION_RE.match(l) for l in tail):
        return False, "dialog open"
    if SURVEY_MARKER in text:
        return False, "survey"
    return True, "ok"


def human_present(session):
    out = _tmux("list-clients", "-t", session, "-F", "#{client_activity}")
    now = time.time()
    for line in out.stdout.split():
        try:
            if now - int(line) < PRESENCE_SECONDS:
                return True
        except ValueError:
            continue
    return False


def send(pane, text=None, key=None):
    if text is not None:
        _tmux("send-keys", "-t", pane, "-l", text)
    if key is not None:
        _tmux("send-keys", "-t", pane, key)


def attempt_reset(reset):
    """Run the gate and inject for one pending reset. Returns (outcome, detail)."""
    agent = reset.get("agent")
    pids = _pids.get(agent)
    if not pids:
        return "deferred", "agent process not found"
    found = next((f for f in map(find_pane, pids) if f), None)
    if found is None:
        return "failed", "agent is not running in a tmux pane"
    pane, session = found
    if human_present(session):
        return "deferred", "human keypress on %s within %ss" % (session, PRESENCE_SECONDS)
    lines = capture(pane)
    ok, why = analyze(lines)
    if why == "input not empty":
        # Keep the styled row in the journal so a stuck gate can be diagnosed later.
        row = [l for l in lines if _plain(l).lstrip().startswith(PROMPT)][-1]
        print("%s: input row %r" % (agent, row[:300]), file=sys.stderr)
    if not ok and why == "survey":
        send(pane, text="0")
        time.sleep(1)
        ok, why = analyze(capture(pane))
        if not ok:
            if why == "input not empty":
                send(pane, key="BSpace")
            return "deferred", "survey dismiss did not settle: " + why
    if not ok:
        return "deferred", why
    send(pane, text="/clear")
    time.sleep(0.5)
    send(pane, key="Enter")
    time.sleep(2.5)
    after = capture(pane)
    if any(_is_anchor(_plain(l)) for l in after):
        return "failed", "park banner/nudge still on screen after /clear (pane %s)" % pane
    states = _line_states(after)
    inputs = [i for i, l in enumerate(after) if _plain(l).lstrip().startswith(PROMPT)]
    if not inputs or _after_prompt(after[inputs[-1]], states[inputs[-1]]):
        return "failed", "input line not empty/absent after /clear (pane %s)" % pane
    send(pane, text="go")
    time.sleep(0.5)
    send(pane, key="Enter")
    return "done", "cleared + go sent (pane %s, session %s)" % (pane, session)


def _text(result):
    try:
        return result.content[0].text
    except Exception:
        return ""


async def run():
    reports = collect()
    if not reports:
        print("no context-metrics files found for running agents")
        return
    async with streamablehttp_client(URL) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            start = await session.call_tool("memory_start_session", {
                "project": PROJECT,
                "claude_instance": REPORTER,
                "role_description": "Fleet context-gauge reporter (one per machine).",
            })
            try:
                sid = json.loads(_text(start)).get("session_id")
            except ValueError:
                sid = None
            if not sid:
                print("start_session failed: %s" % _text(start)[:200], file=sys.stderr)
                sys.exit(1)
            result = await session.call_tool(
                "memory_report_context", {"session_id": sid, "reports": reports}
            )
            print("reported %d agents to %s: %s" % (len(reports), URL, _text(result)[:200]))
            try:
                resets = json.loads(_text(result)).get("resets") or []
            except ValueError:
                resets = []
            results = []
            for reset in resets:
                try:
                    outcome, detail = attempt_reset(reset)
                except Exception as e:  # never leave a half-understood pane silently
                    outcome, detail = "deferred", "error: %s" % e
                print("reset %s (%s): %s — %s" % (reset.get("reset_id"), reset.get("agent"), outcome, detail))
                results.append({"reset_id": reset.get("reset_id"), "outcome": outcome, "detail": detail})
            if results:
                ack = await session.call_tool("memory_report_context", {
                    "session_id": sid, "reports": reports, "reset_results": results,
                })
                print("reset results: %s" % _text(ack)[:200])


# Hard ceiling on one run. A run that hung on a blocked syscall (madrox,
# 2026-10-13) kept the oneshot unit "activating" for 11 days, and an
# OnUnitActiveSec timer never re-arms while its unit is still starting, so the
# reporter was silently dead. SIGALRM's default action kills the process even
# inside a blocking call. Pair with TimeoutStartSec=45 on the .service.
RUN_DEADLINE_SECONDS = int(os.environ.get("JUNTO_CTX_REPORT_DEADLINE", "40"))


if __name__ == "__main__":
    signal.alarm(RUN_DEADLINE_SECONDS)
    asyncio.run(run())
