"""Backlog management tools - track tasks for humans and agents."""

import hashlib
import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import BACKLOG_PRIORITIES, BACKLOG_STATUSES, PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import (
    get_project_collection,
    get_shared_collection,
    normalize_project,
    parse_timestamp,
    require_session,
    utc_now,
    utc_now_iso,
)
from shared_memory.op_log import emit_op_log_from_context, fetch_embedding_for_op_log
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_add_backlog_item(
    session_id: str,
    title: str,
    description: str,
    priority: str = "medium",
    project: str = None,
    assigned_to: str = None,
    tags: List[str] = None,
    target_version: str = None,
    deferred_reason: str = None,
    ctx: Context = None
) -> str:
    """
    Add an item to the backlog for future work.

    Use this to track:
    - Features to implement later
    - Tech debt to address
    - Ideas to explore
    - Tasks for other agents

    Args:
        session_id: Your session ID
        title: Short title for the backlog item
        description: Detailed description of what needs to be done
        priority: Priority level (critical, high, medium, low) - default medium
        project: Project this belongs to (omit for cross-project items)
        assigned_to: Agent/team this is assigned to (e.g., "triage-team", "gmail-team")
        tags: Tags for categorization (e.g., ["tech-debt", "v7"])
        target_version: Target version/release for this item (e.g., "v6.1", "sprint-5")
        deferred_reason: Reason for deferring (when status is deferred)
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})

    # Write-lint parity (backlog_8d33a63e2626): strip a leaked tool-call envelope
    # and re-route swallowed params before description enters the doc / op-log.
    from shared_memory.write_lint import recover_envelope_leak
    description, project, _lint_notes = recover_envelope_leak(description, "description", project)

    tags = tags or []
    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    if project:
        project = normalize_project(project)

    # Store in project collection if specified, otherwise shared
    if project:
        collection = await get_project_collection(chroma, project)
        chroma_collection_name = f"{PROJECT_PREFIX}{project}"
    else:
        collection = await get_shared_collection(chroma, "work")
        chroma_collection_name = f"{SHARED_PREFIX}work"

    # Generate ID
    backlog_id = f"backlog_{hashlib.sha256(f'{title}:{now}'.encode()).hexdigest()[:12]}"

    content = f"# {title}\n\n{description}"

    metadata = {
        "title": title,
        "type": "backlog",
        "backlog_status": "open",
        "priority": priority,
        "project": project or "",
        "assigned_to": assigned_to or "",
        "tags": json.dumps(tags),
        "target_version": target_version or "",
        "deferred_reason": deferred_reason or "",
        "created_by": session_info["claude_instance"],
        "created": now,
        "updated": now,
        "edit_count": 0
    }

    await collection.add(
        ids=[backlog_id],
        documents=[content],
        metadatas=[metadata]
    )

    # Phase 1 #2 canary: emit op-log entry per §4.3.a (best-effort).
    embedding = await fetch_embedding_for_op_log(collection, backlog_id)
    emit_op_log_from_context(
        db=get_mongo(),
        op_type="backlog.added",
        actor={
            "agent": session_info["claude_instance"],
            "project": project,
            "session_id": session_id,
        },
        ref={"collection": chroma_collection_name, "doc_id": backlog_id},
        payload={
            "title": title,
            "description": description,
            "priority": priority,
            "project": project or "",
            "assigned_to": assigned_to or "",
            "tags": tags,
            "target_version": target_version or "",
            "deferred_reason": deferred_reason or "",
            "created": now,
            "embedding": embedding,
        },
    )

    result = {
        "status": "added_with_recovery" if _lint_notes else "added",
        "id": backlog_id,
        "title": title,
        "priority": priority,
        "project": project or "shared",
        "assigned_to": assigned_to,
        "target_version": target_version,
        "deferred_reason": deferred_reason
    }
    if _lint_notes:
        result["write_lint"] = _lint_notes
        result["action_required"] = (
            "Your tool-call emission is malformed — it serialized the call's own "
            "XML envelope into `description`. The server repaired this write, but "
            "it cannot repair the client. Fix the emission; see write_lint above "
            "for what was recovered."
        )
    return json.dumps(result)


@mcp.tool()
async def memory_list_backlog(
    session_id: str,
    project: str = None,
    status: str = None,
    priority: str = None,
    assigned_to: str = None,
    target_version: str = None,
    tags: List[str] = None,
    tags_match: str = "all",
    include_done: bool = False,
    updated_within_days: int = None,
    limit: int = 20,
    offset: int = 0,
    ctx: Context = None
) -> str:
    """
    List backlog items with optional filters.

    ALWAYS pass project and/or assigned_to — an unfiltered call returns many
    items and floods your context (typical: project=YOUR_PROJECT,
    assigned_to=YOUR_NAME).

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects + shared)
        status: Filter by status (open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info)
        priority: Filter by priority (critical, high, medium, low)
        assigned_to: Filter by assignee
        target_version: Filter by milestone/version (e.g., "meural-beta", "v2.0", "sprint-5")
        tags: Filter by tags (e.g., ["patch", "required"]). Combine with tags_match
            to control AND/OR semantics. Omit for no tag filtering.
        tags_match: How to match `tags` — "all" (default; item must carry every
            listed tag) or "any" (item carries at least one listed tag).
        include_done: Include completed items (default False)
        updated_within_days: Only return items whose `updated` timestamp is within
            the last N days. Omit (default None) to disable the recency filter.
            Server-side filter — cheaper than load-then-filter at the caller.
        limit: Maximum items to return (default 20, max 100). Use 0 for no limit.
        offset: Skip this many items (for pagination, default 0)
    """
    error = require_session(session_id)
    if error:
        return error

    if status and status not in BACKLOG_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {BACKLOG_STATUSES}"})
    if priority and priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})
    if updated_within_days is not None and updated_within_days < 1:
        return json.dumps({"error": "updated_within_days must be >= 1"})
    if tags_match not in ("all", "any"):
        return json.dumps({"error": 'tags_match must be "all" or "any"'})

    # Normalize the requested tag filter once.
    filter_tags = [t for t in (tags or []) if t]

    # Compute the recency cutoff once, comparing against meta["updated"] strings.
    recency_cutoff = None
    if updated_within_days is not None:
        from datetime import timedelta
        recency_cutoff = utc_now() - timedelta(days=int(updated_within_days))

    chroma = await get_chroma()
    items = []

    # Get collections to search
    collections = await chroma.list_collections()
    target_collections = []

    if project:
        project = normalize_project(project)

    for col in collections:
        if project:
            if col.name == f"{PROJECT_PREFIX}{project}":
                target_collections.append(col)
        else:
            # All project and shared collections
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                target_collections.append(col)

    # Priority order for sorting
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    for col in target_collections:
        try:
            # Get all backlog items
            results = await col.get(
                where={"type": "backlog"},
                include=["metadatas", "documents"]
            )

            for i, meta in enumerate(results["metadatas"]):
                item_status = meta.get("backlog_status", "open")
                item_priority = meta.get("priority", "medium")
                item_assigned = meta.get("assigned_to", "")

                # Apply filters
                if status and item_status != status:
                    continue
                if priority and item_priority != priority:
                    continue
                if assigned_to and item_assigned != assigned_to:
                    continue
                if target_version and meta.get("target_version", "") != target_version:
                    continue
                if filter_tags:
                    try:
                        item_tags = json.loads(meta.get("tags", "[]"))
                    except (ValueError, TypeError):
                        item_tags = []
                    item_tag_set = set(item_tags)
                    if tags_match == "all":
                        if not all(t in item_tag_set for t in filter_tags):
                            continue
                    else:  # "any"
                        if not any(t in item_tag_set for t in filter_tags):
                            continue
                if not include_done and item_status in ["done", "wont_do"]:
                    continue
                if recency_cutoff is not None:
                    item_updated = parse_timestamp(meta.get("updated"))
                    if item_updated is None or item_updated < recency_cutoff:
                        continue

                items.append({
                    "id": results["ids"][i],
                    "title": meta.get("title", "Untitled"),
                    "status": item_status,
                    "priority": item_priority,
                    "priority_order": priority_order.get(item_priority, 99),
                    "project": meta.get("project", "shared"),
                    "assigned_to": item_assigned or None,
                    "target_version": meta.get("target_version") or None,
                    "deferred_reason": meta.get("deferred_reason") or None,
                    "created_by": meta.get("created_by", "unknown"),
                    "created": meta.get("created"),
                    "updated": meta.get("updated"),
                    "edit_count": meta.get("edit_count", 0),
                    "tags": json.loads(meta.get("tags", "[]"))
                })
        except Exception:
            continue

    # Sort by priority (critical first), then by created date
    items.sort(key=lambda x: (x["priority_order"], x["created"]))

    # Remove priority_order from output
    for item in items:
        del item["priority_order"]

    # Apply pagination
    total = len(items)
    effective_limit = min(limit, 100) if limit > 0 else total
    paginated = items[offset:offset + effective_limit] if effective_limit else items[offset:]

    result = {
        "count": len(paginated),
        "total": total,
        "items": paginated,
    }
    if offset > 0:
        result["offset"] = offset
    if total > offset + len(paginated):
        result["next_offset"] = offset + len(paginated)

    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_update_backlog_item(
    session_id: str,
    item_id: str,
    status: str = None,
    priority: str = None,
    assigned_to: str = None,
    title: str = None,
    description: str = None,
    target_version: str = None,
    deferred_reason: str = None,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Update a backlog item's status, priority, or assignment.

    Args:
        session_id: Your session ID
        item_id: The backlog item ID
        status: New status (open, in_progress, deferred, done, wont_do, retest, blocked, duplicate, needs_info)
        priority: New priority (critical, high, medium, low)
        assigned_to: New assignee (use empty string to unassign)
        title: New title
        description: New description
        target_version: Target version/release (e.g., "v6.1", "sprint-5")
        deferred_reason: Reason for deferring (when status is deferred)
        project: Move item to a different project (deletes from current collection, adds to new one)
    """
    error = require_session(session_id)
    if error:
        return error

    if status and status not in BACKLOG_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {BACKLOG_STATUSES}"})
    if priority and priority not in BACKLOG_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {BACKLOG_PRIORITIES}"})

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Search all collections for this item
    collections = await chroma.list_collections()
    found = False

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[item_id], include=["metadatas", "documents"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                # Update fields
                if status:
                    meta["backlog_status"] = status
                if priority:
                    meta["priority"] = priority
                if assigned_to is not None:
                    meta["assigned_to"] = assigned_to
                if title:
                    meta["title"] = title
                    # Update document too
                    doc = f"# {title}\n\n" + doc.split("\n\n", 1)[-1] if "\n\n" in doc else f"# {title}\n\n{doc}"
                if description:
                    doc = f"# {meta['title']}\n\n{description}"
                if target_version is not None:
                    meta["target_version"] = target_version
                if deferred_reason is not None:
                    meta["deferred_reason"] = deferred_reason

                meta["updated"] = now
                meta["updated_by"] = session_info["claude_instance"]
                meta["edit_count"] = meta.get("edit_count", 0) + 1

                # Move to different project if requested
                moved = False
                if project:
                    project = normalize_project(project)
                    new_collection = await get_project_collection(chroma, project)
                    if new_collection.name == col.name:
                        # SAME-PROJECT "move" is identity — treat as a plain
                        # update. Before this guard (pipeline msg_39a8771d8721,
                        # DATA LOSS 2026-08-07): add(existing-id) on the same
                        # collection silently skipped, the delete then
                        # DESTROYED the item, and the response said "moved"
                        # with the item's own metadata echoed back. Passing
                        # project=<current> reads as scoping (it IS scoping on
                        # get_by_id/get_spec) — the safe-looking call must be
                        # safe.
                        meta["project"] = project
                        await col.update(
                            ids=[item_id],
                            documents=[doc] if (title or description) else None,
                            metadatas=[meta]
                        )
                        op_collection_name = col.name
                        op_collection = col
                    else:
                        meta["project"] = project
                        await new_collection.add(
                            ids=[item_id],
                            documents=[doc],
                            metadatas=[meta]
                        )
                        # Verify the item LANDED before deleting the source —
                        # a half-failed move must leave the original intact,
                        # never a deleted item and a success response.
                        _landed = await new_collection.get(ids=[item_id], include=[])
                        if not (_landed.get("ids") or []):
                            return json.dumps({
                                "error": (
                                    f"Move failed: item did not land in project "
                                    f"'{project}'. Source item untouched."
                                ),
                                "id": item_id,
                            })
                        await col.delete(ids=[item_id])
                        moved = True
                        op_collection_name = f"{PROJECT_PREFIX}{project}"
                        op_collection = new_collection
                else:
                    await col.update(
                        ids=[item_id],
                        documents=[doc] if (title or description) else None,
                        metadatas=[meta]
                    )
                    op_collection_name = col.name
                    op_collection = col

                # Phase 1 #2 canary: emit op-log entry per §4.3.a (best-effort).
                # ref.collection is the doc's CURRENT home — for a move, that's
                # the new project's collection; the old collection is in payload.
                # Phase 2 A-path: fetch the embedding from the doc's CURRENT
                # home (post-move if applicable) so peers pin the same vector.
                embedding = await fetch_embedding_for_op_log(op_collection, item_id)
                emit_op_log_from_context(
                    db=get_mongo(),
                    op_type="backlog.updated",
                    actor={
                        "agent": session_info["claude_instance"],
                        "project": meta.get("project") or None,
                        "session_id": session_id,
                    },
                    ref={"collection": op_collection_name, "doc_id": item_id},
                    payload={
                        "title": meta.get("title"),
                        "backlog_status": meta.get("backlog_status"),
                        "priority": meta.get("priority"),
                        "assigned_to": meta.get("assigned_to") or "",
                        "target_version": meta.get("target_version") or "",
                        "deferred_reason": meta.get("deferred_reason") or "",
                        "moved_from_collection": col.name if moved else None,
                        "edit_count": meta.get("edit_count"),
                        "updated": now,
                        "embedding": embedding,
                    },
                )

                found = True
                return json.dumps({
                    "status": "moved" if moved else "updated",
                    "id": item_id,
                    "title": meta["title"],
                    "project": project if project else meta.get("project", ""),
                    "backlog_status": meta.get("backlog_status"),
                    "priority": meta.get("priority"),
                    "assigned_to": meta.get("assigned_to") or None,
                    "target_version": meta.get("target_version") or None,
                    "deferred_reason": meta.get("deferred_reason") or None
                })
        except Exception:
            continue

    if not found:
        return json.dumps({"error": f"Backlog item not found: {item_id}"})


@mcp.tool()
async def memory_complete_backlog_item(
    session_id: str,
    item_id: str,
    resolution: str = None,
    wont_do: bool = False,
    ctx: Context = None
) -> str:
    """
    Mark a backlog item as completed or won't do.

    Args:
        session_id: Your session ID
        item_id: The backlog item ID
        resolution: Optional notes about how it was resolved
        wont_do: If True, marks as "wont_do" instead of "done"
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Search all collections for this item
    collections = await chroma.list_collections()

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[item_id], include=["metadatas", "documents"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                new_status = "wont_do" if wont_do else "done"
                meta["backlog_status"] = new_status
                meta["completed_at"] = now
                meta["completed_by"] = session_info["claude_instance"]
                if resolution:
                    meta["resolution"] = resolution
                    doc += f"\n\n## Resolution\n{resolution}"

                meta["updated"] = now

                await col.update(
                    ids=[item_id],
                    documents=[doc],
                    metadatas=[meta]
                )

                # Phase 1 #2 canary: emit op-log entry per §4.3.a.
                # Completing IS an update — new_status (done/wont_do) lives in
                # payload.backlog_status. Same op_type as memory_update_backlog_item
                # because replay should treat both identically.
                embedding = await fetch_embedding_for_op_log(col, item_id)
                emit_op_log_from_context(
                    db=get_mongo(),
                    op_type="backlog.updated",
                    actor={
                        "agent": session_info["claude_instance"],
                        "project": meta.get("project") or None,
                        "session_id": session_id,
                    },
                    ref={"collection": col.name, "doc_id": item_id},
                    payload={
                        "title": meta.get("title"),
                        "backlog_status": new_status,
                        "completed_at": now,
                        "completed_by": session_info["claude_instance"],
                        "resolution": resolution or "",
                        "updated": now,
                        "embedding": embedding,
                    },
                )

                return json.dumps({
                    "status": new_status,
                    "id": item_id,
                    "title": meta["title"],
                    "completed_by": session_info["claude_instance"],
                    "resolution": resolution
                })
        except Exception:
            continue

    return json.dumps({"error": f"Backlog item not found: {item_id}"})


@mcp.tool()
async def memory_batch_backlog(
    session_id: str,
    action: str,
    items: list,
    ctx: Context = None
) -> str:
    """
    Batch backlog operations — create, update, or complete multiple items in one call.

    Much more efficient than calling memory_add/update/complete_backlog_item individually
    when you have many items to process.

    Actions:
        create   - Create multiple items. Each item needs: title, description.
                   Optional: priority, project, assigned_to, tags, target_version.
        update   - Update multiple items. Each item needs: id.
                   Optional: status, priority, assigned_to, title, description, target_version.
        complete - Complete multiple items. Each item needs: id.
                   Optional: resolution, wont_do (bool).

    Args:
        session_id: Your session ID
        action: One of: create, update, complete
        items: List of item dicts (see action descriptions for required/optional fields)
    """
    error = require_session(session_id)
    if error:
        return error

    if action not in ("create", "update", "complete"):
        return json.dumps({"error": f"Unknown action '{action}'. Use: create, update, complete"})

    if not items or not isinstance(items, list):
        return json.dumps({"error": "items must be a non-empty list"})

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()
    results = {"succeeded": 0, "failed": 0, "ids": [], "errors": []}

    if action == "create":
        for i, item in enumerate(items):
            try:
                title = item.get("title")
                description = item.get("description", "")
                if not title:
                    results["failed"] += 1
                    results["errors"].append({"index": i, "error": "title is required"})
                    continue

                project = normalize_project(item.get("project", "")) if item.get("project") else ""
                priority = item.get("priority", "medium")
                if priority not in BACKLOG_PRIORITIES:
                    priority = "medium"

                batch_id = f"backlog_{hashlib.sha256(f'{title}:{now}:{i}'.encode()).hexdigest()[:12]}"
                content = f"# {title}\n\n{description}"

                if project:
                    col = await get_project_collection(chroma, project)
                    op_collection_name = f"{PROJECT_PREFIX}{project}"
                else:
                    col = await get_shared_collection(chroma, "work")
                    op_collection_name = f"{SHARED_PREFIX}work"

                metadata = {
                    "title": title,
                    "type": "backlog",
                    "backlog_status": "open",
                    "priority": priority,
                    "project": project,
                    "assigned_to": item.get("assigned_to", ""),
                    "tags": json.dumps(item.get("tags", [])),
                    "target_version": item.get("target_version", ""),
                    "deferred_reason": "",
                    "created_by": session_info["claude_instance"],
                    "created": now,
                    "updated": now,
                    "edit_count": 0,
                }

                await col.add(ids=[batch_id], documents=[content], metadatas=[metadata])

                # Phase 1 #2 canary: one op-log entry per touched doc (keeps
                # granularity uniform with single-item memory_add_backlog_item).
                # Phase 2 A-path: per-item embedding fetch. Large batches pay
                # N extra Chroma round-trips; acceptable for v1 since adds
                # already serialize. Bulk-fetch optimization deferred.
                embedding = await fetch_embedding_for_op_log(col, batch_id)
                emit_op_log_from_context(
                    db=get_mongo(),
                    op_type="backlog.added",
                    actor={
                        "agent": session_info["claude_instance"],
                        "project": project or None,
                        "session_id": session_id,
                    },
                    ref={"collection": op_collection_name, "doc_id": batch_id},
                    payload={
                        "title": title,
                        "description": description,
                        "priority": priority,
                        "project": project,
                        "assigned_to": item.get("assigned_to", ""),
                        "tags": item.get("tags", []),
                        "target_version": item.get("target_version", ""),
                        "deferred_reason": "",
                        "created": now,
                        "batch_index": i,
                        "embedding": embedding,
                    },
                )

                results["succeeded"] += 1
                results["ids"].append(batch_id)
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"index": i, "error": str(e)})

    elif action == "update":
        collections = await chroma.list_collections()
        for i, item in enumerate(items):
            try:
                update_id = item.get("id")
                if not update_id:
                    results["failed"] += 1
                    results["errors"].append({"index": i, "error": "id is required"})
                    continue

                found = False
                for col in collections:
                    if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
                        continue
                    try:
                        result = await col.get(ids=[update_id], include=["metadatas", "documents"])
                        if result["ids"]:
                            meta = result["metadatas"][0]
                            doc = result["documents"][0]

                            for field in ("status", "priority", "assigned_to", "title",
                                          "description", "target_version"):
                                if field in item:
                                    if field == "status":
                                        meta["backlog_status"] = item[field]
                                    elif field == "description":
                                        doc = f"# {meta['title']}\n\n{item[field]}"
                                    elif field == "title":
                                        meta["title"] = item[field]
                                    else:
                                        meta[field] = item[field]

                            meta["updated"] = now
                            meta["updated_by"] = session_info["claude_instance"]
                            meta["edit_count"] = meta.get("edit_count", 0) + 1

                            await col.update(ids=[update_id], documents=[doc], metadatas=[meta])

                            # Phase 1 #2 canary: one op-log entry per item.
                            embedding = await fetch_embedding_for_op_log(col, update_id)
                            emit_op_log_from_context(
                                db=get_mongo(),
                                op_type="backlog.updated",
                                actor={
                                    "agent": session_info["claude_instance"],
                                    "project": meta.get("project") or None,
                                    "session_id": session_id,
                                },
                                ref={"collection": col.name, "doc_id": update_id},
                                payload={
                                    "title": meta.get("title"),
                                    "backlog_status": meta.get("backlog_status"),
                                    "priority": meta.get("priority"),
                                    "assigned_to": meta.get("assigned_to") or "",
                                    "target_version": meta.get("target_version") or "",
                                    "edit_count": meta.get("edit_count"),
                                    "updated": now,
                                    "batch_index": i,
                                    "embedding": embedding,
                                },
                            )

                            results["succeeded"] += 1
                            results["ids"].append(update_id)
                            found = True
                            break
                    except Exception:
                        continue

                if not found:
                    results["failed"] += 1
                    results["errors"].append({"index": i, "error": f"Item {update_id} not found"})
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"index": i, "error": str(e)})

    elif action == "complete":
        collections = await chroma.list_collections()
        for i, item in enumerate(items):
            try:
                complete_id = item.get("id")
                if not complete_id:
                    results["failed"] += 1
                    results["errors"].append({"index": i, "error": "id is required"})
                    continue

                wont_do = item.get("wont_do", False)
                resolution = item.get("resolution", "")
                new_status = "wont_do" if wont_do else "done"

                found = False
                for col in collections:
                    if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
                        continue
                    try:
                        result = await col.get(ids=[complete_id], include=["metadatas", "documents"])
                        if result["ids"]:
                            meta = result["metadatas"][0]
                            doc = result["documents"][0]

                            meta["backlog_status"] = new_status
                            meta["completed_at"] = now
                            meta["completed_by"] = session_info["claude_instance"]
                            meta["updated"] = now
                            if resolution:
                                meta["resolution"] = resolution
                                doc += f"\n\n## Resolution\n{resolution}"

                            await col.update(ids=[complete_id], documents=[doc], metadatas=[meta])

                            # Phase 1 #2 canary: complete IS an update on the
                            # backlog row — same op_type as the update branch.
                            embedding = await fetch_embedding_for_op_log(col, complete_id)
                            emit_op_log_from_context(
                                db=get_mongo(),
                                op_type="backlog.updated",
                                actor={
                                    "agent": session_info["claude_instance"],
                                    "project": meta.get("project") or None,
                                    "session_id": session_id,
                                },
                                ref={"collection": col.name, "doc_id": complete_id},
                                payload={
                                    "title": meta.get("title"),
                                    "backlog_status": new_status,
                                    "completed_at": now,
                                    "completed_by": session_info["claude_instance"],
                                    "resolution": resolution or "",
                                    "updated": now,
                                    "batch_index": i,
                                    "embedding": embedding,
                                },
                            )

                            results["succeeded"] += 1
                            results["ids"].append(complete_id)
                            found = True
                            break
                    except Exception:
                        continue

                if not found:
                    results["failed"] += 1
                    results["errors"].append({"index": i, "error": f"Item {complete_id} not found"})
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"index": i, "error": str(e)})

    if not results["errors"]:
        del results["errors"]

    return json.dumps(results, indent=2)
