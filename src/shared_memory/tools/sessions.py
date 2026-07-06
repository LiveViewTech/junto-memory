"""Session management tools - start and end Claude sessions."""

import json
import uuid
from datetime import timedelta
from typing import Any, Dict, List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.helpers import (
    _match_path_patterns,
    cleanup_stale_sessions,
    generate_doc_id,
    get_blocking_others,
    get_interface_updates,
    get_pending_signals,
    get_project_collection,
    get_recent_modifications,
    get_relevant_locks_for_session,
    get_shared_collection,
    normalize_project,
    release_session_locks,
    require_session,
    utc_now,
    utc_now_iso,
)
from shared_memory.state import active_sessions

# Curated "use-when" hints for tools agents commonly miss in deferred-tool-load
# harnesses (Claude Code et al.), where only tool *names* appear until a
# ToolSearch. Surfaced in every memory_start_session response so the discovery
# work doesn't depend on name-scanning alone.
#
# Design constraints (see backlog notable_tools follow-up):
#  - Capped at 5 entries. Bounds the token cost of every session start.
#  - NO rotation. This block is byte-stable across calls on purpose — the
#    response is assembled stable→dynamic for Anthropic prefix-cache hits
#    (see the field-order note below). Rotating would change the prefix every
#    call and forfeit that. If the list outgrows 5, prune — don't rotate.
#  - Edit this constant when the tool surface changes; keep the most
#    impactful/recently-added tools at the top.
NOTABLE_TOOLS = [
    {"tool": "memory_set_reminder",
     "when": "Schedule a message to your future self (or a peer) at a specific time — deadlines, follow-ups, 'ping me after X'."},
    {"tool": "memory_find_function",
     "when": "BEFORE writing any function — 200+ are already registered; check if it exists first."},
    {"tool": "memory_register_function",
     "when": "AFTER writing/modifying a function — register name, file:line, purpose, gotchas so the next agent doesn't re-read your file."},
    {"tool": "memory_define_spec",
     "when": "Publish an interface contract (API shape, schema, topic) with spec_type='interface' BEFORE coding both sides of a boundary."},
    {"tool": "memory_record_learning",
     "when": "Hit a non-obvious bug, gotcha, or >10-min debug? Record it immediately — don't wait for park."},
]


def _active_component_peers(db, project, instance, subscribed_components,
                            window_minutes=15, limit=20):
    """Other agents recently active in any of `instance`'s subscribed components.

    design:unified-messaging-v0 Stage 1 (ADDRESSING). Reads the PERSISTENT
    agent_directory (not just in-process active_sessions) so it also catches
    peers connected to a federated peer server. Best-effort: returns [] on a
    missing db or any error — must never fail session start.

    15-min activity window: one heartbeat-ish span, deliberately wider than the
    5-min message-recency window — a teammate who parked 10 min ago is still
    "who's in your component". Self and duplicate instances are filtered; each
    peer reports only the components it shares with the caller.
    """
    subs = [c for c in (subscribed_components or []) if c]
    if not subs or db is None:
        return []
    peers = []
    try:
        cutoff = utc_now() - timedelta(minutes=window_minutes)
        seen = set()
        for peer in db.agent_directory.find(
            {
                "project": project,
                "subscribed_components": {"$in": subs},
                "last_seen": {"$gt": cutoff},
            },
            {"instance": 1, "subscribed_components": 1, "last_task": 1},
        ):
            name = peer.get("instance")
            if not name or name == instance or name in seen:
                continue
            seen.add(name)
            shared = [
                c for c in (peer.get("subscribed_components") or []) if c in subs
            ]
            peers.append({
                "agent": name,
                "components": shared,
                "task": (peer.get("last_task") or "")[:50],
            })
            if len(peers) >= limit:
                break
    except Exception as e:
        print(f"[MCP] Component-peer lookup failed (non-fatal): {e}")
    return peers


