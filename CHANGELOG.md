# Changelog

All notable changes to `junto-memory` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the server advertises its running
version via `memory_health` (`server_version`).

## [Unreleased]

## [1.41.1] - 2026-10-28

### Security
- **Server-wide HTTP views need an unscoped key under `JUNTO_REQUIRE_KEY`** (`interface:project-scope-enforcement-v0` R7). `/dashboard/api/{sessions,agents,messages,backlog}` and `/hook/compact-log/stats` had NO auth at all, even under `JUNTO_REQUIRE_KEY`. Any peer on the network could read every project's sessions, agents, backlog and the last 30 messages (200-char previews) with no key. v1.34 has the same gap.
  - These routes and every `/admin/api/*` read route now share one gate (`admin_ui._lan_only`): tunnel origin → 403. Under `JUNTO_REQUIRE_KEY`: no key or an invalid key → 401, a **project-scoped key → 403** (these views span every project), any unscoped key → allowed.
  - `POST /hook/compact-log` (a caller-owned write) takes any valid key under `JUNTO_REQUIRE_KEY`.
  - The `/admin` HTML shell now checks the tunnel origin only (`_tunnel_only`). A browser navigation cannot send a Bearer header, so 1.40's key gate made the admin page unloadable on keyed servers. The page sends the key on its API calls.
  - `/dashboard` gains a key box (sessionStorage `junto_admin_key`, shared with `/admin`) and sends it as a Bearer header. A 401/403 says the server needs an unscoped key.
- With `JUNTO_REQUIRE_KEY` off (e.g. home LAN soft-auth), nothing changes.

## [1.41.0] - 2026-10-28

### Security
- **Tenant isolation for project-scoped API keys** (`interface:project-scope-enforcement-v0`, backlog_8cd9708f4ad2). A key scoped to [alpha] could read and write project beta by passing `project="beta"` on most tools. Project checks were opt-in per tool and only `memory_store` / `memory_record_learning` / skills had one. Found in the 2026-10-28 work-upgrade rehearsal; v1.34 has it too.
  - New MCP-layer wrapper (`project_scope.py`, between session_alias and idle_park in `app.py`): any `project` / `projects` argument outside the key's projects is rejected with `ERROR: Tenant isolation: …` (`isError=True`).
  - Exempt on purpose: `memory_send_message` (cross-project messaging), `memory_start_session`, `memory_get_messages`, `memory_admin`.
  - "All projects" listings filter to the key's projects plus shared: list_backlog, list_specs, search_global, get_active_work (sessions, work items, blocked agents, signals), list_agents, get_agent_status, list_alerts, get_emission_stats, get_locks, archive/restore_by_tag, enrichment queue. `memory_list_projects` (session-less) scopes by the Bearer key; under `JUNTO_REQUIRE_KEY` a keyless caller sees shared only.
  - Doc-id lookups (get_by_id, backlog update/complete/batch, enrich_function, skills by id) treat another project's doc as not found. change_status's locate-on-miss no longer reveals it.
  - New helpers in `auth.py`: `scoped_projects`, `project_allowed`, `collection_allowed`, `tenant_isolation_error`.
- Who is affected: only sessions whose key carries a `projects` list (`MCP_AUTH_ENABLED=true`). Unscoped keys, keyless soft-auth sessions and auth-disabled servers behave exactly as before.

### Docs
- `.env.example` now documents every env var the server reads (28 were missing), grouped by area, with code defaults: auth (`JUNTO_REQUIRE_KEY`, `JUNTO_TUNNEL_REQUIRES_KEY`), deployment, sessions/SSE, messaging, identity lifecycle, privacy (`JUNTO_LOG_QUERY_TEXT`), optional local inference (rerank, claim gate, facets, chunk index), librarian webhook, sync engine, diagnostics.
- README:
  - new sections: **Action Lapse**, **Optional Local Inference** (reranker / write-time contradiction check / facets: what each needs, how to turn it on, fail-open), **Ambient Recall (`/recall`)**, **Auth modes** (soft-auth vs `JUNTO_REQUIRE_KEY`);
  - the idle-park section now covers policy text, per-agent `timezone=`, the DAY/NIGHT line and context self-report;
  - the Optional Variables table gains the new vars;
  - `user` role added to the role table;
  - stale MONGO_PORT and "DB ports exposed" notes fixed;
  - roadmap updated.
