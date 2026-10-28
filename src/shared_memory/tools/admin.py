"""Admin tools - API key management and audit log access."""

import json
from datetime import datetime

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.auth import (
    AUTH_ENABLED,
    ROLES,
    create_api_key,
    list_api_keys,
    require_auth,
    revoke_api_key,
)
from shared_memory.helpers import require_session
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_admin(
    session_id: str,
    action: str,
    name: str = None,
    role: str = "agent",
    projects: list = None,
    limit: int = 50,
    event_type: str = None,
    from_project: str = None,
    from_agent: str = None,
    to_project: str = None,
    to_agent: str = None,
    dry_run: bool = True,
    alias_type: str = None,
    # Push-control admin args
    project: str = None,
    key: str = None,
    value: object = None,
    alert_id: str = None,
    agent: str = None,
    reason: str = None,
    ctx: Context = None,
) -> str:
    """
    Admin operations: manage API keys, view audit logs, rename agents/projects.

    When auth is enabled:
      - Read-only actions (auth_status, list_keys, list_aliases, audit_log,
        push_control_get_config) require admin or owner.
      - Mutation actions (create_key, revoke_key, rename_agent, rename_project,
        push_control_set_config, push_control_reset_config,
        push_control_ack_alert, push_control_unsuspend_agent) require owner.

    Actions:
        create_key   - Create a new API key (requires name, optional role + projects)
        revoke_key   - Revoke an API key by name
        list_keys    - List all active API keys
        audit_log    - View recent audit log entries (optional event_type filter)
        auth_status  - Check if auth is enabled and current session's role
        rename_agent - Rename an agent across all stores. Args: from_project,
                       from_agent, to_project, to_agent, dry_run (default True).
                       Default dry_run reports impact counts without writing.
        rename_project - Rename a whole project. Args: from_project, to_project,
                       dry_run (default True).
        list_aliases - List active rename aliases. Args: alias_type=agent|project|None.
        push_control_get_config - Read effective push-control config for one
                       project (default+override) or all scopes. Args: project=None
                       returns server default + all per-project overrides; project=<name>
                       returns the effective merged config for that project.
        push_control_set_config - Upsert one config key. Args: project=None for
                       server default, project=<name> for per-project override;
                       key (depth_cap|push_budget|hard_ceiling|recovery_behavior|
                       incident_pad_messages|incident_pad_seconds|webhook_url|
                       webhook_token); value.
        push_control_reset_config - Drop a per-project override. Args: project
                       (required); key=None drops ALL overrides for that project.
        push_control_ack_alert - Mark an alert acknowledged (operator has seen it).
                       Args: alert_id. Note: ack does NOT unsuspend the agent —
                       use push_control_unsuspend_agent for that.
        push_control_unsuspend_agent - Lift suspension on an agent so its pushes
                       resume. Args: project, agent, reason (optional).
        query_get_config - Read effective memory_query defaults. Args: project=None
                       returns server default + all per-project overrides; project=<name>
                       returns the effective merged config for that project.
        query_set_config - Upsert one query default key. Args: project=None for
                       server default, project=<name> for per-project override;
                       key (default_expand|default_expand_top|default_snippet_length);
                       value.
        query_reset_config - Drop a per-project override. Args: project (required);
                       key=None drops ALL overrides for that project.
        drain        - Owner-only. value=True/False toggles the drain gate; value=None
                       reads current state. When draining, memory_start_session refuses
                       new sessions (existing continue). Resets on process restart.
                       Args: value (bool|None), reason (optional explanatory note).
        broadcast_restart_warning - Owner-only. Fans out a system@junto restart-warning
                       notice to every active agent (deduped by instance+project).
                       Notice is push-enabled. Args:
                         value={"seconds_until": <0-3600>, "reason": <optional str>}
                         reason (top-level arg also accepted as fallback).

    Args:
        session_id: Your session ID
        action: see Actions above
        name: Key name (for create_key/revoke_key)
        role: Role for new key (owner, admin, agent, readonly). Default: agent
        projects: List of project names the key can access (empty = all projects)
        limit: Max entries for audit_log (default 50)
        event_type: Filter audit_log by event type (e.g., "auth.login", "spec.created")
        from_project / from_agent: source identity for rename_agent / rename_project
        to_project / to_agent: target identity
        dry_run: rename actions default to dry_run=True; pass dry_run=False to commit
        alias_type: filter for list_aliases ("agent" or "project")
        project: target project for push_control_* actions (None = server default)
        key: config key name for push_control_set_config / push_control_reset_config
        value: config value for push_control_set_config
        alert_id: alert ID for push_control_ack_alert
        agent: agent name for push_control_unsuspend_agent
        reason: explanatory note for push_control_unsuspend_agent (audit log)
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]

    # Read-tier gate: any admin or owner may enter the admin tool. Mutation
    # actions re-check below against admin.write (owner-only).
    auth_error = require_auth(session_info, "admin")
    if auth_error:
        return json.dumps({"error": auth_error})

    write_actions = {
        "create_key", "revoke_key", "rename_agent", "rename_project",
        "push_control_set_config", "push_control_reset_config",
        "push_control_ack_alert", "push_control_unsuspend_agent",
        "query_set_config", "query_reset_config",
        "drain", "broadcast_restart_warning",
    }
    if action in write_actions:
        # Rename dry-run delegation (backlog_cf11cec31a94): project admins may
        # run rename_agent DRY-RUN for their OWN project (read-shaped impact
        # counts — converts "coordinator wants a rename" into a scoped request).
        # Commit (dry_run=False) and cross-project renames stay owner-only:
        # the host-side wiring a rename requires cannot be migrated server-side
        # and its failure mode is 30-day-delayed (alias expiry).
        _delegated_dry_run = False
        if action == "rename_agent" and dry_run:
            try:
                from shared_memory.clients import get_mongo as _gm
                _db = _gm()
                _caller = session_info.get("claude_instance", "")
                _fp = (from_project or "").lower().replace("-", "_").replace(" ", "_")
                _tp = (to_project or from_project or "").lower().replace("-", "_").replace(" ", "_")
                if _db is not None and _fp and _fp == _tp:
                    _proj = _db.projects.find_one({"name": _fp}, {"admins": 1})
                    if _proj and _caller in (_proj.get("admins") or []):
                        _delegated_dry_run = True
            except Exception:
                pass
        if not _delegated_dry_run:
            write_error = require_auth(session_info, "admin.write")
            if write_error:
                return json.dumps({"error": write_error})

    # ── Graceful-restart ops ──
    if action == "drain":
        from shared_memory.restart import is_draining, set_draining
        if value is None:
            draining, drain_reason = is_draining()
            return json.dumps({
                "status": "drain_state",
                "draining": draining,
                "reason": drain_reason,
            })
        new_state = bool(value)
        set_draining(new_state, reason=reason)
        return json.dumps({
            "status": "drain_set",
            "draining": new_state,
            "reason": reason,
        })

    if action == "broadcast_restart_warning":
        from shared_memory.restart import broadcast_restart_warning
        if not isinstance(value, dict) or "seconds_until" not in value:
            return json.dumps({
                "error": "broadcast_restart_warning requires value={'seconds_until': <int>}; "
                         "optional 'reason': <str>. The top-level reason arg is also accepted.",
            })
        try:
            seconds_until = int(value["seconds_until"])
        except (TypeError, ValueError):
            return json.dumps({"error": "value.seconds_until must be an integer"})
        if seconds_until < 0 or seconds_until > 3600:
            return json.dumps({"error": "seconds_until must be between 0 and 3600"})
        body_reason = value.get("reason") or reason
        result = await broadcast_restart_warning(
            seconds_until=seconds_until,
            reason=body_reason,
            from_project=session_info.get("project") or "junto",
        )
        return json.dumps(result)

    if action == "auth_status":
        return json.dumps({
            "auth_enabled": AUTH_ENABLED,
            "session_role": session_info.get("role", "agent" if not AUTH_ENABLED else "unknown"),
            "allowed_projects": session_info.get("allowed_projects", []),
            "available_roles": ROLES,
        }, indent=2)

    elif action == "create_key":
        if not name:
            return json.dumps({"error": "'name' is required for create_key"})
        if role not in ROLES:
            return json.dumps({"error": f"Invalid role '{role}'. Must be one of: {ROLES}"})

        try:
            raw_key, record = create_api_key(
                name=name,
                role=role,
                projects=projects,
                created_by=session_info.get("claude_instance", "unknown"),
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

        # Audit
        try:
            from shared_memory.audit import log_audit
            log_audit("admin.key_created", session_info.get("claude_instance", "unknown"),
                      "", {"key_name": name, "role": role, "projects": projects or []},
                      session_id)
        except Exception:
            pass

        return json.dumps({
            "status": "created",
            "api_key": raw_key,
            "name": record["name"],
            "role": record["role"],
            "projects": record["projects"],
            "warning": "Save this key now — it cannot be retrieved later.",
        }, indent=2)

    elif action == "revoke_key":
        if not name:
            return json.dumps({"error": "'name' is required for revoke_key"})

        success = revoke_api_key(name)

        if success:
            try:
                from shared_memory.audit import log_audit
                log_audit("admin.key_revoked", session_info.get("claude_instance", "unknown"),
                          "", {"key_name": name}, session_id)
            except Exception:
                pass

        return json.dumps({
            "status": "revoked" if success else "not_found",
            "name": name,
        })

    elif action == "list_keys":
        keys = list_api_keys()
        # Serialize datetime objects
        for k in keys:
            for field in ("created", "last_used"):
                if isinstance(k.get(field), datetime):
                    k[field] = k[field].isoformat()
                elif k.get(field) is None:
                    k[field] = ""
        return json.dumps({"keys": keys, "count": len(keys)}, indent=2)

    elif action == "audit_log":
        from shared_memory.clients import get_mongo

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        query = {}
        if event_type:
            query["event_type"] = event_type

        entries = list(
            db.audit_log.find(query, {"_id": 0})
            .sort("timestamp", -1)
            .limit(min(limit, 200))
        )

        for e in entries:
            if isinstance(e.get("timestamp"), datetime):
                e["timestamp"] = e["timestamp"].isoformat()

        return json.dumps({"entries": entries, "count": len(entries)}, indent=2)

    elif action in ("rename_agent", "rename_project", "list_aliases"):
        from shared_memory.clients import get_chroma, get_mongo
        from shared_memory.tools.rename import (
            list_aliases,
            perform_rename_agent,
            perform_rename_project,
        )

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        actor = session_info.get("claude_instance", "unknown")

        if action == "list_aliases":
            return json.dumps({
                "aliases": list_aliases(db, alias_type=alias_type),
            }, indent=2, default=str)

        chroma = await get_chroma()

        if action == "rename_agent":
            result = await perform_rename_agent(
                db, chroma,
                from_project=from_project, from_agent=from_agent,
                to_project=to_project, to_agent=to_agent,
                dry_run=dry_run, actor=actor, session_id=session_id,
            )
            return json.dumps(result, indent=2, default=str)

        # rename_project
        result = await perform_rename_project(
            db, chroma,
            from_project=from_project, to_project=to_project,
            dry_run=dry_run, actor=actor, session_id=session_id,
        )
        return json.dumps(result, indent=2, default=str)

    elif action in (
        "query_get_config", "query_set_config", "query_reset_config",
    ):
        from shared_memory import query_config
        from shared_memory.clients import get_mongo

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        actor = session_info.get("claude_instance", "unknown")

        if action == "query_get_config":
            if project is not None:
                return json.dumps(
                    query_config.get_effective_config(db, project),
                    indent=2, default=str,
                )
            server_default = query_config.get_effective_config(db, None)
            overrides = []
            try:
                for doc in db.query_config.find({"scope": {"$ne": query_config.DEFAULT_SCOPE}}):
                    overrides.append({
                        "scope": doc.get("scope"),
                        "project": doc.get("project"),
                        "default_expand": doc.get("default_expand"),
                        "default_expand_top": doc.get("default_expand_top"),
                        "default_snippet_length": doc.get("default_snippet_length"),
                        "updated_at": doc.get("updated_at"),
                        "updated_by": doc.get("updated_by"),
                    })
            except Exception as e:
                return json.dumps({"error": f"read overrides failed: {e}"})
            return json.dumps({
                "server_default": server_default,
                "overrides": overrides,
            }, indent=2, default=str)

        elif action == "query_set_config":
            if not key:
                return json.dumps({"error": "'key' is required for query_set_config"})
            result = query_config.set_config_value(
                db=db, project=project, key=key, value=value, actor=actor,
            )
            return json.dumps(result, indent=2, default=str)

        elif action == "query_reset_config":
            if not project:
                return json.dumps({"error": "'project' is required for query_reset_config (server default cannot be reset)"})
            result = query_config.reset_config(
                db=db, project=project, key=key, actor=actor,
            )
            return json.dumps(result, indent=2, default=str)

    elif action in (
        "push_control_get_config", "push_control_set_config",
        "push_control_reset_config", "push_control_ack_alert",
        "push_control_unsuspend_agent",
    ):
        from shared_memory import push_control
        from shared_memory.clients import get_mongo

        db = get_mongo()
        if db is None:
            return json.dumps({"error": "MongoDB not available"})

        actor = session_info.get("claude_instance", "unknown")

        if action == "push_control_get_config":
            if project is not None:
                return json.dumps(
                    push_control.get_effective_config(db, project),
                    indent=2, default=str,
                )
            # No project given — return both server default and all overrides.
            server_default = push_control.get_effective_config(db, None)
            overrides = []
            try:
                for doc in db.push_control_config.find({"scope": {"$ne": push_control.DEFAULT_SCOPE}}):
                    overrides.append({
                        "scope": doc.get("scope"),
                        "project": doc.get("project"),
                        "depth_cap": doc.get("depth_cap"),
                        "push_budget": doc.get("push_budget"),
                        "hard_ceiling": doc.get("hard_ceiling"),
                        "recovery_behavior": doc.get("recovery_behavior"),
                        "incident_pad_messages": doc.get("incident_pad_messages"),
                        "incident_pad_seconds": doc.get("incident_pad_seconds"),
                        "webhook_url": doc.get("webhook_url"),
                        # webhook_token redacted in list view
                        "webhook_token_set": bool(doc.get("webhook_token")),
                        "updated_at": doc.get("updated_at"),
                        "updated_by": doc.get("updated_by"),
                    })
            except Exception as e:
                return json.dumps({"error": f"read overrides failed: {e}"})
            # Redact webhook_token from default-view too
            sd_view = dict(server_default)
            if sd_view.get("webhook_token"):
                sd_view["webhook_token"] = "<set>"
            return json.dumps({
                "server_default": sd_view,
                "overrides": overrides,
            }, indent=2, default=str)

        elif action == "push_control_set_config":
            if not key:
                return json.dumps({"error": "'key' is required for push_control_set_config"})
            result = push_control.set_config_value(
                db=db, project=project, key=key, value=value, actor=actor,
            )
            return json.dumps(result, indent=2, default=str)

        elif action == "push_control_reset_config":
            if not project:
                return json.dumps({"error": "'project' is required for push_control_reset_config (server default cannot be reset)"})
            result = push_control.reset_config(
                db=db, project=project, key=key, actor=actor,
            )
            return json.dumps(result, indent=2, default=str)

        elif action == "push_control_ack_alert":
            if not alert_id:
                return json.dumps({"error": "'alert_id' is required for push_control_ack_alert"})
            result = push_control.acknowledge_alert(db, alert_id, actor=actor)
            return json.dumps(result, indent=2, default=str)

        elif action == "push_control_unsuspend_agent":
            if not project or not agent:
                return json.dumps({"error": "'project' and 'agent' are required for push_control_unsuspend_agent"})
            success = push_control.set_agent_suspended(
                db=db, project=project, agent=agent,
                suspended=False,
                reason=reason or f"unsuspended by {actor}",
                actor=actor,
            )
            return json.dumps({
                "ok": success,
                "project": project,
                "agent": agent,
                "reason": reason or "",
            })

    else:
        return json.dumps({
            "error": (
                f"Unknown action '{action}'. Use: create_key, revoke_key, "
                "list_keys, audit_log, auth_status, rename_agent, rename_project, "
                "list_aliases, push_control_get_config, push_control_set_config, "
                "push_control_reset_config, push_control_ack_alert, "
                "push_control_unsuspend_agent, query_get_config, "
                "query_set_config, query_reset_config"
            )
        })
