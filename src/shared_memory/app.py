"""
MCP server instance and application setup.

Creates the FastMCP server instance that all tool modules register with.
"""

from mcp import types as _mcp_types
from mcp.server.fastmcp import FastMCP

from shared_memory.clients import app_lifespan
from shared_memory.error_flag import build_call_tool_handler_with_error_flag
from shared_memory.idle_park import build_call_tool_handler_with_idle_park
from shared_memory.intent import (
    build_call_tool_handler_with_intent,
    build_call_tool_handler_with_session_alias,
)
from shared_memory.project_scope import build_call_tool_handler_with_project_scope
from shared_memory.timing import build_call_tool_handler_with_timing

# Allow connections from any host (for remote access via IP or proxy)
# stateless_http=False (the default): persistent client sessions, required
# for resource subscriptions (Phase C2 inbox://) and progress notifications.
# Original stateless_http=True was a workaround for an older FastMCP -32602
# "request before initialization" race; verified resolved in current FastMCP
# via concurrent client harness (2026-04-26, see /tmp/mcp_stress_harness.py).
mcp = FastMCP("junto", lifespan=app_lifespan, host="0.0.0.0", stateless_http=False)


# ── MCP resources.subscribe capability patch (backlog_4218136ef3ce) ──
# The lowlevel MCP server's get_capabilities() hard-codes
# resources.subscribe=False (mcp/server/lowlevel/server.py:211-212). We
# register a real subscribe_resource handler in tools/messaging.py for the
# inbox:// resource family, but without this patch the initialize handshake
# advertises subscribe=False, which causes well-behaved MCP clients (e.g.
# claudeControl) to fall back to polling instead of using the working
# subscribe path. The patch wraps the original method and overrides only
# the resources.subscribe flag, preserving everything else.
_orig_get_capabilities = mcp._mcp_server.get_capabilities

def _get_capabilities_with_subscribe(notification_options, experimental_capabilities):
    caps = _orig_get_capabilities(notification_options, experimental_capabilities)
    if caps.resources is not None and not caps.resources.subscribe:
        caps = caps.model_copy(update={
            "resources": _mcp_types.ResourcesCapability(
                subscribe=True,
                listChanged=caps.resources.listChanged,
            )
        })
    return caps

mcp._mcp_server.get_capabilities = _get_capabilities_with_subscribe


# ── __intent_id MCP-layer extraction ──
# Every tool call routes through the lowlevel server's CallToolRequest handler
# (FastMCP registers it at __init__, mcp/server/fastmcp/server.py:308), so it's
# the right chokepoint for client-supplied reconciliation UUIDs. See
# shared_memory.intent for why and how.
#
# Critical: replacing mcp.call_tool itself would NOT work — the lowlevel server
# captures self.call_tool as a bound method at FastMCP init time, so a later
# reassignment is invisible to the registered handler. Replacing the dict entry
# is the only way to interpose.
_orig_call_tool_handler = mcp._mcp_server.request_handlers[_mcp_types.CallToolRequest]
# Composition (outer → inner):
#   timing        — measures total wall time including all inner wrappers
#   error_flag    — post-processes the final result
#   session_alias — junto_session → session_id (must precede idle_park)
#   project_scope — rejects project args outside a scoped key's projects
#                   (interface:project-scope-enforcement-v0 R1; after
#                   session_alias, before idle_park so a rejection never
#                   resets an idle timer)
#   idle_park     — resets the caller's idle→park timer (sees the raw tool name
#                   + session_id before intent strips its sideband kwargs)
#   intent        — extracts __intent_id from args before dispatch
# When JUNTO_TIMING_LOG is unset (default), the timing wrapper is a no-op.
mcp._mcp_server.request_handlers[_mcp_types.CallToolRequest] = (
    build_call_tool_handler_with_timing(
        build_call_tool_handler_with_error_flag(
            build_call_tool_handler_with_session_alias(
                build_call_tool_handler_with_project_scope(
                    build_call_tool_handler_with_idle_park(
                        build_call_tool_handler_with_intent(_orig_call_tool_handler)
                    )
                )
            )
        )
    )
)


# ── list_tools profile filtering (backlog_0f6b4e4332a0) ──
# When the /mcp/discussion ASGI middleware (see __main__.py) sets the
# tool_profiles.current_profile contextvar to "discussion" for a request, the
# tools/list response is filtered down to DISCUSSION_PROFILE_TOOLS. Default
# profile is "full" — every registered tool surfaces, unchanged from the
# pre-2026-05-20 behavior. Same chokepoint pattern as the CallToolRequest
# patch above.
from shared_memory.tool_profiles import (  # noqa: E402
    DISCUSSION_PROFILE_TOOLS,
    advertise_session_alias,
    current_profile,
)

_orig_list_tools_handler = mcp._mcp_server.request_handlers[_mcp_types.ListToolsRequest]


async def _list_tools_with_profile_filter(req):
    result = await _orig_list_tools_handler(req)
    if current_profile.get() != "discussion":
        return result
    list_result = result.root  # ServerResult.root is the ListToolsResult
    filtered = [advertise_session_alias(t) for t in list_result.tools
                if t.name in DISCUSSION_PROFILE_TOOLS]
    new_list_result = list_result.model_copy(update={"tools": filtered})
    return _mcp_types.ServerResult(new_list_result)


mcp._mcp_server.request_handlers[_mcp_types.ListToolsRequest] = _list_tools_with_profile_filter


def create_app():
    """Import all tool modules to trigger @mcp.tool() registration, then return mcp."""
    # These imports trigger the @mcp.tool() decorators which register tools with mcp
    import shared_memory.tools  # noqa: F401
    return mcp