@mcp.tool()
async def memory_start_session(
    project: str,
    claude_instance: str = "unknown",
    task_description: str = "",
    tmux_target: str = None,
    role_description: str = None,
    working_directory: str = None,
    spawned_by: str = None,
    api_key: str = None,
    subscribed_components: List[str] = None,
    aliases: List[str] = None,
    ctx: Context = None
) -> str:
    """
    START HERE - Call this first before any other memory tools.

    Registers your session and returns:
    - Your session ID (required for all other calls)
    - Recent relevant learnings for your project
    - Active work by other Claudes (avoid conflicts)
    - Handoff notes from previous sessions
    - Active file locks in your working area
    - Signals from other agents (completion notifications)
    - Who you might be blocking
    - Interface updates since your last session

    You MUST call this at the start of your work.

    Args:
        project: Project you're working on (e.g., 'emailtriage', 'nimbus')
        claude_instance: Identifier for this agent (e.g., 'main', 'agent-1')
        task_description: Brief description of what you're about to work on
        tmux_target: Optional delivery target routing string (opaque, used by
            external dispatcher; leave blank unless your setup uses one)
        role_description: What this agent does (e.g., 'Core triage/classification engine').
            Set once - persists across sessions. Other agents can discover you via memory_list_agents.
        working_directory: Your working directory path. Used to auto-identify your agent name
            if path patterns are registered for this project.
        spawned_by: Parent agent that spawned this worker (for worker tier agents).
        api_key: API key for authentication (required when MCP_AUTH_ENABLED=true).
        subscribed_components: Optional list of component names (sub-groups under
            this project) this agent is working in (design:unified-messaging-v0
            Stage 1 — ADDRESSING). Persisted on the agent_directory record;
            session start surfaces a `component_peers` list of OTHER agents
            recently active in any of these components. Stage 1 is discovery
            only — component-addressed pub/sub delivery + claiming arrive in
            Stages 2-3. Omit (default) for nimbus's direct-send world.
        aliases: Optional alternate names this agent should also receive
            messages under (e.g. a lead agent claiming "coordinator"). Senders
            addressing any alias are redirected to this agent. Enforced unique
            per project — an alias that collides with a live agent name or
            another agent's alias is rejected (surfaced in `registry_warning`).
            None = leave existing aliases untouched; [] = clear them.
    """
    # Cleanup stale sessions on each new session start
    cleanup_stale_sessions()

    # ── Drain gate ──
    # When an operator has set drain=true via memory_admin (typically during a
    # graceful-restart prep window), refuse NEW sessions. Existing sessions
    # continue to work. The flag is in-memory and clears on the next process
    # restart by design.
    from shared_memory.restart import drain_error_payload, is_draining
    draining, _ = is_draining()
    if draining:
        return json.dumps(drain_error_payload())

    # ── Authentication (Path B soft-auth) ──
    # Posture: when AUTH_ENABLED=true and no api_key is presented, fall through
    # as default role="agent" instead of rejecting. This lets existing agents
    # connect without keys while still requiring a real key for elevated tiers
    # (user/admin/owner). Invalid keys remain hard-rejected.
    _auth_role = "agent"  # default when auth disabled OR soft-auth fallback
    _auth_projects = []   # empty = all projects
    try:
        from shared_memory.auth import AUTH_ENABLED, check_project_access, get_header_api_key, validate_api_key
        if AUTH_ENABLED:
            # Header-auth fallback (design:header-auth-v0): when no per-tool
            # api_key arg is supplied, fall back to the Authorization: Bearer
            # key parsed by the ASGI middleware into a contextvar. The explicit
            # arg always wins; keyless (no arg, no header) still soft-falls to
            # role=agent below.
            if not api_key:
                api_key = get_header_api_key()
            if api_key:
                key_info = validate_api_key(api_key)
                if not key_info:
                    try:
                        from shared_memory.audit import log_audit
                        log_audit("auth.failed", claude_instance, project,
                                  {"reason": "invalid_key"})
                    except Exception:
                        pass
                    return json.dumps({"error": "Invalid or revoked API key."})

                _auth_role = key_info["role"]
                _auth_projects = key_info.get("projects", [])

                # Tenant isolation: check project access
                if not check_project_access(_auth_projects, project):
                    try:
                        from shared_memory.audit import log_audit
                        log_audit("auth.project_denied", claude_instance, project,
                                  {"key_name": key_info["name"], "allowed": _auth_projects})
                    except Exception:
                        pass
                    return json.dumps({
                        "error": f"Access denied: your API key does not have access to project '{project}'.",
                        "allowed_projects": _auth_projects,
                    })
            else:
                # No key presented. Two rejection gates (design:auth-origin-trust-v0):
                #   1. REQUIRE_KEY — reject EVERY keyless session, any origin. The
                #      correct posture when the transport sets no tunnel header (so
                #      origin-trust can't tell trusted-LAN from remote) and all
                #      clients already hold keys (e.g. a Tailscale-only server).
                #   2. TUNNEL_REQUIRES_KEY — reject keyless traffic detected as
                #      tunnel-origin (CF-Connecting-IP present).
                # Otherwise keyless → agent tier (LAN/local soft-fallback).
                from shared_memory.auth import REQUIRE_KEY, TUNNEL_REQUIRES_KEY, get_via_tunnel
                if REQUIRE_KEY or (TUNNEL_REQUIRES_KEY and get_via_tunnel()):
                    _reject_reason = "no_api_key_required" if REQUIRE_KEY else "no_api_key_via_tunnel"
                    try:
                        from shared_memory.audit import log_audit
                        log_audit("auth.keyless_rejected", claude_instance, project,
                                  {"reason": _reject_reason})
                    except Exception:
                        pass
                    return json.dumps({
                        "error": (
                            "This server requires an API key. Provide an "
                            "'Authorization: Bearer <key>' header (or pass api_key). "
                            "Contact the operator for a key."
                        ),
                        "auth_required": True,
                    })
                # LAN/local keyless → agent tier (logged so we see who's still missing keys).
                print(f"[MCP] soft-auth: unauthenticated LAN/local session {claude_instance}@{project} → agent tier")
                try:
                    from shared_memory.audit import log_audit
                    log_audit("auth.soft_fallback", claude_instance, project,
                              {"reason": "no_api_key", "via_tunnel": False})
                except Exception:
                    pass
    except ImportError:
        pass  # auth module not available, continue without auth

    # Normalize project name (single source of truth: helpers.normalize_project)
    normalized_project = normalize_project(project)
    project = normalized_project

    # ── Rename-alias redirect ──
    # If the (project, agent) being requested has an active rename alias,
    # transparently redirect to the new identity and surface a warning so
    # operators see they're using a stale name. Aliases auto-expire after 30d.
    _rename_redirect_warning = None
    try:
        db_for_alias = get_mongo()
        if db_for_alias is not None:
            from shared_memory.tools.rename import resolve_agent_alias
            redirect = resolve_agent_alias(db_for_alias, normalized_project, claude_instance)
            if redirect:
                new_proj, new_agent, alias_doc = redirect
                _rename_redirect_warning = (
                    f"Rename alias active: '{claude_instance}@{normalized_project}' "
                    f"redirected to '{new_agent}@{new_proj}'. "
                    f"Update CLAUDE.md before alias expiry "
                    f"({alias_doc.get('expires_at')})."
                )
                normalized_project = new_proj
                project = new_proj
                claude_instance = new_agent
                try:
                    from shared_memory.audit import log_audit
                    log_audit("rename.alias_redirect", claude_instance, project, {
                        "alias_id": alias_doc.get("_id") if isinstance(alias_doc, dict) else None,
                        "from_project": alias_doc.get("from_project") if isinstance(alias_doc, dict) else None,
                        "from_agent": alias_doc.get("from_agent") if isinstance(alias_doc, dict) else None,
                    })
                except Exception:
                    pass
    except Exception:
        pass  # Best-effort; never block session start on alias check

    # ── Registry awareness ──
    _needs_role_description = False
    _registry_warning = None
    _identity_suggestion = None
    _is_worker = False

    try:
        db = get_mongo()
        if db is not None:
            registered_project = db.projects.find_one({"name": normalized_project})

            # Path-to-identity: if instance is unknown and working_directory provided,
            # try to match against registered path patterns
            if claude_instance == "unknown" and working_directory and registered_project:
                agents = list(db.registered_agents.find(
                    {"project": normalized_project, "path_patterns": {"$ne": []}},
                    {"name": 1, "path_patterns": 1}
                ))
                for agent in agents:
                    if _match_path_patterns(working_directory, agent.get("path_patterns", [])):
                        _identity_suggestion = agent["name"]
                        break

            # Check if agent is registered (for named agents)
            if registered_project and claude_instance != "unknown":
                registered_agent = db.registered_agents.find_one({
                    "project": normalized_project,
                    "name": claude_instance
                })
                if registered_agent:
                    # Update last_seen on the registered agent
                    db.registered_agents.update_one(
                        {"project": normalized_project, "name": claude_instance},
                        {"$set": {"last_seen": utc_now()}, "$inc": {"session_count": 1}}
                    )
                else:
                    # Agent not registered in project registry
                    if claude_instance.startswith("worker_") or spawned_by:
                        # Worker self-registration - limited capabilities
                        _is_worker = True
                    else:
                        # Auto-register as "pending" tier — full tool access,
                        # coordinator gets notified to approve
                        db.registered_agents.update_one(
                            {"project": normalized_project, "name": claude_instance},
                            {"$set": {
                                "project": normalized_project,
                                "name": claude_instance,
                                "tier": "pending",
                                "last_seen": utc_now(),
                                "auto_registered": True,
                            }, "$inc": {"session_count": 1}},
                            upsert=True
                        )
                        valid_agents = [a["name"] for a in db.registered_agents.find(
                            {"project": normalized_project}, {"name": 1}
                        )]
                        _registry_warning = (
                            f"Agent '{claude_instance}' auto-registered in project '{normalized_project}' "
                            f"with 'pending' tier (full tool access). A coordinator should confirm with: "
                            f"memory_project(action='update_agent', name='{normalized_project}', "
                            f"agent='{claude_instance}', tier='named'). "
                            f"Other agents: {', '.join(a for a in valid_agents if a != claude_instance) or 'none'}"
                        )
                        # Notify coordinator if one exists
                        try:
                            coordinator = db.registered_agents.find_one({
                                "project": normalized_project, "tier": "admin"
                            })
                            if coordinator:
                                db.messages.insert_one({
                                    "_id": f"msg_{uuid.uuid4().hex[:12]}",
                                    "from": "system",
                                    "from_project": normalized_project,
                                    "to_instance": coordinator["name"],
                                    "to_project": normalized_project,
                                    "message": (
                                        f"New agent '{claude_instance}' auto-registered on "
                                        f"project '{normalized_project}' with pending tier. "
                                        f"Approve with: memory_project(action='update_agent', "
                                        f"name='{normalized_project}', agent='{claude_instance}', tier='named')"
                                    ),
                                    "priority": "normal",
                                    "category": "info",
                                    "status": "pending",
                                    "created_at": utc_now(),
                                })
                        except Exception:
                            pass  # Non-fatal if notification fails

            # Worker self-registration
            if _is_worker or spawned_by:
                _is_worker = True
                if not claude_instance.startswith("worker_"):
                    claude_instance = f"worker_{uuid.uuid4().hex[:4]}"

            # Auto-register in agent directory (activity tracking, separate from registry)
            update_fields = {
                "last_seen": utc_now(),
                "last_task": task_description or "",
            }
            if tmux_target:
                update_fields["tmux_target"] = tmux_target
            if role_description:
                update_fields["role_description"] = role_description
            if spawned_by:
                update_fields["spawned_by"] = spawned_by
            # Component subscriptions (design:unified-messaging-v0 Stage 1).
            # Normalize to a clean list of non-empty strings; an explicit empty
            # list clears the subscription (so an agent that moves off a
            # component drops out of its peer list). None = don't touch.
            if subscribed_components is not None:
                seen_comps: set = set()
                clean_comps = []
                for c in subscribed_components:
                    s = (c or "").strip()
                    if s and s not in seen_comps:
                        seen_comps.add(s)
                        clean_comps.append(s)
                update_fields["subscribed_components"] = clean_comps

            insert_defaults = {"first_seen": utc_now()}
            if not role_description:
                insert_defaults["role_description"] = ""

            db.agent_directory.update_one(
                {"project": normalized_project, "instance": claude_instance},
                {
                    "$set": update_fields,
                    "$inc": {"session_count": 1},
                    "$setOnInsert": insert_defaults
                },
                upsert=True
            )

            # Self-declared recipient aliases (e.g. a lead claiming
            # "coordinator"). Persisted on registered_agents — the collection
            # send-validation resolves against. Uniqueness-checked per project.
            if aliases is not None and not _is_worker:
                from shared_memory.tools.projects import persist_agent_aliases
                _alias_result = persist_agent_aliases(
                    db, normalized_project, claude_instance, aliases
                )
                if _alias_result["rejected"]:
                    _rej = "; ".join(
                        f"'{k}' ({v})" for k, v in _alias_result["rejected"].items()
                    )
                    _alias_msg = f"Alias(es) rejected (must be unique per project): {_rej}."
                    _registry_warning = (
                        f"{_registry_warning} {_alias_msg}" if _registry_warning
                        else _alias_msg
                    )

            # Check if agent still needs a role_description
            if not role_description and not _is_worker:
                existing = db.agent_directory.find_one(
                    {"project": normalized_project, "instance": claude_instance}
                )
                if existing and not existing.get("role_description"):
                    _needs_role_description = True
    except Exception as e:
        print(f"[MCP] Agent directory/registry check failed (non-fatal): {e}")

    chroma = await get_chroma()

    # Generate session ID
    session_id = f"{project}_{claude_instance}_{uuid.uuid4().hex[:8]}"

    # Register session
    active_sessions[session_id] = {
        "project": project,
        "claude_instance": claude_instance,
        "task": task_description,
        "started": utc_now_iso(),
        "last_activity": utc_now_iso(),
        "blocked_by": None,
        "blocked_reason": None,
        "waiting_for_signal": None,
        "tmux_target": tmux_target,
        "role": _auth_role,
        "allowed_projects": _auth_projects,
    }

    # Phase C2 inbox auth: bind this app-session to the underlying MCP
    # transport so resource handlers can resolve the caller's identity from
    # mcp._mcp_server.request_context.session.
    try:
        from shared_memory.state import mcp_session_to_app
        if ctx is not None and getattr(ctx, "session", None) is not None:
            mcp_session_to_app[ctx.session] = session_id
    except Exception:
        pass  # binding is best-effort; auth handlers fall back to "unknown"

    # design:push-control-v0 §3 — memory_start_session for this agent opens
    # the recency window. This unblocks the read-side push-suppression
    # filter so suppressed messages surface on the channel-poll path while
    # the agent is plausibly being supervised.
    try:
        db_for_recency = get_mongo()
        if db_for_recency is not None:
            db_for_recency.agent_directory.update_one(
                {"project": project, "instance": claude_instance},
                {"$set": {"last_human_interaction": utc_now_iso(), "last_seen": utc_now_iso()}},
                upsert=True,
            )
    except Exception:
        pass  # recency bump is best-effort; do not fail session start

    # ── Gather context into local variables, assemble dict at the end ──
    # Field order in the returned JSON is intentionally stable→dynamic for
    # prompt-caching friendliness (backlog_c83b0591babd). Anthropic prefix
    # caching keys on byte-identical prompt prefixes, so big stable blocks
    # (guidelines, tip) go first; the unique session_id and changing per-
    # call queries go last. Pure server-side change, no client API change.

    # Recent learnings (DYNAMIC — task-driven query)
    _learnings_titles = None
    try:
        proj_collection = await get_project_collection(chroma, project)
        recent_learnings = await proj_collection.query(
            query_texts=[task_description or "recent learnings"],
            n_results=3,
            where={"type": {"$in": ["learning", "gotcha", "handoff"]}}
        )
        if recent_learnings["documents"] and recent_learnings["documents"][0]:
            _titles = [meta.get("title") for meta in recent_learnings["metadatas"][0]]
            if _titles:
                _learnings_titles = _titles
    except Exception:
        pass

    # Shared patterns (DYNAMIC — task-driven query)
    _patterns_titles = None
    try:
        shared = await get_shared_collection(chroma, "patterns")
        patterns_result = await shared.query(
            query_texts=[task_description or project],
            n_results=2
        )
        if patterns_result["documents"] and patterns_result["documents"][0]:
            _titles = [meta.get("title") for meta in patterns_result["metadatas"][0]]
            if _titles:
                _patterns_titles = _titles
    except Exception:
        pass

    # Scope-matched skills (DYNAMIC — design:skill-registry-v0 Phase-1 surfacing).
    # Active, scope-matched how-to procedures as {id,name,trigger} headers, so a
    # short go->do->park session sees its relevant procedures right when session-
    # start IS the moment of doing. Best-effort: never blocks session start.
    _skills_block = None
    try:
        from shared_memory.tools.skills import get_scope_matched_skills
        _matched = get_scope_matched_skills(
            project, claude_instance, role_description, working_directory
        )
        if _matched:
            _skills_block = _matched
    except Exception:
        pass

    # Fleet directives (DYNAMIC — cross-server "what you need to do" banners).
    # Active, target-matched, un-acked directives for this agent. Best-effort.
    _directives_block = None
    try:
        from shared_memory.directives import get_pending_directives
        _pending = get_pending_directives(get_mongo(), normalized_project, claude_instance)
        if _pending:
            _directives_block = _pending
    except Exception:
        pass

    # Active work by other Claudes (DYNAMIC)
    _other_active = []
    for sid, info in active_sessions.items():
        if sid != session_id:
            _other_active.append(f"{info['claude_instance']}@{info['project']}: {info['task'][:50]}")
            if len(_other_active) >= 20:
                break

    # Component peers (design:unified-messaging-v0 Stage 1 — ADDRESSING):
    # who else is recently active in any of MY subscribed components?
    _component_peers = _active_component_peers(
        get_mongo(), normalized_project, claude_instance, subscribed_components
    )

    # File locks, mods, signals, blocking, interface updates (DYNAMIC)
    _relevant_locks = get_relevant_locks_for_session(session_id, project)
    _recent_mods = await get_recent_modifications(chroma, project, session_id)
    _signals = get_pending_signals(claude_instance)
    _blocking = get_blocking_others(claude_instance)
    _interface_updates = await get_interface_updates(chroma, project)

    # Server-managed guidelines (STABLE per-project — biggest stable block)
    _guidelines_block = None
    try:
        from shared_memory.tools.guidelines import get_guidelines_for_session
        guidelines = get_guidelines_for_session(project)
        if guidelines:
            _guidelines_block = {
                "instructions": "MANDATORY: Follow these rules for the entire session. They are authoritative.",
                "rules": [g["rule"] for g in guidelines],
            }
    except Exception as e:
        print(f"[MCP] Guidelines fetch failed (non-fatal): {e}")

    # Auth info (STABLE — server-wide flag + per-key role)
    _auth_block = None
    try:
        from shared_memory.auth import AUTH_ENABLED as _ae
        if _ae:
            _auth_block = {
                "role": _auth_role,
                "projects": _auth_projects or "all",
            }
    except ImportError:
        pass

    # Tip (STABLE — constant string)
    _tip = "New? Run memory_query(query='shared memory usage guide') for best practices and backlog tools."

    # ── Assemble output: stable fields FIRST, dynamic LAST ──
    output: Dict[str, Any] = {}

    # 1. STABLE always-present: guidelines (big), tip (constant)
    if _guidelines_block is not None:
        output["guidelines"] = _guidelines_block
    output["tip"] = _tip
    # notable_tools: curated, byte-stable (see NOTABLE_TOOLS). Stays in the
    # stable block so it doesn't disturb prefix caching.
    output["notable_tools"] = NOTABLE_TOOLS

    # 2. STABLE conditional but stable when present: auth, project
    if _auth_block is not None:
        output["auth"] = _auth_block
    output["project"] = project

    # 3. ONE-SHOT stable (present in first few sessions, then absent):
    # rename_redirect, identity_suggestion, registry_warning, worker, action_needed.
    # Grouped after the always-stable block since their value text doesn't
    # change once present, even though presence itself can change.
    if _rename_redirect_warning:
        output["rename_redirect"] = _rename_redirect_warning
    if _identity_suggestion:
        output["identity_suggestion"] = {
            "suggested_name": _identity_suggestion,
            "reason": f"Your working directory matches the path pattern for '{_identity_suggestion}'. "
                      f"Call memory_start_session again with claude_instance='{_identity_suggestion}' to use this identity."
        }
    if _registry_warning:
        output["registry_warning"] = _registry_warning
    if _is_worker:
        output["worker"] = {
            "auto_id": claude_instance,
            "spawned_by": spawned_by or "unknown",
            "note": "You are a worker agent. You can add backlog items and learnings but cannot receive messages. "
                    "Your session will auto-expire."
        }
    if _needs_role_description:
        output["action_needed"] = (
            "You have no role_description in the agent directory. "
            "Other agents cannot discover what you do. Please call "
            "memory_start_session again with role_description='brief description of your role and capabilities' "
            "or ask the user what your role should be."
        )

    # 4. DYNAMIC: session_id (unique per call) and all per-call query results.
    output["session_id"] = session_id
    # Directives lead the dynamic block — they're must-action notices that
    # persist until the agent acks them with memory_ack_directive.
    if _directives_block:
        output["directives"] = _directives_block
    if _learnings_titles:
        output["learnings"] = _learnings_titles
    if _skills_block:
        output["skills"] = _skills_block
    if _patterns_titles:
        output["patterns"] = _patterns_titles
    if _other_active:
        output["other_claudes"] = _other_active
    if _component_peers:
        output["component_peers"] = _component_peers
    if _relevant_locks:
        output["relevant_locks"] = _relevant_locks
    if _recent_mods:
        output["recent_modifications"] = _recent_mods
    if _signals:
        output["signals"] = _signals
    if _blocking:
        output["blocking_others"] = _blocking
    if _interface_updates:
        output["interface_updates"] = _interface_updates

    # Audit log (side effect, not in output)
    try:
        from shared_memory.audit import log_audit
        log_audit("session.start", claude_instance, project,
                  {"task": task_description, "worker": _is_worker, "role": _auth_role},
                  session_id)
    except Exception:
        pass

    return json.dumps(output)


