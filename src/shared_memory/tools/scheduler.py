"""Scheduled self-messages — agent-set wake-ups delivered via the inbox push path.

V1 scope:
- Self-only (sender == recipient).
- One-shot (no recurring).
- 30-day deliver_at horizon.
- Per-agent cap on pending schedules.
- Background scanner ticks every 30s, materializes due rows into the messages
  collection and triggers inbox push notification — same delivery surface as
  memory_send_message, so existing channel-render / get_messages paths work
  unchanged.

Not v1:
- Cross-agent scheduling (would need full push-control gating story).
- Recurring (cron) schedules.
- Snooze / reschedule operations (cancel + schedule new instead).
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import Optional

from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.config import MESSAGE_CATEGORIES, MESSAGE_PRIORITIES
from shared_memory.helpers import (
    normalize_project,
    parse_timestamp,
    require_session,
    utc_now,
)
from shared_memory.state import active_sessions

log = logging.getLogger(__name__)

MAX_PENDING_PER_AGENT = 20
MAX_HORIZON_DAYS = 30
SCANNER_TICK_SECONDS = 30

# Matches "+30s", "+5m", "+2h", "+1d". Anchored so partial strings don't slip through.
_RELATIVE_RE = re.compile(r"^\+(\d+)([smhd])$")

# Mirrors messaging._DESTRUCTIVE_KEYWORDS so a scheduled "rm -rf" or "git push
# --force" forces require_human=true at materialization, just like a live send.
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"\b(DELETE|DROP|TRUNCATE|deploy|production|git push --force|rm -rf)\b",
    re.IGNORECASE,
)


def _parse_deliver_at(value: str, now: datetime) -> Optional[datetime]:
    """Accept ISO 8601 ('2026-05-22T15:30:00Z') or relative ('+30s', '+5m', '+2h', '+1d')."""
    if not value:
        return None
    m = _RELATIVE_RE.match(value.strip())
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta_kwargs = {
            "s": {"seconds": amount},
            "m": {"minutes": amount},
            "h": {"hours": amount},
            "d": {"days": amount},
        }[unit]
        return now + timedelta(**delta_kwargs)
    return parse_timestamp(value)


@mcp.tool()
async def memory_set_reminder(
    session_id: str,
    deliver_at: str,
    message: str,
    category: str = "info",
    priority: str = "normal",
    dedup_key: Optional[str] = None,
) -> str:
    """
    Set a reminder to wake yourself at a future time.

    Self-only. The reminder body is delivered to your own inbox at deliver_at,
    routed through the same push path as memory_send_message — surfaces as a
    channel block on next user turn (when inbox push is healthy) and is always
    pullable via memory_get_messages.

    Use for "remind me later" patterns: scheduled follow-ups, time-boxed checks,
    deferred actions that need to wake you up at a specific time. Persists in
    Mongo; restart-safe.

    Args:
        session_id: Your session ID.
        deliver_at: ISO 8601 UTC timestamp ("2026-05-22T15:30:00Z") OR relative
            ("+30s", "+5m", "+2h", "+1d"). Must be in the future, max 30 days.
        message: The reminder body delivered to you at deliver_at.
        category: Same set as memory_send_message (info/task/question/review/blocker).
        priority: low / normal / urgent.
        dedup_key: Optional caller-supplied string. If a pending reminder with
            this key already exists for you, returns that schedule_id instead
            of creating a duplicate. Use for idempotent reminders.

    Limits:
        - Max 20 pending reminders per agent at any time (cancel to free slots).
        - Max 30-day horizon on deliver_at.
        - Destructive keywords (DELETE/DROP/rm -rf/etc.) in body force
          require_human=true on the materialized message.
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in MESSAGE_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {MESSAGE_PRIORITIES}"})
    if category not in MESSAGE_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Must be one of: {MESSAGE_CATEGORIES}"})

    session_info = active_sessions[session_id]
    agent = session_info.get("claude_instance")
    project = normalize_project(session_info.get("project", ""))
    if not agent or not project:
        return json.dumps({"error": "Session missing claude_instance or project"})

    now = utc_now()
    target = _parse_deliver_at(deliver_at, now)
    if target is None:
        return json.dumps({
            "error": "Invalid deliver_at. Use ISO 8601 UTC or relative ('+30s', '+5m', '+2h', '+1d')"
        })
    if target <= now:
        return json.dumps({"error": "deliver_at must be in the future"})
    horizon = now + timedelta(days=MAX_HORIZON_DAYS)
    if target > horizon:
        return json.dumps({
            "error": f"deliver_at exceeds {MAX_HORIZON_DAYS}-day horizon",
            "max_deliver_at": horizon.isoformat(),
        })

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    if dedup_key:
        existing = db.scheduled_messages.find_one({
            "agent_instance": agent,
            "agent_project": project,
            "dedup_key": dedup_key,
            "status": "pending",
        })
        if existing:
            return json.dumps({
                "status": "exists",
                "schedule_id": existing["_id"],
                "deliver_at": existing["deliver_at"].isoformat() if isinstance(existing["deliver_at"], datetime) else existing["deliver_at"],
                "dedup_key": dedup_key,
            })

    pending_count = db.scheduled_messages.count_documents({
        "agent_instance": agent,
        "agent_project": project,
        "status": "pending",
    })
    if pending_count >= MAX_PENDING_PER_AGENT:
        return json.dumps({
            "error": f"Pending reminder cap reached ({MAX_PENDING_PER_AGENT}). "
                     "Cancel some with memory_cancel_reminder to free slots.",
            "pending_count": pending_count,
            "pending_cap": MAX_PENDING_PER_AGENT,
        })

    schedule_id = f"msg_sched_{uuid.uuid4().hex[:12]}"
    doc = {
        "_id": schedule_id,
        "agent_instance": agent,
        "agent_project": project,
        "deliver_at": target,
        "message": message,
        "category": category,
        "priority": priority,
        "dedup_key": dedup_key,
        "status": "pending",
        "created_at": now,
        "created_by_session": session_id,
        "delivered_message_id": None,
        "delivered_at": None,
    }
    try:
        db.scheduled_messages.insert_one(doc)
    except Exception as e:
        log.error("scheduler: insert failed for %s: %s", schedule_id, e)
        return json.dumps({"error": f"Insert failed: {e}"})

    return json.dumps({
        "status": "scheduled",
        "schedule_id": schedule_id,
        "deliver_at": target.isoformat(),
        "agent_instance": agent,
        "agent_project": project,
        "pending_count": pending_count + 1,
        "pending_cap": MAX_PENDING_PER_AGENT,
    })


