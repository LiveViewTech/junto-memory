# Review guide: junto-memory 194ece3 (v1.34.0) → v1.41.3

For a reviewer of the sync PR. The jump is large (159 files, +21.5K / −3.0K lines), so this
groups it by **theme**: review one theme at a time. About 37% of the added lines are tests.
Line counts are `git diff --numstat 194ece3 v1.41.3`.

| Area | Files | Lines |
|---|---|---|
| `src/` (server) | 58 | +10,189 / −1,174 |
| `tests/` | 54 | +7,877 / −385 |
| everything else (contrib, scripts, docs, build) | 47 | +3,395 / −1,476 |

**Off by default:** every model-backed feature (reranker, claim gate, facets, chunk index) and
delegation. Code is present but dormant until configured via env or a database setting.
**On by default:** tenant isolation, the web-UI auth gate, idle nudges (50 min), action lapse
(30 days), session persistence.

---

## 1. Security fixes (start here)
| What | Files | What to check |
|---|---|---|
| **Tenant isolation for project-scoped keys** (1.41.0). A key scoped to project A could read/write project B via `project=` arguments, "all projects" listings and doc ids. | `src/shared_memory/project_scope.py` (new CallToolRequest wrapper), `app.py` (wrapper order), `auth.py` (`scoped_projects`, `project_allowed`), per-tool filters in `tools/{backlog,query,specs,search,functions,lifecycle,locking,messaging,push_control_api,skills}.py`; tests `tests/test_project_scope.py` | Exemptions are deliberate: `memory_send_message` (cross-project messaging), `memory_start_session`, `memory_admin`, `memory_get_messages`. Writes to another project's doc by id return "not found" (they don't confirm the doc exists). Unscoped/keyless sessions are unchanged. |
| **Server-wide web views need an unscoped key under `JUNTO_REQUIRE_KEY`** (1.41.1). `/dashboard/api/*` had no auth at all. | `admin_ui.py` (`_lan_only`, `_tunnel_only`, `_key_if_required`), `__main__.py` (dashboard + compact-log routes), `dashboard.html` (key box); `tests/test_server_wide_gate.py` | Gating matrix: no key / bad key → 401, scoped key → 403, unscoped → 200; tunnel origin → 403. A source-level test asserts every server-wide route calls its gate. |
| Admin read routes require a key under REQUIRE_KEY; self-report limited to own instance | `admin_ui.py`, `tools/messaging.py` (`memory_report_context`); `tests/test_report_context_self.py` | |
| URL scheme validation (1.41.3) | `contrib/runner/junto-runner.py` (`_require_http`, required `server`/`ollama`), `contrib/local-inference/li.py` | Only http/https reach `urlopen` (Semgrep dynamic-urllib-use-detected). |
| Secret vault for delegation connections | `src/shared_memory/vault.py`, `delegation.py` | Fernet at rest under `JUNTO_VAULT_KEY`; per-claim sealing to the runner's X25519 key. |
| Credential removed from test scripts (1.41.3) | `contrib/test/{inbox_resource_smoke,mcp_stress_harness}.py` | A password fallback was removed; both now require `MONGO_PASSWORD`. |
| `JUNTO_LOG_QUERY_TEXT=false` keeps query text out of stored metrics | `recall_metrics.py`, `tools/query.py`; `tests/test_recall_metrics_text.py` | |

## 2. Admin web UI (`/admin`) — new
`src/shared_memory/admin_ui.py`, `src/shared_memory/admin.html`, `__main__.py` (route registration).
Pages: overview, settings (idle-park, push budgets, query defaults), agents, alerts (ack), change log,
delegation connections/tasks/runs/runners. Reads: see §1 gate. **Writes need an owner key.** The key
lives in the browser tab's sessionStorage only. junto-control (the separate UI) is retired.

## 3. Idle-park, context and time zones
`idle_park.py` (nudge after N idle minutes, policy text, DAY/NIGHT line, per-agent `timezone=`,
opt-in auto-reset queue), `context_stamp.py`, `tools/messaging.py` (`memory_report_context`),
`tools/sessions.py` (`timezone`), `push_control.py` (config keys), `contrib/context-report/` (box-side
reporter + systemd timer, with transcript fallback for unattended agents). Tests:
`test_idle_park.py`, `test_park_title.py`, `test_session_*`. Auto-reset types into a tmux pane only
through a safety gate (`contrib/context-report/junto-context-report.py` `analyze()`), and only for
opted-in agents.

