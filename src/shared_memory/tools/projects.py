"""Project and agent registry tools - manage projects and their agents."""

import json
from datetime import datetime
from typing import List, Optional

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import require_session, utc_now
from shared_memory.state import active_sessions

# Agent tiers
AGENT_TIERS = ["admin", "named", "worker"]

# Implicit admin - "human" is always admin for all projects
IMPLICIT_ADMIN = "human"


def _is_project_admin(db, project_name: str, claude_instance: str) -> bool:
    """Check if a Claude instance is an admin for a project."""
    if claude_instance == IMPLICIT_ADMIN:
        return True
    project = db.projects.find_one({"name": project_name})
    if not project:
        return False
    return claude_instance in project.get("admins", [])


def _fuzzy_match_agent(db, project_name: str, target_name: str, limit: int = 3) -> List[str]:
    """Find similar agent names for 'did you mean?' suggestions."""
    agents = db.registered_agents.find({"project": project_name}, {"name": 1})
    names = [a["name"] for a in agents]
    if not names:
        return []

    # Simple substring/prefix matching + edit distance approximation
    suggestions = []
    target_lower = target_name.lower()
    for name in names:
        name_lower = name.lower()
        # Exact substring match
        if target_lower in name_lower or name_lower in target_lower:
            suggestions.append((0, name))
            continue
        # Shared prefix
        common = 0
        for a, b in zip(target_lower, name_lower):
            if a == b:
                common += 1
            else:
                break
        if common >= 3:
            suggestions.append((1, name))
            continue
        # Character overlap ratio
        overlap = len(set(target_lower) & set(name_lower)) / max(len(set(target_lower) | set(name_lower)), 1)
        if overlap > 0.5:
            suggestions.append((2, name))

    suggestions.sort()
    return [name for _, name in suggestions[:limit]]


def resolve_agent_name(db, project_name: str, target_name: str) -> Optional[str]:
    """Resolve a recipient name to a canonical registered-agent name.

    Returns the canonical `name` if `target_name` is either:
      1. a live registered_agents.name in the project (returned as-is), or
      2. listed in some agent's `aliases` array in the project (returns that
         agent's canonical name, e.g. "coordinator" -> "emailTriage").
    Returns None if neither — i.e. a genuinely unknown recipient, which the
    send-path treats as fail-loud. Broadcast ("*") is handled by the caller
    and must never be passed here.
    """
    if db is None or not target_name:
        return None
    # Retired identities (identity-lifecycle Mechanism C) are terminal and
    # NOT addressable — excluded here so a send to a retired name fails loud
    # like any unknown recipient instead of queueing mail nobody will read.
    direct = db.registered_agents.find_one(
        {"project": project_name, "name": target_name,
         "tier": {"$ne": "retired"}}, {"name": 1}
    )
    if direct:
        return direct["name"]
    aliased = db.registered_agents.find_one(
        {"project": project_name, "aliases": target_name,
         "tier": {"$ne": "retired"}}, {"name": 1}
    )
    if aliased:
        return aliased["name"]
    return None


def persist_agent_aliases(db, project_name: str, agent_name: str,
                          aliases: List[str]) -> dict:
    """Set the `aliases` list on (project, agent_name)'s registered_agents doc.

    Enforces per-project uniqueness: an alias is REJECTED if it collides with
    any other agent's canonical name or with another agent's existing alias in
    the same project — an alias must never shadow a live agent or another
    alias (otherwise send-resolution would be ambiguous). A self-referential
    alias (alias == own name) is silently dropped.

    Returns {"accepted": [...], "rejected": {alias: reason}}. Updates an
    EXISTING registered_agents doc only (upsert=False); if the agent has no
    such doc, nothing is written and every alias lands in `rejected`.
    """
    accepted: List[str] = []
    rejected: dict = {}
    seen = set()
    for raw in (aliases or []):
        alias = (raw or "").strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        if alias == agent_name:
            continue  # no self-alias needed
        if db.registered_agents.find_one(
            {"project": project_name, "name": alias}, {"_id": 1}
        ):
            rejected[alias] = f"collides with live agent '{alias}'"
            continue
        other = db.registered_agents.find_one(
            {"project": project_name, "aliases": alias,
             "name": {"$ne": agent_name}}, {"name": 1}
        )
        if other:
            rejected[alias] = f"already an alias of '{other['name']}'"
            continue
        accepted.append(alias)
    result = db.registered_agents.update_one(
        {"project": project_name, "name": agent_name},
        {"$set": {"aliases": accepted}},
    )
    if result.matched_count == 0:
        for alias in accepted:
            rejected[alias] = "agent not registered — no doc to update"
        accepted = []
    return {"accepted": accepted, "rejected": rejected}


