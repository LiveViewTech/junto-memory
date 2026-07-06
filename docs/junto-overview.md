# Junto — What It Is and What It Does

A reference for AI-focused engineers and architects evaluating Junto. This is a *what-it-does* document, not a how-to-use guide. Features are described in narrative terms — when X happens, Y follows, because Z — rather than through API snippets.

---

## 1. What Junto is

Junto is a **multi-agent coordination system for AI code assistants**, built natively on the Model Context Protocol (MCP). It addresses the problem that arises when more than one Claude instance — or any MCP-compatible agent — needs to share knowledge, hand off work, exchange messages, and operate safely under autonomous reply loops, all without a single human in the loop on every turn.

It is not a model. It is not an orchestrator that decides which agent runs when. It is the substrate underneath such things: a persistent, queryable, multi-tenant knowledge base plus a message bus plus a coordination kernel, all exposed as MCP tools and resources that any compliant agent can consume.

The whole system is MIT-licensed and operator-owned. There is no SaaS plane. An adopter runs their own Junto instance against their own MongoDB and ChromaDB; that instance becomes the shared brain for every Claude session that connects to it.

---

## 2. The problem it addresses

Modern AI coding agents operate under three structural pressures that compound when multiple agents are involved:

- **Context is ephemeral.** A Claude session ends, its working memory is gone. The next session starts blank. State has to live somewhere outside the model.
- **Knowledge is siloed by default.** Two agents working in the same codebase, or on related codebases, rediscover the same gotchas independently. The cost of duplicated debugging compounds rapidly.
- **Autonomous reply loops are dangerous.** Agent A messages Agent B; B's autopilot reply messages A; A's autopilot reply messages B. Without explicit caps, infinite loops happen, budgets evaporate, and destructive actions get amplified.

Existing solutions tend to pick one of these problems. Personal-memory products (Mem0, Letta) optimize for single-agent recall. Orchestration frameworks (LangGraph, AutoGen, Microsoft Agent Framework) optimize for explicit workflow modeling. Junto's bet is that the same persistent substrate can address all three — knowledge persistence, cross-agent sharing, and safety gates on autonomy — and that the right shape for that substrate is an MCP server that exposes its capabilities to whichever agent host runs against it.

---

## 3. Architecture

Junto is four components, each MIT-licensed, each able to be adopted independently:

| Component | Role | Form factor |
|-----------|------|-------------|
| **junto-memory** | The MCP server itself. Persistent knowledge base, message bus, coordination kernel. | Python service backed by MongoDB + ChromaDB. |
| **junto-inbox** | Channel-plugin that gives a Claude Code (or compatible) host live push notifications for incoming messages, plus client-side autopilot controls. | TypeScript / Bun runtime. |
| **junto-control** | Web UI for the human in the loop — read messages, reply, approve flagged sends, browse the shared knowledge base. | Python + FastAPI + HTMX. |
| **junto-stack** | A compose-recipe repository that bootstraps a full Junto install from scratch on an adopter's machine. | docker-compose + bootstrap scripts. |

A minimal adoption is just **junto-memory** — any MCP-compatible agent can talk to it directly. **junto-inbox** is the recommended add-on for hosts that need push-style message delivery rather than polled reads. **junto-control** is optional; it exists for the case where a human operator wants to participate in the message stream from a browser. **junto-stack** is the on-ramp for new adopters who don't want to assemble the pieces by hand.

The runtime topology, when all four are deployed, looks like this:

```
   Agent A ─┐                 ┌── Agent B
            │                 │
            ▼                 ▼
       ┌────────────────────────┐        ┌────────────┐
       │   junto-memory (MCP)   │◀──────▶│   Human    │
       │                        │  HTTP  │ via junto- │
       │   • Mongo (state)      │        │  control   │
       │   • Chroma (semantic)  │        └────────────┘
       └────────────────────────┘
            ▲                 ▲
            │                 │
       junto-inbox       junto-inbox
       (live push)       (live push)
```

Agents call MCP tools against junto-memory directly. junto-inbox runs inside each agent's host as a plugin that subscribes to that agent's inbox resource and surfaces incoming messages to the host without the agent having to poll. junto-control is just another MCP client, scoped to a human user, with a browser-based UI bolted on.

---

## 4. Core features

The feature surface is large; this section organizes it by capability area, with a scenario for each that shows the feature in action.

### 4.1 Cross-session memory

Every agent has access to a project-scoped semantic index and a cross-project shared index. Anything written via `memory_store`, `memory_record_learning`, or `memory_register_function` becomes queryable by every future session — within that project by default, and across projects via explicit cross-project search.

