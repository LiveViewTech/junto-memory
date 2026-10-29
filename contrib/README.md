# Contrib

Optional tools, templates, and examples that complement the core MCP server.

## runner/

The local-model runner for delegated tasks (`memory_delegate`). It claims runs from the server, fetches items through the connection's adapter (inline, imap read-only, file, mcp), asks the local Ollama model for one schema-valid JSON result per item, and reports back. The server applies routes. Setup, the safety model and limits: the main README, section **Local-Model Delegation**.

- `junto-runner.py` — the runner (single file; needs `cryptography`, plus `mcp` for MCP sources)
- `junto-runner.service` — systemd user unit (install steps in its header)

## context-report/

`junto-context-report.py` — run one copy per machine on a systemd user timer. It reports each local agent's context fill via `memory_report_context`, so `memory_get_agent_status` and `/admin` show every machine's agents. It also runs the idle-park auto-reset gate for opted-in agents. Off the server box, set `JUNTO_SHARED_MEMORY_URL=http://<server>:8080/mcp`.

## launchers/

PowerShell script for launching multiple Claude Code agents in Windows Terminal tabs, each with MCP guidelines injected at system-prompt authority.

**Why this matters:** Claude Code has a priority hierarchy — system prompt > CLAUDE.md > user messages > tool results. MCP guidelines arrive as tool results (lowest tier). The launcher uses `--append-system-prompt-file` to inject a preamble that says "MCP server guidelines are LAW," elevating them to system-prompt authority.

- `launch-agents.ps1` — Multi-agent launcher for Windows Terminal
- `agents.example.json` — Agent config template (copy to `agents.json` and customize)

```powershell
# Copy and customize the config
cp agents.example.json agents.json

# Launch all agents
.\launch-agents.ps1

# Launch specific agents
.\launch-agents.ps1 -Agents coordinator,backend

# Preview what would be launched
.\launch-agents.ps1 -DryRun
```

## templates/

CLAUDE.md templates for Claude Code projects using the shared memory server.

- `AGENT_CLAUDE.md` — Standard agent template with full session lifecycle, memory rules, and parking checklist. Replace `{PROJECT}`, `{AGENT_NAME}`, `{ROLE_DESCRIPTION}`, and `{TECH_STACK_LIST}` with your values.
- `COORDINATOR_CLAUDE.md` — Coordinator agent template (diagnoses and delegates, doesn't implement). Replace placeholders with your team structure.

Also see the root-level files:
- `GLOBAL_CLAUDE.md.example` — Minimal bootstrap for `~/.claude/CLAUDE.md` (goes on every machine)
- `CLAUDE.md.template` — Lightweight per-project template

**Recommended setup:**
1. Copy `GLOBAL_CLAUDE.md.example` to `~/.claude/CLAUDE.md` on every machine
2. Copy `AGENT_CLAUDE.md` or `COORDINATOR_CLAUDE.md` into each project directory as `CLAUDE.md`
3. Fill in the placeholders
4. Set behavioral rules server-side with `memory_guidelines` — they'll push to all agents automatically

## orchestrator/

Experimental Claude-powered orchestration system for automatically spawning and coordinating multiple Claude instances.

- `brain.py` — Decision-making engine (requires Anthropic API key)
- `dispatcher.py` — Message delivery via tmux injection
- `claude-spawn.py` — Claude instance launcher

Status: Early prototype. Not production-ready.

## ui/

Experimental web dashboard for monitoring orchestrated Claude sessions.

- `app.py` — FastAPI backend
- `tmux-proxy.py` — Terminal proxy for web-based tmux interaction
- `static/` — Frontend HTML

Status: Early prototype. Not production-ready.