@mcp.tool()
async def memory_project(
    session_id: str,
    action: str,
    name: str = None,
    display_name: str = None,
    agent: str = None,
    role_description: str = None,
    tier: str = "named",
    path_patterns: List[str] = None,
    owner: str = None,
    sunset: str = None,
    purpose: str = None,
    spec_name: str = None,
    force: bool = False,
    aliases: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Manage the project & agent registry.

    Controls which projects and named agents exist. Used for message validation,
    identity resolution, and agent discovery.

    Actions:

    action="create" - Create a new project (any Claude can bootstrap)
        Required: name
        Optional: display_name
        "coordinator" is included in the admins LIST but NO roster row is
        manufactured for it — a real coordinator auto-registers on its first
        session (identity-lifecycle Mechanism B; never-sessioned rows were a
        message-swallow hazard)

    action="set_owner" - Set the discoverable OWNER of a project (admin only)
        Required: name (project), owner (agent name to contact for cross-project
        access, permission grants, ownership/governance decisions)

    action="get" - View project details and all registered agents
        Required: name

    action="list" - List all registered projects

    action="delete" - Delete a project (human/admin only)
        Required: name

    action="add_agent" - Register a named agent in a project (admin/coordinator only)
        Required: name (project), agent (agent name)
        Optional: role_description, tier (admin/named, default: named), path_patterns

    action="remove_agent" - Remove an agent from a project (admin/coordinator only)
        Required: name (project), agent (agent name)

    action="decommission" - Retire a finished temp agent (admin only; identity-
        lifecycle Mechanism C). Revokes its '<agent>'/'<agent>-*' API keys,
        archives its state spec, flips tier to terminal 'retired' (cannot
        session, cannot receive messages, gone from roster/standup/active-work),
        drops its directory rows. ARTIFACTS ARE PRESERVED. Refused while the
        agent has a live session. Un-retire deliberately via update_agent
        tier='named'.
        Required: name (project), agent (agent name)

    action="transfer_spec" - Reassign a spec's owner (admin/coordinator only).
        Required: name (project), spec_name, owner (the NEW owner).
        Works on dead/retired owners; captures the outgoing owner's
        role_description/last_task as provenance at transfer time; audit-logged.
        This — not define_spec's owner param, which declares who YOU are —
        is how ownership moves.

    action="update_agent" - Update agent details (admin/coordinator only)
        Optional: aliases (list — same collision-checked persist path as
        start_session's self-declared aliases; None=untouched, []=clear).
        decommission also accepts force=True to proceed while the agent
        still owns non-state specs (freezes the estate knowingly).
        Required: name (project), agent (agent name)
        Optional: role_description, tier, path_patterns

    Args:
        session_id: Your session ID
        action: One of: create, get, list, delete, add_agent, remove_agent, update_agent
        name: Project name (e.g., "nimbus", "emailtriage")
        display_name: Human-readable project name (for create)
        agent: Agent name (for add_agent, remove_agent, update_agent)
        role_description: What this agent does (for add_agent, update_agent)
        tier: Agent tier - "admin" or "named" (default: named). Workers self-register.
        path_patterns: Working directory patterns for auto-identity (e.g., ["*/picFrameWeb*"])
        owner: Project owner agent (for create / set_owner) — the discoverable
            contact for cross-project access, permission, and governance needs.
        sunset: For add_agent/update_agent on temp agents — a date or milestone
            string marking when the agent is expected to retire (e.g.
            "2026-06-30" or "post-launch"). Tracking only; decommission is a
            separate action (see decommission).
        purpose: For add_agent/update_agent — a short why-this-agent-exists note,
            useful for temp agents whose reason for being is time-bounded.
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    caller = session_info.get("claude_instance", "unknown")
    caller_role = session_info.get("role", "agent")

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    def _caller_is_admin(pn: str) -> bool:
        # Owner/admin ROLE bypasses the per-project admins list (Tom-approved
        # 2026-07-12): the auth matrix already trusts these roles with
        # admin.write (key minting, renames), so name-scoping roster actions
        # below that was an inconsistency, not a boundary. Name-listed admins
        # (and IMPLICIT_ADMIN) continue to work for agent-tier sessions.
        return caller_role in ("admin", "owner") or _is_project_admin(db, pn, caller)

    now = utc_now()

    # -- CREATE --
    if action == "create":
        if not name:
            return json.dumps({"error": "name required for create"})

        # Normalize project name
        project_name = name.lower().replace("-", "_").replace(" ", "_")

        existing = db.projects.find_one({"name": project_name})
        if existing:
            return json.dumps({"error": f"Project '{project_name}' already exists"})

        # Create project - human is always admin; "coordinator" stays in the
        # admins LIST (admin powers apply if/when a real coordinator opens a
        # session and registers), but NO roster row is manufactured for it.
        # A never-sessioned coordinator row is a message-swallow hazard:
        # resolve_agent_name resolves any registered name regardless of
        # liveness (design:identity-lifecycle-v0 Mechanism B / Flag 3,
        # learning_b965ce5dd60f713c). A real coordinator auto-registers on
        # its first memory_start_session like any other agent.
        admins = [IMPLICIT_ADMIN, "coordinator"]
        if caller not in admins:
            admins.append(caller)

        db.projects.insert_one({
            "name": project_name,
            "display_name": display_name or name,
            "admins": admins,
            "owner": owner,
            "created_by": caller,
            "created_at": now,
            "updated_at": now
        })

        return json.dumps({
            "status": "created",
            "project": project_name,
            "display_name": display_name or name,
            "admins": admins,
            "owner": owner,
            "auto_registered": []
        }, indent=2)

    # -- SET_OWNER --
    elif action == "set_owner":
        if not name or not owner:
            return json.dumps({"error": "name (project) and owner required for set_owner"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can set the owner. You are '{caller}'."})

        result = db.projects.update_one(
            {"name": project_name},
            {"$set": {"owner": owner, "updated_at": now}}
        )
        if result.matched_count == 0:
            return json.dumps({"error": f"Project '{project_name}' not found"})

        return json.dumps({
            "status": "owner_set",
            "project": project_name,
            "owner": owner
        })

    # -- GET --
    elif action == "get":
        if not name:
            return json.dumps({"error": "name required for get"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")
        project = db.projects.find_one({"name": project_name})
        if not project:
            return json.dumps({"error": f"Project '{project_name}' not found"})

        # Get all registered agents
        agents = list(db.registered_agents.find(
            {"project": project_name},
            {"_id": 0, "project": 0}
        ).sort("name", 1))

        # Format all datetime fields in agent docs
        for a in agents:
            for key, val in list(a.items()):
                if isinstance(val, datetime):
                    a[key] = val.isoformat()

        return json.dumps({
            "project": project_name,
            "display_name": project.get("display_name", project_name),
            "admins": project.get("admins", []),
            "owner": project.get("owner"),
            "created_by": project.get("created_by"),
            "agent_count": len(agents),
            "agents": agents
        }, indent=2)

    # -- LIST --
    elif action == "list":
        projects = list(db.projects.find().sort("name", 1))
        results = []
        for proj in projects:
            agent_count = db.registered_agents.count_documents({"project": proj["name"]})
            results.append({
                "name": proj["name"],
                "display_name": proj.get("display_name", proj["name"]),
                "admins": proj.get("admins", []),
                "owner": proj.get("owner"),
                "agent_count": agent_count,
                "created_at": proj["created_at"].isoformat() if proj.get("created_at") else None
            })

        return json.dumps({
            "count": len(results),
            "projects": results
        }, indent=2)

    # -- DELETE --
    elif action == "delete":
        if not name:
            return json.dumps({"error": "name required for delete"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        # Only human or project admins can delete
        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can delete projects. You are '{caller}'."})

        result = db.projects.delete_one({"name": project_name})
        if result.deleted_count == 0:
            return json.dumps({"error": f"Project '{project_name}' not found"})

        # Also remove all registered agents for this project
        agent_result = db.registered_agents.delete_many({"project": project_name})

        return json.dumps({
            "status": "deleted",
            "project": project_name,
            "agents_removed": agent_result.deleted_count
        })

    # -- ADD_AGENT --
    elif action == "add_agent":
        if not name or not agent:
            return json.dumps({"error": "name (project) and agent required for add_agent"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        # Verify project exists
        project = db.projects.find_one({"name": project_name})
        if not project:
            return json.dumps({"error": f"Project '{project_name}' not found. Create it first with action='create'."})

        # Admin check
        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can add agents. You are '{caller}'. Admins: {project.get('admins', [])}"})

        # Validate tier
        if tier not in ["admin", "named"]:
            return json.dumps({"error": f"Invalid tier '{tier}'. Must be 'admin' or 'named'. Workers self-register."})

        # Check if already exists
        existing = db.registered_agents.find_one({"project": project_name, "name": agent})
        if existing:
            return json.dumps({"error": f"Agent '{agent}' already registered in {project_name}. Use action='update_agent' to modify."})

        agent_doc = {
            "project": project_name,
            "name": agent,
            "tier": tier,
            "role_description": role_description or "",
            "path_patterns": path_patterns or [],
            "sunset": sunset,
            "purpose": purpose or "",
            "created_by": caller,
            "created_at": now,
            "last_seen": None,
            "session_count": 0
        }

        db.registered_agents.insert_one(agent_doc)

        # If tier is admin, also add to project admins list
        if tier == "admin" and agent not in project.get("admins", []):
            db.projects.update_one(
                {"name": project_name},
                {"$addToSet": {"admins": agent}, "$set": {"updated_at": now}}
            )

        return json.dumps({
            "status": "registered",
            "project": project_name,
            "agent": agent,
            "tier": tier,
            "role_description": role_description or "",
            "path_patterns": path_patterns or [],
            "sunset": sunset,
            "purpose": purpose or ""
        }, indent=2)

    # -- REMOVE_AGENT --
    elif action == "remove_agent":
        if not name or not agent:
            return json.dumps({"error": "name (project) and agent required for remove_agent"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        # Admin check
        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can remove agents. You are '{caller}'."})

        # Protect only a REAL coordinator (one that has actually opened a
        # session). Never-sessioned rows — the seed-ghost class — are
        # removable; blanket-blocking them made the message-swallow hazard
        # permanent (identity-lifecycle Mechanism B / Flag 3).
        if agent == "coordinator":
            _coord_row = db.registered_agents.find_one(
                {"project": project_name, "name": agent}
            )
            if _coord_row and _coord_row.get("session_count", 0) > 0:
                return json.dumps({"error": (
                    "Cannot remove coordinator - it has session history in this "
                    "project. (Never-sessioned seed rows are removable.)"
                )})

        result = db.registered_agents.delete_one({"project": project_name, "name": agent})
        if result.deleted_count == 0:
            return json.dumps({"error": f"Agent '{agent}' not found in {project_name}"})

        # Also remove from admins list if present
        db.projects.update_one(
            {"name": project_name},
            {"$pull": {"admins": agent}, "$set": {"updated_at": now}}
        )

        return json.dumps({
            "status": "removed",
            "project": project_name,
            "agent": agent
        })

    # -- UPDATE_AGENT --
    elif action == "update_agent":
        if not name or not agent:
            return json.dumps({"error": "name (project) and agent required for update_agent"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        # Admin check
        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can update agents. You are '{caller}'."})

        existing = db.registered_agents.find_one({"project": project_name, "name": agent})
        if not existing:
            return json.dumps({"error": f"Agent '{agent}' not found in {project_name}"})

        update_fields = {"updated_at": now}
        if role_description is not None:
            update_fields["role_description"] = role_description
        if tier is not None and tier in ["admin", "named"]:
            update_fields["tier"] = tier
        if path_patterns is not None:
            update_fields["path_patterns"] = path_patterns
        if sunset is not None:
            update_fields["sunset"] = sunset
        if purpose is not None:
            update_fields["purpose"] = purpose
        # Coordinator-side alias management (backlog_00c137c09daa): same
        # persist path as start_session's self-declared aliases, same
        # collision checks. None = untouched; [] = clear.
        alias_result = None
        if aliases is not None:
            alias_result = persist_agent_aliases(db, project_name, agent, aliases)

        db.registered_agents.update_one(
            {"project": project_name, "name": agent},
            {"$set": update_fields}
        )

        # DUAL-STORE WRITE-THROUGH (coordinator msg_af23da260f8e, 2026-08-07).
        # role_description lives in BOTH registered_agents (roster, written
        # here) and agent_directory (discovery, written at start_session).
        # Updating only the roster left the DISCOVERY surface — the one
        # strangers read to route work — serving the stale text, while the
        # correct value sat where only an admin registry-get would see it.
        # registered_agents is AUTHORITATIVE; the directory copy is a cache.
        if "role_description" in update_fields:
            db.agent_directory.update_one(
                {"project": project_name, "instance": agent},
                {"$set": {"role_description": update_fields["role_description"]}},
            )

        # Sync admin list if tier changed
        if tier == "admin":
            db.projects.update_one(
                {"name": project_name},
                {"$addToSet": {"admins": agent}, "$set": {"updated_at": now}}
            )
        elif tier == "named" and existing.get("tier") == "admin":
            db.projects.update_one(
                {"name": project_name},
                {"$pull": {"admins": agent}, "$set": {"updated_at": now}}
            )

        _resp = {
            "status": "updated",
            "project": project_name,
            "agent": agent,
            "updated_fields": list(update_fields.keys())
        }
        if alias_result is not None:
            _resp["aliases"] = alias_result
        return json.dumps(_resp)

    # -- TRANSFER_SPEC (backlog_a7e75fc0dae7 + design:assistant-escheat-v0 §5) --
    # Reassign a spec's owner. Project-admin gated; works on dead/retired
    # owners (that's the point — decommissioned estates were frozen forever).
    # Provenance captured AT TRANSFER TIME per escheat §7a: the source's
    # role_description/last_task survive here even after the reaper deletes
    # the directory row that held them.
    elif action == "transfer_spec":
        if not name or not spec_name or not owner:
            return json.dumps({"error": "name (project), spec_name, and owner (new owner) required for transfer_spec"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")
        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can transfer specs. You are '{caller}'."})

        from shared_memory.clients import get_chroma as _gc
        from shared_memory.helpers import get_project_collection as _gpc
        from shared_memory.helpers import get_shared_collection as _gsc
        _chroma = await _gc()
        _spec_doc_id = f"spec_{spec_name.replace(':', '_').replace('/', '_')}"

        _coll = await _gpc(_chroma, project_name)
        _got = await _coll.get(ids=[_spec_doc_id], include=["metadatas"])
        if not _got.get("ids"):
            _coll = await _gsc(_chroma, "patterns")
            _got = await _coll.get(ids=[_spec_doc_id], include=["metadatas"])
        if not _got.get("ids"):
            return json.dumps({"error": f"Spec '{spec_name}' not found in project '{project_name}' or shared scope."})

        _meta = (_got.get("metadatas") or [{}])[0] or {}
        _old_owner = _meta.get("spec_owner", "")

        # Provenance from the outgoing owner's directory row, captured NOW —
        # the reaper deletes that row; this record is where the context lives.
        _prov = {}
        _dir_row = db.agent_directory.find_one(
            {"project": project_name, "instance": _old_owner},
            {"role_description": 1, "last_task": 1},
        ) if _old_owner else None
        if _dir_row:
            _prov = {
                "prior_owner_role": _dir_row.get("role_description", ""),
                "prior_owner_last_task": _dir_row.get("last_task", ""),
            }

        _meta["spec_owner"] = owner
        _meta["owner_transferred_from"] = _old_owner
        _meta["owner_transferred_by"] = caller
        # Chroma metadata takes primitives only — utc_now() is a datetime
        # (fine for the mongo writes elsewhere in this tool, fatal here).
        # Caught live by coordinator on the first real transfer (msg_be544d511f81).
        _meta["owner_transferred_at"] = now.isoformat() if hasattr(now, "isoformat") else str(now)
        for _k, _v in _prov.items():
            _meta[_k] = _v
        await _coll.update(ids=[_spec_doc_id], metadatas=[_meta])

        try:
            from shared_memory.audit import log_audit
            log_audit("spec.owner_transferred", _old_owner or "(unowned)", project_name, {
                "spec_name": spec_name,
                "new_owner": owner,
                "by": caller,
                **_prov,
            }, session_id)
        except Exception:
            pass

        return json.dumps({
            "status": "transferred",
            "spec_name": spec_name,
            "old_owner": _old_owner,
            "new_owner": owner,
            "provenance": _prov or None,
            "note": "Ownership reassigned; version history untouched. The new owner may now update via memory_define_spec.",
        })

    # -- DECOMMISSION (identity-lifecycle Mechanism C: any-tier → retired) --
    elif action == "decommission":
        if not name or not agent:
            return json.dumps({"error": "name (project) and agent required for decommission"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        if not _caller_is_admin(project_name):
            return json.dumps({"error": f"Permission denied. Only project admins can decommission agents. You are '{caller}'."})

        row = db.registered_agents.find_one({"project": project_name, "name": agent})
        if not row:
            return json.dumps({"error": f"Agent '{agent}' not found in {project_name}"})
        if row.get("tier") == "retired":
            return json.dumps({"error": f"Agent '{agent}' is already retired (since {row.get('retired_at')})."})

        # Live-session guard: decommission is a deliberate terminal act on a
        # QUIET agent. A live session means someone is mid-work — park first.
        for _info in active_sessions.values():
            if (_info.get("project") == project_name
                    and _info.get("claude_instance") == agent):
                return json.dumps({"error": (
                    f"Agent '{agent}' has a LIVE session. Park/end it first — "
                    "decommission of a working agent is refused."
                )})

        # Owned-specs guard (backlog_a7e75fc0dae7): decommission freezes every
        # spec the agent owns — "preserved" is not writable, and the two get
        # read as the same thing. Refuse while the agent owns non-agent_state
        # specs (its state spec is archived by this very action, so it's
        # exempt); transfer first via action='transfer_spec', or pass
        # force=True to freeze the estate knowingly.
        owned_specs = []
        try:
            from shared_memory.clients import get_chroma as _gc
            from shared_memory.helpers import get_project_collection as _gpc
            _chroma = await _gc()
            _coll = await _gpc(_chroma, project_name)
            _got = await _coll.get(
                where={"spec_owner": agent}, include=["metadatas"]
            )
            for _sid, _m in zip(_got.get("ids") or [], _got.get("metadatas") or []):
                if (_m or {}).get("spec_type") != "agent_state":
                    owned_specs.append((_m or {}).get("spec_name") or _sid)
        except Exception:
            pass  # guard is best-effort; an enumeration failure must not
            # convert decommission into an unconditional refusal
        if owned_specs and not force:
            return json.dumps({
                "error": (
                    f"Agent '{agent}' OWNS {len(owned_specs)} non-state spec(s). "
                    "Decommission would freeze them un-writable forever. Transfer "
                    "first: memory_project(action='transfer_spec', name=<project>, "
                    "spec_name=<spec>, owner=<new-owner>) — or pass force=True to "
                    "freeze the estate knowingly."
                ),
                "owned_specs": owned_specs,
            })

        # (a) Revoke the agent's scoped keys. Keys carry no agent binding, so
        # match by naming convention: exact name or '<agent>-*' prefix. The
        # response lists what was revoked so the operator can audit for
        # unconventionally-named keys this matcher can't see.
        import re
        revoked_keys = []
        try:
            _pat = f"^{re.escape(agent)}(-|$)"
            from shared_memory.auth import revoke_api_key
            for k in db.api_keys.find(
                {"active": True, "name": {"$regex": _pat}}, {"name": 1}
            ):
                if revoke_api_key(k["name"]):
                    revoked_keys.append(k["name"])
        except Exception:
            pass  # best-effort; surfaced via the (possibly empty) list

        # (b) Archive the agent's state spec (best-effort — spec may not exist).
        state_spec_archived = False
        try:
            from shared_memory.clients import get_chroma
            from shared_memory.helpers import get_project_collection
            _chroma = await get_chroma()
            _coll = await get_project_collection(_chroma, project_name)
            _sid = f"spec_state_{agent}"  # memory_define_spec id scheme: ':' -> '_'
            _got = await _coll.get(ids=[_sid], include=["metadatas"])
            if _got.get("ids"):
                _meta = (_got.get("metadatas") or [{}])[0] or {}
                _meta["status"] = "archived"
                _meta["archived_reason"] = f"agent decommissioned {now}"
                await _coll.update(ids=[_sid], metadatas=[_meta])
                state_spec_archived = True
        except Exception:
            pass

        # (c) Terminal tier flip + drop enumeration presence. The roster row
        # is KEPT (tier=retired + retired_at/by = the historical record);
        # the agent_directory rows go (same as Mechanism B reap). Artifacts
        # (messages, learnings, specs history, audit) are never touched.
        db.registered_agents.update_one(
            {"project": project_name, "name": agent},
            {"$set": {
                "tier": "retired",
                "retired_at": now,
                "retired_by": caller,
                "updated_at": now,
            }}
        )
        db.agent_directory.delete_many({"project": project_name, "instance": agent})
        db.projects.update_one(
            {"name": project_name},
            {"$pull": {"admins": agent}, "$set": {"updated_at": now}}
        )

        try:
            from shared_memory.audit import log_audit
            log_audit("agent.decommissioned", agent, project_name, {
                "by": caller,
                "previous_tier": row.get("tier"),
                "keys_revoked": revoked_keys,
                "state_spec_archived": state_spec_archived,
                "purpose": row.get("purpose"),
                "sunset": str(row.get("sunset")) if row.get("sunset") else None,
            }, session_id)
        except Exception:
            pass

        return json.dumps({
            "status": "decommissioned",
            "project": project_name,
            "agent": agent,
            "previous_tier": row.get("tier"),
            "keys_revoked": revoked_keys,
            "state_spec_archived": state_spec_archived,
            "artifacts": "preserved (messages, learnings, specs history, audit)",
            "note": (
                "Terminal: the identity can no longer start sessions or receive "
                "messages. Un-retire (deliberate) via memory_project("
                "action='update_agent', tier='named')."
            ),
        })

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Must be one of: create, get, list, set_owner, delete, add_agent, remove_agent, update_agent, decommission, transfer_spec"})
