"""Function reference tools - register, find, and enrich function references."""

import asyncio
import hashlib
import json
import os
from typing import Any, Dict, List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import (
    MIN_RELEVANCE_THRESHOLD,
    calculate_relevance,
    get_project_collection,
    get_shared_collection,
    normalize_project,
    require_session,
    update_access_stats,
    utc_now_iso,
)
from shared_memory.op_log import emit_op_log_from_context, fetch_embedding_for_op_log
from shared_memory.state import active_sessions

# Enrichment queue for librarian processing (in-memory, processed async)
# Structure: { func_id: { project, file, name, registered_at, enriched: bool } }
function_enrichment_queue: Dict[str, Dict[str, Any]] = {}

# Librarian webhook URL (set to None to disable)
LIBRARIAN_WEBHOOK_URL = os.getenv("LIBRARIAN_WEBHOOK_URL", "http://localhost:8085/webhook")


async def notify_librarian(func_info: Dict[str, Any]):
    """Send webhook notification to librarian service."""
    if not LIBRARIAN_WEBHOOK_URL:
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                LIBRARIAN_WEBHOOK_URL,
                json={
                    "event": "function_registered",
                    "function": func_info
                }
            )
    except Exception as e:
        # Don't fail the registration if librarian is down
        print(f"[MCP] Librarian webhook failed (non-fatal): {e}")