- `docs/junto-complete-reference.md`: session persistence (sessions no longer "in-memory only"), idle nudge + time zone, REQUIRE_KEY side effects, rerank relevance note, server env pointers.

## [1.40.0] - 2026-10-27

### Upgrading from an older build (read first — e.g. the June `0e2a7ce` / 1.34 work box)
- **Chroma 1.2.4 → 1.5.9 is a ONE-WAY on-disk migration** (0121f14; `chromadb==1.5.9`
  client pin in requirements.txt). Before the first boot:
  - stop the stack;
  - cold-copy the `chroma-persistent` and Mongo volumes;
  - rehearse on a copy.
  There is no downgrade without the old files.
- **Global guidelines reseed on boot.** The code set is now the 13 `trim_*` rules plus
  `session_length_discipline`. The seed never deletes, so the old globals stay active
  next to the new ones. Retire them with `scripts/retire_orphan_globals.py`. It defaults to
  a dry run and takes `--execute`, `--keep a,b` and `--rollback`. It works before or after
  boot and archives what it retires. Don't use the older `guideline_trim_flip.py` for
  upgrades: it inserts July-era rule text. `memory_guidelines` refuses global-scope edits
  (321808d), and guideline identity is now `(name, scope)` (91eae5d, unique index).
- **Removed tools:** the six `memory_autopilot_*` tools. The `agent_autopilot` /
  `autopilot_events` collections are dropped at boot (8591fd2, design:autopilot-removal-v0).
- **`memory_db` writes now COMMIT** (5995cee). They were silently rolled back before.
  Audit `read_only:false` handles.
- **Defaults that are ON:**
  - idle→park nudge after 50 min (`idle_park_minutes`);
  - action-message lapse after 30 days (`action_lapse_days`). The first sweep runs
    ~5 min after boot and sends one digest per agent;
  - session persistence across restarts (`JUNTO_SESSION_PERSIST`);
  - 6 h idle expiry for sessions without a live inbox subscription.
- **`/health` returns 503 without a writable Mongo primary** (d6508d7). Watchdogs act on it.
- **Stricter calls:**
  - sending to an unknown recipient errors (d1061ee);
  - case-variant spec names are refused (ff3795f);
  - `memory_guidelines set` without a scope on an ambiguous name is refused.
- **Compose:**
  - Mongo `ulimits nofile 64000`;
  - `extra_hosts host.docker.internal:host-gateway`;
  - `JUNTO_MCP_LAN_IP` still defaults to the home IP, so re-apply local binds.
- **Optional, off unless configured:** claim gate / facets / chunk index / rerank / delegation
  runner (need Ollama or the reranker service). New env vars, all optional:
  `JUNTO_CLAIM_GATE_*`, `JUNTO_FACETS_ENABLED`, `JUNTO_EMBED_URL`,
  `JUNTO_CHUNK_SWEEP_SECONDS`, `JUNTO_RERANK_URL`, `JUNTO_VAULT_KEY`,
  `JUNTO_PENDING_REAP_*`, `JUNTO_SESSION_IDLE_HOURS`.
- **Home-only pieces, not for other deployments:** `infra/sage/*`, `contrib/backup/sync-to-storm.sh`,
  the default IPs in `contrib/local-inference/li.py` and `contrib/reranker`, and the
  `coordinator` directive banner.

### Security / hardening (this release)
- **Admin dashboard READ routes require a valid key when `JUNTO_REQUIRE_KEY=true`.** On a
  network without a tunnel header (Tailscale-only), origin trust let any peer read
  project, agent and settings data with no key. Writes were already owner-only.
- **Chunk-index sweep is gated on the feature:** each 15-min pass is skipped unless some
  `query_config` scope has `chunk_index` on. Before, it polled the embedder regardless,
  and embedded every collection if anything answered.
- **Code-seeded shared `parking` skill**, insert-if-absent (never overwrites a local edit).
  The global rules point every agent at `memory_get_skill("parking")`, which didn't exist
  on a fresh or isolated server.

