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
    direct = db.registered_agents.find_one(
        {"project": project_name, "name": target_name}, {"name": 1}
    )
    if direct:
        return direct["name"]
    aliased = db.registered_agents.find_one(
        {"project": project_name, "aliases": target_name}, {"name": 1}
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
        Auto-creates: "coordinator" agent as co-admin with human

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

    action="update_agent" - Update agent details (admin/coordinator only)
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
            separate (future) action.
        purpose: For add_agent/update_agent — a short why-this-agent-exists note,
            useful for temp agents whose reason for being is time-bounded.
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    caller = session_info.get("claude_instance", "unknown")

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

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

        # Create project - human is always admin, coordinator auto-added
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

        # Auto-register coordinator as admin agent
        db.registered_agents.update_one(
            {"project": project_name, "name": "coordinator"},
            {"$setOnInsert": {
                "project": project_name,
                "name": "coordinator",
                "tier": "admin",
                "role_description": f"Coordinator for {project_name} - manages cross-team work, delegates tasks, tracks progress",
                "path_patterns": [],
                "created_by": caller,
                "created_at": now,
                "last_seen": None,
                "session_count": 0
            }},
            upsert=True
        )

        return json.dumps({
            "status": "created",
            "project": project_name,
            "display_name": display_name or name,
            "admins": admins,
            "owner": owner,
            "auto_registered": ["coordinator"]
        }, indent=2)

    # -- SET_OWNER --
    elif action == "set_owner":
        if not name or not owner:
            return json.dumps({"error": "name (project) and owner required for set_owner"})

        project_name = name.lower().replace("-", "_").replace(" ", "_")

        if not _is_project_admin(db, project_name, caller):
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
        if not _is_project_admin(db, project_name, caller):
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
        if not _is_project_admin(db, project_name, caller):
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
        if not _is_project_admin(db, project_name, caller):
            return json.dumps({"error": f"Permission denied. Only project admins can remove agents. You are '{caller}'."})

        # Don't allow removing coordinator (always exists)
        if agent == "coordinator":
            return json.dumps({"error": "Cannot remove coordinator - it is required for every project."})

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
        if not _is_project_admin(db, project_name, caller):
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

        db.registered_agents.update_one(
            {"project": project_name, "name": agent},
            {"$set": update_fields}
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

        return json.dumps({
            "status": "updated",
            "project": project_name,
            "agent": agent,
            "updated_fields": list(update_fields.keys())
        })

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Must be one of: create, get, list, set_owner, delete, add_agent, remove_agent, update_agent"})
