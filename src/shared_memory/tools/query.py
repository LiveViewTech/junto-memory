"""Query and retrieval tools - search knowledge base, get documents."""

import json
from datetime import timedelta
from typing import List

from mcp.server.fastmcp import Context

from shared_memory import query_config
from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import OVERLAP_WINDOW_HOURS, PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import (
    cleanup_stale_signals,
    embed_query_once,
    format_age,
    format_staleness_warning,
    format_status_warning,
    get_project_collection,
    get_shared_collection,
    is_expired,
    normalize_project,
    parse_timestamp,
    project_from_collection,
    query_kwargs,
    require_session,
    update_access_stats,
    utc_now,
    utc_now_iso,
)
from shared_memory.state import active_sessions, active_signals

# Relevance threshold - results below this are excluded
# Chroma L2 distance: 0 = identical, 1 = quite different, 2+ = very different
# We convert to similarity: 1 - (dist/2) gives 0-1 range
MIN_RELEVANCE_THRESHOLD = 0.3  # 30% minimum relevance


def calculate_relevance(distance: float) -> float:
    """Convert Chroma L2 distance to 0-1 relevance score.

    L2 distances typically range 0-2 for normalized embeddings.
    We clamp and convert to similarity percentage.
    """
    # Clamp distance to reasonable range
    dist = max(0, min(distance, 2.0))
    # Convert to similarity (0-1 range)
    return 1 - (dist / 2)