@mcp.tool()
async def memory_cancel_reminder(session_id: str, schedule_id: str) -> str:
    """
    Cancel a pending reminder. Caller must own the reminder.

    Idempotent on already-cancelled / already-delivered reminders: returns the
    current status instead of failing.
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    agent = session_info.get("claude_instance")
    project = normalize_project(session_info.get("project", ""))

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    doc = db.scheduled_messages.find_one({"_id": schedule_id})
    if not doc:
        return json.dumps({"error": "Schedule not found", "schedule_id": schedule_id})
    if doc.get("agent_instance") != agent or doc.get("agent_project") != project:
        return json.dumps({
            "error": "Permission denied — caller does not own this schedule",
            "schedule_id": schedule_id,
        })
    if doc.get("status") != "pending":
        return json.dumps({
            "status": doc["status"],
            "schedule_id": schedule_id,
            "note": "Schedule is no longer pending; nothing to cancel.",
        })

    db.scheduled_messages.update_one(
        {"_id": schedule_id, "status": "pending"},
        {"$set": {"status": "cancelled", "cancelled_at": utc_now()}},
    )
    return json.dumps({"status": "cancelled", "schedule_id": schedule_id})


@mcp.tool()
async def memory_list_reminders(
    session_id: str,
    include_history: bool = False,
    limit: int = 20,
) -> str:
    """
    List your reminders.

    Args:
        session_id: Your session ID.
        include_history: When True, includes delivered/cancelled rows. Default
            False shows only pending.
        limit: Max rows to return (default 20).
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    agent = session_info.get("claude_instance")
    project = normalize_project(session_info.get("project", ""))

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    query = {"agent_instance": agent, "agent_project": project}
    if not include_history:
        query["status"] = "pending"

    cursor = db.scheduled_messages.find(query).sort("deliver_at", 1).limit(limit)
    items = []
    for doc in cursor:
        deliver_at = doc.get("deliver_at")
        items.append({
            "schedule_id": doc["_id"],
            "deliver_at": deliver_at.isoformat() if isinstance(deliver_at, datetime) else deliver_at,
            "status": doc.get("status"),
            "category": doc.get("category"),
            "priority": doc.get("priority"),
            "message_preview": (doc.get("message") or "")[:200],
            "dedup_key": doc.get("dedup_key"),
            "delivered_message_id": doc.get("delivered_message_id"),
        })
    return json.dumps({"count": len(items), "items": items, "pending_cap": MAX_PENDING_PER_AGENT})


# ── Background scanner ───────────────────────────────────────────────────

_scanner_task: Optional[asyncio.Task] = None


