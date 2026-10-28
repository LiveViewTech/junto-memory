# Changelog

All notable changes to `junto-memory` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the server advertises its running
version via `memory_health` (`server_version`).

## [Unreleased]

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