@mcp.tool()
async def memory_query(
    session_id: str,
    query: str,
    project: str = None,
    memory_types: List[str] = None,
    include_inactive: bool = False,
    include_shared: bool = True,
    updated_within_days: int = None,
    expand: bool = None,
    expand_top: int = None,
    snippet_length: int = None,
    limit: int = 3,
    ctx: Context = None
) -> str:
    """
    Search the knowledge base for relevant information.

    Use this BEFORE implementing something to check:
    - Has this been done before?
    - Are there known patterns or gotchas?
    - What decisions were made about this area?

    FRESHNESS DISCIPLINE (relocated from session guidelines): results rank by
    text relevance, NOT recency — check the age field on every result; prefer
    newer when several cover one topic; verify anything older than ~30 days
    against reality before acting on it (results carry staleness warnings).
    Handoffs older than 14 days are noise — flag them for archival. If a
    result is wrong or outdated and it's yours (or a simple factual fix),
    memory_change_status(new_status="superseded") + record the correction; if
    it's another agent's spec/decision, message their project coordinator
    rather than leaving it for the next reader.

    Args:
        session_id: Your session ID
        query: Natural language query
        project: Project to search (omit to search shared memories only)
        memory_types: Filter by types (api_spec, architecture, learning, pattern, etc.)
        include_inactive: Include deprecated/superseded/archived documents
        include_shared: Search shared patterns/context (default True, set False for project-only)
        updated_within_days: Only return hits whose `updated` (or `created` fallback)
            timestamp is within the last N days. Omit (default None) to disable
            the recency filter. Server-side filter — cheaper than load-then-filter.
        expand: When True, results include full `content`. When False, results
            include a `snippet` (first `snippet_length` chars) instead — much
            smaller payload for triage-style queries. Omit (default None) to
            use the per-project / server default (server default ships True
            so existing callers see no change). Caller-supplied value always
            wins over config.
        expand_top: If >0, the top-N results get full `content` even when
            `expand=False`; the rest get snippets. Lets callers say "real
            content for the most relevant 1-3, previews for the rest."
            Omit (default None) to use the configured default (0 = off).
        snippet_length: Max chars in the preview snippet when expand=False.
            Omit (default None) to use the configured default (200).
        limit: Maximum number of results (1-10, default 3)
    """
    error = require_session(session_id)
    if error:
        return error

    if updated_within_days is not None and updated_within_days < 1:
        return json.dumps({"error": "updated_within_days must be >= 1"})

    chroma = await get_chroma()
    active_sessions[session_id]["last_activity"] = utc_now_iso()

    if project:
        project = normalize_project(project)

    # Resolve expand / expand_top / snippet_length from caller args, falling
    # back to per-project then server-default config. backlog_6d5aa1a2849f.
    _qcfg = query_config.get_effective_config(get_mongo(), project)
    if expand is None:
        expand = bool(_qcfg["default_expand"])
    if expand_top is None:
        expand_top = int(_qcfg["default_expand_top"])
    if snippet_length is None:
        snippet_length = int(_qcfg["default_snippet_length"])
    if expand_top < 0:
        expand_top = 0
    if snippet_length < 50:
        snippet_length = 50

    recency_cutoff = None
    if updated_within_days is not None:
        recency_cutoff = utc_now() - timedelta(days=int(updated_within_days))

    results = []

    # Build where filter
    where_filter = {}
    if not include_inactive:
        where_filter["status"] = "active"
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    where_clause = where_filter if where_filter else None

    # ── ONE embedding for the whole request (backlog_c5dba6d13707) ──
    # This used to embed `query` once PER COLLECTION — project + shared
    # patterns + shared context — at ~96-138 ms each, on the event loop.
    # See embed_query_once() for why that serialised the whole server.
    # _qvec stays None until the first collection is in hand (the embedding
    # function lives on the collection object); every later scan reuses it.
    _qvec = None

    # Search project collection if specified
    if project:
        try:
            proj_collection = await get_project_collection(chroma, project)
            _qvec = await embed_query_once(proj_collection, query)
            proj_results = await proj_collection.query(
                **query_kwargs(query, _qvec),
                n_results=limit,
                where=where_clause
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                )):
                    # Skip expired documents
                    if is_expired(meta):
                        continue

                    # Calculate relevance and skip if below threshold
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    # Recency filter (optional)
                    if recency_cutoff is not None:
                        ts = parse_timestamp(meta.get("updated") or meta.get("created"))
                        if ts is None or ts < recency_cutoff:
                            continue

                    status = meta.get("status", "active")
                    doc_id = proj_results["ids"][0][i] if proj_results["ids"] else None

                    staleness = format_staleness_warning(meta)
                    warning = format_status_warning(status, meta.get("superseded_by"))
                    if staleness:
                        warning = (warning + " " + staleness).strip() if warning else staleness

                    results.append({
                        "source": f"project:{project}",
                        "id": doc_id or meta.get("id", "unknown"),
                        "title": meta.get("title", "Untitled"),
                        "type": meta.get("type"),
                        "status": status,
                        "relevance": f"{relevance:.0%}",
                        "created": meta.get("created", ""),
                        "updated": meta.get("updated", ""),
                        "age": format_age(meta.get("updated") or meta.get("created")),
                        "content": doc,
                        "access_count": meta.get("access_count", 0),
                        # Creation identity (ratified authored_by v1.1.0,
                        # msg_96c9f7fc73ee): the claude_instance stamped at
                        # record_learning/store time, never rewritten by any
                        # update path. None when the doc predates identity
                        # capture — consumers fail open on null.
                        "authored_by": meta.get("claude_instance"),
                        "warning": warning if warning else None
                    })

                    # Track access (fire-and-forget)
                    if doc_id:
                        await update_access_stats(proj_collection, doc_id)
        except Exception:
            pass

    # Search shared collections only if requested and with higher threshold
    if include_shared:
        # Shared results need higher relevance to be included (reduces noise)
        shared_threshold = MIN_RELEVANCE_THRESHOLD + 0.1  # 40% for shared

        for shared_name in ["patterns", "context"]:
            try:
                shared = await get_shared_collection(chroma, shared_name)
                if _qvec is None:
                    # No project scan happened, so this is the first collection
                    # we hold — embed here and the second shared scan reuses it.
                    _qvec = await embed_query_once(shared, query)
                shared_results = await shared.query(
                    **query_kwargs(query, _qvec),
                    n_results=min(2, limit),  # Max 2 from each shared collection
                    where=where_clause
                )

                if shared_results["documents"] and shared_results["documents"][0]:
                    for i, (doc, meta, dist) in enumerate(zip(
                        shared_results["documents"][0],
                        shared_results["metadatas"][0],
                        shared_results["distances"][0]
                    )):
                        # Skip expired documents
                        if is_expired(meta):
                            continue

                        # Calculate relevance and skip if below threshold
                        relevance = calculate_relevance(dist)
                        if relevance < shared_threshold:
                            continue

                        # Recency filter (optional)
                        if recency_cutoff is not None:
                            ts = parse_timestamp(meta.get("updated") or meta.get("created"))
                            if ts is None or ts < recency_cutoff:
                                continue

                        doc_id = shared_results["ids"][0][i] if shared_results["ids"] else None

                        staleness = format_staleness_warning(meta)

                        results.append({
                            "source": f"shared:{shared_name}",
                            "id": doc_id,
                            "title": meta.get("title", "Untitled"),
                            "type": meta.get("type"),
                            "relevance": f"{relevance:.0%}",
                            "created": meta.get("created", ""),
                            "updated": meta.get("updated", ""),
                            "age": format_age(meta.get("updated") or meta.get("created")),
                            "content": doc[:500] + "..." if len(doc) > 500 else doc,
                            "access_count": meta.get("access_count", 0),
                            "authored_by": meta.get("claude_instance"),
                            "warning": staleness if staleness else None
                        })

                        # Track access (fire-and-forget)
                        if doc_id:
                            await update_access_stats(shared, doc_id)
            except Exception:
                pass

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching memories found. This might be new territory - consider recording what you learn!"
        }, indent=2)

    # Tiered sort: group by relevance band, then sort by recency within each band.
    # This prevents old docs from outranking fresh ones on the same topic.
    def _sort_key(r):
        # Relevance band: >70% = 0 (best), 50-70% = 1, <50% = 2
        pct = int(r["relevance"].rstrip("%")) / 100
        band = 0 if pct > 0.70 else (1 if pct > 0.50 else 2)
        # Within band, sort by updated timestamp descending (newest first)
        ts = parse_timestamp(r.get("updated") or r.get("created"))
        epoch = ts.timestamp() if ts else 0
        return (band, -epoch)

    results.sort(key=_sort_key)
    results = results[:limit]

    # ── Facets inline delivery (design:memory-facets-v0 §consumer) ──
    # Contract guarantee: when a result row's learning has facets, they ride
    # the row itself — consumers (sub's rater) must never need a per-candidate
    # get_by_id round-trip. Batched single find; absent rows stay absent (the
    # facets container is optional forever). Best-effort by design.
    try:
        from shared_memory.facets import get_facets_for_ids
        _facet_map = get_facets_for_ids(
            get_mongo(), [r["id"] for r in results if str(r.get("id", "")).startswith("learning_")]
        )
        for r in results:
            f = _facet_map.get(r.get("id"))
            if f:
                r["facets"] = f
    except Exception:
        pass

    # ── Modality shape flag (Tom-approved 2026-08-10) ──
    # A SHAPE flag, never a defect predictor: "the body declares a proposal or
    # a deployment state — open it" is true for every row it fires on, so it
    # cannot mis-fire. Derived from the content already on the row (same
    # title + first-300 window the 75-doc measurement used), so it needs no
    # stored field and covers the whole historical corpus. See facets.modality_shape.
    try:
        from shared_memory.facets import SHAPE_LABELS, modality_shape
        for r in results:
            if not str(r.get("id", "")).startswith("learning_"):
                continue
            shape = modality_shape(r.get("title", ""), r.get("content", ""))
            if shape:
                r["shape"] = shape
                r["shape_note"] = (
                    f"{SHAPE_LABELS[shape]}: this doc's body declares a proposal or an "
                    f"undeployed state. The extracted claim may render it as fact — "
                    f"open the body before acting."
                )
    except Exception:
        pass

    # ── Preview-mode trim (backlog_6d5aa1a2849f) ──
    # After sort+limit, decide per-position whether to ship full content or
    # just a snippet. Top-N hits get content when `expand_top > 0`; rest get
    # snippets when `expand=False`. When `expand=True` everyone gets content.
    for i, r in enumerate(results):
        full_content = r.get("content", "") or ""
        # Always provide a snippet for predictable shape, even when expanded.
        snippet = full_content[:snippet_length]
        if len(full_content) > snippet_length:
            snippet = snippet.rstrip() + "…"
        r["snippet"] = snippet
        give_content = bool(expand) or (expand_top > 0 and i < expand_top)
        if not give_content:
            r.pop("content", None)

    return json.dumps({
        "query": query,
        "result_count": len(results),
        "expand": bool(expand),
        "expand_top": int(expand_top),
        "snippet_length": int(snippet_length),
        "results": results
    }, indent=2)


