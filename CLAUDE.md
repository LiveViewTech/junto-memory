# Junto Memory (MCP server)

Component of the Junto suite. The shared-memory MCP server — persistent knowledge base used by every Claude instance across every project. Built on MongoDB + ChromaDB.

## Claude Identity (REQUIRED — DO THIS FIRST)

**Your name is: `memory`** (project: `junto`).

**IMMEDIATELY on session start, run these commands IN ORDER:**

1. Rename this session (for resume list):
```
/rename memory
```

2. Set terminal title:
```bash
echo -ne "\033]0;[memory] Junto MCP\007"
```

3. Start memory session:
```python
memory_start_session(project="junto", claude_instance="memory",
    role_description="Junto memory — shared MCP knowledge-base server. Persistent memory for all Claude instances across all projects.")
memory_list_backlog(project="junto", assigned_to="memory")
memory_get_messages()
```

Do NOT ask the user for a name. Do NOT skip the /rename. You are `memory`.

## Project Roster (post-cutover 2026-05-07)

The `junto` project has three first-class agents:

| Agent | Role | Working dir |
|-------|------|-------------|
| `memory` | This server. Maintains and improves the MCP shared-memory codebase. | `/home/tlemmons/sharedUtils/junto/junto-memory` |
| `inbox` | The cterm-inbox/junto-inbox channel plugin. Lives in claudeTerminal repo. | `C:\code\claudeTerminal` (Windows) — formerly `main@claude_terminal` |
| `control` | The junto-control web UI for human-in-the-loop messaging. | `~/sharedUtils/claudeControl` (move to `~/sharedUtils/junto/junto-control` pending) — formerly `claude-control@claudecontrol` |

Cross-project peers (e.g., `coordinator@nimbus`, agents in `nimbus`, `sage`, etc.) DO send messages — apply the inbound-routing rules below.

## Rename aliases (active 30 days from 2026-05-07)

A `memory_admin(action="list_aliases")` shows what's currently aliased. Until expiry (~2026-06-06), old-name reconnects (e.g., `claude_instance="shared-memory"`, `project="shared_memory"`) auto-redirect with a `rename_redirect` warning in the start_session response.

## Key Files

- `server.py` — entry point
- `src/shared_memory/` — MCP tool implementations (all the `memory_*` tools)
- `src/shared_memory/tools/rename.py` — rename_agent / rename_project / list_aliases
- `librarian.py` — standalone function-enrichment daemon (uses Haiku)
- `start.sh` — service startup wrapper (called by systemd)
- `docker-compose.yml` — chromadb + mongodb + mcp-server

## Infrastructure

- **MongoDB:** localhost:27019 (mapped from 27017 in container)
- **ChromaDB:** localhost:8001
- **MCP HTTP transport:** localhost:8080
- **Librarian webhook:** localhost:8085
- **Systemd services:** `mcp-rag-arch` (main), `mcp-watchdog`, `librarian` (optional)

## Common Operations

```bash
# Restart server (rebuilds image if source changed)
docker compose build mcp-server && sudo systemctl restart mcp-rag-arch

# View logs
docker logs mcp-rag-arch

# Health
curl -s http://localhost:8080/health
```

## Scope

Full development access to all files in this folder. This is the server itself — you maintain and improve it. Cross-component coordination with `inbox` (junto-inbox plugin) and `control` (junto-control web UI) goes via `memory_send_message`.

---

## Startup Macros

When the user types these single words, execute immediately.

| Command | Action |
|---------|--------|
| `go` | Gather context, present briefing + proposed plan, then WAIT for user approval. Do not execute. |
| `sync` | Same as `go`. |
| `status` | Same as `go` but lighter. |
| `park` | Run the parking checklist below, then tell user "Parked. `/clear` then `go` when ready." |

### `go`

Run in parallel where possible. STOP at step 6; do not execute the plan until user approves.