@mcp.tool()
async def memory_register_function(
    session_id: str,
    name: str,
    file: str,
    purpose: str,
    project: str = None,
    gotchas: str = None,
    prefer_over: str = None,
    requires: List[str] = None,
    code: str = None,
    ctx: Context = None
) -> str:
    """
    Register a function for AI-optimized reference.

    Call this AFTER creating or significantly modifying any function: name,
    file:LINE, purpose, gotchas (include `code` when behavior is non-obvious).
    Unregistered functions force the next agent to re-read entire files —
    touching 3+ functions without registering them is technical debt.

    MINIMAL INPUT - just register what you know, librarian enriches the rest.

    For simple functions:
        memory_register_function(name="get_user", file="src/users.py:45", purpose="Fetch user by ID")

    For tricky/weird functions, include code for librarian analysis:
        memory_register_function(name="parse_email", file="src/parser.py:145",
            purpose="Parse raw email", gotchas="Use over v1 - attachment bug", code="def parse_email...")

    Args:
        session_id: Your session ID
        name: Function name
        file: File path with line number (e.g., "src/parser.py:145")
        purpose: One-line description of what this function does
        project: Project this belongs to (omit for shared/cross-project)
        gotchas: Non-obvious behaviors, pitfalls, or warnings (optional)
        prefer_over: Other functions this should be used instead of (optional)
        requires: Functions/setup that must be called first (optional)
        code: Full function code - include for tricky functions so librarian can analyze (optional)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()
    requires = requires or []

    # Write-lint (backlog_1115f9fe35f7): register_function was the ONE free-text
    # writer NOT routed through the envelope recovery path, so func-ref bodies
    # leaked freely (`**Gotchas:** …</gotchas>\n</invoke>\n<invoke name=…`,
    # learning_08dfd3f8cbf6f0ce). Recover gotchas + purpose and route a swallowed
    # project BEFORE collection selection. Best-effort; never blocks the write.
    try:
        from shared_memory.write_lint import recover_envelope_leak
        if gotchas:
            gotchas, project, _ = recover_envelope_leak(gotchas, "gotchas", project)
        if purpose:
            purpose, project, _ = recover_envelope_leak(purpose, "purpose", project)
    except Exception:
        pass

    if project:
        project = normalize_project(project)

    # Determine collection. chroma_collection_name mirrors get_*_collection
    # output so op-log `ref.collection` matches what §5.1 sync replay and
    # §4.7 reconciliation will look up.
    if project:
        collection = await get_project_collection(chroma, project)
        chroma_collection_name = f"proj_{project}"
    else:
        collection = await get_shared_collection(chroma, "patterns")
        chroma_collection_name = "shared_patterns"

    # Generate stable ID based on project + file + name
    id_base = f"{project or 'shared'}:{file}:{name}"
    func_id = f"func_{hashlib.sha256(id_base.encode()).hexdigest()[:12]}"

    # Check if function already exists and preserve enrichment data
    existing = await collection.get(ids=[func_id], include=["metadatas", "documents"])
    existing_meta = existing["metadatas"][0] if existing["metadatas"] else None
    is_update = existing_meta is not None

    # Build the document content (AI-readable format)
    doc_parts = [
        f"# Function: {name}",
        f"**Location:** {file}",
        f"**Purpose:** {purpose}"
    ]

    if gotchas:
        doc_parts.append(f"**Gotchas:** {gotchas}")
    if prefer_over:
        doc_parts.append(f"**Prefer over:** {prefer_over}")
    if requires:
        doc_parts.append(f"**Requires:** {', '.join(requires)}")
    if code:
        doc_parts.append(f"\n**Code:**\n```\n{code}\n```")

    content = "\n\n".join(doc_parts)

    # Build metadata - preserve enrichment fields if they exist
    metadata = {
        "title": f"{name} - {purpose[:50]}",
        "type": "function_ref",
        "status": "active",
        "func_name": name,
        "func_file": file,
        "func_purpose": purpose,
        "project": project or "",
        "session_id": session_id,
        "claude_instance": session_info["claude_instance"],
        "created": existing_meta.get("created", now) if existing_meta else now,
        "updated": now,
        "enriched": existing_meta.get("enriched", "false") if existing_meta else "false",
        "has_code": "true" if code else "false",
        "access_count": existing_meta.get("access_count", 0) if existing_meta else 0,
        "last_accessed": now
    }

    # Preserve librarian enrichment fields if they exist
    if existing_meta:
        enrichment_fields = ["signature", "parameters", "returns", "calls",
                           "side_effects", "complexity", "search_summary"]
        for field in enrichment_fields:
            if field in existing_meta:
                metadata[field] = existing_meta[field]

        # Preserve search_summary in document content if it exists
        if existing_meta.get("search_summary") and existing_meta.get("enriched") == "true":
            content = f"**Search Summary:** {existing_meta['search_summary']}\n\n" + content

    if gotchas:
        metadata["gotchas"] = gotchas
    if prefer_over:
        metadata["prefer_over"] = prefer_over
    if requires:
        metadata["requires"] = json.dumps(requires)

    # Upsert (allows updates to same function)
    await collection.upsert(
        ids=[func_id],
        documents=[content],
        metadatas=[metadata]
    )

    # Phase 1 #2 canary 3/13: emit op-log entry per §4.3.a (best-effort).
    # First-registration and re-registration both go through the same upsert
    # and emit the same op_type — `is_update` is replay-side state, not part
    # of the op. Code is included in payload so sync replay can rebuild the
    # exact document content; enrichment merge (when is_update) is reapplied
    # by a subsequent function.enriched op in the same log.
    embedding = await fetch_embedding_for_op_log(collection, func_id)
    emit_op_log_from_context(
        db=get_mongo(),
        op_type="function.registered",
        actor={
            "agent": session_info["claude_instance"],
            "project": project,
            "session_id": session_id,
        },
        ref={"collection": chroma_collection_name, "doc_id": func_id},
        payload={
            "name": name,
            "file": file,
            "purpose": purpose,
            "gotchas": gotchas,
            "prefer_over": prefer_over,
            "requires": requires,
            "has_code": bool(code),
            "code": code,
            "registered_at": now,
            "embedding": embedding,
        },
    )

    # Add to enrichment queue for librarian
    queue_entry = {
        "id": func_id,
        "project": project,
        "file": file,
        "name": name,
        "purpose": purpose,
        "gotchas": gotchas,
        "has_code": bool(code),
        "registered_at": now,
        "enriched": False
    }
    function_enrichment_queue[func_id] = queue_entry

    # Notify librarian service (async, non-blocking)
    asyncio.create_task(notify_librarian(queue_entry))

    result = {
        "status": "updated" if is_update else "registered",
        "id": func_id,
        "name": name,
        "file": file,
        "queued_for_enrichment": True,
        "preserved_enrichment": is_update and existing_meta.get("enriched") == "true"
    }

    if is_update and existing_meta.get("enriched") == "true":
        result["note"] = "Updated - preserved existing librarian enrichment"
    elif code:
        result["note"] = "Code included - librarian will perform deep analysis"
    else:
        result["note"] = "Basic registration - librarian will enrich if file accessible"

    return json.dumps(result)


@mcp.tool()
async def memory_find_function(
    session_id: str,
    query: str,
    project: str = None,
    include_shared: bool = True,
    limit: int = 5,
    ctx: Context = None
) -> str:
    """
    Find functions by purpose, name, or description.

    Call this BEFORE implementing ANY function — 200+ are registered and most
    of what you'd write probably exists. Skipping this check produces
    duplicates the next agent has to reconcile.

    Use this BEFORE implementing something to check:
    - Does a function for this already exist?
    - Which function should I use for X?
    - Are there gotchas I should know about?

    Args:
        session_id: Your session ID
        query: What you're looking for (e.g., "parse email", "user authentication", "database connection")
        project: Limit to specific project (omit to search project + shared)
        include_shared: Include cross-project functions (default True)
        limit: Maximum results (default 5)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    active_sessions[session_id]["last_activity"] = utc_now_iso()
    session_info = active_sessions[session_id]

    results = []
    where_filter = {"$and": [{"type": "function_ref"}, {"status": "active"}]}

    # Search project collection
    search_project = project or session_info.get("project")
    if search_project:
        search_project = normalize_project(search_project)
    if search_project:
        try:
            proj_collection = await get_project_collection(chroma, search_project)
            proj_results = await proj_collection.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter
            )

            if proj_results["documents"] and proj_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    proj_results["documents"][0],
                    proj_results["metadatas"][0],
                    proj_results["distances"][0]
                )):
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    doc_id = proj_results["ids"][0][i]
                    results.append({
                        "source": f"project:{search_project}",
                        "id": doc_id,
                        "name": meta.get("func_name"),
                        "file": meta.get("func_file"),
                        "purpose": meta.get("func_purpose"),
                        "gotchas": meta.get("gotchas"),
                        "prefer_over": meta.get("prefer_over"),
                        "requires": json.loads(meta.get("requires", "[]")),
                        "relevance": f"{relevance:.0%}",
                        "enriched": meta.get("enriched") == "true",
                        "access_count": meta.get("access_count", 0)
                    })

                    # Track access
                    await update_access_stats(proj_collection, doc_id)
        except Exception as e:
            print(f"[memory_find_function] Error searching project collection: {e}")

    # Search shared collection
    if include_shared:
        try:
            shared = await get_shared_collection(chroma, "patterns")
            shared_results = await shared.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter
            )

            if shared_results["documents"] and shared_results["documents"][0]:
                for i, (doc, meta, dist) in enumerate(zip(
                    shared_results["documents"][0],
                    shared_results["metadatas"][0],
                    shared_results["distances"][0]
                )):
                    relevance = calculate_relevance(dist)
                    if relevance < MIN_RELEVANCE_THRESHOLD:
                        continue

                    doc_id = shared_results["ids"][0][i]
                    results.append({
                        "source": "shared",
                        "id": doc_id,
                        "name": meta.get("func_name"),
                        "file": meta.get("func_file"),
                        "purpose": meta.get("func_purpose"),
                        "gotchas": meta.get("gotchas"),
                        "prefer_over": meta.get("prefer_over"),
                        "requires": json.loads(meta.get("requires", "[]")),
                        "relevance": f"{relevance:.0%}",
                        "enriched": meta.get("enriched") == "true",
                        "access_count": meta.get("access_count", 0)
                    })

                    await update_access_stats(shared, doc_id)
        except Exception as e:
            print(f"[memory_find_function] Error searching shared collection: {e}")

    # Sort by relevance
    results.sort(key=lambda x: x["relevance"], reverse=True)
    results = results[:limit]

    # Clean up None values for cleaner output
    for r in results:
        r = {k: v for k, v in r.items() if v is not None and v != [] and v != ""}

    if not results:
        return json.dumps({
            "query": query,
            "results": [],
            "message": "No matching functions found. Consider registering functions as you write them!"
        })

    return json.dumps({
        "query": query,
        "result_count": len(results),
        "results": results
    }, indent=2)


