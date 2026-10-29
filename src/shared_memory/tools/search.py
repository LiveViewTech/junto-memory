"""Search and discovery tools - cross-project search, list projects."""

import json
from typing import List, Optional

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.auth import collection_allowed
from shared_memory.clients import get_chroma
from shared_memory.config import PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import format_age, format_staleness_warning, parse_timestamp, require_session
from shared_memory.state import active_sessions


@mcp.tool()
async def memory_search_global(
    session_id: str,
    query: str,
    memory_types: List[str] = None,
    limit: int = 10,
    ctx: Context = None
) -> str:
    """
    Search across ALL projects and shared memories.

    Use this to find:
    - Patterns that might apply to your current project
    - How similar problems were solved elsewhere
    - Cross-project learnings and gotchas

    Args:
        session_id: Your session ID
        query: Search query
        memory_types: Filter by types (optional)
        limit: Maximum number of results (1-30)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    all_results = []

    # Build where filter
    where_filter = {"status": "active"}
    if memory_types:
        where_filter["type"] = {"$in": memory_types}

    # Search all project collections
    collections = await chroma.list_collections()
    _me = active_sessions.get(session_id)
    for col in collections:
        if ((col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX))
                and collection_allowed(_me, col.name)):  # tenant isolation R5
            try:
                results = await col.query(
                    query_texts=[query],
                    n_results=min(5, limit),
                    where=where_filter
                )

                if results["documents"] and results["documents"][0]:
                    for doc, meta, dist in zip(
                        results["documents"][0],
                        results["metadatas"][0],
                        results["distances"][0]
                    ):
                        staleness = format_staleness_warning(meta)
                        all_results.append({
                            "collection": col.name,
                            "title": meta.get("title", "Untitled"),
                            "type": meta.get("type"),
                            "relevance": 1 - dist,
                            "created": meta.get("created", ""),
                            "updated": meta.get("updated", ""),
                            "age": format_age(meta.get("updated") or meta.get("created")),
                            "content_preview": doc[:300] + "..." if len(doc) > 300 else doc,
                            "warning": staleness if staleness else None
                        })
            except Exception:
                continue

    # Tiered sort: relevance bands + recency within each band
    def _sort_key(r):
        rel = max(0, r["relevance"])
        band = 0 if rel > 0.70 else (1 if rel > 0.50 else 2)
        ts = parse_timestamp(r.get("updated") or r.get("created"))
        epoch = ts.timestamp() if ts else 0
        return (band, -epoch)

    all_results.sort(key=_sort_key)
    all_results = all_results[:limit]

    # Format relevance as percentage
    for r in all_results:
        r["relevance"] = f"{max(0, r['relevance']):.0%}"

    return json.dumps({
        "query": query,
        "result_count": len(all_results),
        "results": all_results,
        "note": "Results from all projects and shared collections, sorted by relevance."
    }, indent=2)


def _caller_scope() -> Optional[dict]:
    """A session_info-shaped scope for a session-less call, from the Bearer key.

    None = unrestricted (auth off, or an unscoped/keyless caller on a server
    that allows keyless). A key with a project list scopes to it. Under
    JUNTO_REQUIRE_KEY a missing/invalid key gets a scope no project matches.
    """
    from shared_memory import auth
    if not auth.AUTH_ENABLED:
        return None
    key = auth.get_header_api_key()
    info = auth.validate_api_key(key) if key else None
    if info:
        return {"allowed_projects": info.get("projects") or []}
    if auth.REQUIRE_KEY:
        return {"allowed_projects": ["\x00no-project"]}
    return None


@mcp.tool()
async def memory_list_projects(ctx: Context = None) -> str:
    """
    List all projects with memory collections.

    No session required - useful for initial orientation.
    """
    chroma = await get_chroma()
    collections = await chroma.list_collections()

    projects = []
    shared = []
    # Session-less, so scope comes from the request's Bearer key (tenant
    # isolation R5): a scoped key sees its own projects; under
    # JUNTO_REQUIRE_KEY a keyless or invalid caller sees shared only.
    caller = _caller_scope()

    for col in collections:
        if col.name.startswith(PROJECT_PREFIX) and not collection_allowed(caller, col.name):
            continue
        if col.name.startswith(PROJECT_PREFIX):
            project_name = col.name[len(PROJECT_PREFIX):]
            try:
                count = await col.count()
            except Exception:
                count = 0
            projects.append({
                "project": project_name,
                "collection": col.name,
                "document_count": count
            })
        elif col.name.startswith(SHARED_PREFIX):
            shared_name = col.name[len(SHARED_PREFIX):]
            try:
                count = await col.count()
            except Exception:
                count = 0
            shared.append({
                "name": shared_name,
                "collection": col.name,
                "document_count": count
            })

    return json.dumps({
        "projects": projects,
        "shared_collections": shared,
        "active_sessions": len(active_sessions)
    }, indent=2)
