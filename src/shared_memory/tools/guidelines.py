"""Guidelines tools - server-managed behavioral rules for agents."""

import json

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import utc_now_iso

# Global-scope guidelines are CODE-MANAGED: the source of truth is
# GLOBAL_GUIDELINES in src/shared_memory/global_guidelines.py, re-asserted into
# db.guidelines by seed_global_guidelines() on every boot. A live DB edit to a
# global row is therefore reverted at the next restart, which used to happen
# SILENTLY — memory_guidelines(set/delete) returned success and the change
# vanished hours later. This tool now REFUSES to set or delete global rows so the
# API can no longer modify them at all (Tom, 2026-08-11: "the global rules are
# only set or changed with my approval to you" — learning_66a2a5d8bb8f13c1). The
# boot seeder writes db.guidelines directly and does NOT route through this tool,
# so it is unaffected by this guard.
GLOBAL_WRITE_REFUSAL = (
    "Global-scope guidelines are code-managed and cannot be set or deleted via "
    "memory_guidelines. The source of truth is GLOBAL_GUIDELINES in "
    "src/shared_memory/global_guidelines.py — edit it there and deploy (the boot "
    "seeder re-asserts it on every server). Governance: global rules change ONLY "
    "with Tom's approval routed through memory@junto (learning_66a2a5d8bb8f13c1). "
    "Project-scoped guidelines (scope=<project>) are still writable here."
)


def get_guidelines_for_session(project: str = None) -> list:
    """Fetch all applicable guidelines for a session.

    Returns global guidelines + project-specific guidelines, sorted by priority.
    Called internally by memory_start_session.
    """
    db = get_mongo()
    if db is None:
        return []

    guidelines = []

    # Global guidelines (scope="global")
    for doc in db.guidelines.find({"scope": "global", "active": True}).sort("priority", 1):
        guidelines.append({
            "name": doc["name"],
            "scope": "global",
            "rule": doc["rule"],
            "priority": doc.get("priority", 50),
        })

    # Project-specific guidelines
    if project:
        normalized = project.lower().replace("-", "_").replace(" ", "_")
        for doc in db.guidelines.find({"scope": normalized, "active": True}).sort("priority", 1):
            guidelines.append({
                "name": doc["name"],
                "scope": f"project:{project}",
                "rule": doc["rule"],
                "priority": doc.get("priority", 50),
            })

    # Sort by priority (lower = more important)
    guidelines.sort(key=lambda g: g["priority"])

    return guidelines


def get_guidelines_version(db=None) -> int:
    """Monotonic corpus version (design:guideline-trim-v0 instrumentation).

    Bumped by any script/tool that mutates the guidelines corpus; stamped into
    every session record at start so compliance data is attributable to the
    exact block the agent held (trickle adoption becomes a natural experiment,
    not a confound). 0 = pre-versioning corpus."""
    if db is None:
        db = get_mongo()
    if db is None:
        return 0
    try:
        doc = db.guidelines_meta.find_one({"_id": "version"})
        return int(doc["value"]) if doc else 0
    except Exception:
        return 0


def bump_guidelines_version(db, actor: str) -> int:
    """Increment the corpus version. Call from any corpus mutation path."""
    from shared_memory.helpers import utc_now
    doc = db.guidelines_meta.find_one_and_update(
        {"_id": "version"},
        {"$inc": {"value": 1}, "$set": {"updated": utc_now(), "updated_by": actor}},
        upsert=True, return_document=True,
    )
    return int(doc["value"]) if doc else 1


