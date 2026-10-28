"""MCP-layer tenant isolation for project-scoped API keys (R1).

interface:project-scope-enforcement-v0 (backlog_8cd9708f4ad2). A key scoped
to [alpha] used to read or write project beta by passing project="beta" on
most tools: require_auth(..., project) was opt-in per tool and only
memory_store / memory_record_learning / skills called it. Found in the
2026-10-28 work-upgrade rehearsal (learning_1b5bf5e0b1f5071d).

This wrapper checks every `project` (str) and `projects` (list) argument
of every tool call against the session's allowed_projects, so a new tool
with a project argument is covered without remembering to call anything.
It only acts on scoped sessions (auth.scoped_projects returns a list):
unscoped keys, keyless soft-auth sessions and auth-disabled servers pass
through untouched.

Exempt on purpose (R2):
- memory_start_session: its own scope check runs at session start.
- memory_send_message: cross-project messaging is how teams talk.
- memory_admin: role-gated, and owner keys are unscoped.
- memory_get_messages: has its own for_instance/message_id permission rules.

What it does NOT do: omitted-project "all projects" listings (R3), doc-id
addressed tools (R4) and cross-project listings (R5) need per-tool
knowledge and are filtered inside those tools with auth.collection_allowed
/ auth.project_allowed.

Must sit INSIDE session_alias (it reads session_id after the junto_session
rename) and outside idle_park, so a rejected call never resets an idle timer.
"""

from __future__ import annotations

from mcp import types as _mcp_types

EXEMPT_TOOLS = frozenset({
    "memory_start_session",
    "memory_send_message",
    "memory_admin",
    "memory_get_messages",
})


def _denied_project(session_info, args) -> str | None:
    """First project argument the session may not touch, else None."""
    from shared_memory.auth import project_allowed

    project = args.get("project")
    if isinstance(project, str) and project and not project_allowed(session_info, project):
        return project
    projects = args.get("projects")
    if isinstance(projects, list):
        for p in projects:
            if isinstance(p, str) and p and not project_allowed(session_info, p):
                return p
    return None


def _rejection(text: str):
    return _mcp_types.ServerResult(_mcp_types.CallToolResult(
        content=[_mcp_types.TextContent(type="text", text=text)],
        isError=True,
    ))


def build_call_tool_handler_with_project_scope(orig_handler):
    """Wrap an MCP CallToolRequest handler so scoped keys cannot name other projects."""

    async def wrapped(req):
        params = getattr(req, "params", None)
        name = getattr(params, "name", None)
        args = getattr(params, "arguments", None)
        if name not in EXEMPT_TOOLS and isinstance(args, dict):
            from shared_memory.auth import scoped_projects, tenant_isolation_error
            from shared_memory.state import active_sessions

            session_info = active_sessions.get(args.get("session_id") or "")
            # A missing session passes through: the tool's own require_session
            # error is the right answer there.
            if session_info and scoped_projects(session_info) is not None:
                denied = _denied_project(session_info, args)
                if denied is not None:
                    return _rejection(tenant_isolation_error(session_info, denied))
        return await orig_handler(req)

    return wrapped