@mcp.tool()
async def memory_get_enrichment_queue(
    session_id: str,
    ctx: Context = None
) -> str:
    """
    Get pending function references awaiting librarian enrichment.

    For librarian use - returns functions that need code analysis.

    Args:
        session_id: Your session ID
    """
    error = require_session(session_id)
    if error:
        return error

    pending = [
        {
            "id": func_id,
            "name": info["name"],
            "file": info["file"],
            "project": info["project"],
            "has_code": info["has_code"],
            "registered_at": info["registered_at"]
        }
        for func_id, info in function_enrichment_queue.items()
        if not info["enriched"]
    ]

    return json.dumps({
        "pending_count": len(pending),
        "items": pending
    }, indent=2)


@mcp.tool()
async def memory_become_librarian(
    session_id: str,
    project: str = None,
    limit: int = 20,
    ctx: Context = None
) -> str:
    """
    Get the librarian prompt and unenriched functions for your project.

    Returns instructions that turn you into a librarian for your local project.
    You read source files locally, analyze them, and call memory_enrich_function.

    This solves the cross-machine problem: the central librarian can't read files
    on remote machines, but YOU can read files on YOUR machine.

    Args:
        session_id: Your session ID
        project: Project to enrich functions for (uses session project if omitted)
        limit: Max functions to return (default 20)
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    target_project = project or session_info.get("project")

    if not target_project:
        return json.dumps({"error": "No project specified and none in session"})

    target_project = normalize_project(target_project)

    # Find unenriched functions from ChromaDB
    chroma = await get_chroma()
    unenriched = []

    try:
        collection = await get_project_collection(chroma, target_project)
        # Get all function_refs that aren't enriched
        results = await collection.get(
            where={"type": "function_ref"},
            include=["metadatas", "documents"]
        )

        for i, doc_id in enumerate(results.get("ids", [])):
            meta = results["metadatas"][i] if results.get("metadatas") else {}
            if meta.get("enriched") == "true":
                continue
            unenriched.append({
                "id": doc_id,
                "name": meta.get("func_name", "unknown"),
                "file": meta.get("func_file", ""),
                "purpose": meta.get("func_purpose", ""),
                "gotchas": meta.get("gotchas", ""),
            })

            if len(unenriched) >= limit:
                break

    except Exception as e:
        return json.dumps({"error": f"Failed to query functions: {e}"})

    # Also check shared collection
    try:
        shared = await get_shared_collection(chroma, "patterns")
        results = await shared.get(
            where={"type": "function_ref"},
            include=["metadatas", "documents"]
        )
        for i, doc_id in enumerate(results.get("ids", [])):
            meta = results["metadatas"][i] if results.get("metadatas") else {}
            if meta.get("enriched") == "true":
                continue
            if meta.get("project") and meta.get("project") != target_project:
                continue
            unenriched.append({
                "id": doc_id,
                "name": meta.get("func_name", "unknown"),
                "file": meta.get("func_file", ""),
                "purpose": meta.get("func_purpose", ""),
                "gotchas": meta.get("gotchas", ""),
            })
            if len(unenriched) >= limit:
                break
    except Exception:
        pass

    prompt = f"""# Librarian Mode - Function Enrichment