@mcp.tool()
async def memory_get_by_id(
    session_id: str,
    doc_id: str,
    project: str = None,
    ctx: Context = None
) -> str:
    """
    Retrieve a document by its ID.

    Use this when you have a specific document ID (from memory_store, memory_query, etc.)
    and want to retrieve the full content.

    Also accepts:
    - Message IDs (msg_*) — returns the message in the same shape as
      memory_get_messages(message_id=...), same permission rules.
    - Unique ID prefixes of 12+ chars, with or without the type prefix
      (e.g. "7cc0b78f0e53" or "learning_7cc0b78f0e53" for
      "learning_7cc0b78f0e53b4bb"). Ambiguous prefixes return the candidates.

    Args:
        session_id: Your session ID
        doc_id: The document ID (e.g., "34e6c10ceecf9b59" or full ID)
        project: Project to search (omit to search all projects + shared)
    """
    error = require_session(session_id)
    if error:
        return error

    # ── Message dispatch (backlog_f6f950b3b4ce) ──
    # Messages live in Mongo, not Chroma. Same auth + read-marking semantics
    # as memory_get_messages(message_id=...); entry shape shared via
    # _message_entry so the two surfaces cannot diverge.
    if doc_id.startswith("msg_"):
        from shared_memory.tools.messaging import _mark_messages_read, _message_entry
        from shared_memory.tools.projects import _is_project_admin

        db = get_mongo()
        if db is None:
            return json.dumps({"found": False, "id": doc_id, "error": "MongoDB unavailable"})
        doc = db.messages.find_one({"_id": doc_id})
        if not doc:
            return json.dumps({
                "found": False,
                "id": doc_id,
                "error": f"Message not found: {doc_id}"
            }, indent=2)
        session_info = active_sessions[session_id]
        my_instance = session_info["claude_instance"]
        my_project = normalize_project(session_info.get("project", ""))
        my_role = session_info.get("role", "agent")
        is_admin = (my_role in ("admin", "user")) or _is_project_admin(db, my_project, my_instance)
        if not is_admin:
            msg_to = doc.get("to_instance", doc.get("to", ""))
            msg_project = doc.get("to_project", "")
            if msg_to != my_instance and msg_to != "*":
                return json.dumps({
                    "found": False,
                    "id": doc_id,
                    "error": "Permission denied. Only admins/coordinators can view other agents' messages."
                }, indent=2)
            if msg_project and msg_project != my_project:
                return json.dumps({
                    "found": False,
                    "id": doc_id,
                    "error": "Permission denied. Message belongs to a different project."
                }, indent=2)
        # Body-pull marks read — canonical read of your own message; skip when
        # an admin peeks at someone else's (don't mark read on their behalf).
        if doc.get("to_instance", doc.get("to")) in (my_instance, "*"):
            _mark_messages_read(db, [doc["_id"]], my_instance)
        return json.dumps({
            "found": True,
            "id": doc_id,
            "collection": "messages",
            "type": "message",
            "message": _message_entry(doc)
        }, indent=2)

    chroma = await get_chroma()

    if project:
        project = normalize_project(project)

    # Build list of collections to search
    collections_to_search = []

    if project:
        # Search specific project + shared collections
        collections_to_search.append(await get_project_collection(chroma, project))
        collections_to_search.append(await get_shared_collection(chroma, "patterns"))
        collections_to_search.append(await get_shared_collection(chroma, "context"))
        collections_to_search.append(await get_shared_collection(chroma, "work"))
    else:
        # Search all collections
        all_collections = await chroma.list_collections()
        for col in all_collections:
            if col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX):
                collections_to_search.append(col)

    async def _found_response(col, found_id: str, meta: dict, doc: str,
                              resolved_from: str = None) -> str:
        # Update access tracking
        meta["access_count"] = meta.get("access_count", 0) + 1
        meta["last_accessed"] = utc_now_iso()
        await col.update(ids=[found_id], metadatas=[meta])

        payload = {
            "found": True,
            "id": found_id,
            "collection": col.name,
            "title": meta.get("title", "Untitled"),
            "type": meta.get("type", "unknown"),
            "status": meta.get("status", "active"),
            # Collection wins when the metadata copy is absent — see
            # project_from_collection(). Learnings have never carried the key.
            "project": meta.get("project") or project_from_collection(col.name),
            "tags": json.loads(meta.get("tags", "[]")),
            "created": meta.get("created"),
            "updated": meta.get("updated"),
            # Creation identity (interface:recall-v0 §authored_by; the
            # get_by_id surface gap was a proven false-clear trap,
            # backlog_601d268dbe6b). Null = pre-identity-capture doc —
            # consumers fail open.
            "authored_by": meta.get("claude_instance"),
            "content": doc
        }
        if resolved_from:
            payload["resolved_from_prefix"] = resolved_from
        # Facets ride the follow-through surface too (get_by_id is the
        # documented pointer-chase for /recall). Best-effort, learnings only.
        if found_id.startswith("learning_"):
            try:
                from shared_memory.facets import (
                    SHAPE_LABELS,
                    get_facets_for_ids,
                    modality_shape,
                )
                f = get_facets_for_ids(get_mongo(), [found_id]).get(found_id)
                if f:
                    payload["facets"] = f
                _shape = modality_shape(meta.get("title", ""), doc)
                if _shape:
                    payload["shape"] = _shape
                    payload["shape_note"] = (
                        f"{SHAPE_LABELS[_shape]}: this doc's body declares a proposal "
                        f"or an undeployed state — read the body, not the claim."
                    )
            except Exception:
                pass
        # Pull-through join (recall_metrics): if this doc was recall-injected
        # to this project in the last 24h, this fetch IS the follow-through
        # the A2(ii) metric wants. Best-effort, one indexed read.
        try:
            from shared_memory.recall_metrics import log_pull_if_injected
            _sess = active_sessions.get(session_id, {})
            log_pull_if_injected(get_mongo(), found_id,
                                 _sess.get("claude_instance"),
                                 _sess.get("project"))
        except Exception:
            pass
        return json.dumps(payload, indent=2)

    # Exact-ID lookup
    for col in collections_to_search:
        try:
            result = await col.get(
                ids=[doc_id],
                include=["metadatas", "documents"]
            )

            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0] if result["documents"] else ""
                return await _found_response(col, doc_id, meta, doc)
        except Exception:
            continue

    # ── Prefix resolution fallback (backlog_fa06355f851b) ──
    # Agents cite 12-char ID prefixes in messages/logs. On exact miss, scan
    # ids (cheap, ids-only get) for a unique startswith match. A typed input
    # ("learning_abc...") only matches that type; a bare hex input matches
    # against each id's post-type tail. Minimum 12 chars of tail to keep
    # matches meaningful.
    tail = doc_id.split("_", 1)[1] if "_" in doc_id else doc_id
    if len(tail) >= 12:
        matches = {}  # full_id -> collection (dedupe: same id in 2 collections)
        for col in collections_to_search:
            try:
                res = await col.get(include=[])
                for fid in res["ids"]:
                    if fid in matches:
                        continue
                    if "_" in doc_id:
                        matched = fid.startswith(doc_id)
                    else:
                        ftail = fid.split("_", 1)[1] if "_" in fid else fid
                        matched = fid.startswith(doc_id) or ftail.startswith(doc_id)
                    if matched:
                        matches[fid] = col
            except Exception:
                continue

        if len(matches) == 1:
            fid, col = next(iter(matches.items()))
            try:
                result = await col.get(ids=[fid], include=["metadatas", "documents"])
                if result["ids"]:
                    meta = result["metadatas"][0]
                    doc = result["documents"][0] if result["documents"] else ""
                    return await _found_response(col, fid, meta, doc,
                                                 resolved_from=doc_id)
            except Exception:
                pass
        elif len(matches) > 1:
            return json.dumps({
                "found": False,
                "id": doc_id,
                "error": f"Ambiguous prefix: {len(matches)} documents match",
                "candidates": sorted(matches.keys())[:10]
            }, indent=2)

    return json.dumps({
        "found": False,
        "id": doc_id,
        "error": f"Document not found with ID: {doc_id}",
        "hint": "Try memory_query() to search by content, or check the project parameter. "
                "ID prefixes need at least 12 chars (excluding the type prefix); "
                "msg_* IDs resolve from the message store."
    }, indent=2)


