"""Author context-fill stamps on learnings and corrections.

Tom-approved ask (coordinator msg_5b394f48b4d2, 2026-10-12): test whether agent
mistakes cluster at high context fill. The server already receives each
agent's context size every ~60s from the box reporters (memory_report_context
-> agent_status), so the stamp needs no client change.

One row per event in `context_stamps`:
  {doc_id, kind: "learning" | "status_change", new_status?, agent, project,
   ts, context_tokens, context_window, context_reported_at, known}
known=False (tokens/window None, never 0) when the agent has no gauge row or
its last report is older than FRESH_SECONDS.
"""

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

FRESH_SECONDS = 600


def current_context(db, agent: str, now) -> Dict[str, Any]:
    """The agent's freshly reported context, or known=False."""
    from shared_memory.helpers import parse_timestamp

    unknown = {"known": False, "context_tokens": None, "context_window": None,
               "context_reported_at": None}
    if db is None or not agent:
        return unknown
    try:
        doc = db.agent_status.find_one(
            {"instance": agent},
            {"context_tokens": 1, "context_window": 1, "context_reported_at": 1})
    except Exception:
        return unknown
    tokens = (doc or {}).get("context_tokens")
    reported = parse_timestamp((doc or {}).get("context_reported_at"))
    if not isinstance(tokens, int) or tokens <= 0 or reported is None:
        return unknown
    if now - reported > timedelta(seconds=FRESH_SECONDS):
        return dict(unknown, context_reported_at=reported)
    window = doc.get("context_window")
    return {"known": True, "context_tokens": tokens,
            "context_window": window if isinstance(window, int) and window > 0 else None,
            "context_reported_at": reported}


def stamp(db, session_id: str, kind: str, doc_id: str,
          new_status: Optional[str] = None) -> None:
    """Record one stamp for the session's agent. db=None fetches the shared
    client inside the guard. Never raises."""
    try:
        from shared_memory.helpers import utc_now
        from shared_memory.state import active_sessions

        if db is None:
            from shared_memory.clients import get_mongo
            db = get_mongo()
        if db is None:
            return
        info = active_sessions.get(session_id) or {}
        agent = info.get("claude_instance")
        now = utc_now()
        row = {"doc_id": doc_id, "kind": kind, "agent": agent,
               "project": info.get("project"), "ts": now}
        if new_status is not None:
            row["new_status"] = new_status
        row.update(current_context(db, agent, now))
        db.context_stamps.insert_one(row)
    except Exception as e:  # noqa: BLE001
        log.warning("context_stamp: failed for %s (%s)", doc_id, e)


# ── Time-in-band denominator ───────────────────────────────────────────────
# agent_status keeps only the LATEST gauge reading, so "how long did agents
# spend at 400-600K" is unanswerable from it. memory_report_context samples
# into context_history: one row per agent per HISTORY_BUCKET_MINUTES (upsert,
# so ~1/min reporting stays bounded), TTL HISTORY_TTL_DAYS.
HISTORY_BUCKET_MINUTES = 10
HISTORY_TTL_DAYS = 90
_history_indexed = False


def record_history(db, instance: str, tokens: int, window, now) -> None:
    """Sample one gauge reading into context_history. Never raises."""
    global _history_indexed
    try:
        if not _history_indexed:
            db.context_history.create_index([("instance", 1), ("bucket", 1)], unique=True)
            db.context_history.create_index("ts", expireAfterSeconds=HISTORY_TTL_DAYS * 86400)
            _history_indexed = True
        bucket = now.replace(minute=now.minute - now.minute % HISTORY_BUCKET_MINUTES,
                             second=0, microsecond=0)
        db.context_history.update_one(
            {"instance": instance, "bucket": bucket},
            {"$set": {"instance": instance, "bucket": bucket, "ts": now,
                      "context_tokens": tokens, "context_window": window}},
            upsert=True,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("context_stamp: history sample failed for %s (%s)", instance, e)