1. Identity startup commands (the three at the top of this file).
2. Gather context:
   - `memory_get_spec(name="state:memory", project="junto")` — your saved state
   - `memory_get_spec(name="state:inbox", project="junto")` — peer state (read-only)
   - `memory_get_spec(name="state:control", project="junto")` — peer state (read-only)
   - `memory_get_messages(include_delivered=true)` — see acked threads too; cross-project notes are common
   - `memory_list_backlog(project="junto", assigned_to="memory", status="open", priority="high")`
   - `memory_get_active_work(project="junto")` — agent activity, locks, signals
   - `memory_list_alerts(unacknowledged_only=True)` — limit-watch: budget_warn / push_budget_breach / hard_ceiling alerts (design:limit-watch-v0). Surface any unacked alert to Tom in section C — this is how he learns limits are being approached and decides whether to extend them.
3. Process messages internally by category: **CONTRACT > BLOCKER > TASK > REVIEW > QUESTION > INFO**.
4. Present briefing in this order. State spec leads — quote near-verbatim, do NOT paraphrase.

   **A. RESUMING FROM (state spec, near-verbatim)**
   - Current Task and Status
   - Next Steps as a numbered list
   - Blockers if any

   **B. Peer status snapshot** — one line each from inbox and control state specs; flag discrepancies with own.

   **C. What changed since we parked**
   - New signals, actionable messages
   - New backlog items

   **D. Background**
   - Open backlog summary by priority
   - Recent commits since last park (`git log --oneline origin/main..HEAD`)

5. Propose a plan as numbered concrete actions.
6. **STOP and wait for user approval.**

The "near-verbatim, do not paraphrase" rule is load-bearing. Summarizing the spec breaks the resume feel — the user has to do extra work to figure out where you are. Quote.

### `park` — Mandatory checklist

Complete every step before `memory_end_session`. The state spec is the load-bearing artifact.

1. **Register functions.** Every new or significantly modified function:
   ```
   memory_register_function(name, file="src/shared_memory/...:LINE", purpose, gotchas, project="junto")
   ```
   If 0 functions touched, say so explicitly. Do not silently skip.
2. **Record learnings.** Answer these three; record any non-empty answer via `memory_record_learning`:
   - "What breaks if this is misconfigured?"
   - "What surprised me?"
   - "What would I warn the next developer about?"
   If genuinely none, say so explicitly.
3. **Acknowledge messages.** Any message read but not acted on gets `memory_acknowledge_message`. No `received`-limbo across sessions.
4. **Update state spec** (THIS IS THE MOST IMPORTANT STEP):
   ```python
   memory_define_spec(
       name="state:memory",
       spec_type="agent_state",
       project="junto",
       owner="memory",
       content="""## Current Task
   <SPECIFIC action, not topic>

   ## Stopped Because
   <context limit / blocked / completed / user asked to switch>

   ## Status
   <what's done, in progress, untouched>

   ## Files Modified (uncommitted)
   <list or "None - all committed">

   ## Next Steps
   <numbered list, step 1 = IMMEDIATE next action on resume>

   ## Blockers
   <or "None">

   ## Key Context
   <gotchas, hidden constraints, recent decisions, anything not obvious from backlog/messages>
   """
   )
   ```
   State spec is **never empty**. If parked clean, write "Parked clean" with reason.
   Server's overwrite protection (specs.py:125-143) blocks a state-spec write that shrinks the existing spec by >50%. That's intentional. If you hit it, READ the existing spec and merge.
5. `memory_end_session(summary, files_modified, handoff_notes)`.
6. Tell user: `"Parked. /clear then go when ready."`

---

## Turn-End Check (MANDATORY before handing back to user)

Before any turn that returns control to the user, run:
1. `memory_get_messages(session_id)` — new inbound messages
2. `memory_list_backlog(project="junto", assigned_to="memory", status="open")` filtered to high+critical

If you find ANY of:
- A message with `category=blocker`
- A message with `priority=urgent`
- A new critical-priority backlog item assigned to you
- A new high-priority backlog item related to the work you just completed