### Added (this release, for multi-site / API-billed deployments)
- **Per-agent time zone for the idle nudge.**
  - Set it with `memory_start_session(timezone="America/Denver")`, persisted on agent_directory. An invalid zone is reported in `timezone_warning`.
  - Fallbacks: push_control `idle_park_timezone` (project, then server default), then America/Chicago.
  - Every nudge now ends with `Your local time: HH:MM (zone) — DAY|NIGHT`, so idle policies no longer need a hardcoded `TZ=... date`.
- **Server-default idle policy:** push_control `idle_park_policy`. Its text goes into the nudge for any project without its own `park_on_idle_nudge` guideline, so a server with many projects sets it once.
- **Self-report context on keyed servers:** with `JUNTO_REQUIRE_KEY` on, a non-owner/admin `memory_report_context` call may only report its own instance; others land in `skipped`. This is the server half of interface:context-self-report-v0, where the junto-inbox plugin reports its own agent's context on any OS.
- **`JUNTO_LOG_QUERY_TEXT=false`** stops storing query/prompt text in `query_events` / `recall_events`. The ids/scores rows are kept for metrics. Default true.

### Previously undocumented (shipped between 1.34 and this release)
- Retrieval:
  - chunk index on arctic-embed2 (d777671, cf703c6, 932ae57, 715c07e, 37e3c05);
  - lexical lane for exact-term hits (419bdf1);
  - cross-encoder rerank (78b1951; default `rerank_min` 0.05, 32be3ee);
  - query/recall event logging for evals (9153dc2; `query_events` / `recall_events`
    keep query text 90 days).
- Recall: the `/recall` endpoint and its gates (9c530c8, 801e273, 1802eea, d901c27), plus the
  prompt hook (fbb8b79). Facets on learnings (b2ea77f…). Write-time contradiction claim
  gate (94c759a, 73a4f29, b42f496).
- Sessions:
  - session persistence (6e69e7e);
  - `guidelines_held` on start_session (010f2dd);
  - one roster line per agent, and plugin boilerplate no longer titles work items
    (a77a086, 9c24e0d);
  - no idle-expiry while the agent's own plugin is subscribed (8e7ffee);
  - orphaned-session delivery fixes (1b0cba8);
  - `junto_session` alias for clients that strip `session_id` (251c8f7).
- Identity:
  - lifecycle phases A/B/C (7bbaea2, 862d981, 3956949);
  - escheat of stale app identities (4afe4f6);
  - role_description write-through (63af082);
  - owner/admin role bypass for roster actions (6c22f86).
- Specs and skills:
  - `transfer_spec` (449a12a, 7a82612);
  - skills registry and `/export-skills` (a417e5a, 0ce7e8a, 98b175b, 6ee07fc);
  - directives (eea6a3c);
  - state-spec write guard and standing-rule detection (43c9733, 8da96f9).
- Messaging:
  - push-all-info (4cea043);
  - alias fail-loud (d1061ee);
  - blocker lane count (dac0e26).
- Fixes:
  - backlog same-project move no longer deletes the item (316b351);
  - Mongo credentials are URL-escaped (304911c);
  - `archive_by_tag` records the real previous status (04cff77);
  - `get_by_id` accepts id prefixes and message ids (1c324e9);
  - `memory_db` audit trail (0c537a4);
  - backup consistency and offsite fixes (d78badb, 026b880, 151a845).
- Guideline trim from 15 rules to 2 + 13 `trim_*` (f7b4cdf, 77832eb, 9db9170, 7cf1a9e).

### Added
- **Separate model for claim extraction + facets.** `JUNTO_CLAIM_GATE_EXTRACT_MODEL`
  (claim EXTRACT, operation, shelf_life, trigger) and
  `JUNTO_CLAIM_GATE_EXTRACT_REASONING` (sent as `reasoning_effort`, e.g. `none`
  for thinking models). The contradiction judge stays on
  `JUNTO_CLAIM_GATE_MODEL`. Both unset = unchanged behavior. Basis: n=150
  bake-off 2026-10-27 (learning_85ee50137127e19c).