*Scenario.* An agent debugging an obscure production bug spends ninety minutes tracing the root cause to a non-obvious interaction between a config flag and a database migration. It records a learning capturing the root cause, the symptom, and the workaround. Three weeks later, in an unrelated session, a different agent on a different codebase hits the same symptom. It runs a query against the shared index for the symptom string, gets the original learning back ranked by semantic similarity, and applies the workaround in two minutes instead of ninety.

Memory entries have status (active, superseded, archived) and tags. An agent that discovers an old entry is now wrong can supersede it explicitly, leaving an audit trail rather than silently overwriting. Old entries that haven't been touched in months carry staleness warnings on retrieval so callers know to verify before acting.

### 4.2 Function registry

Distinct from generic memory, Junto exposes a typed registry for functions. Each entry knows the function's name, file location, purpose in a sentence, known gotchas, and (optionally) a snippet of the actual code. The registry is queryable by purpose ("what handles user authentication?") or by name.

*Scenario.* A new agent picks up a task in a 50,000-line codebase. Before writing any new code, it queries the function registry for what already exists. The query returns three relevant functions: one that already does what the agent was about to write, one adjacent helper, and one that the prior author flagged with a non-obvious gotcha ("this returns None on timeout, not an exception"). The agent calls the existing function instead of writing a duplicate, and the gotcha makes the call sites correct on the first try.

A separate enrichment daemon (using Claude Haiku) continuously fills in purposes and gotchas for functions whose authors registered only the location. The result is a self-maintaining map of the codebase that survives across sessions and across personnel.

### 4.3 Specs (versioned shared documents)

Specs are first-class, long-lived shared documents with explicit owners and version history. They are the right shape for things like interface contracts between two agents, architecture overviews, design decisions, state-of-the-world snapshots, and patterns intended to be reused across projects.

*Scenario.* Two agents need to agree on the shape of a message they will exchange. Rather than one writing the code and hoping the other can read its source, the producer writes an interface spec — with explicit fields, types, semantics, and version — and the consumer references that spec when implementing its side. Six months later, when the producer wants to extend the message, it bumps the spec version and notifies the consumer. The contract is the coordination point, not the code.

Each spec carries an `owner` identifier (the agent or human responsible) and a `spec_type` (architecture, interface, requirement, design, decision, research, agent_state, pattern). Updates are version-bumped; full history is recoverable. Specs with sensitive ownership semantics (a peer-reviewed research note, say) can be filed with `owner=human` so that only a human or a designated decider can promote, dispose, or supersede them.

### 4.4 Inter-agent messaging

Junto's message bus is fully typed: messages have a sender (project + agent identity), a recipient (same), a category (info, task, question, blocker, contract, review), an optional reply-to chain, and full provenance metadata for safety gates (chain depth, sent-by-human flag, human-interacted flag).

*Scenario.* An agent on Team A discovers what looks like a contract violation in an API exposed by Team B's agent. Rather than waiting for a human to spot the report and forward it, Team A's agent sends a category=review message to Team B's agent directly. Team B's agent receives the push notification within seconds via its inbox subscription, reads the message inline, fixes the bug, and replies — closing the loop without any human round-trip. The full thread is preserved as durable artifact, complete with the chain of replies, for later audit.

Messages are never silently dropped. If the recipient is offline or over a safety threshold, the message persists; only its live push notification is suppressed. The recipient sees it on next reconnect.

### 4.5 Autopilot (autonomous reply control)

Autopilot is the mechanism by which an agent decides whether to auto-process an incoming message without human prompting. Each agent has a per-agent autopilot config: a `chain_depth` cap (how many auto-reply hops are allowed before the chain must stop and surface to a human), an `hourly_budget` (how many autonomous processings are allowed per hour), and a `destructive_gate` (whether to require a human approval on messages matching dangerous keyword patterns).

When an inbound message arrives, the agent's client (typically the channel plugin) calls into Junto's budget-check tool. Junto runs a gate stack: is autopilot enabled, is the destructive gate tripped, is this message human-rooted (and therefore exempt), is the budget exhausted, is the chain too deep? The first gate that fails returns a clean denial reason. If the budget threshold is exceeded, autopilot auto-disables itself and posts a system notification — preventing runaway loops at the cost of a brief halt.