## 4. Messaging and action lapse
`action_lapse.py` (unanswered action messages leave the action lane after 30 days; not deleted),
`tools/messaging.py` (lanes, `unresolved_refs`, recipient checks), `write_lint.py` (strips leaked tool
XML from bodies), `push_control.py`. Removed: `tools/autopilot.py` (the six `memory_autopilot_*`
tools; clients need junto-inbox ≥ 0.0.26). Tests: `test_action_lapse.py`, `test_write_lint.py`.

## 5. Delegation (local-model jobs), runner, connections
`delegation.py`, `tools/delegation.py` (`memory_delegate` / `memory_schedule` / `memory_job` /
`memory_runners`), `runner_api.py` (HTTP API the runner polls; runner keys), `vault.py`,
`contrib/runner/` (runner + systemd unit). **Idle unless a runner is deployed and keyed.** New
connections take their runner hosts from `JUNTO_DEFAULT_RUNNER_HOSTS` (no built-in host).
Tests: `test_delegation.py`, `test_runner_workers.py`, `test_service_principal.py`.

## 6. Retrieval: recall, rerank, claim gate, facets, chunk index (all OFF by default)
`rerank.py` + `contrib/reranker/` (cross-encoder service, `query_config.rerank`), `claim_gate.py`
(write-time contradiction check, `JUNTO_CLAIM_GATE_ENABLED`), `facets.py` (`JUNTO_FACETS_ENABLED`),
`chunk_index.py` (`query_config.chunk_index`), `lexical.py` (exact-match lane), `recall_metrics.py`,
`POST /recall` in `__main__.py` + `contrib/hooks/recall_prompt_hook.py`. Each fails open: if the
endpoint is down, the query or write proceeds without it. Tests: `test_rerank.py`,
`test_write_time_gate.py`, `test_recall_*`, `test_query_embed_reuse.py`.

## 7. Sessions, identity, guidelines, skills
`session_persist.py` (sessions survive a restart), `identity_gc.py` (reap pending-tier ghosts; off
by default), `state_guard.py` (state-spec shrink guard), `global_guidelines.py` (13 `trim_*` rules),
`default_skills.py` (shared `parking` skill), `tools/{sessions,guidelines,skills,specs}.py`,
`scripts/retire_orphan_globals.py` (upgrade helper; dry-run by default). Tests: `test_session_*`,
`test_state_guard.py`, `test_global_guidelines_seed.py`, `test_skills.py`, `test_default_skills.py`, `test_spec_case_variant.py`.

## 8. Build, compose, CI
- `docker-compose.yml`: chroma image 1.5.9 pinned by digest (the line-10 comment is an image digest, not a key); Mongo `ulimits nofile 64000`; `extra_hosts host.docker.internal`; **`JUNTO_MCP_LAN_IP` required** (no default).
- `requirements.txt` / `pyproject.toml`: `mcp<2` pin, `chromadb==1.5.9`, `cryptography`.
- `.github/workflows/ci.yml`: lint + tests + compose build.

**Upgrade hazard:** Chroma 1.4.1→1.5.9 is a one-way on-disk migration. The deploy runbook cold-backs-up first.

## 9. Docs
`README.md` (new sections: optional local inference, recall, action lapse, auth modes, admin UI),
`.env.example` (every env var with its code default), `CHANGELOG.md` (1.35→1.41.3; the 1.40.0
entry has an "Upgrading from an older build" section), `AGENT_INSTALL.md`, `AGENT_OPERATIONS.md`,
`docs/*`.

## 10. Removed from the shipped tree (1.41.3)
`infra/sage/`, `contrib/backup/sync-to-storm.sh`, `scripts/check_usage.py`, one-off/maintainer
scripts, retrieval research harnesses, `docs/workjunto-pilot-setup.md`, and `data/` (now gitignored).
These were maintainer home-ops material. See CHANGELOG 1.41.3 for the list.
