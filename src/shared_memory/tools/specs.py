"""Spec management tools - versioned specifications with owner enforcement."""

import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import MAX_CONTENT_SIZE, ORIGIN_SERVER_ID, PROJECT_PREFIX
from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    normalize_project,
    require_session,
    utc_now_iso,
)
from shared_memory.op_log import (
    claim_or_verify_state_owner,
    emit_op_log_from_context,
    fetch_embedding_for_op_log,
)
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_define_spec(
    session_id: str,
    name: str,
    content: str,
    owner: str = None,
    version: str = None,
    spec_type: str = "interface",
    project: str = None,
    json_schema: dict = None,
    tags: List[str] = None,
    force: bool = False,
    ctx: Context = None
) -> str:
    """
    Define or update a versioned spec with owner-only enforcement.

    Use this for any long-lived shared document with an owner and version:
    interface contracts, API specifications, data schemas, requirements,
    architecture overviews, agent state, design decisions, research notes,
    cross-project patterns. Tooling for `memory_list_specs` etc. filters
    by spec_type, so consistent tagging helps discovery.

    Owner Enforcement:
    - First definition sets the owner
    - Only the owner can update the spec
    - Set owner to "human" or your name for human-controlled specs
    - AIs can read but not modify human-owned specs

    Versioning:
    - Uses semver (e.g., "1.0.0", "1.2.3")
    - Previous versions are preserved for history
    - Omit version to auto-increment patch version

    Args:
        session_id: Your session ID
        name: Unique spec name (e.g., "mqtt:frame-status", "api:user-auth")
        content: The spec content (markdown, JSON, any text)
        owner: Owner identifier (defaults to session's claude_instance)
        version: Version string (semver). Omit to auto-increment
        spec_type: Free-form category string for the spec. The server does
            NOT enforce an accept-list — any value works and `memory_list_specs`
            can filter by it. Canonical values in active use today:
            `interface`, `api`, `schema`, `requirement`, `architecture`,
            `agent_state`, `design`, `decision`, `pattern`, `research`.
            Introduce new values when the existing ones don't fit; new
            categories show up cleanly in `memory_list_specs(spec_type=...)`.
        project: Project this belongs to (omit for shared specs)
        json_schema: Optional JSON schema for validation
        tags: Tags for categorization
        force: Override safety checks (e.g., state spec overwrite protection). Default False.
    """
    error = require_session(session_id)
    if error:
        return error

    # Check content size limit
    if len(content.encode('utf-8')) > MAX_CONTENT_SIZE:
        return json.dumps({
            "error": f"Content exceeds maximum size of {MAX_CONTENT_SIZE // 1024}KB",
            "size": f"{len(content.encode('utf-8')) // 1024}KB"
        })

    # Write-lint parity (backlog_8d33a63e2626): a tool-call serialization that
    # leaks into the content body — observed 2026-04-22: content ending
    # `</content><parameter name="spec_type">agent_state</parameter>...</invoke>`,
    # which swallowed the real spec_type arg and left a ghost doc — used to be
    # REJECTED here. Reconciled to the shared RECOVERY posture every other
    # free-text writer uses: strip + re-route swallowed params + warn, so no
    # work is lost and no retry is needed (legacy-team: a validator that alone
    # rejects while its siblings recover trains agents to expect the catch).
    # The helper never raises. A swallowed non-project param (e.g. spec_type)
    # is preserved in-doc under a marked heading and flagged, rather than
    # silently defaulting.
    from shared_memory.write_lint import recover_envelope_leak
    content, project, _spec_lint_notes = recover_envelope_leak(content, "content", project)

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    if project:
        project = normalize_project(project)

    # Default owner to session's claude_instance
    if not owner:
        owner = session_info.get("claude_instance", "unknown")

    # Normalize spec name for doc_id
    spec_doc_id = f"spec_{name.replace(':', '_').replace('/', '_')}"

    # Determine collection. chroma_collection_name mirrors get_*_collection
    # output so op-log `ref.collection` matches §5.1 sync / §4.7 reconciliation.
    if project:
        collection = await get_project_collection(chroma, project)
        location = f"project:{project}"
        chroma_collection_name = f"proj_{project}"
    else:
        collection = await get_shared_collection(chroma, "patterns")
        location = "shared:patterns"
        chroma_collection_name = "shared_patterns"

    # Check if spec already exists
    existing = None
    try:
        result = await collection.get(ids=[spec_doc_id], include=["documents", "metadatas"])
        if result["ids"]:
            existing = {
                "content": result["documents"][0],
                "metadata": result["metadatas"][0]
            }
    except Exception:
        pass

    # §7.4 — state-spec multi-instance detection. Every state-spec write
    # registers (project, owner) → ORIGIN_SERVER_ID on first write; later
    # writes from a different origin (i.e., a peer pushed a state spec
    # claiming the same identity earlier) are refused with a critical alert.
    # See design:local-first-junto-v0-mvp v0.5.0 §7.4 + op_log.claim_or_verify_state_owner.
    if spec_type == "agent_state" and project:
        mongo_db_for_state = get_mongo()
        if mongo_db_for_state is not None:
            outcome, registered = claim_or_verify_state_owner(
                mongo_db_for_state, project, owner, ORIGIN_SERVER_ID
            )
            if outcome == "mismatch":
                return json.dumps({
                    "error": "State-spec multi-instance detected (§7.4)",
                    "spec_name": name,
                    "project": project,
                    "agent": owner,
                    "this_origin": ORIGIN_SERVER_ID,
                    "registered_origin": registered,
                    "suggestion": (
                        "Another origin has previously claimed this (project, agent) "
                        "state spec. Refusing the local write to prevent silent "
                        "divergence. If this is a recovery scenario, manually "
                        "reconcile the agent_state_owner collection first."
                    ),
                })

    # Version history collection (shared for all specs)
    history_collection = await get_shared_collection(chroma, "context")

    if existing:
        # Check owner permission
        existing_owner = existing["metadata"].get("spec_owner", "")
        if existing_owner and existing_owner != owner:
            return json.dumps({
                "error": "Permission denied - spec owned by another entity",
                "spec_name": name,
                "owner": existing_owner,
                "requester": owner,
                "suggestion": (
                    "Only the owner can update this spec. NOTE: the `owner` "
                    "parameter here declares who YOU are writing as — it does "
                    "NOT reassign ownership. To transfer ownership (e.g. from "
                    "a retired agent), a project admin runs memory_project("
                    "action='transfer_spec', name=<project>, spec_name=<spec>, "
                    "owner=<new-owner>). Otherwise contact the owner."
                )
            })

        # State spec overwrite protection: reject if new content is much shorter
        # This prevents tangent sessions from accidentally erasing prior context
        if name.startswith("state:") and not force:
            existing_len = len(existing["content"])
            new_len = len(content)
            # If new content is less than half the length, block it
            if existing_len > 200 and new_len < existing_len * 0.5:
                return json.dumps({
                    "error": "State spec overwrite protection triggered",
                    "spec_name": name,
                    "existing_size": existing_len,
                    "new_size": new_len,
                    "reduction": f"{((existing_len - new_len) / existing_len) * 100:.0f}%",
                    "suggestion": (
                        "Your new state spec is significantly shorter than the existing one. "
                        "This usually means a tangent session is about to erase the prior session's context. "
                        "READ the existing spec with memory_get_spec(name='" + name + "') first, "
                        "then merge your updates with the existing Next Steps. "
                        "Set force=True to override this check if you intentionally want to replace it."
                    )
                })

        # Auto-increment version if not provided
        current_version = existing["metadata"].get("spec_version", "1.0.0")
        if not version:
            # Parse and increment patch version
            parts = current_version.split(".")
            if len(parts) == 3:
                parts[2] = str(int(parts[2]) + 1)
            version = ".".join(parts)

        # Archive the previous version to history
        history_id = f"spec_history_{name.replace(':', '_')}_{current_version.replace('.', '_')}"
        history_metadata = {
            "title": f"Spec History: {name} v{current_version}",
            "type": "spec",
            "spec_name": name,
            "spec_version": current_version,
            "spec_owner": existing_owner,
            "archived_at": now,
            "archived_by": owner,
            "status": "archived"
        }
        try:
            await history_collection.add(
                ids=[history_id],
                documents=[existing["content"]],
                metadatas=[history_metadata]
            )
        except Exception:
            pass  # History is best-effort

        action = "updated"
    else:
        # New spec - default to version 1.0.0
        if not version:
            version = "1.0.0"
        action = "created"

    # Build metadata
    tags = tags or []
    metadata = {
        "title": f"Spec: {name}",
        "type": "spec",
        "spec_name": name,
        "spec_version": version,
        "spec_type": spec_type,
        "spec_owner": owner,
        "status": "active",
        "tags": json.dumps(tags),
        "project": project or "",
        "created": existing["metadata"].get("created", now) if existing else now,
        "updated": now,
        "created_by": existing["metadata"].get("created_by", owner) if existing else owner,
        "updated_by": owner
    }

    if json_schema:
        metadata["json_schema"] = json.dumps(json_schema)

    # Upsert the spec
    await collection.upsert(
        ids=[spec_doc_id],
        documents=[content],
        metadatas=[metadata]
    )

    # Phase 1 #2 canary 5/13: emit op-log entry per §4.3.a (best-effort).
    # Two op_types map to memory_define_spec's two write modes:
    #   action="created"  → spec.defined  (first version of this name)
    #   action="updated"  → spec.updated  (subsequent version; conflict target per §7.2)
    # `previous_version` only meaningful on the updated path; peers consume
    # it to detect §7.2 fast-forward conflicts. The history-collection
    # archive write (lines above) is NOT separately logged — peers
    # reconstruct version history from the sequence of spec.defined /
    # spec.updated ops in the same log.
    #
    # Phase 2 A-path: embedding is for the CURRENT spec row only. The history
    # archive write (history_collection.add above) re-embeds on the peer side
    # too — if vector skew there matters, §4.7 reconciliation backfills it.
    spec_op_type = "spec.defined" if action == "created" else "spec.updated"
    previous_version = existing["metadata"].get("spec_version") if existing else None
    embedding = await fetch_embedding_for_op_log(collection, spec_doc_id)
    spec_payload = {
        "spec_name": name,
        "version": version,
        "previous_version": previous_version,
        "owner": owner,
        "spec_type": spec_type,
        "content": content,
        "tags": tags,
        "json_schema": json_schema,
        "project": project,
        "updated_at": now,
        "embedding": embedding,
    }
    # §7.4 — state-spec ops carry origin_server_id so peers can enforce the
    # multi-instance check on receive. Only state specs need this field today
    # (see design:local-first-junto-v0-mvp v0.5.0 §7.4).
    if spec_type == "agent_state":
        spec_payload["origin_server_id"] = ORIGIN_SERVER_ID
    emit_op_log_from_context(
        db=get_mongo(),
        op_type=spec_op_type,
        actor={
            "agent": session_info["claude_instance"],
            "project": project,
            "session_id": session_id,
        },
        ref={"collection": chroma_collection_name, "doc_id": spec_doc_id},
        payload=spec_payload,
    )

    # Audit log for spec changes
    try:
        from shared_memory.audit import log_audit
        log_audit(f"spec.{action}", owner, project or "",
                  {"spec_name": name, "version": version}, session_id)
    except Exception:
        pass

    result = {
        "status": f"{action}_with_recovery" if _spec_lint_notes else action,
        "spec_name": name,
        "version": version,
        "owner": owner,
        "location": location,
        "note": "Owner-only updates enforced. Previous versions preserved in history."
    }
    if _spec_lint_notes:
        result["write_lint"] = _spec_lint_notes
        result["action_required"] = (
            "Your tool-call emission is malformed — it serialized the call's own "
            "XML envelope into `content`. The server repaired the body, but it "
            "cannot repair the client. IMPORTANT: any parameter the leak swallowed "
            f"(e.g. spec_type — this spec persisted as spec_type='{spec_type}') was "
            "NOT received as an argument and fell back to its default. For a "
            "versioned spec that changes get/list filtering and owner enforcement, "
            "so verify spec_type/version/owner and re-define if wrong. See "
            "write_lint above for what was recovered into the body."
        )
    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_get_spec(
    session_id: str,
    name: str,
    version: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Get a spec by name, optionally at a specific version.

    Args:
        session_id: Your session ID
        name: Spec name (e.g., "mqtt:frame-status")
        version: Optional specific version (omit for current)
        project: Project to search (omit for shared specs)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()

    if project:
        project = normalize_project(project)

    # Normalize spec name for doc_id
    spec_doc_id = f"spec_{name.replace(':', '_').replace('/', '_')}"

    # Resolve which collection(s) to read. A spec doc lives in exactly ONE
    # collection: proj_<project> when written with project=..., else
    # shared_patterns. get_spec historically read ONLY the collection named by
    # the `project` ARG, so a project-scoped spec (every state: spec) was
    # invisible when the caller omitted project, and a stale/orphaned doc at the
    # shared id could shadow it (the 2026-06-14 frames-team ghost). Fix: when
    # project is omitted, try the CALLER'S OWN project first (where its state
    # specs live), then fall back to shared. An explicit project is honored
    # exactly as before.
    if project:
        collections_to_try = [await get_project_collection(chroma, project)]
    else:
        collections_to_try = []
        sess_proj = normalize_project(active_sessions[session_id].get("project", ""))
        if sess_proj:
            collections_to_try.append(await get_project_collection(chroma, sess_proj))
        collections_to_try.append(await get_shared_collection(chroma, "patterns"))

    # Collect ACTIVE matches across the candidate collections, then pick the
    # most-recently-updated one. Two reasons:
    #  - Skip non-active docs (archived / orphaned ghosts sitting at a live id)
    #    so get_spec agrees with list_specs / standup (which filter active).
    #  - A spec can wrongly exist in BOTH shared and a project collection
    #    (historic divergence: some specs were written without project, then
    #    later with it). Picking the newest active copy self-heals: a state
    #    spec's live project copy beats a stale shared duplicate, while a
    #    genuinely-shared spec whose shared copy is ahead is NOT regressed to an
    #    older project shadow. (max() is stable → ties keep candidate order,
    #    i.e. the caller's own project first.)
    candidates = []
    for collection in collections_to_try:
        try:
            result = await collection.get(ids=[spec_doc_id], include=["documents", "metadatas"])
        except Exception as e:
            return json.dumps({"error": f"Failed to retrieve spec: {str(e)}"})
        if not result["ids"]:
            continue
        meta = result["metadatas"][0]
        status = meta.get("status")
        if status and status != "active":
            continue  # archived / orphaned ghost — not the current spec
        candidates.append({"meta": meta, "content": result["documents"][0]})
    current_doc = max(
        candidates, key=lambda d: d["meta"].get("updated") or "", default=None
    ) if candidates else None

    if version:
        # 1) Try the historical version in the shared history collection.
        history_collection = await get_shared_collection(chroma, "context")
        history_id = f"spec_history_{name.replace(':', '_')}_{version.replace('.', '_')}"
        try:
            result = await history_collection.get(ids=[history_id], include=["documents", "metadatas"])
            if result["ids"]:
                meta = result["metadatas"][0]
                return json.dumps({
                    "spec_name": name,
                    "version": version,
                    "owner": meta.get("spec_owner"),
                    "content": result["documents"][0],
                    "archived_at": meta.get("archived_at"),
                    "note": "This is a historical version, not the current spec."
                }, indent=2)
        except Exception:
            pass
        # 2) The CURRENT version is never archived to history (only superseded
        # versions are), so an explicit request for the current label would 404.
        # Fall through to the live doc when the requested version IS current;
        # otherwise it genuinely isn't available.
        if not (current_doc and current_doc["meta"].get("spec_version") == version):
            return json.dumps({
                "error": f"Version {version} not found for spec '{name}'",
                "suggestion": "Use memory_list_specs to see available versions"
            })

    # Current version (or current-as-requested-version).
    if current_doc:
        meta = current_doc["meta"]
        response = {
            "spec_name": name,
            "version": meta.get("spec_version"),
            "owner": meta.get("spec_owner"),
            "spec_type": meta.get("spec_type"),
            "content": current_doc["content"],
            "created": meta.get("created"),
            "updated": meta.get("updated"),
            "tags": json.loads(meta.get("tags", "[]"))
        }
        if meta.get("json_schema"):
            response["json_schema"] = json.loads(meta["json_schema"])
        return json.dumps(response, indent=2)

    return json.dumps({
        "error": f"Spec '{name}' not found",
        "suggestion": "Use memory_list_specs to see available specs"
    })


@mcp.tool()
async def memory_list_specs(
    session_id: str,
    project: str = None,
    include_versions: bool = False,
    spec_type: str = None,
    ctx: Context = None
) -> str:
    """
    List all specs, optionally with version history.

    Args:
        session_id: Your session ID
        project: Filter by project (omit for shared + all projects)
        include_versions: Include previous version numbers
        spec_type: Filter by spec type (free-form string; see
            memory_define_spec for canonical values in active use)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    specs = []

    if project:
        project = normalize_project(project)

    # Build where filter - use $and for compound conditions (ChromaDB requirement)
    conditions = [{"type": {"$eq": "spec"}}, {"status": {"$eq": "active"}}]
    if spec_type:
        conditions.append({"spec_type": {"$eq": spec_type}})
    _where_filter = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    # Search collections
    collections_to_search = []
    if project:
        collections_to_search.append(await get_project_collection(chroma, project))
    else:
        # Search shared and all project collections
        all_collections = await chroma.list_collections()
        for col in all_collections:
            if col.name.startswith(PROJECT_PREFIX) or col.name == "shared_patterns":
                collections_to_search.append(col)

    for collection in collections_to_search:
        try:
            # Get all docs and filter in Python (ChromaDB where filter unreliable)
            all_docs = await collection.get(include=["metadatas"])
            for doc_id, meta in zip(all_docs.get("ids", []), all_docs.get("metadatas", [])):
                if meta and meta.get("type") == "spec" and meta.get("status") == "active":
                    if spec_type and meta.get("spec_type") != spec_type:
                        continue
                    specs.append({
                        "name": meta.get("spec_name"),
                        "version": meta.get("spec_version"),
                        "owner": meta.get("spec_owner"),
                        "spec_type": meta.get("spec_type"),
                        "project": meta.get("project") or "shared",
                        "updated": meta.get("updated")
                    })
        except Exception:
            continue

    # Get version history if requested
    if include_versions:
        history_collection = await get_shared_collection(chroma, "context")
        # Build a spec_name -> [archived versions] index in ONE pass. The old
        # per-spec where={spec_name, status} multi-key filter was UNRELIABLE in
        # ChromaDB (same reason the main query above filters in Python) — it
        # silently returned nothing, so every spec reported all_versions=[current]
        # even though the history is fully intact. That false "no history" is
        # what made a clobbered state spec look unrecoverable.
        hist_by_name = {}
        try:
            all_hist = await history_collection.get(include=["metadatas"])
            for m in all_hist.get("metadatas", []) or []:
                if m and m.get("status") == "archived" and m.get("spec_name"):
                    hist_by_name.setdefault(m["spec_name"], []).append(m.get("spec_version"))
        except Exception:
            hist_by_name = {}
        for spec in specs:
            versions = list(hist_by_name.get(spec["name"], []))
            versions.append(spec["version"])  # include current
            spec["all_versions"] = sorted({v for v in versions if v}, reverse=True)

    return json.dumps({
        "specs": specs,
        "count": len(specs),
        "filter": {"project": project, "spec_type": spec_type}
    }, indent=2)