@mcp.tool()
async def memory_guidelines(
    action: str,
    name: str = None,
    rule: str = None,
    scope: str = "global",
    priority: int = 50,
    ctx: Context = None
) -> str:
    """
    Manage server-side behavioral guidelines that ALL agents receive at session start.

    These replace CLAUDE.md rules — update once here, every agent on every machine
    picks them up immediately on their next session.

    Actions:
        list   - Show all guidelines (optionally filtered by scope)
        set    - Create or update a guideline (requires name + rule)
        delete - Remove a guideline by name
        get    - Get a single guideline by name

    ⛔ GLOBAL SCOPE IS READ-ONLY THROUGH THIS TOOL. `set` and `delete` on a
    global-scope guideline are refused with an error — global rules are
    code-managed in src/shared_memory/global_guidelines.py and re-seeded on every
    boot. `list`/`get` still work at every scope. Project-scoped rules
    (scope=<project>) remain fully writable.

    Args:
        action: One of: list, set, delete, get
        name: Guideline name (e.g., "freshness_check", "function_registry")
        rule: The behavioral rule text. Be explicit — agents will follow this literally.
        scope: "global" (all projects) or a project name for project-specific rules
        priority: 1-100, lower = shown first. Default 50.
    """
    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB not available"})

    normalized_scope = scope.lower().replace("-", "_").replace(" ", "_")

    if action == "list":
        query = {}
        if scope != "global":
            query["scope"] = normalized_scope
        docs = list(db.guidelines.find(query).sort("priority", 1))
        guidelines = []
        for doc in docs:
            guidelines.append({
                "name": doc["name"],
                "scope": doc["scope"],
                "rule": doc["rule"],
                "priority": doc.get("priority", 50),
                "active": doc.get("active", True),
                "updated": doc.get("updated", ""),
                "updated_by": doc.get("updated_by", ""),
            })
        return json.dumps({"guidelines": guidelines, "count": len(guidelines)}, indent=2)

    elif action == "set":
        if normalized_scope == "global":
            return json.dumps({"error": GLOBAL_WRITE_REFUSAL, "scope": "global", "rejected": True})
        if not name or not rule:
            return json.dumps({"error": "Both 'name' and 'rule' are required for set action"})

        now = utc_now_iso()
        existing = db.guidelines.find_one({"name": name})

        db.guidelines.update_one(
            {"name": name},
            {"$set": {
                "name": name,
                "rule": rule,
                "scope": normalized_scope,
                "priority": max(1, min(100, priority)),
                "active": True,
                "updated": now,
            }},
            upsert=True
        )

        action_taken = "updated" if existing else "created"

        try:
            from shared_memory.audit import log_audit
            log_audit(f"guideline.{action_taken}", "admin", normalized_scope,
                      {"name": name, "priority": priority})
        except Exception:
            pass

        return json.dumps({"status": action_taken, "name": name, "scope": normalized_scope, "priority": priority})

    elif action == "delete":
        if not name:
            return json.dumps({"error": "'name' is required for delete action"})
        # Guard on the TARGET's actual scope, not the (default-global) scope param:
        # a caller deleting a project rule need not pass scope, so we must not
        # infer intent from it. A global row is code-managed and undeletable here.
        target = db.guidelines.find_one({"name": name})
        if target and target.get("scope") == "global":
            return json.dumps({"error": GLOBAL_WRITE_REFUSAL, "scope": "global", "rejected": True})
        result = db.guidelines.delete_one({"name": name})
        if result.deleted_count:
            try:
                from shared_memory.audit import log_audit
                log_audit("guideline.deleted", "admin", "", {"name": name})
            except Exception:
                pass
            return json.dumps({"status": "deleted", "name": name})
        return json.dumps({"error": f"Guideline '{name}' not found"})

    elif action == "get":
        if not name:
            return json.dumps({"error": "'name' is required for get action"})
        doc = db.guidelines.find_one({"name": name})
        if not doc:
            return json.dumps({"error": f"Guideline '{name}' not found"})
        return json.dumps({
            "name": doc["name"],
            "scope": doc["scope"],
            "rule": doc["rule"],
            "priority": doc.get("priority", 50),
            "active": doc.get("active", True),
            "updated": doc.get("updated", ""),
        }, indent=2)

    else:
        return json.dumps({"error": f"Unknown action '{action}'. Use: list, set, delete, get"})
