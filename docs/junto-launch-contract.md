# Junto Launch Contract

**What this is:** the conditions a junto agent's launch must satisfy for junto to work
correctly. It is implementation-agnostic — a human, a script, or a Claude can build any
launcher (or none) as long as the launch meets these requirements. `junto-launch.sh` is
*one reference implementation* of this contract, not the definition. Home and work launch
differently; both must satisfy the same contract.

**Mental model:** the server is the single source of truth for *rule content* (global
guidelines are code-seeded into the server). The launch's #1 job is not to carry rules —
it is to **force the agent to fetch them and obey them.**

---

## R1 — System-prompt forcing of the guideline contract  *(THE critical requirement)*

The launch MUST inject, **at instruction authority** (system prompt or CLAUDE.md — not
optional prose, not a tool-result the model may ignore), an instruction that:

1. Makes **`memory_start_session` the mandatory first call**, with the resolved identity.
2. Declares the response's **`guidelines` field authoritative for the session**: read it,
   **do not skip / summarize / paraphrase**, and it **overrides conflicting defaults —
   including the launcher's own injected prompt and the local CLAUDE.md.**

**Do not weaken the wording — all four clauses are load-bearing and must appear:**
(1) `start_session` is the *mandatory first call*; (2) the guidelines are *authoritative*;
(3) *do not skip / summarize / paraphrase*; (4) they *override conflicting defaults including
this file*. Dropping (3) lets an agent acknowledge-then-dilute the rules; dropping (4) makes
conflicts resolve unpredictably. A launch that says only "the guidelines are authoritative and
override defaults" (missing (3) and the explicit "this file") is **under-compliant** — it
usually still works, which is exactly why the omission rots silently. Mechanism is free
(`--append-system-prompt-file` or CLAUDE.md — no correctness difference); the *wording* is not.

**Observed insufficient wording (do not copy):** "treat the guidelines with the *same
authority* as these system rules." Same authority is a **tie**, not an override — on conflict
the precedence is undefined. Clause (4) requires the guidelines to **beat** the injected prompt
*and* the local CLAUDE.md, not merely match them.

**Plugin-path variant (REQUIRED when R4 is in play — this is where the forcing usually leaks).**
When the inbox plugin is loaded it **pre-opens the session**, and the agent must reuse it via
`get_session_id` instead of calling `memory_start_session` (calling it again opens a duplicate).
But `get_session_id` returns only `{status, session_id, project, agent}` — **the guidelines are
NOT in that response.** So on this (common) path R1's force-delivery has a hole: the agent must
be *separately and authoritatively* instructed to **fetch the guidelines via
`memory_guidelines(action="list")`** and apply the exact same read / no-paraphrase / override
authority. Do **not** fetch them with `memory_query("session guidelines")` — a fuzzy vector
search can miss rows; it is the wrong tool and has caused real under-delivery in the field. So
R1's forcing instruction must cover *both* entry paths: "call `start_session` (or, if the plugin
already opened the session, reuse via `get_session_id` **and** fetch guidelines via
`memory_guidelines(list)`); either way the guidelines are authoritative as above."

**Why it's #1:** the returned `guidelines` is just JSON data. Without a system-prompt-level
instruction elevating it to binding-and-overriding, an agent can fetch the guidelines and
quietly not follow them. This forcing is what turns server-managed rules into actual agent
behavior. The server carries the rules' *content*; the launch carries the *forcing*.

**Verify:** the agent's effective system prompt / CLAUDE.md contains, verbatim-equivalent:
"call `memory_start_session` first; the returned `guidelines` are authoritative — read, do
not paraphrase, they override defaults including this file."

**Reference text (from `junto-system-prompt.md.tmpl`):**
> The response contains a `guidelines` field — server-managed behavioral rules authoritative
> for this session. Read them; do not skip, summarize, or paraphrase. They override defaults
> including this file when they conflict.

---

## R2 — Agent identity, resolved before `start_session`

The launch MUST make `(project, agent_name)` and optionally `component` available before the
agent calls `memory_start_session`, and the **same values must be visible to the inbox
plugin** if R4 is in play.

- *How is free:* env vars, a CLAUDE.md marker, an interactive prompt, hardcoded macro.
- *Constraint:* identity is consistent between the agent's `start_session` call and the
  plugin's subscription. A mismatch = the agent and its push stream bind to different inboxes.

**Verify:** `memory_start_session` is called with the intended `(project, claude_instance)`
and the plugin (if loaded) subscribes to `inbox://<project>/<agent>` for the same pair.

---

## R3 — MCP server reachable and authenticated

Claude Code MUST have the junto server registered so `memory_*` tools resolve:

- `"type": "http"`, the server `url` (e.g. `http://<host>:8080/mcp`), and
  `"Authorization": "Bearer smk_…"` in the headers.
- Required when `JUNTO_REQUIRE_KEY=true` (any non-localhost deployment): a keyless session is
  rejected.

**Verify:** a `memory_*` call reaches the server and authenticates (e.g. `memory_health` or a
successful `memory_start_session`).