You are now a **librarian** for the `{target_project}` project. Your job is to enrich
function references so other AI assistants can find and use them correctly.

## Your Task

Below are {len(unenriched)} unenriched function references. For each one:

1. **Read the source file** using the Read tool (the `file` field has the path and line number)
2. **Extract the function** starting at that line
3. **Analyze it** and determine:
   - `signature`: Full function signature with types
   - `parameters`: List of {{name, type, description}} dicts
   - `returns`: Return type and description
   - `calls`: Key functions this calls internally
   - `side_effects`: File I/O, network calls, DB writes, state mutations
   - `complexity`: Performance notes (O(n), "loops over all emails", etc.)
   - `additional_gotchas`: Non-obvious behaviors, edge cases, warnings
   - `search_summary`: A rich 1-2 sentence description for semantic search.
     Include action verbs, domain terms, and synonyms so other AIs can find this
     function when searching by concept. Example: "ML classification pipeline for
     email triage. Classifies incoming emails by priority, assigns labels."
4. **Call `memory_enrich_function()`** with the results. Only include fields you have
   good data for - omit fields rather than guess.

## Rules

- **Read before analyzing** - don't guess from the name/purpose alone
- **Be concise** - one line per parameter, short gotcha descriptions
- **Focus on gotchas** - the most valuable thing you provide is warnings about
  non-obvious behavior that could cause bugs
- **search_summary is critical** - it's how other AIs find this function. Include
  what it does, what domain it's in, and what problem it solves
- **Skip functions whose files you can't read** - report them at the end
- If file paths are relative, try resolving from your working directory or project root
- Process them in batches - call multiple Read tools in parallel when possible

## Functions to Enrich

"""

    for i, func in enumerate(unenriched, 1):
        prompt += f"### {i}. `{func['name']}`\n"
        prompt += f"- **ID:** `{func['id']}`\n"
        prompt += f"- **File:** `{func['file']}`\n"
        prompt += f"- **Purpose:** {func['purpose']}\n"
        if func.get('gotchas'):
            prompt += f"- **Known gotchas:** {func['gotchas']}\n"
        prompt += "\n"

    if not unenriched:
        prompt += "\n*No unenriched functions found! All functions in this project are already enriched.*\n"

    prompt += f"""
## When Done

