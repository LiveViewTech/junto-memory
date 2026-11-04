"""Diagnostic tools - connection and health checks.

These tools are intentionally lightweight and do NOT require a session,
since their primary use is to verify the server is reachable before
calling any session-bound tool.
"""

import json
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import Context

from shared_memory import __version__ as SERVER_VERSION
from shared_memory.app import mcp
from shared_memory.clients import get_mongo

_SERVER_START_TS = time.time()


@mcp.tool()
async def memory_health(
    include_storage: bool = True,
    ctx: Context = None,
) -> str:
    """
    Connection diagnostic — confirm the server is reachable and healthy.

    This tool does NOT require a session. Use it before memory_start_session
    to verify connectivity, or after a transport error to distinguish
    "server down" from "session expired" from "client misconfigured."

    Use this as your heartbeat target. A client wrapper can poll it on a
    regular interval (10-60s); any error or timeout means the MCP transport
    may be silently broken — warn the operator loudly rather than swallow it.

    Returns JSON with:
    - status: "ok" | "degraded" | "error"
    - server_version: current junto-memory version string
    - server_time: current UTC timestamp (useful for clock-skew detection)
    - uptime_seconds: seconds since server process started
    - storage: when include_storage=True, mongo ping result
    - features: server-default feature switches a client may need before
      doing costly work. Today: {"transcript_extract": bool} (query_config
      `transcript_extract`; a per-project override can still turn one
      project off or on, enforced at write time).

    Args:
        include_storage: When True (default), pings Mongo for storage
            confirmation. Set False for the very lightest possible health
            check (responds quickly but doesn't verify backend storage).
    """
    result = {
        "status": "ok",
        "server_version": SERVER_VERSION,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": round(time.time() - _SERVER_START_TS, 1),
    }

    try:
        from shared_memory.query_config import get_effective_config
        _cfg = get_effective_config(get_mongo(), None)
        result["features"] = {"transcript_extract": bool(_cfg.get("transcript_extract"))}
    except Exception:
        result["features"] = {"transcript_extract": False}

    if include_storage:
        try:
            mongo_start = time.time()
            mongo = get_mongo()
            mongo.command("ping")
            mongo_ms = round((time.time() - mongo_start) * 1000, 1)
            result["storage"] = {
                "mongo": {"status": "ok", "ping_ms": mongo_ms},
            }
            if mongo_ms > 500:
                result["status"] = "degraded"
                result["storage"]["mongo"]["note"] = "Ping >500ms"
        except Exception as e:
            result["status"] = "error"
            result["storage"] = {
                "mongo": {"status": "error", "error": str(e)},
            }

    return json.dumps(result, indent=2)