@mcp.tool()
async def memory_get_active_work(
    session_id: str,
    project: str = None,
    instance: str = None,
    since_hours: int = None,
    limit: int = 20,
    ctx: Context = None
) -> str:
    """
    See what other Claudes are currently working on.

    Use this to:
    - Avoid working on the same files
    - Understand what's in progress
    - Coordinate with other Claude instances

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
        instance: Filter by specific Claude instance name
        since_hours: Only show work updated within this many hours
        limit: Maximum results to return (default 20)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()

    if project:
        project = normalize_project(project)

    # Get from active sessions (in-memory)
    active_work = []
    since_cutoff = None
    if since_hours:
        since_cutoff = (utc_now() - timedelta(hours=since_hours)).isoformat()

    for sid, info in active_sessions.items():
        if sid != session_id:
            # Service principals are invisible to active-work enumeration
            # (design:identity-lifecycle-v0 Mechanism A) — this is the one
            # enumerator keyed off the session rather than a registry row.
            if info.get("is_principal"):
                continue
            if project and normalize_project(info["project"]) != project:
                continue
            if instance and info["claude_instance"] != instance:
                continue
            if since_cutoff and info.get("last_activity", "") < since_cutoff:
                continue
            active_work.append({
                "session_id": sid,
                "claude_instance": info["claude_instance"],
                "project": info["project"],
                "task": info["task"],
                "started": info["started"],
                "last_activity": info["last_activity"]
            })

    # Apply limit to active sessions
    active_work = active_work[:limit]

    # Also get recent work items from Chroma
    work_collection = await get_shared_collection(chroma, "work")
    cutoff = (utc_now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    where_filter = None
    if project:
        where_filter = {"project": project}

    try:
        recent = await work_collection.get(
            where=where_filter,
            include=["documents", "metadatas"]
        )

        recent_work = []
        if recent["documents"]:
            for doc, meta in zip(recent["documents"], recent["metadatas"]):
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                recent_work.append({
                    "title": meta.get("title"),
                    "status": meta.get("status"),
                    "claude": meta.get("claude_instance"),
                    "project": meta.get("project"),
                    "files": json.loads(meta.get("files_touched", "[]")),
                    "updated": meta.get("updated")
                })
    except Exception:
        recent_work = []

    # NEW: Include blocked agents info
    blocked_agents = []
    for sid, info in active_sessions.items():
        if info.get("blocked_by"):
            blocked_agents.append({
                "agent": info.get("claude_instance"),
                "waiting_for": info.get("blocked_by"),
                "signal": info.get("waiting_for_signal"),
                "reason": info.get("blocked_reason")
            })

    # NEW: Include recent signals
    cleanup_stale_signals()
    recent_signals = list(active_signals.values())[:10]

    return json.dumps({
        "currently_active": active_work[:limit],
        "blocked_agents": blocked_agents[:limit],
        "recent_signals": recent_signals[:10],
        "recent_work_items": recent_work[:limit],
        "overlap_window_hours": OVERLAP_WINDOW_HOURS
    }, indent=2)