async def _materialize_one(db, sched_doc: dict) -> Optional[str]:
    """Convert a due scheduled row into an inbox message + push notification.

    Returns the new message_id on success, None on failure (row stays pending
    and gets retried on the next tick).
    """
    # Local import: avoid module-load cycles via tools/__init__.py.
    from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send

    agent = sched_doc["agent_instance"]
    project = sched_doc["agent_project"]
    body = sched_doc.get("message", "")
    is_destructive = bool(_DESTRUCTIVE_KEYWORDS.search(body))

    now = utc_now()
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    msg_doc = {
        "_id": message_id,
        "to_instance": agent,
        "to_project": project,
        "from_instance": agent,
        "from_project": project,
        "from_session": sched_doc.get("created_by_session"),
        "message": body,
        "priority": sched_doc.get("priority", "normal"),
        "category": sched_doc.get("category", "info"),
        "reply_to": None,
        "in_response_to": None,
        # Self-message at chain depth 0 — sender == recipient, no auto-reply loop.
        "chain_depth": 0,
        "require_human": is_destructive,
        "sent_by_human": False,
        "human_interacted": False,
        "user_originated": False,
        "status": "pending",
        "push_suppressed": False,
        "push_suppress_reason": None,
        "emission_count": 0,
        "recency_bypass": False,
        # Provenance markers so consumers can distinguish a scheduled
        # materialization from a live send if they care.
        "scheduled_delivery": True,
        "scheduled_schedule_id": sched_doc["_id"],
        "created_at": now,
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }
    try:
        db.messages.insert_one(msg_doc)
    except Exception as e:
        log.error("scheduler: message insert failed for sched_id=%s: %s", sched_doc["_id"], e)
        return None

    try:
        # Server-authoritative delivery §E: content-push the announce (None for
        # badge-only/info) so a scheduled action message wakes the recipient too.
        await _notify_inbox_for_send(project, agent, _build_announce_packet(msg_doc))
    except Exception as e:
        # Push failure is non-fatal — message is in the inbox for pull-side delivery.
        log.warning("scheduler: inbox notify failed for sched_id=%s: %s", sched_doc["_id"], e)

    return message_id


async def _scanner_loop():
    """Background tick that drains due schedules. Survives transient Mongo blips."""
    log.info(
        "scheduler: started (tick=%ss, horizon=%sd, max_pending=%s)",
        SCANNER_TICK_SECONDS, MAX_HORIZON_DAYS, MAX_PENDING_PER_AGENT,
    )
    while True:
        try:
            db = get_mongo()
            if db is not None:
                now = utc_now()
                due = list(db.scheduled_messages.find({
                    "status": "pending",
                    "deliver_at": {"$lte": now},
                }))
                for sched_doc in due:
                    new_msg_id = await _materialize_one(db, sched_doc)
                    if new_msg_id is None:
                        continue
                    db.scheduled_messages.update_one(
                        {"_id": sched_doc["_id"], "status": "pending"},
                        {"$set": {
                            "status": "delivered",
                            "delivered_message_id": new_msg_id,
                            "delivered_at": utc_now(),
                        }},
                    )
                    log.info(
                        "scheduler: delivered %s → %s (agent=%s/%s)",
                        sched_doc["_id"], new_msg_id,
                        sched_doc["agent_instance"], sched_doc["agent_project"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("scheduler: loop error: %s", e)
        await asyncio.sleep(SCANNER_TICK_SECONDS)


def start_scheduler() -> None:
    """Start the scheduler background task. Idempotent if already running."""
    global _scanner_task
    if _scanner_task is not None and not _scanner_task.done():
        return
    _scanner_task = asyncio.create_task(_scanner_loop(), name="scheduled_message_scanner")
    log.info("scheduler: background task created")


def stop_scheduler() -> None:
    """Cancel the scanner task on shutdown."""
    global _scanner_task
    if _scanner_task is not None and not _scanner_task.done():
        _scanner_task.cancel()
    _scanner_task = None


def ensure_indexes() -> None:
    """Idempotently create the scheduled_messages indexes. Called from app_lifespan."""
    db = get_mongo()
    if db is None:
        return
    try:
        db.scheduled_messages.create_index([("deliver_at", 1), ("status", 1)])
        db.scheduled_messages.create_index(
            [("agent_instance", 1), ("agent_project", 1), ("status", 1)]
        )
        db.scheduled_messages.create_index(
            [("dedup_key", 1), ("agent_instance", 1), ("agent_project", 1), ("status", 1)],
            sparse=True,
        )
    except Exception as e:
        log.warning("scheduler: index creation failed (non-fatal): %s", e)