*Scenario.* Agent A is autopilot-enabled with a budget of 30/hour and a depth cap of 12. A spike in user activity sends agent A 50 inbound messages over fifteen minutes. The first 30 process automatically. The 31st trips the budget gate. Autopilot auto-disables; agent A posts a system notification to the operator inbox; further messages persist but don't auto-process until the operator either raises the budget or re-enables autopilot. No silent loss. No silent loop.

The trust model deserves note: the "human-interacted" flag on messages is **sender-asserted**, meaning the sender's host claims the message is part of a fresh human turn rather than a recursive autopilot reply. The server records this assertion and uses it for budget and chain-depth bypass, but doesn't compute it server-side from chain ancestry. This is acceptable at current scale (all agents operator-controlled) and an explicit upgrade path exists (server-derived chain ancestry) for the day Junto serves third-party agents.

### 4.6 Human-in-the-loop gates

Two gates surface decisions to humans rather than letting autopilot proceed:

- **Destructive-keyword gate.** Outbound messages matching configured patterns (`DELETE`, `DROP TABLE`, `rm -rf`, `git push --force`, and similar) are flagged `require_human=True`. The recipient's autopilot refuses to process; the message surfaces to a human via the operator inbox or junto-control UI.
- **Chain-depth cap.** Once a chain exceeds the configured depth cap (12 by default, per-project), the live push notification is suppressed — silently; the message still persists and stays pullable, so nothing is lost while the runaway chain stops interrupting. The cap is unconditional: it does not consult human presence. The separate five-minute human-recency window instead governs read-side *release* of already-suppressed messages to an agent in an active human session — it is not a send-time cap waiver.

*Scenario.* An agent is told to "clean up the staging database." It autopilots a reply to a peer that includes the literal text `DROP TABLE staging_users;`. Junto's gate flags the message `require_human=True`. The peer's autopilot refuses to process. The operator gets a notification in junto-control. The operator can then approve, reject, or rewrite the action — the chain doesn't get to "yes, run DROP TABLE" without an explicit human sign-off.

### 4.7 Project, agent, and identity model

Every Claude instance that connects to Junto identifies itself with a (project, agent_instance) pair — for example, `(nimbus, coordinator)` or `(emailtriage, classifier)`. The pair is the unit of identity for messages, locks, state specs, and most reads.

The system supports four auth tiers — readonly, agent, user, admin (plus owner) — with a per-tool permission matrix. Most agents run as `agent` tier, scoped to a single project. A human operator gets a `user`-tier key scoped to the projects they participate in. Admin and owner tiers are reserved for operator-level concerns (creating keys, renaming agents).

Identity is **renameable**. An agent that needs to change its name — or a project that needs to be re-scoped — can be renamed via an admin tool that walks every collection (messages, learnings, specs, function registry, locks, audit log) and rewrites references. Old-name reconnects auto-redirect via a rename-alias table with a configurable TTL, so an in-flight session that hasn't picked up the new identifier keeps working through the transition.

### 4.8 Coordination kernel: locks, active work, presence

Beyond messaging and memory, Junto exposes lightweight coordination primitives:

- **File locks.** An agent can declare it is editing a set of files. Other agents who query active locks see the conflict before they start work, rather than discovering it at merge time.
- **Active-work tracking.** Each agent can post what it is currently doing. A peer asking "is anyone working on the deployment scripts right now?" gets a live answer.
- **Heartbeats and signals.** Agents post liveness pings; signals (such as "I just finished the database migration; downstream consumers can resume") fan out as discoverable events.

*Scenario.* Three agents share a codebase. Agent A is mid-refactor on the auth module and posts file locks on the affected files. Agent B picks up a new task that, on inspection, touches one of those files. B's resolver sees A's active lock, sends A a coordination message, and switches to a different task in the meantime. The conflict that would have been a painful three-way merge is resolved before any second commit lands.

### 4.9 Backlog

A per-project, queryable backlog with priority, status, assignment, and tags. Items can be filed by any agent or by a human via junto-control. Filters by assignee + priority let an agent fetch "my high-priority open items" as the first step of any new session.

*Scenario.* On session start, an agent runs three queries: pending messages, its own state spec, and high-priority backlog items assigned to it. The three together reconstruct enough context to resume coherently after an arbitrary gap — including across machine moves, version upgrades, or weeks-long pauses.

### 4.10 Push delivery (junto-inbox)

The MCP protocol itself supports a "resources" abstraction with optional subscriptions. Junto exposes a per-agent inbox as an MCP resource at `inbox://<project>/<agent>`. junto-inbox, the channel plugin, subscribes to that resource at session start; when a new message arrives, Junto fans out a `resources/updated` notification, the plugin fetches the new page, and surfaces it to the host (typically as an inline notification in the Claude Code UI) without the agent having to poll.

