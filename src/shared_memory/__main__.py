"""
Entry point for running as: python -m shared_memory

Usage:
    python -m shared_memory [--host HOST] [--port PORT] [--transport TRANSPORT]
"""

import argparse
from pathlib import Path

from shared_memory.app import create_app
from shared_memory.auth import AUTH_ENABLED
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import CHROMA_HOST, CHROMA_PORT, PROJECT_PREFIX, SHARED_PREFIX
from shared_memory.helpers import utc_now
from shared_memory.state import active_sessions, active_signals, file_locks


def main():
    parser = argparse.ArgumentParser(description="Shared Memory MCP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport mode (default: streamable-http)",
    )
    args = parser.parse_args()

    mcp = create_app()

    if args.transport == "stdio":
        transport_line = "║  Transport: stdio"
    else:
        transport_line = f"║  Endpoint: http://{args.host}:{args.port}/mcp (stateless HTTP)"

    auth_line = f"║  Auth:     {'ENABLED (API key required)' if AUTH_ENABLED else 'disabled (open access)'}"

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║       Shared Memory MCP Server v1.0.0                        ║
║       Multi-Agent Coordination + Knowledge Base              ║
╠══════════════════════════════════════════════════════════════╣
{transport_line}
║  Chroma:   {CHROMA_HOST}:{CHROMA_PORT}
{auth_line}
║                                                              ║
║  Session Management:                                         ║
║    memory_start_session   - START HERE (gets locks/signals)  ║
║    memory_end_session     - Record work, release locks       ║
║                                                              ║
║  Knowledge Base:                                             ║
║    memory_query / memory_store / memory_record_learning      ║
║    memory_search_global   - Cross-project search             ║
║                                                              ║
║  Coordination:                                               ║
║    memory_lock_files / memory_unlock_files / memory_get_locks║
║    memory_send_message / memory_get_messages                 ║
║    memory_heartbeat / memory_list_agents                     ║
║                                                              ║
║  Task Management:                                            ║
║    memory_add_backlog_item / memory_list_backlog             ║
║    memory_checklist (CRUD)                                   ║
║                                                              ║
║  Function References:                                        ║
║    memory_register_function / memory_find_function           ║
║                                                              ║
║  Specs & Registry:                                           ║
║    memory_define_spec / memory_get_spec / memory_list_specs  ║
║    memory_project (CRUD) / memory_db (read-only SQL)         ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Configure MCP settings
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    # Add custom /health endpoint
    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from starlette.responses import JSONResponse
        try:
            chroma = await get_chroma()
            await chroma.heartbeat()
            chroma_status = "healthy"
        except Exception as e:
            chroma_status = f"unhealthy: {str(e)}"

        status = "healthy" if chroma_status == "healthy" else "degraded"
        return JSONResponse({
            "status": status,
            "chroma": chroma_status,
            "active_sessions": len(active_sessions),
            "active_locks": len(file_locks),
            "active_signals": len(active_signals)
        }, status_code=200 if status == "healthy" else 503)

    # ── Dashboard (read-only web UI) ──

    dashboard_html_path = Path(__file__).parent / "dashboard.html"

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard_page(request):
        from starlette.responses import HTMLResponse
        try:
            html = dashboard_html_path.read_text(encoding="utf-8")
            return HTMLResponse(html)
        except Exception as e:
            return HTMLResponse(f"<h1>Dashboard error</h1><p>{e}</p>", status_code=500)

    @mcp.custom_route("/dashboard/api/sessions", methods=["GET"])
    async def dashboard_sessions(request):
        from starlette.responses import JSONResponse
        sessions = []
        for sid, info in active_sessions.items():
            sessions.append({
                "session_id": sid,
                "claude_instance": info.get("claude_instance", "unknown"),
                "project": info.get("project", ""),
                "task": info.get("task", ""),
                "started": info.get("started", ""),
                "last_activity": info.get("last_activity", ""),
            })
        sessions.sort(key=lambda s: s.get("started", ""), reverse=True)
        return JSONResponse({"sessions": sessions})

    @mcp.custom_route("/dashboard/api/agents", methods=["GET"])
    async def dashboard_agents(request):
        from starlette.responses import JSONResponse
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable", "agents": []})
        try:
            cursor = db.agent_directory.find({}).sort("last_seen", -1).limit(50)
            agents = []
            for doc in cursor:
                last_seen = doc.get("last_seen")
                if hasattr(last_seen, "isoformat"):
                    last_seen = last_seen.isoformat()
                agents.append({
                    "project": doc.get("project", ""),
                    "instance": doc.get("instance", ""),
                    "role_description": doc.get("role_description", ""),
                    "last_seen": last_seen,
                    "session_count": doc.get("session_count", 0),
                    "last_task": doc.get("last_task", ""),
                })
            return JSONResponse({"agents": agents})
        except Exception as e:
            return JSONResponse({"error": str(e), "agents": []})

    @mcp.custom_route("/dashboard/api/messages", methods=["GET"])
    async def dashboard_messages(request):
        from starlette.responses import JSONResponse
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable", "messages": []})
        try:
            cursor = db.messages.find({}).sort("created_at", -1).limit(30)
            messages = []
            for doc in cursor:
                created = doc.get("created_at")
                if hasattr(created, "isoformat"):
                    created = created.isoformat()
                messages.append({
                    "id": doc.get("_id", ""),
                    "from": doc.get("from_instance") or doc.get("from", ""),
                    "to": doc.get("to_instance", ""),
                    "category": doc.get("category", ""),
                    "priority": doc.get("priority", ""),
                    "status": doc.get("status", ""),
                    "created": created,
                    "preview": (doc.get("message", "") or "")[:200],
                })
            return JSONResponse({"messages": messages})
        except Exception as e:
            return JSONResponse({"error": str(e), "messages": []})

    @mcp.custom_route("/dashboard/api/backlog", methods=["GET"])
    async def dashboard_backlog(request):
        from starlette.responses import JSONResponse
        try:
            chroma = await get_chroma()
            all_collections = await chroma.list_collections()
            items = []
            priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
            for col in all_collections:
                if not (col.name.startswith(PROJECT_PREFIX) or col.name.startswith(SHARED_PREFIX)):
                    continue
                try:
                    results = await col.get(
                        where={"type": "backlog"},
                        include=["metadatas"]
                    )
                    for i, meta in enumerate(results.get("metadatas", []) or []):
                        if not meta:
                            continue
                        status = meta.get("backlog_status", "open")
                        if status not in ("open", "in_progress", "blocked"):
                            continue
                        items.append({
                            "id": results["ids"][i],
                            "title": meta.get("title", ""),
                            "project": meta.get("project", "shared") or "shared",
                            "priority": meta.get("priority", "medium"),
                            "status": status,
                            "assigned_to": meta.get("assigned_to") or None,
                            "updated": meta.get("updated", ""),
                            "target_version": meta.get("target_version", "") or None,
                        })
                except Exception:
                    continue
            items.sort(key=lambda x: (
                priority_order.get(x.get("priority", "medium"), 99),
                x.get("updated", ""),
            ), reverse=False)
            # Reverse updated for descending within same priority
            items.sort(key=lambda x: (priority_order.get(x.get("priority", "medium"), 99), -_ts(x.get("updated", ""))))
            return JSONResponse({"items": items[:100]})
        except Exception as e:
            return JSONResponse({"error": str(e), "items": []})

    # ── Compaction logging endpoint (per ADR 822c260ccfda) ──

    @mcp.custom_route("/hook/compact-log", methods=["POST"])
    async def hook_compact_log(request):
        """Log a compaction event for measurement. Called by Claude Code hooks.

        Expected JSON body:
        {
            "event": "PreCompact" | "PostCompact",
            "agent": "<agent name>",
            "project": "<project name>",
            "session_start": "<iso timestamp>",  // optional
            "reason": "auto" | "manual"          // optional
        }

        Data is collected for one week to inform the v3 PostCompact decision.
        """
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
        except Exception:
            body = {}

        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable"}, status_code=503)

        try:
            db.compaction_events.insert_one({
                "event": body.get("event", "unknown"),
                "agent": body.get("agent", "unknown"),
                "project": body.get("project", ""),
                "session_start": body.get("session_start", ""),
                "reason": body.get("reason", ""),
                "logged_at": utc_now(),
            })
            return JSONResponse({"status": "logged"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/hook/compact-log/stats", methods=["GET"])
    async def hook_compact_stats(request):
        """Read-only stats for compaction event collection."""
        from starlette.responses import JSONResponse
        db = get_mongo()
        if db is None:
            return JSONResponse({"error": "MongoDB unavailable"}, status_code=503)
        try:
            total = db.compaction_events.count_documents({})
            by_agent = list(db.compaction_events.aggregate([
                {"$group": {"_id": "$agent", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]))
            by_event = list(db.compaction_events.aggregate([
                {"$group": {"_id": "$event", "count": {"$sum": 1}}},
            ]))
            recent = list(db.compaction_events.find({}, {"_id": 0}).sort("logged_at", -1).limit(10))
            for r in recent:
                if hasattr(r.get("logged_at"), "isoformat"):
                    r["logged_at"] = r["logged_at"].isoformat()
            return JSONResponse({
                "total_events": total,
                "by_agent": [{"agent": x["_id"], "count": x["count"]} for x in by_agent],
                "by_event": [{"event": x["_id"], "count": x["count"]} for x in by_event],
                "recent": recent,
            })
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # Run with the selected transport.
    # For streamable-http we go through uvicorn manually so we can install
    # the /mcp/discussion path-rewriting ASGI middleware (backlog_0f6b4e4332a0).
    # stdio path is unchanged.
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        from shared_memory.auth import (
            detect_tunnel_origin,
            parse_bearer_token,
            reset_header_api_key,
            reset_via_tunnel,
            set_header_api_key,
            set_via_tunnel,
        )
        from shared_memory.tool_profiles import current_profile

        starlette_app = mcp.streamable_http_app()

        DISCUSSION_PREFIX = "/mcp/discussion"

        async def discussion_route_middleware(scope, receive, send):
            """Rewrite /mcp/discussion[/...] → /mcp[/...] and set the
            tool_profiles.current_profile contextvar to "discussion" so the
            list_tools handler (app.py) filters the surface. Default /mcp path
            is untouched and continues to serve the full tool surface."""
            if scope["type"] in ("http", "websocket"):
                path = scope.get("path", "")
                if path == DISCUSSION_PREFIX or path.startswith(DISCUSSION_PREFIX + "/"):
                    new_path = "/mcp" + path[len(DISCUSSION_PREFIX):]
                    scope = {**scope, "path": new_path, "raw_path": new_path.encode()}
                    token = current_profile.set("discussion")
                    try:
                        await starlette_app(scope, receive, send)
                    finally:
                        current_profile.reset(token)
                    return
            await starlette_app(scope, receive, send)

        async def auth_header_middleware(scope, receive, send):
            """Parse 'Authorization: Bearer <key>' into the header_api_key
            contextvar so memory_start_session can fall back to it when no
            per-tool api_key arg is given (design:header-auth-v0), and flag
            tunnel-origin requests via CF-Connecting-IP so keyless traffic over
            the public tunnel can be rejected (design:auth-origin-trust-v0).
            Outermost wrapper: sets/resets with the proven try/finally token
            pattern, then delegates to discussion_route_middleware."""
            if scope["type"] in ("http", "websocket"):
                key = parse_bearer_token(scope.get("headers"))
                via_tunnel = detect_tunnel_origin(scope.get("headers"))
                key_token = set_header_api_key(key) if key else None
                tunnel_token = set_via_tunnel(via_tunnel)
                try:
                    await discussion_route_middleware(scope, receive, send)
                finally:
                    if key_token is not None:
                        reset_header_api_key(key_token)
                    reset_via_tunnel(tunnel_token)
                return
            await discussion_route_middleware(scope, receive, send)

        config = uvicorn.Config(
            auth_header_middleware,
            host=args.host,
            port=args.port,
            log_level=mcp.settings.log_level.lower(),
        )
        uvicorn.Server(config).run()


def _ts(iso_str: str) -> float:
    """Parse ISO timestamp to epoch seconds, 0 on failure."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0.0


if __name__ == "__main__":
    main()