Report a summary: how many enriched, how many skipped (and why).
Then call `memory_update_work(title="Librarian enrichment", status="completed",
notes="Enriched X/{len(unenriched)} functions for {target_project}")`.
"""

    return json.dumps({
        "project": target_project,
        "unenriched_count": len(unenriched),
        "prompt": prompt
    }, indent=2)


@mcp.tool()
async def memory_enrich_function(
    session_id: str,
    func_id: str,
    signature: str = None,
    parameters: List[Dict] = None,
    returns: str = None,
    calls: List[str] = None,
    called_by: List[str] = None,
    side_effects: List[str] = None,
    complexity: str = None,
    additional_gotchas: str = None,
    search_summary: str = None,
    ctx: Context = None
) -> str:
    """
    Enrich a function reference with analyzed details.

    For librarian use - adds deep analysis to existing function refs.

    Args:
        session_id: Your session ID
        func_id: Function reference ID to enrich
        signature: Full function signature
        parameters: List of {name, type, description} dicts
        returns: Return type and description
        calls: Functions this calls internally
        called_by: Functions that call this
        side_effects: Side effects (file I/O, network, state mutation)
        complexity: Performance/complexity notes
        additional_gotchas: Extra gotchas discovered during analysis
        search_summary: Rich description for semantic search (generated by librarian)
    """
    error = require_session(session_id)
    if error:
        return error

    chroma = await get_chroma()
    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Find the function in any collection
    collections = await chroma.list_collections()

    for col in collections:
        if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
            continue

        try:
            result = await col.get(ids=[func_id], include=["documents", "metadatas"])
            if result["ids"]:
                meta = result["metadatas"][0]
                doc = result["documents"][0]

                # Add enrichment data to document
                enrichment_parts = []
                if signature:
                    enrichment_parts.append(f"**Signature:** `{signature}`")
                if parameters:
                    param_lines = [f"  - `{p['name']}`: {p.get('type', 'any')} - {p.get('description', '')}"
                                   for p in parameters]
                    enrichment_parts.append("**Parameters:**\n" + "\n".join(param_lines))
                if returns:
                    enrichment_parts.append(f"**Returns:** {returns}")
                if calls:
                    enrichment_parts.append(f"**Calls:** {', '.join(calls)}")
                if called_by:
                    enrichment_parts.append(f"**Called by:** {', '.join(called_by)}")
                if side_effects:
                    enrichment_parts.append(f"**Side effects:** {', '.join(side_effects)}")
                if complexity:
                    enrichment_parts.append(f"**Complexity:** {complexity}")
                if additional_gotchas:
                    existing_gotchas = meta.get("gotchas", "")
                    if existing_gotchas:
                        meta["gotchas"] = f"{existing_gotchas}; {additional_gotchas}"
                    else:
                        meta["gotchas"] = additional_gotchas

                # Add search summary at the TOP of the document for better embedding
                if search_summary:
                    doc = f"**Search Summary:** {search_summary}\n\n" + doc
                    meta["search_summary"] = search_summary

                # Append enrichment to document
                if enrichment_parts:
                    doc += "\n\n## Librarian Analysis\n" + "\n\n".join(enrichment_parts)

                # Update metadata
                meta["enriched"] = "true"
                meta["enriched_at"] = now
                meta["updated"] = now
                if signature:
                    meta["signature"] = signature
                if calls:
                    meta["calls"] = json.dumps(calls)
                if called_by:
                    meta["called_by"] = json.dumps(called_by)
                if side_effects:
                    meta["side_effects"] = json.dumps(side_effects)

                await col.update(
                    ids=[func_id],
                    documents=[doc],
                    metadatas=[meta]
                )

                # Phase 1 #2 canary 4/13: emit op-log entry per §4.3.a.
                # Chroma update already landed. chroma_collection_name comes
                # from the discovered collection name (col.name), not pre-
                # computed, because enrich finds the doc by scanning. Actor
                # project comes from the session, not the doc — op-log
                # records who performed the write, not the doc's home.
                embedding = await fetch_embedding_for_op_log(col, func_id)
                emit_op_log_from_context(
                    db=get_mongo(),
                    op_type="function.enriched",
                    actor={
                        "agent": session_info["claude_instance"],
                        "project": session_info.get("project"),
                        "session_id": session_id,
                    },
                    ref={"collection": col.name, "doc_id": func_id},
                    payload={
                        "signature": signature,
                        "parameters": parameters,
                        "returns": returns,
                        "calls": calls,
                        "called_by": called_by,
                        "side_effects": side_effects,
                        "complexity": complexity,
                        "additional_gotchas": additional_gotchas,
                        "search_summary": search_summary,
                        "enriched_at": now,
                        "embedding": embedding,
                    },
                )

                # Mark as enriched in queue
                if func_id in function_enrichment_queue:
                    function_enrichment_queue[func_id]["enriched"] = True

                return json.dumps({
                    "status": "enriched",
                    "id": func_id,
                    "name": meta.get("func_name"),
                    "enriched_at": now
                })
        except Exception:
            continue

    return json.dumps({"error": f"Function reference not found: {func_id}"})