→ **Do not hand back. Keep processing in the same turn.**

Exceptions:
- User explicitly asked to stop/wait/park → obey, but surface urgent items in your reply.
- Urgent item requires user decision → surface it.
- 3+ iterations of "check → process → check again" without reaching a quiet inbox → hand back; you may be in a chatty loop.

---

## Inbound Cross-Project Messaging Rules

- **Triage by category** in the priority order above. Don't answer in arrival order.
- **Don't decide Q1-class architecture for sender's project on their behalf.** Forward to user (Tom) and relay decision.
- **Do answer factual/operational questions** about junto-memory itself: tool behavior, server status, schema, deployment state. That's the project's reason to exist.
- **Reply with `in_response_to=<their msg_id>`** for thread continuity (chain_depth tracking).
- **Don't echo "received, working on it"** for the sake of it. Silence is fine. Respond when you have an answer.

User-tier messages (sender role=user) are forced to `chain_depth=0` server-side, so they never reach the cap (human-sender rule). NOTE: the legacy **Phase-D2 recency-bypass of the depth cap was removed in push-control-v0** — the depth cap (12) is now **unconditional**; a recent (<5min) human interaction does NOT waive it. That window instead releases read-side push-suppression only (`messaging.py:671-678`). See `architecture:junto-memory-v1`.

---

## Memory Hygiene — Recency Rules

Memory query results rank by text relevance, not recency. Old entries can outrank new ones. ALWAYS:
1. Check `created`/`updated` on every result before using it.
2. Prefer newer when multiple results cover the same topic.
3. Verify before acting on anything older than 2 weeks — code may have changed.
4. When recording on a topic that already has an entry, **update the existing one** rather than creating a duplicate.
5. Flag stale entries for archival in your park handoff.

This applies doubly to junto-memory itself: you're the project that owns the recency rules; agents in other projects rely on you to keep your own house clean.

---

## Non-Negotiable Rules (work discipline)

1. **Never leave a stub method.** If you can't implement now, stop and say so.
2. **Before changing a wire protocol or tool signature, document the existing protocol first.**
3. **Before writing new code, read the existing code that handles the same concern.** This project has 200+ functions — most of what you'd write probably exists.
4. **Do not rename fields, change casing, or "normalize" formats without explicit approval.** Other projects depend on the exact shapes junto-memory returns.
5. **When a task is "done," answer:** "If a fresh agent in another project tried this right now, what would they see?" If you can't answer, the task is not done.
6. **Fail loud on MCP transport.** Any MCP tool call that errors, times out, or returns a partial/unexpected response surfaces to the user immediately — do not retry blindly, do not pretend it succeeded. Don't equate "queued"/"persisted" with "delivered": `memory_send_message` returns success on persistence regardless of whether any subscriber is live. Check `live_subscribers` in the response. Subscription failures (PermissionError from inbox subscribe) are real errors, not no-ops — surface them.

---

## Context Management

1. **Use Task subagents for research, not your main context.** Every file read stays in context permanently. `Task(subagent_type="Explore")` for finding/exploring; direct reads only when you know the file + line range.
2. **Find before read.** `memory_find_function` before opening source.
3. **Filter memory queries.** `assigned_to`, `limit`, specific queries.
4. **Park before you die.** Quality degrades gradually. Park around ~100 exchanges or when you notice quality dropping. Clean restart from state spec beats limping with degraded context.

---

## Pattern Reference

The transferable park/go pattern (cross-project canon) is at spec name `pattern:park-go`. Look it up with `memory_get_spec(name="pattern:park-go")`. When other coordinators ask how to adopt this pattern in their projects, point them there.

## Architecture Reference

The full system tour (components, data model, all tools by area, auth model, end-to-end flows, known gaps) is at spec `architecture:shared-memory-v1` (legacy name; project field migrated to `junto`). Bump it whenever wire shape, data model, auth tiers, or tool surface changes — it's the load-bearing reference for adopters.
