"""
Tool registration module.

Importing this package triggers all @mcp.tool() decorators,
registering every tool with the FastMCP server instance.
"""

from shared_memory.tools import (  # noqa: F401
    admin,
    autopilot,
    backlog,
    checklists,
    database,
    diagnostics,
    directives,
    functions,
    guidelines,
    lifecycle,
    locking,
    messaging,
    projects,
    push_control_api,
    query,
    scheduler,
    search,
    sessions,
    skills,
    specs,
    standup,
    storage,
    sync,
)
