"""File locking tools - coordinate exclusive file access."""

import json
from typing import List

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.auth import project_allowed, scoped_projects
from shared_memory.helpers import (
    is_lock_stale,
    normalize_path,
    path_matches_pattern,
    release_session_locks,
    require_session,
    utc_now_iso,
)
from shared_memory.state import active_sessions, file_locks


@mcp.tool()
async def memory_lock_files(
    session_id: str,
    files: List[str],
    reason: str,
    ctx: Context = None
) -> str:
    """
    Atomically lock files for exclusive editing.

    Use this before editing shared code (like NimbusCommon) to prevent conflicts.
    Locks are automatically released when your session ends.

    Args:
        session_id: Your session ID
        files: List of file paths to lock (e.g., ["NimbusCommon/FrameRedis.cs"])
               Use trailing "/" for directory locks (e.g., "NimbusCommon/")
        reason: Why you need these files (e.g., "Implementing MQTT status handler")

    Returns:
        {
            "success": true/false,
            "locked": [...],      # Files you now hold
            "conflicts": [...]    # Files held by others (with stale flag)
        }

    Behavior:
        - Atomic: Either all files lock or none do
        - Auto-release: Locks released on memory_end_session
        - Directory locks: "path/" locks all files within
        - Stale detection: Locks from inactive sessions (>30min) marked as stale
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    now = utc_now_iso()

    # Update session activity
    active_sessions[session_id]["last_activity"] = now

    # Normalize all paths
    normalized_files = [normalize_path(f) for f in files]

    # Check for conflicts first (atomic - check all before locking any)
    conflicts = []
    for file_path in normalized_files:
        # Check if this exact file is locked
        if file_path in file_locks:
            lock_info = file_locks[file_path]
            if lock_info.get("session_id") != session_id:
                conflicts.append({
                    "file": file_path,
                    "held_by": lock_info.get("claude_instance"),
                    "session_id": lock_info.get("session_id"),
                    "since": lock_info.get("locked_at"),
                    "reason": lock_info.get("reason"),
                    "stale": is_lock_stale(lock_info)
                })
                continue

        # Check if any existing lock conflicts with this path
        for locked_path, lock_info in file_locks.items():
            if lock_info.get("session_id") == session_id:
                continue  # Own lock, skip

            # Check if paths conflict (one is prefix of other, or same)
            if (path_matches_pattern(file_path, locked_path) or
                path_matches_pattern(locked_path, file_path)):
                if not any(c["file"] == locked_path for c in conflicts):
                    conflicts.append({
                        "file": locked_path,
                        "held_by": lock_info.get("claude_instance"),
                        "session_id": lock_info.get("session_id"),
                        "since": lock_info.get("locked_at"),
                        "reason": lock_info.get("reason"),
                        "stale": is_lock_stale(lock_info)
                    })

    # If any conflicts exist, don't lock anything (atomic)
    if conflicts:
        return json.dumps({
            "success": False,
            "locked": [],
            "conflicts": conflicts,
            "message": "Cannot acquire locks due to conflicts. Use memory_get_locks to see details."
        })

    # No conflicts - acquire all locks
    locked = []
    for file_path in normalized_files:
        file_locks[file_path] = {
            "session_id": session_id,
            "claude_instance": session_info["claude_instance"],
            "reason": reason,
            "locked_at": now
        }
        locked.append(file_path)

    return json.dumps({
        "success": True,
        "locked": locked,
        "conflicts": []
    })


@mcp.tool()
async def memory_unlock_files(
    session_id: str,
    files: List[str] = None,
    ctx: Context = None
) -> str:
    """
    Release file locks before session ends.

    Args:
        session_id: Your session ID
        files: Specific files to unlock (None = unlock all held by this session)

    Returns:
        {
            "released": [...],    # Files that were unlocked
            "still_held": [...]   # Files you still hold (if you specified specific files)
        }
    """
    error = require_session(session_id)
    if error:
        return error

    # Update session activity
    active_sessions[session_id]["last_activity"] = utc_now_iso()

    if files is None:
        # Release all locks for this session
        released = release_session_locks(session_id)
        return json.dumps({
            "released": released,
            "still_held": []
        })

    # Release specific files
    normalized_files = [normalize_path(f) for f in files]
    released = []
    still_held = []

    for file_path in list(file_locks.keys()):
        lock_info = file_locks[file_path]
        if lock_info.get("session_id") != session_id:
            continue  # Not our lock

        if file_path in normalized_files:
            del file_locks[file_path]
            released.append(file_path)
        else:
            still_held.append(file_path)

    return json.dumps({
        "released": released,
        "still_held": still_held
    })


@mcp.tool()
async def memory_get_locks(
    session_id: str,
    path_pattern: str = None,
    ctx: Context = None
) -> str:
    """
    View current file locks.

    Use this to see what files are locked before deciding what to work on.

    Args:
        session_id: Your session ID
        path_pattern: Optional glob pattern to filter (e.g., "NimbusCommon/*")

    Returns:
        {
            "locks": [...],       # List of current locks with stale flags
            "total_count": N,
            "your_locks": [...]   # Locks held by your session
        }
    """
    error = require_session(session_id)
    if error:
        return error

    # Update session activity
    active_sessions[session_id]["last_activity"] = utc_now_iso()

    all_locks = []
    your_locks = []

    for file_path, lock_info in file_locks.items():
        # Filter by pattern if specified
        if path_pattern and not path_matches_pattern(file_path, path_pattern):
            continue
        # A scoped key lists only locks held by its own projects' sessions
        # (tenant isolation R5). Conflict checks in lock_files still see all.
        if scoped_projects(active_sessions[session_id]) is not None:
            holder = active_sessions.get(lock_info.get("session_id")) or {}
            if not holder.get("project") or not project_allowed(active_sessions[session_id], holder["project"]):
                continue

        lock_data = {
            "file": file_path,
            "held_by": lock_info.get("claude_instance"),
            "session_id": lock_info.get("session_id"),
            "since": lock_info.get("locked_at"),
            "reason": lock_info.get("reason"),
            "stale": is_lock_stale(lock_info)
        }

        if lock_info.get("session_id") == session_id:
            your_locks.append(lock_data)
        else:
            all_locks.append(lock_data)

    return json.dumps({
        "locks": all_locks,
        "your_locks": your_locks,
        "total_count": len(all_locks) + len(your_locks)
    })