The fallback when junto-inbox isn't present is straightforward polling: any agent can call `memory_get_messages` at any time and get the same data, just without the push latency benefit.

### 4.11 Human web UI (junto-control)

junto-control is a thin web application — login, project picker, unified inbox, compose-with-destructive-preview, backlog viewer, spec viewer — that lets a human operator participate in the Junto message stream from a browser. It exists for the case where the operator isn't actively in a Claude session but wants to be reachable by their agents, or wants to approve a gated message, or wants to browse what their agents have been doing.

Architecturally, junto-control is just another MCP client. It enforces user-tier auth, subscribes to a "self-inbox" so that replies addressed to the operator always reach the UI regardless of which project the operator is currently viewing, and renders everything via server-side templates plus HTMX for live updates.

---

## 5. Design philosophy and tradeoffs

A few design choices are worth surfacing explicitly because they shape what Junto is and isn't.

**MCP-native.** Junto exposes its capabilities as MCP tools and resources, not as a custom REST API. This is a deliberate bet that MCP is the right cross-vendor interface for agent runtimes, and that adopters benefit from being able to use Junto from any MCP-compatible host without writing an adapter. The cost is that some capabilities — bulk admin operations, for instance — fit MCP's request/response shape awkwardly, and the surface (around 47 tools today) is large enough that careless tool descriptions waste context budget on every session start.

**Single-server, operator-owned.** There is no Junto cloud. Each operator runs their own instance. This is also deliberate: shared knowledge across operators implies multi-tenant trust boundaries that the current design doesn't enforce. Two organizations should not share a Junto instance; each runs their own.

**Sender-asserted trust for human-interaction flags.** The "is this part of a fresh human turn" signal is the sender's claim, not a server derivation. At current scale (all agents operator-controlled) this is the right cost/value tradeoff — the server still records and audits — but at the scale of third-party agents calling in, server-derived chain ancestry is the documented upgrade path.

**Append-mostly storage.** Messages, audit events, and autopilot events are append-only. State that genuinely mutates (autopilot config, agent presence, spec content) lives in a separate mutable store. Semantic-search artifacts (learnings, function refs, specs) live in a vector store. The split mirrors the access pattern: timeline reads vs point reads vs semantic reads each get the right index.

**Knowledge persistence is not the same as automated learning.** Junto persists what agents record but doesn't synthesize new knowledge from observations. There is no implicit observer loop scoring confidence on every claim. Agents record explicitly; the system surfaces those records on query. This is conservative — it deliberately rejects the "agent self-modifies its own behavior model" pattern in favor of human-reviewable artifacts.

**Hand-written state specs, not auto-generated.** Each long-lived agent maintains a state spec by hand at park time, summarizing current task, status, next steps, and key context. Auto-generation is technically feasible (a per-agent event log would make it trivial) but the hand-written form forces the agent to *think* about what matters at handoff — and a human can read it to see what the agent thinks it's doing. The auto-gen idea is on the roadmap as a complement, not a replacement.

---

## 6. Planned enhancements

The roadmap below is grouped by theme. Items vary from days of work to multi-week design efforts.

### 6.1 Tool surface optimization

Junto's 47-tool surface predates MCP's deferred-loading conventions. Modern Claude clients now defer full tool schemas until the model searches for them, which means tool *names*, short descriptions, and the server `instructions` field carry the entire orientation budget. A tool-surface audit is queued: rewrite descriptions for keyword coverage, author a strong server `instructions` field, identify the always-loaded core (probably session, messaging, memory, autopilot — five to eight tools), and group remaining tools into discoverable clusters by naming convention. The audit is gated on an empirical multi-agent research study to set baseline performance metrics.

Related: some current tools (`memory_guidelines`, `memory_checklist`) are functionally documentation rather than mutations, and should be reframed as MCP **prompts** or **resources** rather than tools — removing them from the tool catalog entirely.

### 6.2 Session-delta endpoint

Today, an agent resuming from a park calls 5-7 separate endpoints (messages, learnings, specs, locks, work, backlog, signals) and synthesizes a delta client-side. A proposed `memory_session_delta(since=...)` would collapse those into a single server-side merged query, returning everything that changed in a (project, agent) scope since the agent last parked. Net effect: faster resumes, smaller hand-written state specs (because the structured delta is server-derived), less drift between what the agent thinks happened and what actually happened.

### 6.3 Per-agent event log

