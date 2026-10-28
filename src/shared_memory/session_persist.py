"""Sessions survive a server restart.

active_sessions is in-process, so every restart used to end every agent's
session: the next call got "Session not found" and the agent re-ran
memory_start_session (re-receiving the ~15K-token bundle). This module
mirrors active_sessions into Mongo (`persisted_sessions`) on a short loop and
restores it once at process start, so an agent's session_id keeps working
across a bounce.

- Snapshot every SNAPSHOT_SECONDS: upsert every live session, delete rows for
  sessions that ended or expired. A session created and lost inside one
  interval just falls back to the old behaviour (re-start).
- Restore once per process, BEFORE the loop starts. If restore fails the loop
  never starts, so a failed restore can't wipe the persisted rows.
- Inbox subscriptions / transport bindings are NOT restored: those are live
  connections, and the junto-inbox plugin re-binds itself on reconnect.
- Hard-TTL-expired rows are dropped at restore; idle expiry then applies as
  normal through cleanup_stale_sessions.
- Ephemeral identities (per-box context reporters) are not persisted.
- JUNTO_SESSION_PERSIST=false disables it entirely.
"""

import asyncio
import logging
import os
from datetime import timedelta

from shared_memory.state import active_sessions

log = logging.getLogger(__name__)

COLLECTION = "persisted_sessions"
SNAPSHOT_SECONDS = int(os.getenv("JUNTO_SESSION_SNAPSHOT_SECONDS", "60"))

_restored = False
_task = None


def persist_enabled() -> bool:
    return os.getenv("JUNTO_SESSION_PERSIST", "true").lower() in ("1", "true", "yes")


def _persistable(info: dict) -> bool:
    from shared_memory.helpers import EPHEMERAL_INSTANCE_PREFIXES
    return not (info.get("claude_instance") or "").startswith(EPHEMERAL_INSTANCE_PREFIXES)


def snapshot_once(db) -> int:
    """Mirror active_sessions into Mongo. Returns the number of rows written."""
    from shared_memory.helpers import utc_now
    if db is None:
        return 0
    coll = db[COLLECTION]
    live = {sid: dict(info) for sid, info in list(active_sessions.items()) if _persistable(info)}
    now = utc_now()
    for sid, info in live.items():
        coll.replace_one({"_id": sid}, {"_id": sid, "info": info, "saved_at": now}, upsert=True)
    coll.delete_many({"_id": {"$nin": list(live)}})
    return len(live)


def restore_once(db) -> int:
    """Load persisted sessions into active_sessions. Returns the number restored."""
    from shared_memory.config import SESSION_TTL_DAYS
    from shared_memory.helpers import parse_timestamp, utc_now
    if db is None:
        raise RuntimeError("no mongo")
    cutoff = utc_now() - timedelta(days=SESSION_TTL_DAYS)
    restored = 0
    for row in db[COLLECTION].find({}):
        sid, info = row.get("_id"), row.get("info") or {}
        if not sid or sid in active_sessions:
            continue
        last = parse_timestamp(info.get("last_activity", ""))
        if not last or last < cutoff:
            continue
        active_sessions[sid] = info
        restored += 1
    return restored


async def _loop() -> None:
    from shared_memory.clients import get_mongo
    while True:
        try:
            await asyncio.sleep(SNAPSHOT_SECONDS)
            snapshot_once(get_mongo())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("session-persist: snapshot failed: %s", e)


def start_session_persist() -> None:
    """Restore once, then start the snapshot loop. Idempotent (lifespan runs
    once per MCP client session)."""
    global _restored, _task
    if not persist_enabled():
        return
    if not _restored:
        from shared_memory.clients import get_mongo
        try:
            n = restore_once(get_mongo())
        except Exception as e:
            log.error("session-persist: restore failed, snapshots disabled: %s", e)
            return
        _restored = True
        log.info("session-persist: restored %d session(s)", n)
        print(f"[MCP] Restored {n} session(s) from {COLLECTION}")
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="session_persist")
