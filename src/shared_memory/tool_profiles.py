"""
Per-route tool-surface profiles.

Two profiles:

- "full"       (default) — every registered tool is exposed. Reached at /mcp.
- "discussion" — the subset useful for discussion/planning clients (claude.ai
                 desktop, etc.) that pay the full schema cost upfront because
                 they lack Tool Search. Reached at /mcp/discussion.

The /mcp/discussion route is opt-in: an ASGI middleware in __main__.py rewrites
the scope path and sets `current_profile` to "discussion" for the duration of
the request. The list_tools handler in app.py reads this contextvar and
filters the returned tool list.

Background: backlog_0f6b4e4332a0. Primary driver is billable API-rate cost on
desktop-to-junto sessions — full ~50-tool schema burns ~15-25K tokens per
session for tools the client won't exercise.
"""

from contextvars import ContextVar

current_profile: ContextVar[str] = ContextVar("junto_tool_profile", default="full")


DISCUSSION_PROFILE_TOOLS: frozenset[str] = frozenset({
    "memory_start_session",
    "memory_query",
    "memory_get_by_id",
    "memory_get_spec",
    "memory_list_specs",
    "memory_search_global",
    "memory_list_projects",
    "memory_send_message",
    "memory_get_messages",
    "memory_acknowledge_message",
    "memory_store",
    "memory_define_spec",
    "memory_health",
})


def advertise_session_alias(tool):
    """Discussion-profile schema: rename the `session_id` parameter to
    `junto_session` (msg_50ff99f0ead1). Claude desktop/Cowork strips any
    argument named exactly `session_id`; the CallToolRequest wrapper
    (intent.build_call_tool_handler_with_session_alias) maps it back. Returns a
    copy — the registered tool is never mutated. Clients cache tool schemas
    until a fresh conversation, so old schemas keep sending session_id, which
    still works."""
    from shared_memory.intent import SESSION_ALIAS_KWARG
    schema = tool.inputSchema or {}
    props = schema.get("properties") or {}
    if "session_id" not in props:
        return tool
    new_props = {}
    for k, v in props.items():
        if k == "session_id":
            v = {**v, "title": "Junto Session",
                 "description": "Your session ID (from memory_start_session)."}
            k = SESSION_ALIAS_KWARG
        new_props[k] = v
    required = [SESSION_ALIAS_KWARG if r == "session_id" else r
                for r in schema.get("required") or []]
    new_schema = {**schema, "properties": new_props}
    if "required" in schema:
        new_schema["required"] = required
    desc = (tool.description or "").replace("session_id:", f"{SESSION_ALIAS_KWARG}:")
    return tool.model_copy(update={"inputSchema": new_schema, "description": desc})