A unified append-only event log, one row per state-mutating write, per agent. Functions registered, learnings recorded, messages sent, specs defined — each appends an event. The event log is a denormalized index; source rows stay in their existing collections. Benefits: trivially efficient session-delta queries, trivial state-spec auto-generation, clean audit trail. This is treated as a possible foundation for the session-delta endpoint above; the open question is whether to build session-delta as a federated query first (faster value, throwaway work) or wait for the event log (slower but cleaner).

### 6.4 Confidence and evidence on learnings

Adapted from external research: extend the learning schema with `confidence` (0–1), `evidence[]` (list of references — message IDs, session IDs, other learnings — that support the claim), `domain` tag, and `scope` (project-local vs global). Add reinforce/contradict tools that adjust confidence and append to evidence. Optionally, add a clustering pass that proposes promotion of high-confidence project-scoped learnings to global when they appear consistently across multiple projects. The hard design questions are confidence math (decay rates, reinforcement weights, auto-apply thresholds) and governance of auto-applied learnings; both need empirical work before committing.

### 6.5 Temporal validity windows

The staleness problem — old learnings can outrank new ones on relevance — is currently handled by per-result age warnings and the agent's responsibility to verify. A richer model under consideration borrows from temporal knowledge graphs: every fact has a validity window, supersession invalidates the window rather than deleting the row, and queries can ask "what's true now?" or "what was true at time T?" This is a structurally bigger change than per-result warnings, and is research-stage.

### 6.6 Hybrid retrieval

Current `memory_query` is similarity-only. Modern retrieval systems augment this with reciprocal-rank-fusion over full-text plus vector, and (optionally) a cross-encoder reranking pass. The expected lift is meaningful enough that this is a credible roadmap item once the surface above stabilizes.

### 6.7 Runtime profile env vars

A small ergonomic enhancement: expose tunable knobs via environment variables — `JUNTO_TOOL_PROFILE`, `JUNTO_AUTOPILOT_PROFILE`, `JUNTO_DISABLED_TOOLS` — so adopters can shape Junto's behavior at startup without editing per-project configs. Low-risk, incremental work.

### 6.8 Health and diagnostics tool

A `memory_health` / `memory_ping` tool that returns a transparent OK/version/uptime payload for connection diagnosis. Today, when a client gets an opaque transport error, there is no clean way to distinguish "Junto down" from "auth misconfigured" from "transient network." A diagnostic tool closes that gap. Small effort, high value for adopter friction.

### 6.9 Function-registry enhancements

Three related items: cross-team impact alerts (when a function with cross-project consumers changes, notify them), stale-entry detection (functions whose file no longer contains the named identifier), and caller-tracking ("who calls this function?"). Together these promote the registry from a passive lookup index toward a refactoring-safety net.

### 6.10 Auto-reconnect for live agents

Today, a Junto server restart kills active sessions and inbox subscriptions; clients must re-handshake. The channel plugin handles this gracefully; raw MCP HTTP clients do not. A transparent reconnect layer — either client-side in junto-inbox or via a session-resume protocol extension — would close the "silent miss" window during deploys.

### 6.11 Auth model maturation

Junto's auth is currently in "soft auth" — keys exist and are validated when present, but unauthenticated sessions fall through to agent-tier. The maturation path is to flip auth to required, complete the user-tier dogfood, and document the role/permission matrix at the level of a public adopter guide. This unlocks the third-party-agent scenario that gates several of the deeper roadmap items above.

### 6.12 Cross-server federation

Explicitly not on the near-term roadmap, but worth noting as a known non-feature: multiple Junto instances do not sync with each other. Each operator's instance is independent. Federation across operators implies a trust and identity layer that doesn't exist today; if it ever lands, it lands as a separate component, not a modification to the core server.

---

## 7. Status and adoption

Junto is in active production use against a small number of multi-agent fleets. The core surface (memory, messaging, autopilot, specs) is stable. The auth model is in dogfood. junto-control and junto-inbox are at v0.x releases — usable, MIT, public, but not yet community-vetted at scale.

Adopters interested in trying Junto can start with the **junto-stack** bootstrap repository, which provides a docker-compose setup for the full stack plus example MCP client configs. Single-component adoption (just junto-memory, talking to it from a custom client) is also supported and tested.

The cleanest mental model for adopters: Junto is the **substrate**, not the agent runtime. Bring your own agents — Claude Code, Claude Desktop, custom MCP clients, anything compatible — and let Junto handle the parts that no individual agent runtime can solve alone: persistence across sessions, coordination across agents, and safety gates on autonomous behavior.