### Fixed
- **Agents that keep working after parking get idle nudges again.** An agent
  woken while parked usually works on the inbox plugin's session and never calls
  `memory_start_session`, so the parked flag stuck and its idle timer never armed
  (nimbus server-team/billing-team sat at ~700K all night on 2026-10-26). Real
  work while parked (`memory_update_work`, `memory_lock_files`, spec/learning/
  backlog writes, …: `idle_park.UNPARK_TOOLS`) now unparks the agent and arms the
  timer. Reads, acks and `memory_send_message` still don't, so answering one
  message while parked doesn't earn a nudge.

### Changed
- **Auto-reset agents get a direct "park now" nudge.** An agent listed in
  `idle_park_autoreset` is told to park now, shown its reported context size, and
  told the box will /clear + go it after it parks. This overrides the project's
  idle-policy text (e.g. nimbus keep-alive), because the reset only fires on a
  park that follows the nudge. The body still starts with "Idle" for the reset
  gate's anchor (Tom, 2026-10-17).
- **Parked agents are no longer nudged.** After `memory_end_session`, the agent's
  tool calls (including its still-attached inbox plugin's) don't arm an idle timer
  until the agent starts a new session itself; a plugin rebind doesn't count.
  Before this a parked agent was re-armed by its plugin and nudged every ~2h onto
  an expired cache, rebuilding its whole context to reply "still parked"
  (coordinator@nimbus, msg_9684a5d90e59).
- **Idle-park nudge skips agents that already armed a wake.** No nudge while the
  agent has its own pending reminder due within 60 min (e.g. a nimbus keep-alive).
  Before this the 50-min nudge always landed ahead of a +55m keep-alive and reset
  its count, so the chain never timed out and idle agents kept themselves awake for
  hours (learning_222e630bf8b35d24).
- **Idle-park nudge follows the project's idle policy.** The nudge no longer says
  "park now". Its subject is neutral, and a project with an active project-scoped
  guideline `park_on_idle_nudge` gets that guideline's live text in the body
  instead of the default park-at-a-clean-stop advice. Agents with a stale cached
  copy of the guideline still act on the current rule (coordinator@nimbus,
  msg_9f1cf850e7d6).

### Added
- **Local-model delegation** (interface:delegated-inference-v0 1.5.1; README →
  "Local-Model Delegation"). Agents own tasks that run on a local Ollama model:
  `memory_delegate` creates one (connection/scope, instructions, JSON output schema,
  routes, optional schedule and canaries; no schedule = one-off), `memory_schedule`
  manages them (edit is versioned; pause/resume/delete/run_now/history/runs/approve),
  `memory_job` reads one run, `memory_runners` lists runners and connections. The server
  schedules runs and applies routes (`notify` / daily `digest` / `drop`); the model never
  acts. Default model `gpt-oss:20b` (resisted the injection tests). Canaries run daily and
  after any edit to instructions, schema, model or source.
- **Connections + secret vault.** Agents request a connection (imap, mcp, file, inline,
  gmail); the operator activates it in `/admin` by pasting the secret. Secrets are stored
  encrypted under the new **`JUNTO_VAULT_KEY`** env var and sealed per claim to the
  runner's X25519 key. Revoke cancels queued runs; every change is audited.
- **Runner** `contrib/runner/junto-runner.py` + systemd user unit: generic adapters
  (inline, imap read-only, file, mcp source + read-only tools), Ollama structured output,
  LAN-only `/runner/api/*`. `cryptography` is now a pinned direct dependency.
- **Admin web UI** at `/admin` (LAN-only; edits need an owner key): Overview, Settings
  (code default vs. server value, per project), Agents, Alerts (ack), Local tasks, Connections
  (+ audit), Change log.
- **Context history**: gauge readings sampled into `context_history` (10-min buckets,
  90-day TTL); `record_learning` / `change_status` stamp the author's context fill.
- **Action lapse** (design:action-lapse-v0): action messages unanswered for
  `action_lapse_days` (default 30) and answered-but-never-closed tasks/blockers after
  `action_lapse_responded_days` (default 7) leave the action lane (not deleted). Send
  responses add `size_hint` and `possible_parent`; recall rows gain `age_days`.
- `contrib/context-report/junto-context-report.py`: box-agnostic reporter for
  `memory_report_context`, including the idle-park auto-reset gate.
- **Idle-park auto-reset** (design:idle-park-autoreset-v0, phase 2 of the nudge). An agent
  that ends its session while its idle timer is in the nudged state, and is listed in the
  new push-control key `idle_park_autoreset` (comma-separated, opt-in), gets a pending
  `idle_park_resets` doc (one per agent; expires after 6h). `memory_report_context` now
  returns `resets` (pending resets for the agents in that call's reports) and accepts
  `reset_results` ([{reset_id, outcome: done|failed|deferred, detail}]). The per-box
  reporter runs a tmux safe-inject gate (no recent keypress, park banner after the last
  prompt, not busy, no dialog, empty input) and types `/clear` + `go`. Additive: old
  reporters ignore `resets`, so their agents keep nudge-only behaviour.
- **Idle→park nudge** (`src/shared_memory/idle_park.py`). After `idle_park_minutes`
  (default **50**, under the ~60-min prompt-cache TTL) with no real tool call from an
  agent name, the server pushes that agent one `is_system_notice` message
  (`system_notice_kind="idle_park_nudge"`) telling it to park if it's at a clean stop.
  One timer per (project, agent); inbox-plugin background calls (heartbeat, health,
  start_session, emission stats) and `memory_report_context` don't reset it;
  `memory_end_session` disarms it; it fires once per idle period. Only agents whose
  reported context (`memory_report_context` gauge) is at least `idle_park_min_context`
  (default 200K) are nudged; agents with no report are still nudged. New push-control
  config keys `idle_park_minutes` (0 disables), `idle_park_optout` (comma-separated
  agent names) and `idle_park_min_context`, set via `memory_admin push_control_set_config`.

### Fixed
- **Runner 0.4.2:** a gpt-oss reply cut at the cap is retried once at low reasoning effort
  (a phishing canary reasoned past 8,192 tokens); `low_effort_retries` in run evidence.
- **Idle-park nudge no longer fires mid-conversation.** The idle timer counted only junto
  tool calls, so turns spent talking to the user (research, explanation) looked idle and
  coordinator@nimbus was nudged into parking while Tom was working with it. The context
  reporter's `written_at` (stamped by the statusline once per turn) now also resets an
  agent's timer (`idle_park.note_turn`). It never arms a timer, and renders inside the
  nudge's 10-min grace (the agent answering the nudge) don't count. No client change.
- **Runner 0.5.0: parallel runs.** New config `workers` (default 1; sage runs 3): that
  many claim loops share one runner key, and the server's atomic claim keeps them apart.
  Ollama already served 3 requests per model, so the serial runner was the bottleneck
  (measured 1.5x same-model, 2.5x across models on sage's two GPUs). Models bigger than
  `exclusive_model_gb` (default 24, by Ollama's on-disk size, e.g. qwen2.5:72b) run alone,
  so they don't evict the resident models; a waiting exclusive run holds back new runs
  so it can't starve. A server outage no longer kills the runner process; each loop
  logs and retries.
- **Runner 0.4.1:** 0.4.0's 2,048-token reply cap counted gpt-oss reasoning and cut
  replies before any JSON; now min(8,192, context/2), with a clear error on a cap hit.
- **Failed items of scheduled tasks notify the owner** (action notice per run with each
  item's ref, from/subject and error). Before, they were only counted while the cursor
  moved past them.
- **Scheduled tasks' runs are claimed before one-off batches** (`lane` on runs; queued
  runs backfilled at startup).
- **Runner 0.4.0: no more silent truncation or runaway replies.** The runner set neither
  `num_ctx` nor `num_predict`, so Ollama's 4,096-token default silently truncated long
  items, and a model could generate until the 600 s timeout (one call held the queue
  10+ min). It now sizes `num_ctx` per run (buckets 8K/16K, model caps), cuts oversize
  items itself with a visible marker, caps replies at 2,048 tokens, and reports `num_ctx` +
  `items_truncated` in run evidence (only when items exist, so the server's
  "0 items, no evidence" guard still fires).
- Ephemeral reporter identities (`ctx-reporter-*`) replace their earlier sessions on start
  instead of piling up (108 active sessions → 18).
- Spec names that differ only by case are refused; `get_spec` suggests the existing twin.
- Recall no longer serves other agents' session digests and logs scores per recall event.
- Mongo container `nofile` ulimit raised to 64000.
- **Background tasks no longer die when any client disconnects.** The MCP SDK enters
  the app lifespan once per client session; its teardown was cancelling the
  process-global scheduler and SSE keepalive (and resetting the Chroma client) on every
  disconnect. That is why `memory_set_reminder` delivery stalled intermittently. The
  teardown is removed; the idempotent `start_*` calls self-heal on each session.

## [1.39.0] - 2026-09-10

If you run a junto-memory server, the two `Changed` items below alter behavior on
your next restart — read the **Upgrade note** under each.

### Changed
- **Global guidelines now ship in code and seed on every server.**
  `src/shared_memory/global_guidelines.py` is the source of truth for the universal
  `scope="global"` rule set; `seed_global_guidelines()` upserts all of them (by name)
  into `db.guidelines` on every boot. Previously most global rules were DB-resident
  only, so a fresh or peer server started with almost none. Global scope is
  code-managed and **read-only through the `memory_guidelines` tool** (`set`/`delete`
  on a global rule are refused).
  - **Upgrade note:** on your next restart the seeder re-asserts these rules, so a
    live DB edit to a *global* rule is overwritten by the code version. Put fleet- or
    deployment-specific rules under `scope="<project>"` (still writable at runtime via
    `memory_guidelines`). To remove a shipped global, delete it in code **and** delete
    its `db.guidelines` row once (the seeder never deletes non-code rows).
- **`execute-don't-ask` guideline generalized.** The shipped default now states the
  recoverability principle (do reversible work; a human approves the irreversible;
  restoring never needs approval) and **defers the exact approval boundaries to a
  per-fleet "approval contract"** (a spec or project-scoped guideline) instead of
  hard-coding one fleet's staging/prod/merge policy.
  - **Upgrade note:** define your own approval boundaries — e.g.
    `memory_define_spec(name="approval-contract", ...)` or a `scope="<project>"`
    guideline — rather than relying on the shipped rule for your specific gates.
- **Docs reconciled to the "run longer sessions" context bands.** The README, the
  agent/coordinator `CLAUDE.md` templates, and the reference doc now match the
  server's `session_length_discipline` guideline (keep working <500K, watch
  500–800K, park >800K; coordinators shift ~150–200K lower) and drop the stale
  200K-era "park after 1–3 tasks" heuristic.

### Fixed
- **Envelope-leak write-time lint false-negative.** The tail-guard only fired when an
  envelope token sat *immediately* after a field's closing tag, so the two dominant
  real leak shapes slipped through at write time — `record_learning`'s
  `…</details><project>…</invoke>` and `end_session`'s `…</summary><files_modified>…`,
  plus a stray-quote variant. The guard now also recognizes the emitters' own bare
  parameter-tags **when corroborated** by a real envelope token elsewhere in the tail,
  so a doc that merely *documents* the leak shape is never truncated (no false-positive
  data loss). Also routed `end_session` (summary) and `register_function` through the
  shared recovery path. Transparent to callers; broadens write-time leak coverage.

### Added
- **Bearer / header authentication** (`design:header-auth-v0`). When `MCP_AUTH_ENABLED=true`,
  `memory_start_session` now accepts the API key from an `Authorization: Bearer <key>` HTTP
  header in addition to the explicit `api_key` tool argument. This lets adopters configure
  auth the standard MCP way — a `headers` block in `~/.mcp.json` — instead of injecting the
  key into the system prompt. Verified end-to-end against a live Claude Code client
  (CC forwards the static header untouched; the server resolves it to the keyed role).
  - Precedence: explicit `api_key` arg wins; header is the fallback; keyless stays
    `agent`-tier; a present-but-invalid header key hard-rejects (fails loud).
  - New surface: `auth.py` `parse_bearer_token()` + `_header_api_key` contextvar;
    `auth_header_middleware` ASGI wrapper in `__main__.py`; one-line fallback in
    `tools/sessions.py`.

## Earlier

This file was introduced 2026-06-04. For history before this point, see the git log and the
in-server architecture spec (`memory_get_spec(name="architecture:shared-memory-v1")`).
