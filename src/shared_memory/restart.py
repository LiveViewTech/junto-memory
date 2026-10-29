"""Graceful-restart ops: drain flag + restart-warning broadcast.

Drain:
    A module-level boolean (set via memory_admin(action="drain", value=True)).
    When set, memory_start_session refuses NEW sessions with a structured error
    instructing the caller to retry shortly. Existing sessions continue to work
    so in-flight tool calls complete. Resets to False on every process restart
    (the whole point is short-lived gating during the restart prep window).

Broadcast:
    memory_admin(action="broadcast_restart_warning", seconds_until=N, reason=...)
    fans out a single system@junto notice to every currently-active agent (one
    per (instance, project) pair, dedup'd). Notice is push-enabled (push_suppressed
    =False) so it surfaces immediately on sage-side and lands in the inbox for
    pull-side delivery elsewhere.
"""

import logging
import uuid
from typing import Optional, Tuple

from shared_memory.clients import get_mongo
from shared_memory.helpers import normalize_project, utc_now
from shared_memory.state import active_sessions

log = logging.getLogger(__name__)


# ── Drain ────────────────────────────────────────────────────────────────

_draining: bool = False
_drain_reason: Optional[str] = None


def is_draining() -> Tuple[bool, Optional[str]]:
    """Return current drain state. (draining, reason)."""
    return _draining, _drain_reason


def set_draining(state: bool, reason: Optional[str] = None) -> None:
    """Toggle the drain flag. Owner-tier only — gated at the admin tool layer."""
    global _draining, _drain_reason
    _draining = bool(state)
    _drain_reason = reason if state else None
    log.info("restart: drain=%s reason=%r", _draining, _drain_reason)


def drain_error_payload() -> dict:
    """Standard error shape returned when memory_start_session is gated by drain."""
    return {
        "error": "Server is draining for graceful restart. Retry shortly.",
        "draining": True,
        "drain_reason": _drain_reason,
    }


# ── Broadcast ────────────────────────────────────────────────────────────

async def broadcast_restart_warning(
    seconds_until: int,
    reason: Optional[str] = None,
    from_project: str = "junto",
) -> dict:
    """Fan out a system@junto restart-warning notice to every active agent.

    "Active" = distinct (claude_instance, project) pairs seen in active_sessions.
    Dedupes across multiple concurrent sessions for the same agent so a single
    agent doesn't get N copies just because it's spun up N Claude tabs.

    Returns {recipients_count, recipient_pairs, message_ids}.
    """
    # Local imports to avoid load-time cycles.
    from shared_memory.tools.messaging import _notify_inbox_for_send

    db = get_mongo()
    if db is None:
        return {"error": "MongoDB unavailable"}

    # Build the deduplicated recipient set from active sessions.
    pairs: set[Tuple[str, str]] = set()
    for sess in active_sessions.values():
        agent = sess.get("claude_instance")
        project = normalize_project(sess.get("project") or "")
        if agent and project and agent != "system":
            pairs.add((agent, project))

    if not pairs:
        return {
            "recipients_count": 0,
            "recipient_pairs": [],
            "message_ids": [],
            "note": "No active sessions to notify",
        }

    body_lines = [
        f"Server restarting in {seconds_until}s.",
    ]
    if reason:
        body_lines.append(f"Reason: {reason}")
    body_lines.append(
        "Recommended action: complete current tool call and pause before "
        "issuing further calls. The MCP transport will reset, but your "
        "session_id survives the restart (session_persist.py): keep using it. "
        "Only if a call returns 'Session not found', re-run memory_start_session "
        "with guidelines_held=<your guidelines.hash>."
    )
    body = "\n".join(body_lines)

    now = utc_now()
    norm_from_project = normalize_project(from_project) or from_project

    message_ids: list[str] = []
    for agent, project in sorted(pairs):
        notice_id = f"msg_{uuid.uuid4().hex[:12]}"
        doc = {
            "_id": notice_id,
            "to_instance": agent,
            "to_project": project,
            "from_instance": "system",
            "from_project": norm_from_project,
            "from_session": None,
            "message": body,
            "priority": "urgent",
            "category": "info",
            "reply_to": None,
            "in_response_to": None,
            "chain_depth": 0,
            "require_human": False,
            "sent_by_human": False,
            "human_interacted": False,
            "user_originated": False,
            "status": "pending",
            # Push-enabled so the warning surfaces immediately. Differs from
            # push_control.recovery notices which are intentionally non-pushing.
            "push_suppressed": False,
            "push_suppress_reason": None,
            "emission_count": 0,
            "recency_bypass": False,
            "is_system_notice": True,
            "system_notice_kind": "restart_warning",
            "restart_seconds_until": int(seconds_until),
            "restart_reason": reason,
            "created_at": now,
            "delivered_at": None,
            "received_at": None,
            "completed_at": None,
        }
        try:
            db.messages.insert_one(doc)
            message_ids.append(notice_id)
        except Exception as e:
            log.warning("restart: notice insert failed for %s/%s: %s", agent, project, e)
            continue
        try:
            await _notify_inbox_for_send(project, agent)
        except Exception as e:
            log.warning("restart: notify failed for %s/%s: %s", agent, project, e)

    return {
        "recipients_count": len(message_ids),
        "recipient_pairs": [{"agent": a, "project": p} for a, p in sorted(pairs)],
        "message_ids": message_ids,
        "seconds_until": int(seconds_until),
        "reason": reason,
    }
