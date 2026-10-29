"""
Mutable global state for the Shared Memory MCP Server.

These in-memory dictionaries track runtime state that does not need
persistence -- they are rebuilt each time the server starts.
"""

from typing import Any, Dict

# Active sessions stored in memory; mirrored to Mongo and restored at start
# by session_persist.py so session ids survive a restart.
active_sessions: Dict[str, Dict[str, Any]] = {}

# File locks stored in memory (auto-released on session end)
# Structure: { file_path: { session_id, claude_instance, reason, locked_at } }
file_locks: Dict[str, Dict[str, Any]] = {}

# Signals stored in memory (retained for 24 hours)
# Structure: { signal_name: { from_session, from_claude, timestamp, details } }
active_signals: Dict[str, Dict[str, Any]] = {}

# Phase C2 inbox auth: maps the lowlevel MCP ServerSession (the connection
# the agent is talking to us over) to our app-level session_id so that
# resource handlers can resolve the calling agent's identity. Populated by
# memory_start_session, drained by memory_end_session and stale cleanup.
# Keyed by the ServerSession object itself (identity-hashable).
mcp_session_to_app: Dict[Any, str] = {}