**Minimal example (`~/.mcp.json`):**
```json
{ "mcpServers": { "junto": {
  "type": "http",
  "url": "http://spg-junto-central:8080/mcp",
  "headers": { "Authorization": "Bearer smk_…" }
}}}
```

---

## R4 — Inbox plugin  *(optional — only for live push)*

Without it, the agent sees messages on `memory_get_messages` (session start / explicit
check). With it, messages arrive live as `<channel>` blocks. Two sub-requirements:

**R4a — load the plugin into Claude Code:**
- `--channels "plugin:junto-inbox@<marketplace-id>"` (primary), or
  `--dangerously-load-development-channels "plugin:junto-inbox@<id>"` (escape hatch when the
  plugin isn't on the allowlist), AND
- `channelsEnabled: true` + the plugin allowlisted in CC's settings (`remote-settings.json`).

**R4b — give the plugin its identity and connection (env-only — source-verified).** The plugin
is a *separate subprocess* that subscribes to `inbox://<project>/<agent>` over its own HTTP
connection. It reads its config **only from environment variables** at startup (`server.ts`
`envVar()`, verified by inbox 2026-06-16) — it does **not** read `~/.mcp.json`, the CC MCP
registry, or any file. So the launch MUST export, into the plugin's launch environment:

- `JUNTO_SHARED_MEMORY_URL` — the server URL. **Mandatory for any non-localhost server.** The
  plugin's built-in default is `http://localhost:8080/mcp`; if you don't set this, the plugin
  silently tries localhost and never reaches a remote server (e.g. spg-junto-central). This is
  the #1 work-box footgun.
- `JUNTO_API_KEY` — **required whenever the server runs `MCP_AUTH_ENABLED=true` / `JUNTO_REQUIRE_KEY=true`** (work). Omitted-if-unset is correct only for a keyless server (home).
- `JUNTO_AGENT`, `JUNTO_PROJECT` — bind the right inbox. Optional: `JUNTO_COMPONENT`,
  `JUNTO_CHANNEL_DELAY`.

**⚠️ Known security limitation (do not mistake for a free choice).** Because the plugin is
env-only, on an auth-required server the API key **must** sit in the launch environment — and a
process env var is inherited by *every* child the agent spawns (every Bash command, every other
MCP server, every hook), so any of them can `env | grep JUNTO_API_KEY` and read or leak it. For
agents that execute arbitrary commands (all of ours) this is a live exposure, not theoretical.
There is currently **no file-sourced alternative** in the plugin, so a launcher cannot avoid it.

Therefore:
- **Minimize the blast radius:** export `JUNTO_API_KEY` as narrowly as possible — set it inline
  on the `claude` invocation (so it lives in the plugin's launch env, not in `~/.bashrc` or a
  global profile inherited by unrelated shells). Do **not** persist it in shell rc files.
- **The real fix is a plugin change, not a launcher change** — tracked: add a file/registry
  key-source to the plugin so the secret need never enter the environment (`backlog`, owner
  `inbox`).
- **Home is not a counter-example:** home avoids the key-in-env exposure only because its server
  is **keyless** (`MCP_AUTH_ENABLED=false`) — it has no secret to expose, not a securer sourcing
  path. Under auth, every environment hits this same limitation.

**Verify:** a send to this agent while it's live returns `live_subscribers ≥ 1`, and a test push
arrives as a `<channel>` block without a manual `/go`. On an auth server, also confirm the key
is scoped to the launch env only (absent from `~/.bashrc`/global profile).

---

## R5 — Environment-conditional addenda  *(call out, don't mandate)*

- **MDM settings persistence:** corporate MDM (Jamf/EDR) may strip `channelsEnabled` from
  managed settings. Mitigation: point `CLAUDE_CODE_REMOTE_SETTINGS_PATH` at a file the MDM
  doesn't touch + a hook that re-patches it each launch. Only needed on managed machines.
- **1M context model:** `ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-6[1m]` — same per-token
  cost, fewer compactions. Optional performance opt-in.

---

## R6 — Skill materialization  *(optional — Phase-2 native skill surfacing)*

Standing launcher behavior (the durable counterpart of the one-time
`skill-materialization-rollout` directive). Without it, agents still get
scope-matched skills as **headers** in the `start_session` bundle (`skills`
field — Phase 1, server-side, no launcher work). With it, active skills also
become **native Claude Code skills** that the model's matcher fires mid-turn —
the win for long sessions where the session-start list scrolls out of context.

Because Claude Code discovers skills **at startup**, this MUST happen in the
launcher, before `claude` is invoked (a plugin writing them mid-session is too
late, and the server can't write the agent's filesystem). Steps:

1. After identity is resolved and the MCP server is reachable, call
   `memory_export_skills` (project + agent name + cwd). Needs a session id —
   reuse the launch's `start_session` or a short-lived export session.
2. Write each returned `skills[].content` to `<repo>/.claude/skills/<relpath>`
   (`relpath` = `<name>/SKILL.md`), creating directories.
3. **Prune** previously-materialized junto skills no longer in the export:
   identify junto-managed files by the provenance footer
   `<!-- junto skill … -->` and delete only those — **never** touch a
   hand-authored `SKILL.md`.
4. Idempotent: only rewrite when content changed (byte-compare / content-hash),
   so a no-change launch does zero writes.
5. `.claude/skills/` is gitignored on consumer repos (materialized artifacts,
   not source).

Full producer/consumer contract + open questions: spec
`interface:skill-materialization-v0`. This is launcher work (`tlemmons/junto`);
the server half (`memory_export_skills`) is live.

---

## Profiles — what each environment needs

Same contract, different surface depending on whether the server requires auth and whether the
host is managed. Use this as the per-environment setup checklist.

| Item | **Home** (keyless server, unmanaged host) | **Work** (auth server, possibly managed) |
|---|---|---|
| R1 forcing | Required (via global `~/.claude/CLAUDE.md`) | Required (rendered prompt or CLAUDE.md) |
| R2 identity | Required | Required |
| R3 MCP URL | Required — hardcoded in `.mcp.json` (`sage.lemmons.net:8080`) | Required — `spg-junto-central:8080` (or tailnet `100.83.241.96`) |
| R3 Bearer key | **Omit** — server runs `MCP_AUTH_ENABLED=false` | **Required** — `JUNTO_REQUIRE_KEY=true` rejects keyless |
| R4a `channelsEnabled` + plugin allowlist | Already set globally (one-time, done) | **One-time CC-settings step on each new machine** — without it the plugin silently won't load (≠ per-launch; do it once at setup) |
| R4b `JUNTO_SHARED_MEMORY_URL` | Must be set if plugin host ≠ server¹ | **Required** — localhost default won't reach `spg-junto-central` |
| R4b `JUNTO_API_KEY` | **Omit** — keyless server | **Required** — auth on; scope to the launch env only |
| R4 prerequisite | `bun` on PATH (plugin spawns `bun server.ts`) | `bun` on PATH |
| R5 MDM persistence | **Not needed** — unmanaged | Needed **only** on a corporate-managed machine |
| R5 1M model | Optional | Optional |

*¹ Open detail (inbox flagged): home's plugin defaults to `localhost:8080` yet reaches sage, so
either `JUNTO_SHARED_MEMORY_URL` is set in the plugin's launch env or there's a localhost→sage
forward on the host — unconfirmed. Doesn't affect the work profile.*

**Reading of the table:** the things WORK adds over HOME fall in two kinds. **Per-launch:** the
Bearer key (R3), the plugin's `JUNTO_API_KEY` + explicit URL (R4b). **One-time per machine:**
the `channelsEnabled` + plugin allowlist CC-settings step (R4a — home has it globally, a fresh
machine doesn't, and without it the plugin silently won't load) and MDM persistence (R5, managed
hosts only). Home legitimately omits all of these; that's profile difference, not
non-compliance.

## Compliance checklist

- [ ] **R1** Guideline-forcing present at instruction authority (mandatory `start_session`;
      guidelines authoritative + override).
- [ ] **R2** Identity `(project, agent[, component])` resolved pre-`start_session`, consistent
      with the plugin.
- [ ] **R3** junto MCP registered with `type:http` + URL + Bearer key; `memory_*` authenticates.
- [ ] **R4** (if live push wanted) plugin loaded + `channelsEnabled`; plugin env set:
      `JUNTO_SHARED_MEMORY_URL` (explicit, non-localhost), `JUNTO_API_KEY` (if server requires
      auth), `JUNTO_AGENT`/`JUNTO_PROJECT`. Key scoped to the launch invocation, **not** rc
      files / global profile.
- [ ] **R5** MDM persistence + 1M model handled if the environment needs them.
- [ ] **R6** (optional) Skill materialization: `memory_export_skills` written to
      `.claude/skills/` pre-launch, footer-pruned, idempotent, gitignored.

## On variation between launches

This is a contract, not a launcher — but "implementation-agnostic" does **not** mean "any
approach is fine." Two axes:

- **Incidental mechanism (vary freely):** how identity is supplied, whether the forcing lives
  in `--append-system-prompt-file` or CLAUDE.md, how the plugin is loaded. No correctness or
  security difference, so pick what fits the host.
- **Security/correctness invariants (do NOT vary):** R1's full four-clause forcing; R4b's
  explicit non-localhost `JUNTO_SHARED_MEMORY_URL` and `JUNTO_API_KEY`-scoped-to-the-launch
  (not rc files). These have a *right* answer with a stated cost for getting it wrong; a launch
  that deviates is under-compliant even if it appears to work. They're written prescriptively on
  purpose — the failure modes (diluted rules, a plugin silently talking to localhost, a key
  leaked through a global profile) are silent, so the spec forces the shape rather than trusting
  each author to rediscover it.

Note the plugin's env-only key handling (R4b) is a *known limitation*, not an invariant we
endorse — under auth the key has to enter the environment because the plugin offers no other
source. The invariant is to scope it tightly; the fix is a plugin enhancement (tracked).

`junto-launch.sh` is the work reference implementation. Home satisfies R1–R3 with a different
*mechanism* (CLAUDE.md + a hardcoded `.mcp.json` URL, keyless server) — allowed — while both
must satisfy the invariants identically.
