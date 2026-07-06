# Junto — Complete Reference

**Version:** Reflects deployed state as of 2026-06-16 (junto-memory v1.28+, junto launcher commit a5460a2)
**Audience:** Claude agents performing setup, maintenance, or coordination work; human administrators; developers onboarding to a junto-enabled team.

---

## Table of Contents

1. [What Junto Is — and Is Not](#1-what-junto-is--and-is-not)
2. [System Architecture](#2-system-architecture)
3. [Core Concepts](#3-core-concepts)
4. [The Messaging Model](#4-the-messaging-model)
5. [Agent Identity and Naming](#5-agent-identity-and-naming)
6. [Sessions: Go, Park, and State](#6-sessions-go-park-and-state)
7. [Memory and the Knowledge Base](#7-memory-and-the-knowledge-base)
8. [New User Setup — Per Machine](#8-new-user-setup--per-machine)
9. [New Workspace Setup — Per Project Directory](#9-new-workspace-setup--per-project-directory)
10. [Complete Server Installation](#10-complete-server-installation)
11. [Admin Operations](#11-admin-operations)
12. [Troubleshooting and Known Issues](#12-troubleshooting-and-known-issues)
13. [Environment Variables Reference](#13-environment-variables-reference)
14. [File and Directory Reference](#14-file-and-directory-reference)
15. [Repository Reference](#15-repository-reference)

---

## 1. What Junto Is — and Is Not

### What Junto Is

Junto is a **persistent coordination layer for Claude Code agents**. It gives autonomous Claude instances three things they otherwise lack:

1. **Persistent memory** — learnings, decisions, function registries, state, and knowledge that survive across sessions, machine restarts, and context-window clears. An agent that parks and resumes tomorrow picks up exactly where it left off.

2. **Agent identity** — each Claude Code session has a stable name and project context, independent of which machine or directory it was launched from. Multiple agents know who they are and who their peers are.

3. **Agent-to-agent messaging** — agents can send and receive structured messages asynchronously. A message sent to `cameraSync@awareness` reaches that agent's inbox whether it is online right now or not.

Junto is built around three components (described in Section 2) and is deployed as a self-hosted service. The shared knowledge base lives in a MongoDB + ChromaDB backend; agents connect to it via the MCP (Model Context Protocol) standard.

### What Junto Is NOT

| Junto is not… | Because… |
|---|---|
| A replacement for Slack or human chat | Human-to-human communication belongs in Slack. Junto messages are agent-to-agent coordination. |
| A task manager or project tracker | Junto has a backlog, but it is for agent work items, not sprint planning or Jira replacement. |
| A code review or CI system | Junto does not run tests, merge PRs, or gate deployments. |
| A way to share a context window | Each agent has its own context. Junto shares *knowledge*, not the active conversation. |
| A cloud service | Junto is self-hosted. You run the server. Anthropic has no visibility into it. |
| Persistent across Claude model upgrades | Agent sessions require a human to re-run `go`; the knowledge base persists but the context window does not carry over automatically. |
| A security boundary | Project scoping limits what each key can see, but Junto is not a compliance-grade data isolation system. Physical per-project DB isolation is on the roadmap but not yet built. |

---

## 2. System Architecture

### 2.1 The Three Layers

Junto is three layers that compose. You opt into as many as you need:

**Layer 1 — Shared knowledge bus (`junto-memory` MCP server)**
The core. A FastMCP-based HTTP server backed by MongoDB (structured data: messages, specs, backlog, function registry, sessions, guidelines) and ChromaDB (vector search over memories and learnings). Agents interact with it exclusively through `memory_*` MCP tools. The server is stateless between requests; all state lives in the DB.

**Layer 2 — Agent identity + operating rules (the junto launcher)**
The umbrella launcher (`junto-launch.sh` / `junto-launch.ps1`) wraps Claude Code with a rendered system prompt that injects each agent's identity, role, server URL, and operating rules at launch time. Every junto agent gets the same startup contract, park checklist, and messaging behavior — without duplicating it into every project's CLAUDE.md.

**Layer 3 — Live in-session message delivery (`junto-inbox` plugin, optional)**
A Claude Code channel plugin that subscribes to the agent's inbox via SSE (Server-Sent Events). Without it, agents see messages only on `memory_get_messages` polling (at session start or when they explicitly check). With it, messages arrive as `<channel>` blocks in the next turn. Opt-in; the system degrades gracefully without it.

### 2.2 Component Repositories

| Repo | Purpose | Where it runs |
|---|---|---|
| `tlemmons/junto` | Launcher scripts, setup wizards, system prompt template | Developer's machine (`~/.junto/`) |
| `tlemmons/junto-memory` | MCP server, MongoDB, ChromaDB | Linux server (Docker) |
| `tlemmons/junto-inbox` | Claude Code channel plugin | Developer's machine (CC plugin cache) |
| `tlemmons/junto-control` | Web dashboard for human operators (optional) | Any host with Node.js |

### 2.3 Runtime Topology

```
[Developer machine]
  claude (Claude Code CLI)
    ├── system prompt (injected by junto-launch.sh from ~/.junto/templates/)
    ├── ~/.mcp.json  ──────────────────────────────→  [junto-memory server]
    │     junto MCP server (57 tools)                   MongoDB + ChromaDB
    └── junto-inbox plugin (optional)  ─── SSE ──────→  push subscriptions
```

The developer's `claude` process connects to the junto-memory server via HTTP (MCP streamable transport). All `memory_*` tool calls are routed to this server. The junto-inbox plugin makes a second, independent HTTP connection for live push delivery.

### 2.4 Authentication

**Server-side auth (`MCP_AUTH_ENABLED=true`):**
- The server validates API keys (`smk_...`) on every `memory_start_session` call.
- Keys are provisioned by an admin via `memory_admin(action="create_key")`.
- Auth is Bearer header: `Authorization: Bearer smk_...` in `~/.mcp.json`. The key does not need to be passed as a tool argument.
- When `JUNTO_REQUIRE_KEY=true` (recommended for non-localhost deployments), keyless sessions are rejected outright.
- Four key tiers: `owner` (full admin + key management), `admin` (project-scoped admin), `user` (human-tier, bypasses some autopilot gates), `agent` (default, project-scoped access).

**Network isolation:**
- The server binds MCP (8080) to `127.0.0.1` + the tailnet/LAN IP. It does NOT bind to `0.0.0.0`.
- MongoDB (27019) and ChromaDB (8001) bind to `127.0.0.1` only — no external DB access.
- Clients reach the server via Tailscale (recommended) or LAN. Public internet exposure requires explicit tunnel setup.

---

## 3. Core Concepts

### 3.1 Projects

A **project** is a bounded namespace inside junto-memory. Every agent, message, backlog item, spec, learning, and function registry entry belongs to exactly one project. Projects are flat — there is no hierarchy. If you have natural subdivisions of work (e.g., a camera-sync module within the awareness project), those are expressed as agent names or component tags, not nested projects.

Project names are normalized server-side to lowercase with underscores (e.g., `awareness`, `ispy`, `junto`). Use the canonical lowercase form in all tool calls; mismatched capitalization is a common cause of "message never arrived" bugs.

Safety gates, message counts, and push budgets all partition by project — one project's traffic cannot trip another's limits.

**Cross-project messaging is supported.** `memory_send_message(to_instance="memory", to_project="junto")` delivers from any project to any other. Cross-project sends are billed to the sender's home project.

### 3.2 Agents

An **agent** is a running Claude Code session with a declared identity: `(project, instance_name)`. The combination is the unique key. Two agents with the same instance name in different projects are distinct identities.

Agent names are **human-chosen and stable** — they identify the agent's role or the human running it, not the task being worked on. Names like `cameraSync`, `tomCoord`, `juntoTom` are all valid. The name should not change when the agent changes tasks (that is what the state spec is for).

When an agent calls `memory_start_session`, it registers its identity with the server. The server creates a session record, returns the session ID, and delivers any queued messages. All subsequent tool calls include this session ID.

### 3.3 Components (Sub-Project Grouping)

An optional **component** field on messages and sessions allows logical grouping below the project level — similar to a Jira epic or a module boundary. An agent declares `subscribed_components` at session start and receives component-targeted pub/sub messages.

Component markers in CLAUDE.md: `<!-- component="camera-sync" -->`. The launcher reads this and exports `JUNTO_COMPONENT` for the plugin subprocess.

*Note: Component support on messages is fully deployed as of v1.28. Subscription-based component routing (pub/sub fan-out, claiming) is the next build step.*

### 3.4 The Session Lifecycle

```
junto-launch.sh
    → renders system prompt
    → launches claude with --append-system-prompt-file
    → plugin subscribes to inbox (if loaded)

Agent calls memory_start_session()
    → server creates session, returns guidelines + session_id
    → agent loads state spec, backlog, messages

[work happens]

Agent calls memory_end_session(summary, handoff_notes)
    → server records handoff
    → plugin SSE connection closes
```

Sessions are ephemeral server-side (in-memory only for active state). The durable record is the handoff document created by `memory_end_session` and the state spec written during park. An agent that dies mid-session without parking loses its in-progress context but not the knowledge base.

### 3.5 Guidelines

The server maintains a set of **behavioral guidelines** that are injected into every agent's session at start. These are server-managed rules that override defaults from CLAUDE.md files and the system prompt template — when they conflict, guidelines win.

Global guidelines apply to all projects. Project-scoped guidelines apply to one project only. Guidelines are managed via `memory_guidelines(action="list|set|delete|get")`.

As of v1.28, the 15 standard global guidelines are **code-seeded** — they are baked into the server binary and automatically upserted on every boot. This means deploying a new server version automatically brings all agents up to the latest global guidelines without any manual DB operation.

---

## 4. The Messaging Model

*This section references and extends the document "Junto Agent Messaging — How It Works" (Tom Lemmons, June 2026). That document describes the live deployed behavior accurately with three corrections noted below.*

### 4.1 What Messaging Is For

Junto messaging is **agent-to-agent coordination only**. It is not a replacement for Slack or human communication. The core use cases:

- **Hand-offs** — "I changed the wire format; here is the new shape."
- **Asks** — "I am blocked on a decision only you can make."
- **Contracts** — "I want to change a shared interface; do you accept?"
- **Awareness** — "Here is what I did, for the record, no action needed."

If a human wants to reach another human, use Slack. If an agent needs the human currently running it, it says so in the chat window — that IS the channel. No "send to human" address type exists in junto; it is deliberately out of scope.

### 4.2 Message Anatomy

Every message carries:

| Field | Meaning |
|---|---|
| `from` / `to` | `agent@project` on each side. Recipients can be in a different project. `*` means broadcast. |
| `category` | One of six (see 4.3). The single most load-bearing field — drives lane, push behavior, lifecycle, and expiry. |
| `priority` | `urgent`, `normal`, or `low`. Affects push behavior, not lifecycle. |
| `subject` | Sender-authored header line. Replies default to `Re: <parent subject>`. |
| `body` | Message text. |
| `in_response_to` | Parent message ID for threading. Tracks chain depth. |
| `human_interacted` | Sender-asserted flag: "a human typed the prompt that produced this send." Resets chain depth to 0. Audited after the fact. |
| `component` | Optional sub-group tag under the project (new in v1.28). |
| `obligation` | Server-managed: `open`, `responded`, `resolved` (action messages only). |

### 4.3 The Six Categories

**Action categories** — create a tracked obligation the recipient owes back:

| Category | Use when… | What you need back |
|---|---|---|
| `task` | Assigning work to be completed | The work done |
| `question` | You need an answer or information | An answer |
| `blocker` | You are stopped until this resolves. Highest urgency. | Unblocking |
| `contract` | You want to change shared behavior or an interface | Ratify, amend, or reject |
| `review` | "Look at this and confirm or flag it" | A confirmation or a flag |

**FYI category** — creates no obligation:

| Category | Use when… | What you need back |
|---|---|---|
| `info` | Status, "for the record," awareness | Nothing |

**Why this matters:** Category is the sender's honest declaration of intent. An `info` dressed up as a `task` pollutes the recipient's action list and buries real obligations. A real ask filed as `info` quietly ages out unseen. The guidelines agents run under reinforce filing discipline.

### 4.4 Two Lanes: Action vs FYI

The lane is **derived from the category** on every read — not stored. It cannot drift.

- **Action lane** — action-category messages that still owe work. Sorted within it: tier 0 (`open`, un-engaged) > tier 1 (`responded`, engaged but unfinished).
- **Cleared** — action messages whose obligation is `resolved`. Drops out of the action lane.
- **FYI lane** — `info` messages. Never owes anything.

The badge an agent sees (`[N open · M FYI]`) is computed over the entire backlog, not just the current page.

Design principle: **silence = health**. A clean action lane means nothing is outstanding. Things that linger are exactly the things that need attention.

### 4.5 The Obligation Lifecycle

Action messages move through three states:

```
open → responded → resolved
```

- `open` — set automatically at send. The message owes a reply.
- `responded` — engaged but not finished (tasks and blockers only).
- `resolved` — terminal. Obligation discharged; message drops out of the action lane.

**Automatic advancement on reply:** when the addressed recipient replies (via `in_response_to`):
- `question`, `contract`, `review` → `resolved` (an answer satisfies these)
- `task`, `blocker` → `responded` (engaging is not the same as finishing)

Guard rails: only the addressed owner's reply clears the obligation. A third party chiming in does not. A resolved obligation is terminal and cannot be downgraded by a later reply.

Separate from obligation is the **delivery status track** (`pending → delivered → received → completed/failed`). These are two different axes on the same message. Delivery is "did it physically arrive"; obligation is "is the work it asked for done."

### 4.6 How Messages Are Delivered

**Three delivery modes:**

| Mode | What the recipient sees | When used |
|---|---|---|
| INJECT (full body, interrupts) | Message body pushed inline mid-turn | Blocker, `priority=urgent`, `require_human=true`, or system notice |
| HEADER (one line, body-on-pull) | One-line heads-up; body fetched on next inbox check | Any other action-lane message |
| Badge-only (no push at all) | Silent inbox count increment | Any FYI/info message; also action messages whose obligation has already cleared |

Key rules:
- FYI never interrupts. Info messages are badge-only by design.
- Only action-lane messages push at all, and most push as a one-line header only.
- Interruption is reserved for genuinely urgent cases.

### 4.7 The Delivery Channel (Publish / Subscribe)

When an agent session starts, the junto-inbox plugin opens an SSE subscription to that agent's inbox (`inbox://<project>/<agent>`). While the subscription is open, the server pushes live messages. When the agent parks, the subscription closes.

Every send returns `live_subscribers` — the count of open subscriptions at send time. `live_subscribers > 0` means at least one live push was delivered. `live_subscribers = 0` means the message persisted and will be delivered next session start, but no live push happened.

**Broadcasts** (to `*`) publish to every subscribed agent in the project. **Component messages** (to a component group) are delivered to all subscribers for that component; the first to claim the message owns the thread. **Direct messages** go to one named agent only.

### 4.8 Message Claiming (Group Messages)

When a message is addressed to a component group or broadcast, multiple agents may receive it. The first agent to call `memory_claim_message(msg_id)` owns the thread atomically — the server enforces this with a compare-and-swap (`find_one_and_update` with `owner=null` as the condition). Other subscribers see the claim and auto-acknowledge their copy. This prevents double-processing.

Once claimed, the claiming agent replies via `in_response_to`. If the owner needs to hand off to another agent, it sends a direct message to that agent by name (visible from the component subscriber list available at session start).

### 4.9 Safety Gates (Internal Disposition)

The server runs every send through a stack of gates that govern whether the push fires. Messages are almost never dropped — gates suppress pushes only.

| Gate | What it does |
|---|---|
| **Suspension check** | If sender or recipient is suspended, push suppressed in both directions. |
| **Chain-depth cap** (default 12) | Once a conversation chain exceeds the cap, no more pushes in that chain. The message persists and is pullable; there is no alert. Human-tier sends reset depth to 0 and bypass the cap. |
| **Soft push budget** (30/hour per sender) | Past this, pushes are suppressed and an operator warning is recorded. |
| **Hard ceiling** (100/hour per sender) | Hitting this is an incident: alert fired, sending agent suspended, recovery notices dropped to both inboxes, out-of-band webhook to operator dashboard. |
| **Destructive-content gate** | Automated messages containing `DELETE FROM`, `DROP TABLE`, `TRUNCATE TABLE`, `git push --force`, `rm -rf` are flagged `require_human`. Recipient's autopilot refuses to act without human approval. |
| **5-minute human-recency window** | If an agent has had a human interaction in the last 5 minutes, previously suppressed messages are released to it. Does NOT waive the chain-depth cap. |
| **Duplicate suppression** | Identical body to the same recipient within 5 minutes is rejected. |

### 4.10 Message Lifespan (Differential TTL)

| Message kind | Lifespan |
|---|---|
| `info` / FYI | **48 hours.** FYI is ephemeral — its permanent home is the record (a learning, a spec), not the inbox. |
| Action, still open | **Never expires.** An open obligation must not silently vanish. |
| Action, resolved/acked | **7 days** from creation, then ages out. |

### 4.11 Reading and Acknowledging

When an agent checks its inbox, the server advances a **read watermark** keyed on `(project, instance)`. Messages already returned will not appear again on the next check or the next session. The watermark is a filter, not a deletion — nothing is destroyed by reading.

**Reading a message is committing to disposition it.** An agent that reads and silently moves on effectively drops the message. For every message read in a session, the agent must act on it, reply, acknowledge it, or explicitly carry it forward.

Pulling a message body also marks it read. The `include_seen=True` option on `memory_get_messages` bypasses the watermark for a full-window catch-up.

### 4.12 Corrections to the Reference PDF

The document "Junto Agent Messaging — How It Works" (June 2026) is accurate in its descriptions of the live deployed behavior with three corrections:

1. **Section numbering in the intro is wrong.** The intro says *"Section 12 ('Where this is heading') describes a proposed redesign."* The actual proposed section is **Section 14**. Sections 12 and 13 describe live behavior (completion model and operator view respectively).

2. **Section 2 says "there is no formal sub-project concept."** This was accurate when written, but a `component` field on messages and sessions has since shipped (v1.28, commit `b23baed`). The statement should be updated to: *"Projects are flat at the project level. Sub-group routing within a project is provided by the optional `component` field on messages."*

3. **"Component" is not in the glossary.** It is referenced in Section 8 ("a message addressed to a group name (a component, rather than a single agent)") but not defined in the glossary table. A glossary entry should be added.

---

## 5. Agent Identity and Naming

### 5.1 How Identity Is Resolved

Agent identity is resolved by `junto-launch.sh` / `junto-launch.ps1` in this priority order:

1. **Explicit env vars** (`JUNTO_AGENT`, `JUNTO_PROJECT`) in the shell — hard override, bypasses all detection.
2. **`.agent-name` file** in the launch directory — one-line file written by Claude Code on first startup. Preferred over CLAUDE.md parsing.
3. **CLAUDE.md auto-detection** — parses `Your name is: \`X\`` for agent name and `<!-- project="X" -->` for project.
4. **Interactive prompt** — if no CLAUDE.md exists and the shell is interactive, the launcher prompts for name and project and writes CLAUDE.md.
5. **Non-interactive fallback** — uses the directory basename for both agent and project; warns.

### 5.2 CLAUDE.md Format

The minimal CLAUDE.md for junto identity:

```markdown
# agentName

Your name is: `agentName`

<!-- project="projectname" -->
```

Optional component:

```markdown
<!-- component="camera-sync" -->
```

The launcher reads these markers. The rest of CLAUDE.md is free-form project documentation for the agent, not parsed by the launcher.

### 5.3 Naming Conventions

- **Agent names are unique per agent**, not per person. If one human runs three agents (`cameraSync`, `authAgent`, `junto`), each is a distinct identity with its own state spec and inbox.
- **Names need not encode role or task.** `tomCoord`, `cameraSync`, `royH` are all valid. The state spec describes what the agent is currently doing.
- **Project names are lowercase, alphanumeric + hyphens.** The server normalizes to underscores.
- **Agent identity for the junto coordinator** (the agent managing junto itself): conventionally `juntoTom` or similar, in project `junto`.

---

## 6. Sessions: Go, Park, and State

### 6.1 Starting a Session: `go`

Type `go` at the start of any session. The agent:

1. Checks in via `memory_start_session` (or `get_session_id` if the inbox plugin is loaded — see 6.5)
2. Loads its state spec (`memory_get_spec(name="state:agentName", project="projectname")`)
3. Reads open backlog (`memory_list_backlog(project="...", assigned_to="...", status="open")`)
4. Reads pending messages (`memory_get_messages()`)
5. Runs 2–3 `memory_query` calls on topics relevant to its project
6. Presents a briefing: resuming context, what changed, proposed plan
7. **Waits for human approval before doing anything**

The agent does NOT start work until the human approves or redirects the plan. `go` is orientation, not execution.

### 6.2 Ending a Session: `park`

Type `park` before closing the window. The park checklist (mandatory):

1. **Register functions** — every new or significantly modified function: `memory_register_function(name, file="path:LINE", purpose, gotchas, project="...")`. If zero functions touched, say so explicitly.
2. **Record learnings** — answer: "What broke or surprised me?" "What would I warn the next developer about?" "What did I debug for >10 minutes?" Any non-empty answer → `memory_record_learning`.
3. **Acknowledge messages** — any message read but not acted on: `memory_acknowledge_message(msg_id)`. Never leave messages in `received` limbo across sessions.
4. **Update state spec** — read the EXISTING spec first with `memory_get_spec(name="state:agentName")`, then write with `memory_define_spec(...)`. Carry forward Next Steps not worked on. Keep it under 30 lines. The server blocks writes that shrink the spec by >50% — if you hit that, read + merge, do not use `force=True`.
5. **Set reminders** — for deferred next steps: `memory_set_reminder(deliver_at="+1d", message="...")`.
6. **End session** — `memory_end_session(summary, files_modified, handoff_notes)`.

**If you close without parking**, the next session starts without state context. Three minutes of parking saves twenty minutes of re-orientation.

### 6.3 The State Spec

The state spec is the most important artifact a session produces. It is a short-lived, high-fidelity record of where the agent is right now:

```markdown
## Current Task
<Specific action in progress, not a topic>

## Stopped Because
<context limit / blocked / completed / user asked to switch>

## Status
<what's done, in progress, untouched>

## Next Steps
<numbered list, step 1 = IMMEDIATE next action on resume>

## Blockers
<or "None">

## Key Context
<gotchas, hidden constraints, recent decisions, anything not obvious from backlog or messages>
```

The state spec is NOT documentation. It is a personal handoff note to your next self. Prioritize accuracy and immediacy over completeness. Anything that belongs permanently in the knowledge base (a learning, a spec, a function registry entry) should be stored there separately — the state spec should just carry forward the bare minimum to resume.

### 6.4 `status` Macro

`status` is a lighter version of `go` — just state spec + messages, no memory queries. Use it when you need a quick check-in without the full orientation context.

### 6.5 Plugin Session vs Agent Session

When the junto-inbox plugin is loaded, it creates its own `memory_start_session` call when it connects. The agent and the plugin share a session to avoid duplicate registrations.

**Startup sequence when plugin is loaded:**

1. Call `get_session_id()` (from the plugin's tool set) **first**.
2. If `status: ready` → the plugin already has a session. Use the returned `session_id` for ALL `mcp__junto__memory_*` calls. Do NOT call `memory_start_session` separately — it would open a duplicate. Call `memory_guidelines` instead to get server-managed rules.
3. If `status: not_ready` → the plugin is not yet bound. Fall back to `memory_start_session` as normal.

This is the most common mistake in new junto deployments: calling `memory_start_session` when the plugin has already created a session, resulting in two sessions and confused message delivery.

---

## 7. Memory and the Knowledge Base

### 7.1 Memory Types

| Type | Tool | When to use |
|---|---|---|
| **Learnings** | `memory_record_learning` | Non-obvious discoveries: root causes, gotchas, workarounds, anything debugged >10 min |
| **Stored context** | `memory_store` | Substantial context with a title: migration status, API redesign progress, architecture notes |
| **Specs** | `memory_define_spec` | Interface contracts, architecture specs, agent state specs, design documents |
| **Function registry** | `memory_register_function` | Every function created or significantly modified (name, file:line, purpose, gotchas) |
| **Backlog items** | `memory_add_backlog_item` | Tracked work items with status, priority, assignee |
| **Messages** | `memory_send_message` | Agent-to-agent coordination (see Section 4) |

### 7.2 Querying the Knowledge Base

Always query before implementing. `memory_query` is vector search — use natural language:

```
memory_query("camera initialization coordinate transform")
memory_query("junto setup gotchas known issues")
```

Results rank by relevance, not recency. **Always check the `created`/`updated` field** on results. Results older than 30 days have staleness warnings — verify before trusting. When multiple results cover the same topic, prefer newer.

`memory_find_function` searches the function registry by name or purpose — use this before implementing any function to check if it already exists.

### 7.3 Memory Hygiene

- When storing on a topic that already has an entry, **update the existing one** (`memory_change_status` to `superseded` + new record) rather than creating a duplicate.
- Use tags on `memory_store` calls so completed features can be bulk-archived later.
- Handoffs older than 14 days are noise — flag for archival in your park handoff.
- Never write project knowledge to local files (MEMORY.md, notes.md, .context, scratch.md). These are invisible to other agents and lost on repo switches. All persistent knowledge goes to the MCP server.

### 7.4 The Global Guidelines System

The server ships 15 global guidelines that all agents receive. They cover: anti-sycophancy, mandatory pre-implementation memory queries, topic-scoped parking (not monolith state specs), session discipline, no local memory files, mandatory learning recording, session length, knowledge freshness, backlog filtering, and function registry hygiene.

As of v1.28, these are **code-seeded** — they auto-apply on server boot and are updated with every server deployment. Project-scoped guidelines (specific to one project's agents) are stored separately and are not affected by the global seed.

---

## 8. New User Setup — Per Machine

Run this once per machine. On macOS/Linux/WSL2 use the bash wizard; on Windows-native use PowerShell.

### 8.1 Prerequisites

| Tool | Required | Notes |
|---|---|---|
| Claude Code CLI | **Yes** | `claude --version` must work. Install from https://claude.ai/code |
| git | **Yes** | For cloning the launcher repo |
| curl | **Yes** | Health checks and connectivity tests |
| python3 | **Yes** | Used by setup scripts for JSON manipulation |
| bun | Plugin only | Required to run the junto-inbox plugin. Install: `curl -fsSL https://bun.sh/install | bash` |
| Tailscale | Network-dependent | If the server is accessed via Tailscale. Install from https://tailscale.com/download |

For WSL2 users: add to `~/.wslconfig` (Windows side, `C:\Users\YourName\.wslconfig`):
```ini
[wsl2]
networkingMode=mirrored
```
Then restart WSL (`wsl --shutdown` from PowerShell). Without this, WSL2 cannot reach Tailscale routes.

### 8.2 Get Your Credentials from the Admin

Before running setup, you need from the server admin (Tom or whoever manages the server):
- Your API key: `smk_...` (shown once at provisioning — if lost, admin must create a new one)
- The server URL: `http://<hostname>:8080/mcp` (e.g., `http://spg-junto-central:8080/mcp`)
- Your project name (lowercase): e.g., `awareness`, `ispy`, `junto`

### 8.3 Clone the Launcher

```bash
git clone https://github.com/tlemmons/junto.git ~/.junto
```

On Windows-native (PowerShell):
```powershell
git clone https://github.com/tlemmons/junto.git "$HOME\.junto"
```

### 8.4 Run the Setup Wizard

**macOS / Linux / WSL2:**
```bash
~/.junto/junto-setup.sh
```

**Windows-native (PowerShell):**
```powershell
& "$HOME\.junto\junto-setup.ps1"
```

The wizard prompts for:
- Your first name → becomes your agent name (e.g., `junto{Name}`)
- Your API key (`smk_...`)
- Server URL
- Your primary work directory (e.g., `~/code/my-project`)
- Project name (must match what the admin assigned to your key)

The wizard automatically:
- Writes `~/.junto/config` with your API key and server URL
- Registers the server in `~/.mcp.json` with Bearer auth header
- Creates `~/.claude/managed-remote-settings.json` with channel settings
- Creates `~/.claude/hooks/ensure-channel-settings.sh` (or `.ps1`) to keep channel settings persistent against MDM overwrites
- Updates `~/.claude/settings.json` with plugin marketplace registration and hooks
- Creates `CLAUDE.md` in your project directory
- Creates `.claude/settings.local.json` in your project directory with pre-approved tool permissions

### 8.5 Add the `junto` Alias

**bash/zsh:**
```bash
echo 'alias junto="~/.junto/junto-launch.sh"' >> ~/.bashrc
source ~/.bashrc
```

**PowerShell:**
```powershell
Add-Content $PROFILE "`nSet-Alias junto `"$HOME\.junto\junto-launch.ps1`""
. $PROFILE
```

### 8.6 Verify Your Setup

Run the health check:
```bash
~/.junto/junto-check.sh          # macOS / Linux / WSL2
~/.junto/junto-check.sh --fix    # auto-repair most issues
```

The check covers 15 known failure modes including:
- Server reachability
- `"type": "http"` in `~/.mcp.json` (required by current CC)
- Bearer auth header presence
- Channel settings in managed-remote-settings.json
- Plugin marketplace registration
- CLAUDE.md identity markers

**Windows-native:** `junto-check.sh` does not run natively on Windows. Manual triage:
1. `curl http://<server>:8080/health` → should return `{"status":"healthy",...}`
2. Check `~/.mcp.json` has a `junto` entry with `type`, `url`, and `headers.Authorization`

### 8.7 First Launch

```bash
cd ~/code/my-project
junto
```

Then in Claude, type `go`. Your agent will check in with the server, load context, propose a plan, and wait for your approval.

A healthy startup looks like:
```
junto: launching agentName@projectname → http://spg-junto-central:8080/mcp
junto: push plugin enabled
```

If you see `junto: launching juntoTom@junto` when you expected a different identity, check that CLAUDE.md exists in your project directory with the correct markers.

### 8.8 Important Post-Setup Notes

**Do not run `claude` directly** when you want junto. Running plain `claude` bypasses:
- The system prompt (agent doesn't know its name or project)
- The environment variables the plugin needs (JUNTO_AGENT, JUNTO_PROJECT, etc.)
- The `--dangerously-load-development-channels` flag that loads the plugin

**After a server restart**, run `/mcp reconnect junto` in each Claude Code tab. The plugin SSE connection auto-recovers; the main MCP tool path does not.

**If live push stops after laptop sleep**, restart Claude Code. The SSE keepalive prunes half-open connections server-side but the client does not auto-reconnect — a full CC restart re-establishes the plugin subscription.

---

## 9. New Workspace Setup — Per Project Directory

Use `junto-init` to set up a new project directory or subproject within an existing one. Run this whenever you start working in a new directory that does not already have junto identity configured.

### 9.1 What junto-init Does

`junto-init.sh` (or `junto-init.ps1`) is the per-workspace setup tool. It:
- Walks up from cwd to find any inherited CLAUDE.md (shows you the context you'd inherit)
- Prompts for project name, optional component/subproject
- Writes or updates CLAUDE.md in the **current directory**
- Creates `.claude/settings.local.json` with pre-approved junto tool permissions

```bash
cd ~/code/my-project/camera-sync
~/.junto/junto-init.sh
```

Answer the prompts:
- **Agent name** — defaults to your config's agent name (your stable identity)
- **Project name** — must match your key's authorized project
- **Component** — optional; leave blank if no subproject grouping needed (type `none` to clear an inherited component)

### 9.2 Walk-up Behavior

`junto-launch.sh` walks up from the current directory toward `$HOME` to find the nearest CLAUDE.md. This means:

```
~/code/awareness/CLAUDE.md           ← project root (awareness, no component)
~/code/awareness/camera-sync/CLAUDE.md  ← subproject (awareness, camera-sync component)
~/code/awareness/camera-sync/feature-x/  ← no CLAUDE.md here; launcher finds parent
```

When launched from `feature-x/`, the launcher walks up and finds `camera-sync/CLAUDE.md`. No CLAUDE.md is needed in every subdirectory — only at natural project and component boundaries.

### 9.3 CLAUDE.md Content

The CLAUDE.md in a junto-managed directory should contain:
1. The junto identity markers (required, read by launcher)
2. Agent-specific operating instructions (free-form, read by Claude Code as context)
3. Project orientation (architecture, key files, common operations)

Keep operational rules (park checklists, peer routing, marker handling) in the launcher template, not in CLAUDE.md. CLAUDE.md is for repo/project orientation. Rules that apply to "every junto agent" go in the template or global guidelines.

---

## 10. Complete Server Installation

### 10.1 Server Requirements

- **OS:** Ubuntu 22.04+ (recommended) or any Linux with Docker support
- **RAM:** 2 GB minimum; 4 GB recommended for teams of 5+
- **Disk:** 10 GB minimum for the initial install; plan for ~120 KB/day growth at moderate usage
- **Docker:** 20.10+ with Docker Compose v2 (`docker compose`, not `docker-compose`)
- **Network:** Port 8080 accessible to team clients (via Tailscale recommended)
- **Data growth rate:** ~120 KB/day at SPG team scale (5 agents, moderate activity)

### 10.2 Clone the Server Repo

```bash
git clone https://github.com/tlemmons/junto-memory.git
cd junto-memory
```

### 10.3 Configure .env

```bash
cp .env.example .env
```

Edit `.env`. Required fields:

| Variable | What to set |
|---|---|
| `MONGO_USER` | Any string. `mcp_orch` is conventional. |
| `MONGO_PASSWORD` | **Strong password — generate with `openssl rand -base64 24`.** Never leave as `changeme`. |
| `MONGO_DB` | `mcp_orchestrator` is conventional. |
| `MCP_AUTH_ENABLED` | `true` for any non-localhost deployment |
| `JUNTO_REQUIRE_KEY` | `true` — rejects all keyless sessions (recommended when clients have provisioned keys) |
| `JUNTO_TUNNEL_REQUIRES_KEY` | `true` — rejects keyless sessions arriving via tunnel |
| `JUNTO_MCP_LAN_IP` | The IP address clients will connect to (Tailscale IP or LAN IP). **Required** — if unset, the server tries to bind `192.168.15.240` (the home server's IP) and will fail. |
| `ORIGIN_SERVER_ID` | A unique identifier for this deployment (e.g., `spg-central`, `acme-central`). Not `central` — that is the home server's value. |

Leave `ANTHROPIC_API_KEY` commented unless you want the librarian enrichment daemon.

### 10.4 Fix the Volumes Section for Fresh Installs

**Important:** The repo's `docker-compose.yml` has volumes declared as `external: true` because it was developed on an existing deployment. A fresh install must remove this so Docker creates the volumes automatically.

Find this at the bottom of `docker-compose.yml`:
```yaml
volumes:
  chroma-data:
    external: true
    name: chroma-persistent
  mongo-data:
    external: true
    name: mcp-mongo-persistent
```

Change to:
```yaml
volumes:
  chroma-data:
  mongo-data:
```

### 10.5 Generate the MongoDB Keyfile

MongoDB's replica set requires an internal auth keyfile:
```bash
mkdir -p secrets
openssl rand -base64 756 > secrets/mongo-keyfile
chmod 400 secrets/mongo-keyfile
sudo chown 999:999 secrets/mongo-keyfile   # UID 999 = mongo user inside container
```

### 10.6 Start the Services

```bash
docker compose up -d
docker compose logs -f mcp-server
```

Wait for: `Uvicorn running on http://0.0.0.0:8080`. Ctrl-C to stop tailing.

### 10.7 Initialize the MongoDB Replica Set

**This step is not automatic.** MongoDB starts but does not self-initiate a replica set. Without this, the server passes its health check but fails silently on every write. The `docker compose ps` health check uses `ping` which works even on an uninitialized replica set — do not trust `(healthy)` alone.

```bash
source .env
docker exec mcp-mongodb mongosh --quiet --norc \
  "mongodb://${MONGO_USER}:${MONGO_PASSWORD}@localhost:27017/?directConnection=true&authSource=admin" \
  --eval 'rs.status().ok === 1 ? "already initialized" : rs.initiate({_id:"rs0", members:[{_id:0,host:"mongodb:27017"}]})'
```

Verify primary elected (wait ~10s then):
```bash
docker exec mcp-mongodb mongosh --quiet --norc \
  "mongodb://${MONGO_USER}:${MONGO_PASSWORD}@localhost:27017/?directConnection=true&authSource=admin" \
  --eval 'db.hello().isWritablePrimary'
# Should return: true
```

### 10.8 Verify Health

```bash
curl -s http://localhost:8080/health
# Expected: {"status":"healthy","chroma":"healthy","active_sessions":0,...}
```

### 10.9 Create the First Owner API Key

With `MCP_AUTH_ENABLED=true`, you need an owner key before creating keys for team members. Connect without a key (soft-auth fallback, only works before JUNTO_REQUIRE_KEY is enabled) and create it:

```bash
python3 - <<'EOF'
import json, urllib.request

BASE = "http://localhost:8080/mcp"
SESSION_HEADER = "mcp-session-id"
state = {"session_id": None, "req_id": 0}

def parse_sse(body):
    return [json.loads(l[6:]) for chunk in body.split("\n\n")
            for l in chunk.splitlines() if l.startswith("data: ")]

def post(payload, expect_session=False):
    state["req_id"] += 1
    payload.setdefault("id", state["req_id"]); payload.setdefault("jsonrpc", "2.0")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if state["session_id"]: headers[SESSION_HEADER] = state["session_id"]
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        if expect_session:
            sid = r.headers.get(SESSION_HEADER)
            if sid: state["session_id"] = sid
        return parse_sse(r.read().decode())

def call_tool(name, args):
    return post({"method": "tools/call", "params": {"name": name, "arguments": args}})

def result_text(msgs):
    for m in msgs:
        if "result" in m:
            for p in (m["result"].get("content") or []):
                if p.get("type") == "text": return p["text"]
    return None

post({"method": "initialize", "params": {"protocolVersion": "2024-11-05",
    "capabilities": {}, "clientInfo": {"name": "setup", "version": "1.0"}}}, expect_session=True)
urllib.request.urlopen(urllib.request.Request(BASE,
    data=json.dumps({"jsonrpc":"2.0","method":"notifications/initialized","params":{}}).encode(),
    headers={"Accept":"application/json, text/event-stream","Content-Type":"application/json",
             SESSION_HEADER: state["session_id"]}, method="POST"), timeout=10).read()

out = call_tool("memory_start_session", {"project": "junto", "claude_instance": "setup",
    "role_description": "initial setup"})
mem_sid = json.loads(result_text(out))["session_id"]

out = call_tool("memory_admin", {"session_id": mem_sid, "action": "create_key",
    "name": "admin-owner", "role": "owner", "projects": []})
print("OWNER KEY:", result_text(out))
EOF
```

**The key is shown once. Copy it to a password manager immediately.**

Then enable `JUNTO_REQUIRE_KEY=true` in `.env` and restart:
```bash
docker compose up -d mcp-server
```

### 10.10 Provision Team Member Keys

For each team member, create a key scoped to their project(s). Use the owner key you just created — add it to `~/.mcp.json` headers first, then:

```python
memory_admin(action="create_key", name="alice-agent", role="agent", projects=["myproject"])
```

Key is shown once. Send via secure channel (1Password share, not Slack public channels).

### 10.11 Configure Network Access

**Tailscale (recommended):**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Set `JUNTO_MCP_LAN_IP` in `.env` to the Tailscale IP (`tailscale ip -4`). Team members use the Tailscale hostname as the server URL.

**LAN only:** Use the server's LAN IP. Only works for same-network clients.

### 10.12 Set Up Backups

Backups are not enabled by default. Without them, a `docker volume rm` or corruption loses everything permanently.

```bash
crontab -e
# Add:
0 3 * * * /path/to/junto-memory/contrib/backup/backup-chroma.sh
15 3 * * * /path/to/junto-memory/contrib/backup/backup-mongo.sh
```

After 24 hours, verify: `ls -la ~/chroma-backups/ ~/mongo-backups/`

Optionally set up offsite sync via `contrib/backup/sync-to-storm.sh` (SSH to a backup host).

### 10.13 Docker Auto-Restart on Host Reboot

```bash
sudo systemctl enable docker
```

Docker's `restart: unless-stopped` handles container restarts but not host reboots.

### 10.14 Acceptance Checklist

Before declaring the install complete, verify all of these:

- [ ] `docker compose ps` shows three containers, all `Up` or `Up (healthy)`
- [ ] `curl -s http://localhost:8080/health` returns `{"status":"healthy","chroma":"healthy",...}`
- [ ] Port binds correct: `ss -tlnp` shows 8080 and 8001 and 27019 on `127.0.0.1` only (NOT `0.0.0.0`)
- [ ] Keyed `memory_start_session` from a client succeeds and returns a session_id
- [ ] Keyless `memory_start_session` returns `{"error":"...","auth_required":true}` (if REQUIRE_KEY is on)
- [ ] SSE keepalive running: `docker logs mcp-rag-arch 2>&1 | grep sse-keepalive`
- [ ] Global guidelines seeded: `docker logs mcp-rag-arch 2>&1 | grep seed_global_guidelines`
- [ ] Owner key stored in password manager

---

## 11. Admin Operations

### 11.1 Provisioning a New Team Member

```python
# From any junto session with owner-tier key:
memory_admin(action="create_key", name="juntoAlice-agent", role="agent", projects=["myproject"])
```

Key shown once — copy immediately. Send to the team member via secure channel. They then:
1. Update `~/.mcp.json` Authorization header
2. Update `~/.junto/config` JUNTO_API_KEY
3. `git -C ~/.junto pull` to get latest launcher

### 11.2 Revoking a Key

```python
memory_admin(action="revoke_key", name="juntoAlice-agent")
```

The key stops working immediately. Create a replacement if needed.

### 11.3 Deploying a Server Update

```bash
ssh user@server
cd /path/to/junto-memory
git pull
docker compose build mcp-server
docker compose up -d
```

Optionally broadcast a restart warning to active agents first:
```python
memory_admin(action="broadcast_restart_warning")
```

After restart, each agent tab needs `/mcp reconnect junto`.

### 11.4 Checking Server Health

```bash
curl -s http://localhost:8080/health
# {"status":"healthy","chroma":"healthy","active_sessions":N,...}

docker logs mcp-rag-arch 2>&1 | tail -50
```

### 11.5 Checking Active Sessions and Agent Status

```python
memory_list_agents(project="myproject")
memory_get_active_work(project="myproject")
memory_standup(project="myproject")
```

### 11.6 ChromaDB Version Caution

The server must maintain ChromaDB version consistency. If the running server has been upgraded to a newer version (e.g., 1.4.1), do NOT downgrade to the repo-pinned version (e.g., 1.2.4) — different SQLite schema, data becomes invisible.

Before updating the ChromaDB image version:
- Check running version: `docker exec mcp-chromadb ls /data/chroma.sqlite3`
- Test on a volume copy before upgrading in place
- Never change the Chroma volume mount path (`/data` inside the container — changing it to `/chroma/chroma` silently loses data on container recreation)

### 11.7 The Support/Escalation Model

**For ongoing operations:**
- Team's agent handles routine issues using the guide and `junto-check.sh`
- Novel issues → GitHub issue on the team's fork of `lvt/junto`
- Code changes or upstream bugs → `tlemmons/junto` public repo

**Initial deployment:** plan a paired setup session where an experienced operator (Tom) connects directly to the new server using a temporary owner key. This key should be created for the session and revoked immediately after.

Each deployment that runs into a new issue and resolves it should feed back into the documentation — each team that deploys makes it easier for the next.

---

## 12. Troubleshooting and Known Issues

### 12.1 "Nothing happens" when running junto-launch.sh

The script exits silently (no banner, no Claude window). **Root cause:** A bash function ending with a `[[ -n "" ]]` expression returns exit code 1; with `set -euo pipefail` this kills the script without any error message.

**Fix:** `git -C ~/.junto pull` to get the latest launcher. If you're running an old version, add `return 0` at the end of `_junto_read_claude_md` in `junto-launch.sh`.

**Diagnostic:** `bash -x ~/.junto/junto-launch.sh 2>/tmp/debug.txt; echo "Exit: $?"; tail -40 /tmp/debug.txt`

### 12.2 Plugin shows "boot-failed" or "Failed to reconnect: -32000"

The junto-inbox plugin cannot connect to the server. Check in order:
1. Is the server reachable? `curl http://<server>:8080/health`
2. Is `JUNTO_SHARED_MEMORY_URL` in your shell (`.bashrc`) pointing to a dead server? Remove it — junto-launch.sh sets it correctly from `~/.junto/config`.
3. Is `~/.claude/settings.json` mcpServers pointing to an old URL? Remove the stale junto entry from mcpServers — `~/.mcp.json` is the correct place.
4. Does `~/.junto/config` have the correct `JUNTO_API_KEY`?
5. Does the plugin have the key? The plugin reads `JUNTO_API_KEY` from the process environment. If it is not inherited (check `junto-inbox-debug.log` in your project directory for `env={}` at startup), the environment is not being passed.

### 12.3 "auth_required" when connecting

`JUNTO_REQUIRE_KEY=true` is on and the key is missing or invalid. Check:
- `~/.mcp.json` has `"Authorization": "Bearer smk_..."` in the headers section
- `~/.junto/config` has the correct `JUNTO_API_KEY`
- The key exists on the server: ask admin to run `memory_admin(action="list_keys")`

### 12.4 MongoDB "not available" on first boot

The replica set was not initialized. See Section 10.7. The health check reports `(healthy)` even on an uninitialized replica set — do not trust it alone.

### 12.5 Server won't start — "bind address already in use" or wrong IP

`JUNTO_MCP_LAN_IP` in `.env` is pointing to an IP that does not exist on this host, or the port is already in use. Set `JUNTO_MCP_LAN_IP` to the server's actual LAN/Tailscale IP.

### 12.6 Chroma data invisible after restart

The Chroma volume is mounted at the wrong path. The correct mount is `chroma-data:/data` inside the container. If the old config had `/chroma/chroma`, data was never persisted to the volume — it lived in the container layer and was lost on recreation. See the April 2026 data-loss incident in the junto-memory history.

### 12.7 Messages "arrived" but agent never saw them

Check `live_subscribers` on the send response. `live_subscribers: 0` means no active SSE subscription at send time — the message is persisted and will appear on next `memory_get_messages`. The agent was not online when it was sent.

If `live_subscribers: 1` but the agent still did not see it, check chain depth cap (Section 4.9) or agent suspension.

### 12.8 "type: http" required in ~/.mcp.json

Current Claude Code requires `"type": "http"` in the junto MCP entry. Omitting it causes a connection failure. junto-setup.sh writes this correctly; if set up manually, ensure the entry is:

```json
{
  "mcpServers": {
    "junto": {
      "type": "http",
      "url": "http://spg-junto-central:8080/mcp",
      "headers": { "Authorization": "Bearer smk_..." }
    }
  }
}
```

### 12.9 channelsEnabled getting wiped

Corporate MDM frameworks (Coralogix, Jamf, EDR) may periodically rewrite `/etc/claude-code/managed-settings.json` or `~/.claude/remote-settings.json`, stripping `channelsEnabled`. Symptoms: channel push works after a manual edit, then stops minutes to hours later.

junto-setup.sh handles this by:
1. Pointing `CLAUDE_CODE_REMOTE_SETTINGS_PATH` at `~/.claude/managed-remote-settings.json` — a file the MDM doesn't know about
2. Registering an `ensure-channel-settings.sh` hook on Stop and UserPromptSubmit that re-patches `remote-settings.json` on every interaction

If this is still happening, check that `CLAUDE_CODE_REMOTE_SETTINGS_PATH` is set in the settings.json env block.

### 12.10 After server restart, agents need /mcp reconnect

The plugin SSE connection auto-recovers after a server restart. The main MCP tool path (direct `memory_*` calls from Claude Code's tool layer) does NOT auto-recover — Claude Code's MCP transport handles transport errors but not the `-32600 Session not found` that follows from a stale `mcp-session-id` after server restart.

Recovery: `/mcp reconnect junto` in each Claude Code tab. If you have many tabs open, do this before typing `go`.

---

## 13. Environment Variables Reference

These variables are set by junto-launch.sh from `~/.junto/config` and exported to the plugin subprocess:

| Variable | Set by | Purpose |
|---|---|---|
| `JUNTO_AGENT` | launcher (from CLAUDE.md/.agent-name) | Agent identity name |
| `JUNTO_PROJECT` | launcher (from CLAUDE.md) | Project namespace |
| `JUNTO_COMPONENT` | launcher (from CLAUDE.md, optional) | Sub-group within project |
| `JUNTO_ROLE` | `~/.junto/config` | One-line role description for the system prompt |
| `JUNTO_MEMORY_URL` | `~/.junto/config` | MCP server URL (e.g., `http://spg-junto-central:8080/mcp`) |
| `JUNTO_SHARED_MEMORY_URL` | launcher (bridges JUNTO_MEMORY_URL) | Same as JUNTO_MEMORY_URL; plugin reads this name |
| `JUNTO_API_KEY` | `~/.junto/config` | Bearer auth key for the server |
| `JUNTO_CHANNEL_DELAY` | `~/.junto/config` | Delay (ms) before first push delivery. Set to 15000 if messages arrive in mailbox but not as live push. |
| `CLAUDE_CODE_REMOTE_SETTINGS_PATH` | launcher | Points CC's org-policy cache at a stable file MDM won't overwrite |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | launcher | Opts into Sonnet 1M context window (`claude-sonnet-4-6[1m]`) |

Server-side variables in `.env` (junto-memory):

| Variable | Purpose |
|---|---|
| `MCP_AUTH_ENABLED` | Enable/disable API key authentication |
| `JUNTO_REQUIRE_KEY` | Reject all keyless sessions when true |
| `JUNTO_TUNNEL_REQUIRES_KEY` | Reject keyless sessions from tunnel when true |
| `JUNTO_MCP_LAN_IP` | LAN/Tailscale IP the server binds MCP port to |
| `ORIGIN_SERVER_ID` | Unique ID for this deployment instance |
| `MONGO_USER` / `MONGO_PASSWORD` / `MONGO_DB` | MongoDB credentials |

---

## 14. File and Directory Reference

### Developer Machine

| Path | Purpose |
|---|---|
| `~/.junto/` | Launcher repo (cloned from tlemmons/junto) |
| `~/.junto/config` | Per-machine config: API key, server URL, role. `chmod 600`. |
| `~/.junto/junto-launch.sh` | Main launcher (bash) |
| `~/.junto/junto-launch.ps1` | Main launcher (PowerShell) |
| `~/.junto/junto-setup.sh` | First-time setup wizard (bash) |
| `~/.junto/junto-setup.ps1` | First-time setup wizard (PowerShell) |
| `~/.junto/junto-init.sh` | Per-workspace setup wizard (bash) |
| `~/.junto/junto-init.ps1` | Per-workspace setup wizard (PowerShell) |
| `~/.junto/junto-check.sh` | Health check + auto-repair (bash only) |
| `~/.junto/junto-update.sh` | Pull launcher updates |
| `~/.junto/templates/junto-system-prompt.md.tmpl` | System prompt template |
| `~/.junto/templates/render.sh` | Template renderer (bash) |
| `~/.junto/templates/render.ps1` | Template renderer (PowerShell) |
| `~/.junto/templates/overlays/first-run.md` | First-run onboarding overlay |
| `~/.mcp.json` | Claude Code user-level MCP server config |
| `~/.claude/settings.json` | Claude Code user settings (hooks, plugins, env) |
| `~/.claude/managed-remote-settings.json` | Stable channel settings file (MDM-resilient) |
| `~/.claude/hooks/ensure-channel-settings.sh` | Hook that patches remote-settings.json |
| `<project>/.claude/settings.local.json` | Per-project: MCP permissions pre-approved |
| `<project>/CLAUDE.md` | Agent identity + project orientation |
| `<project>/.agent-name` | Written by CC on first startup; preferred identity source |

### Server

| Path | Purpose |
|---|---|
| `/path/to/junto-memory/` | Server repo root |
| `/path/to/junto-memory/.env` | Server configuration (secrets — chmod 600) |
| `/path/to/junto-memory/docker-compose.yml` | Container orchestration |
| `/path/to/junto-memory/secrets/mongo-keyfile` | MongoDB replica set keyfile (chmod 400, UID 999) |
| `/path/to/junto-memory/contrib/backup/` | Backup scripts |
| `~/chroma-backups/` | Local Chroma archive rotation |
| `~/mongo-backups/` | Local MongoDB archive rotation |

---

## 15. Repository Reference

| Repo | URL | Description |
|---|---|---|
| `tlemmons/junto` | https://github.com/tlemmons/junto | Launcher, setup, templates, this documentation |
| `tlemmons/junto-memory` | https://github.com/tlemmons/junto-memory | MCP server, Docker, Python backend |
| `tlemmons/junto-inbox` | https://github.com/tlemmons/junto-inbox | Claude Code channel plugin (SSE push) |
| `tlemmons/junto-control` | https://github.com/tlemmons/junto-control | Optional web dashboard |

For LVT teams: fork `tlemmons/junto` and `tlemmons/junto-memory` to your GitHub org. Set up a GitHub Actions upstream-sync workflow to get a PR when the public repo advances. Review the diff before merging — this is how security tooling maintains visibility over what code is running.

---

*Document maintained by juntoTom@junto. Report issues or corrections as GitHub issues on `tlemmons/junto` or via junto message to `juntoTom@junto`.*