@mcp.tool()
async def memory_end_session(
    session_id: str,
    summary: str,
    files_modified: List[str] = None,
    learnings: str = None,
    handoff_notes: str = None,
    ctx: Context = None
) -> str:
    """
    CALL THIS WHEN DONE - Records your work and cleans up session.

    This stores:
    - Summary of what you accomplished (as handoff for next Claude)
    - Files you modified (for overlap detection)
    - Any learnings you want to share

    Args:
        session_id: Your session ID from memory_start_session
        summary: Summary of what you accomplished
        files_modified: List of files you modified
        learnings: Any learnings worth recording for other Claudes
        handoff_notes: Notes for the next Claude who works on this
    """
    error = require_session(session_id)
    if error:
        return error

    files_modified = files_modified or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Store handoff note
    proj_collection = await get_project_collection(chroma, session_info["project"])

    handoff_content = f"""## Session Summary
{summary}

## Files Modified
{chr(10).join('- ' + f for f in files_modified) if files_modified else 'None recorded'}

## Handoff Notes
{handoff_notes or 'None'}

## Session Info
- Claude: {session_info['claude_instance']}
- Started: {session_info['started']}
- Ended: {now}
"""

    handoff_id = f"handoff_{session_id}"
    await proj_collection.upsert(
        ids=[handoff_id],
        documents=[handoff_content],
        metadatas=[{
            "title": f"Handoff: {session_info['task'][:50]}",
            "type": "handoff",
            "status": "active",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "files_modified": json.dumps(files_modified),
            "created": now,
            "updated": now
        }]
    )

    # Store learning if provided
    if learnings:
        learning_id = generate_doc_id(learnings, "learning")
        await proj_collection.add(
            ids=[learning_id],
            documents=[learnings],
            metadatas=[{
                "title": f"Learning from {session_info['claude_instance']}",
                "type": "learning",
                "status": "active",
                "session_id": session_id,
                "created": now,
                "updated": now
            }]
        )

    # Update work item to completed
    work_collection = await get_shared_collection(chroma, "work")
    work_id = f"work_{session_id}"
    await work_collection.upsert(
        ids=[work_id],
        documents=[summary],
        metadatas=[{
            "title": session_info["task"][:100],
            "status": "completed",
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "project": session_info["project"],
            "files_touched": json.dumps(files_modified),
            "created": session_info["started"],
            "updated": now
        }]
    )

    # Auto-release any file locks held by this session
    released_locks = release_session_locks(session_id)

    # Phase C2: drop the MCP-session ↔ app-session binding and any inbox
    # subscriptions held by those transports. Without this, a client that
    # ends its session without explicitly unsubscribing would keep receiving
    # ResourceUpdated notifications until its transport actually dies — and
    # any follow-up resource read would fail authz because the binding above
    # is already gone.
    try:
        from shared_memory.state import mcp_session_to_app
        from shared_memory.tools.messaging import inbox_subscriptions
        dropped_transports = [k for k, v in mcp_session_to_app.items() if v == session_id]
        for k in dropped_transports:
            mcp_session_to_app.pop(k, None)
        if dropped_transports:
            for uri in list(inbox_subscriptions.keys()):
                bucket = inbox_subscriptions.get(uri)
                if bucket is None:
                    continue
                for s in dropped_transports:
                    bucket.discard(s)
                if not bucket:
                    inbox_subscriptions.pop(uri, None)
    except Exception:
        pass

    # Remove from active sessions
    del active_sessions[session_id]

    result = {"status": "ended"}
    if released_locks:
        result["released_locks"] = released_locks

    return json.dumps(result)
