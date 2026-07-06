"""Fleet-directive tools — ack a directive surfaced in the onboarding bundle.

Directives are code-seeded cross-server "here's what you need to do" notices
(see shared_memory.directives). They surface as output["directives"] at session
start and keep surfacing until the recipient acks them here. Acks are per-server,
keyed by (directive key, project, agent)."""

import json

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.directives import _directive_targets
from shared_memory.helpers import require_session, utc_now_iso
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_ack_directive(
    session_id: str,
    key: str,
    note: str = None,
    ctx: Context = None,
) -> str:
    """Acknowledge a fleet directive so it stops surfacing for you.

    Ack it once you've actioned it (or accepted ownership of it). The ack is
    recorded per (directive key, your project, your agent name) — other agents
    still see the directive until they ack their own.

    Args:
        session_id: Your session ID.
        key: The directive `key` from the onboarding bundle's "directives" block.
        note: Optional note (e.g. "done in launcher commit abc123", "handed to X").
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    directive = db.directives.find_one({"key": key, "active": True})
    if not directive:
        return json.dumps({"error": f"No active directive with key '{key}'"})

    session_info = active_sessions[session_id]
    from shared_memory.helpers import normalize_project
    project = normalize_project(session_info.get("project") or "") or ""
    agent = session_info.get("claude_instance") or ""

    if not _directive_targets(directive.get("target"), project, agent):
        return json.dumps({
            "error": "This directive does not target you — nothing to ack",
            "key": key,
            "your_project": project,
            "your_agent": agent,
        })

    now = utc_now_iso()
    ack_id = f"{key}:{project}:{agent}"
    db.directive_acks.update_one(
        {"_id": ack_id},
        {"$set": {"_id": ack_id, "key": key, "project": project,
                  "agent": agent, "acked_at": now, "note": note}},
        upsert=True,
    )
    return json.dumps({"status": "acked", "key": key,
                       "project": project, "agent": agent, "acked_at": now}, indent=2)
