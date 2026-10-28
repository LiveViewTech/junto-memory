"""Inter-agent messaging tools - send/receive messages, agent status, discovery."""

import asyncio
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Set

from mcp.server.fastmcp import Context
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from pydantic import AnyUrl
from pymongo import ReturnDocument

from shared_memory import push_control
from shared_memory.app import mcp
from shared_memory.audit import log_audit
from shared_memory.clients import get_mongo
from shared_memory.config import (
    ACTION_CATEGORIES,
    MESSAGE_ACTION_TTL_DAYS,
    MESSAGE_CATEGORIES,
    MESSAGE_INFO_TTL_HOURS,
    MESSAGE_PRIORITIES,
    MESSAGE_STATUSES,
    NOTIFY_SEND_TIMEOUT,
    OBLIGATION_RESOLVE_ON_REPLY,
    SSE_KEEPALIVE_SECONDS,
    SSE_KEEPALIVE_SEND_TIMEOUT,
    classify_lane,
)
from shared_memory.helpers import normalize_project, parse_timestamp, require_session, utc_now
from shared_memory.intent import get_current_intent_id
from shared_memory.op_log import with_op_log
from shared_memory.state import active_sessions, mcp_session_to_app
from shared_memory.tools.projects import _fuzzy_match_agent, _is_project_admin, resolve_agent_name

log = logging.getLogger(__name__)

# ── Phase C2: inbox resource subscriptions ──
# Maps inbox://<project>/<agent> URI → set of ServerSession objects that have
# subscribed via SubscribeRequest. Populated by the subscribe_resource handler
# below; drained on unsubscribe or on send-failure (dead session). Lives in
# process memory because subscriptions are tied to the live HTTP session — a
# disconnect drops the session and the subscription with it.
inbox_subscriptions: Dict[str, Set[Any]] = {}


def inbox_uri(project: str, agent: str) -> str:
    """Canonical inbox URI for a (project, agent) pair.

    Project component is normalized so all subscribers/notifiers share the
    same key in inbox_subscriptions regardless of how they spelled the
    project name on input.
    """
    return f"inbox://{normalize_project(project)}/{agent}"


def _live_subscribers_count(to_project: str, to_instance: str) -> int:
    """Distinct subscriber sessions that will receive a notify for this send.

    Direct send (to_instance != '*'): count subscribers on the recipient's
    inbox URI.
    Broadcast (to_instance == '*'): union of subscribers across every
    agent-inbox URI in the target project, so a session subscribed to
    several inboxes in the same project counts once, not multiple times.

    Returns 0 if no project, no live subscribers, or no matching URIs.
    Used by memory_send_message to surface delivery confidence to the
    sender (backlog_8e1d3e45f6f1) — pairs with `persisted` to distinguish
    "stored for next /go pickup" from "live-pushed to N sessions".
    """
    if not to_project:
        return 0
    if to_instance == "*":
        prefix = f"inbox://{normalize_project(to_project)}/"
        union: Set[Any] = set()
        for uri, bucket in inbox_subscriptions.items():
            if uri.startswith(prefix):
                union.update(bucket)
        return len(union)
    uri_str = inbox_uri(to_project, to_instance)
    return len(inbox_subscriptions.get(uri_str, set()))


def _recipient_idle_snapshot(db, to_project: str, to_instance: str):
    """What's waiting for a recipient who has NO live subscriber, + how long
    they've been idle — so the SENDER can decide to escalate (manually wake the
    agent) instead of leaving a real ask blind. Read-side idle-queue visibility:
    backlog_da56a6e0c46b, coordinator ask msg_5982f608ec7b.

    Returns None (caller omits the field) for broadcasts, missing db, or missing
    project — there's no single recipient to summarize. The counts MIRROR what the
    recipient's own next get_messages would surface: same instance/project match,
    same per-message-read + watermark-floor unread semantics (via
    _compute_lane_counts), so the number the sender sees == the number the
    recipient will actually find waiting.

    `idle_hours` is derived from agent_directory.last_seen (bumped on start_session,
    heartbeat, and human interaction). It is NOT a liveness proof — a parked agent
    and a half-open-SSE agent can both look "idle"; pair it with live_subscribers=0,
    which is the actual no-live-stream signal that triggers this snapshot.
    """
    if db is None or not to_project or not to_instance or to_instance == "*":
        return None
    proj = normalize_project(to_project)
    # Mirror the recipient-side inbox match (see memory_get_messages): workers get
    # direct-only; everyone else also matches broadcast (*). Project clause accepts
    # the recipient's project plus legacy/empty-project broadcasts.
    if to_instance.startswith("worker_"):
        instance_match = {"to_instance": to_instance}
    else:
        instance_match = {"$or": [{"to_instance": to_instance}, {"to_instance": "*"}]}
    project_match = {
        "$or": [
            {"to_project": proj},
            {"to_project": {"$exists": False}},
            {"to_project": ""},
        ]
    }
    watermark = _get_messages_seen_through(db, proj, to_instance)
    lane = _compute_lane_counts(
        db, [instance_match, project_match], watermark=watermark, reader=to_instance
    )

    last_seen = None
    try:
        doc = db.agent_directory.find_one(
            {"project": proj, "instance": to_instance}, {"last_seen": 1}
        )
        if doc:
            last_seen = parse_timestamp(doc.get("last_seen"))
    except Exception:
        last_seen = None
    idle_hours = None
    if last_seen is not None:
        try:
            idle_hours = round((utc_now() - last_seen).total_seconds() / 3600.0, 1)
        except TypeError:
            idle_hours = None

    return {
        # Real asks waiting on this recipient — the number that drives an escalate
        # decision. Open obligations ignore read state (a seen-but-unanswered ask
        # still owes a reply); responded are tracked-but-discharged.
        "queued_action_open": lane["pending_action_open"],
        "queued_action_responded": lane["pending_action_responded"],
        "queued_fyi_waiting": lane["pending_fyi_waiting"],
        "last_seen": last_seen.isoformat() if last_seen else None,
        "idle_hours": idle_hours,
    }


def _resolve_caller_identity():
    """Look up the calling agent's app session by joining the current MCP
    transport session (from request_context) with the mcp_session_to_app map.

    Returns a dict with project / claude_instance / role / session_id, or
    None if the caller has not started a memory session yet.
    """
    try:
        server_session = mcp._mcp_server.request_context.session
    except (LookupError, AttributeError):
        return None
    app_sid = mcp_session_to_app.get(server_session)
    if app_sid is None:
        return None
    info = active_sessions.get(app_sid)
    if info is None:
        return None
    return {
        "session_id": app_sid,
        "project": normalize_project(info.get("project", "")),
        "claude_instance": info.get("claude_instance", ""),
        "role": info.get("role", "agent"),
    }


def _check_inbox_authz(uri_project: str, uri_agent: str):
    """Authorize a read/subscribe on inbox://<uri_project>/<uri_agent>.

    Returns (ok: bool, reason: str). Allowed if the caller is the named
    agent itself, an admin in the project, or has the cross-project
    'admin' or 'user' role from auth.py. The '*' agent (broadcast inbox)
    is admin-only.
    """
    caller = _resolve_caller_identity()
    if caller is None:
        return False, "no active memory session for this MCP connection"

    if caller["role"] in ("admin", "user"):
        return True, "role"

    db = get_mongo()
    if db is not None and _is_project_admin(db, uri_project, caller["claude_instance"]):
        return True, "project_admin"

    if uri_agent == "*":
        return False, "broadcast inbox URI is admin-only"

    if caller["project"] == uri_project and caller["claude_instance"] == uri_agent:
        return True, "self"

    return False, (
        f"caller {caller['claude_instance']}@{caller['project']} cannot access "
        f"inbox://{uri_project}/{uri_agent}"
    )


def get_tmux_target_for_instance(instance_name: str) -> str:
    """Look up a delivery target for an agent from active sessions.

    The field name is kept for backward compatibility with the external
    dispatcher process, but the value is just an opaque routing string.
    """
    for session_id, info in active_sessions.items():
        if info["claude_instance"] == instance_name and info.get("tmux_target"):
            return info["tmux_target"]
    return None


def get_pending_messages_for_instance(instance_name: str, project: str = None) -> List[Dict]:
    """Get undelivered messages for a specific instance (MongoDB-backed)."""
    db = get_mongo()
    if db is None:
        return []

    instance_match = {
        "$or": [
            {"to_instance": instance_name},
            {"to_instance": "*"}
        ]
    }

    query = {"$and": [instance_match, {"status": "pending"}]}

    # Add project scoping if provided
    if project:
        norm_project = normalize_project(project)
        query["$and"].append({
            "$or": [
                {"to_project": norm_project},
                {"to_project": {"$exists": False}},
                {"to_project": ""},
            ]
        })

    messages = []
    for doc in db.messages.find(query):
        created_at = doc.get("created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        entry = {
            "id": doc["_id"],
            "to": doc.get("to_instance", doc.get("to", "?")),
            "to_project": doc.get("to_project", ""),
            "from_instance": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "subject": doc.get("subject"),
            "message": doc.get("message", ""),
            "priority": doc.get("priority", "normal"),
            "obligation": doc.get("obligation"),
            "component": doc.get("component"),
            "owner": doc.get("owner"),
            "created": created_at,
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        _add_lane_fields(entry)
        messages.append(entry)

    return messages


def _add_lane_fields(entry: Dict[str, Any]) -> None:
    """Stamp lane/tier onto a serialized message entry in place.

    The ONE place every message serializer reaches for the category→lane map
    (interface:lanes-a-server-wire-v0). Used by all four serializers so they
    cannot diverge (learning_84914604d129602e: the 4-serializer divergence
    failure mode). Reads the entry's own category/obligation, so it is always
    consistent with the payload it annotates.
    """
    lane, tier = classify_lane(entry.get("category", "info"), entry.get("obligation"))
    entry["lane"] = lane
    entry["tier"] = tier


def _message_entry(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Full single-message wire entry, lane fields stamped.

    Shared by the memory_get_messages message_id branch and memory_get_by_id's
    msg_* dispatch (backlog_f6f950b3b4ce) so the two single-message surfaces
    cannot diverge in shape.
    """
    entry = {
        "id": doc["_id"],
        "from": doc.get("from_instance", doc.get("from", "?")),
        "from_project": doc.get("from_project", ""),
        "to": doc.get("to_instance", doc.get("to", "?")),
        "to_project": doc.get("to_project", ""),
        "category": doc.get("category", "info"),
        "subject": doc.get("subject"),
        "message": doc["message"],
        "priority": doc.get("priority", "normal"),
        "status": doc.get("status", "?"),
        "obligation": doc.get("obligation"),
        "component": doc.get("component"),
        "owner": doc.get("owner"),
        "claimed_at": doc["claimed_at"].isoformat() if hasattr(doc.get("claimed_at"), "isoformat") else doc.get("claimed_at"),
        "expire_at": doc["expire_at"].isoformat() if hasattr(doc.get("expire_at"), "isoformat") else doc.get("expire_at"),
        "created": doc["created_at"].isoformat() if doc.get("created_at") else (doc["created"].isoformat() if doc.get("created") else None),
        "chain_depth": doc.get("chain_depth", 0),
        "require_human": bool(doc.get("require_human", False)),
        "user_originated": bool(doc.get("user_originated", False)),
        "sent_by_human": bool(doc.get("sent_by_human", False)),
        "human_interacted": bool(doc.get("human_interacted", False)),
        "push_suppressed": bool(doc.get("push_suppressed", False)),
        "push_suppress_reason": doc.get("push_suppress_reason"),
        "recency_bypass": bool(doc.get("recency_bypass", False)),
        "is_system_notice": bool(doc.get("is_system_notice", False)),
        "system_notice_kind": doc.get("system_notice_kind"),
    }
    if doc.get("reply_to"):
        entry["reply_to"] = doc["reply_to"]
    if doc.get("in_response_to"):
        entry["in_response_to"] = doc["in_response_to"]
    _add_lane_fields(entry)
    return entry


# Lanes-A within-page ordering rank (interface:lanes-a-server-wire-v0): an
# un-engaged ask sorts above an engaged one, and both above everything cleared
# or FYI. Anything not (action, 0/1) collapses to rank 2.
_LANE_TIER_RANK = {("action", 0): 0, ("action", 1): 1}
_PRIORITY_SORT = {"urgent": 0, "normal": 1, "low": 2}


def _sort_messages_by_lane(messages: List[Dict[str, Any]]) -> None:
    """In-place two-tier lanes-A sort (interface:lanes-a-server-wire-v0).

    Order: action-open > action-responded > cleared/fyi; newest-first WITHIN a
    tier; message priority (urgent>normal>low) as the FINAL tiebreak only — note
    this DEMOTES priority from the old primary key to a tiebreak, the intended
    lanes-A behavior change. Implemented as stable multi-pass (least-significant
    key first) because the recency key sorts descending while the others ascend.
    """
    messages.sort(key=lambda x: _PRIORITY_SORT.get(x.get("priority"), 99))      # tertiary
    messages.sort(key=lambda x: x.get("created") or "", reverse=True)           # secondary: recency
    messages.sort(key=lambda x: _LANE_TIER_RANK.get((x.get("lane"), x.get("tier")), 2))  # primary


def _compute_lane_counts(db, base_and: List[Dict[str, Any]], watermark=None,
                         reader=None) -> Dict[str, int]:
    """Server-side lane badge counts (interface:lanes-a-server-wire-v0).

    base_and identifies the agent's inbox (instance + project clauses) WITHOUT
    the page limit/cursor, so the badge reflects the whole actionable backlog the
    plugin's page-1 can't see the tail of.

      pending_action_open      — pending ACTION msgs with obligation open (or a
                                 legacy action msg with no obligation set)
      pending_action_responded — pending ACTION msgs with obligation responded
      pending_fyi_waiting      — pending info msgs the reader hasn't READ. UNREAD
                                 is per-message (read_by excludes `reader`,
                                 build-plan task 2) with the seen-watermark kept
                                 as a coarse floor (pre-task-2 history). The M
                                 count stays read-INERT: a resource/announce scan
                                 never writes read_by, so glancing never zeroes M.

    Resolved (cleared) action msgs are in neither action count — they've left the
    lane. Action counts ignore read state by design: a seen-but-unanswered
    question still owes a reply, so it must stay on the badge.
    """
    def _count(extra: Dict[str, Any]) -> int:
        return db.messages.count_documents({"$and": base_and + [extra]})

    action_open = _count({
        "category": {"$in": ACTION_CATEGORIES}, "status": "pending",
        "obligation": {"$in": ["open", None]},
    })
    action_responded = _count({
        "category": {"$in": ACTION_CATEGORIES}, "status": "pending",
        "obligation": "responded",
    })
    # High-signal subset for the statusline (Tom UX, 2026-06-22 via coordinator):
    # UNRESOLVED blockers addressed to the agent — category=blocker that has NOT
    # cleared. open|responded|None all count (a blocker stays blocking until
    # EXPLICITLY resolved; a reply leaves it "responded", still unresolved). This
    # is a subset of the action counts above, surfaced separately because blocker
    # is rare + high-signal (usually 0, spikes meaningfully) whereas action_open
    # is inflated by responded-but-unclosed obligations.
    blocker_open = _count({
        "category": "blocker", "status": "pending",
        "obligation": {"$in": ["open", "responded", None]},
    })
    fyi_clause: Dict[str, Any] = {"category": "info", "status": "pending"}
    if reader is not None:
        fyi_clause["read_by"] = {"$ne": reader}
    if watermark is not None:
        fyi_clause["created_at"] = {"$gt": watermark}
    fyi_waiting = _count(fyi_clause)

    # FYI aging signal (GUIDANCE, not force): age in hours of the OLDEST unread
    # FYI, so a long-session agent can be nudged to drain before info ages out at
    # MESSAGE_INFO_TTL_HOURS (48h). Pure data — nothing auto-expires/blocks on it;
    # the agent/plugin decides whether/how to surface "FYIs aging, check soon".
    # null when there are no waiting FYIs.
    fyi_oldest_age_hours = None
    if fyi_waiting:
        oldest = next(iter(
            db.messages.find({"$and": base_and + [fyi_clause]})
                       .sort([("created_at", 1)]).limit(1)
        ), None)
        oc = (oldest or {}).get("created_at")
        if oc is not None:
            try:
                fyi_oldest_age_hours = round(
                    (utc_now() - oc).total_seconds() / 3600.0, 1
                )
            except TypeError:  # non-datetime created_at — skip the signal
                fyi_oldest_age_hours = None
    return {
        "pending_action_open": action_open,
        "pending_action_responded": action_responded,
        "pending_blocker_open": blocker_open,
        "pending_fyi_waiting": fyi_waiting,
        "pending_fyi_oldest_age_hours": fyi_oldest_age_hours,
        "fyi_ttl_hours": MESSAGE_INFO_TTL_HOURS,
    }


# Phase C1.1: destructive content gate. Body containing any of these patterns
# automatically gets require_human=True so clients never auto-act on it.
# This regex is gated by chain_depth>0 in the caller — a depth-0 send is
# deliberate (human or new agent chain) and trusted to set require_human itself.
#
# Tightened from the v1 pattern (backlog_6bcf2d646772). The original matched
# any case-insensitive mention of DELETE/DROP/TRUNCATE/deploy/production, which
# false-positived on prose ("category=info: we're going to deploy on Friday"
# was getting require_human=True). New rules:
#   - SQL keywords require an adjacent SQL noun (FROM/TABLE/...)
#   - All-caps only — SQL destructive statements are always upper-case in real
#     code; lower-case "drop the table" in prose no longer matches
#   - "deploy/production/prod" removed — too noisy in prose; callers can pass
#     require_human=True when they really mean it
#   - rm -rf added — universally destructive
_DESTRUCTIVE_KEYWORDS = re.compile(
    r"\b(DELETE\s+FROM|DROP\s+(TABLE|DATABASE|SCHEMA|INDEX|COLLECTION|VIEW)|TRUNCATE\s+TABLE)\b"
    r"|git\s+push\s+(--force|-f)\b"
    r"|\brm\s+-rf\b"
)

# ── Recency window (now drives push-suppression filter release, §3) ────────
# Three signals open the 5-minute window for an agent:
#   1. memory_start_session for that agent (set in sessions.py).
#   2. Inbound `sent_by_human=True` message DELIVERED to that agent (set
#      below on read).
#   3. Outbound `human_interacted=True` message FROM that agent (set below
#      on send).
# All three write agent_directory.last_human_interaction. push_control.py's
# delivery filter (§3) checks this timestamp; when the window is open, the
# inbox resource releases push_suppressed messages. The depth cap itself no
# longer consults this window (push-control-v0 §6 — the cap is unconditional).
HUMAN_RECENCY_WINDOW_SECONDS = 300  # 5 minutes


def _has_recent_human_interaction(db, project: str, agent: str) -> bool:
    """Did `agent` in `project` see/produce a human-tagged message within the
    HUMAN_RECENCY_WINDOW_SECONDS? Reads agent_directory.last_human_interaction.
    """
    if db is None or not project or not agent:
        return False
    try:
        doc = db.agent_directory.find_one(
            {"project": project, "instance": agent},
            {"last_human_interaction": 1},
        )
    except Exception:
        return False
    if not doc:
        return False
    last = doc.get("last_human_interaction")
    if last is None:
        return False
    last_dt = parse_timestamp(last)
    if last_dt is None:
        return False
    age = (utc_now() - last_dt).total_seconds()
    return age <= HUMAN_RECENCY_WINDOW_SECONDS


def _bump_human_interaction(db, project: str, agent: str, when=None) -> None:
    """Record that `agent` in `project` was just touched by a human signal.

    Best-effort: missing rows are upserted so the next gate check sees the
    timestamp even for agents that never started a session here.
    """
    if db is None or not project or not agent:
        return
    ts = when or utc_now()
    try:
        db.agent_directory.update_one(
            {"project": project, "instance": agent},
            {"$set": {"last_human_interaction": ts, "last_seen": ts}},
            upsert=True,
        )
    except Exception:
        pass


def _get_messages_seen_through(db, project: str, agent: str):
    """Return the agent's read-watermark (messages_seen_through) datetime, or None.

    See design:message-read-watermark-v0. Distinct from last_seen /
    last_human_interaction — those are recency signals for push-control's
    5-min bypass and MUST NOT be overloaded as the read dedup marker.
    """
    if db is None or not project or not agent:
        return None
    try:
        doc = db.agent_directory.find_one(
            {"project": project, "instance": agent},
            {"messages_seen_through": 1},
        )
    except Exception:
        return None
    if not doc:
        return None
    return parse_timestamp(doc.get("messages_seen_through"))


def _advance_messages_seen_through(db, project: str, agent: str, ts) -> None:
    """Advance the agent's read-watermark to `ts`, forward-only ($max).

    $max only moves the stored value forward, so a late/out-of-order read can
    never rewind the watermark and re-surface already-seen messages. Best-effort.
    """
    if db is None or not project or not agent or ts is None:
        return
    try:
        db.agent_directory.update_one(
            {"project": project, "instance": agent},
            {"$max": {"messages_seen_through": ts}},
            upsert=True,
        )
    except Exception:
        pass


def _mark_messages_read(db, message_ids, instance: str) -> None:
    """Mark messages READ by `instance` — per-message read state, the SOURCE OF
    TRUTH for unread (design:server-authoritative-delivery-v0 §E / contract:
    message-lanes-v0 §E5, build-plan task 2). Replaces the auto-advancing
    messages_seen_through watermark, which conflated "I glanced at a new message"
    with "every older message is read". Idempotent ($addToSet read_by); marks ONLY
    the given messages (per-message), so a truncated page never marks the
    unreturned tail read. Does NOT touch the watermark (now a coarse floor only).
    Best-effort: a read-state write must never break the read itself.
    """
    if db is None or not instance or not message_ids:
        return
    try:
        db.messages.update_many(
            {"_id": {"$in": list(message_ids)}},
            {"$addToSet": {"read_by": instance}},
        )
    except Exception as e:  # pragma: no cover — defensive
        log.debug("read-state: mark-read failed for %s: %s", instance, e)


def _advance_parent_obligation_on_reply(db, parent_id: str, replier: str, now) -> "str | None":
    """Auto-ack: when a reply's sender is the parent's OWNER, advance the parent's
    obligation (design:unified-messaging-v0 Stage 3 / lanes-B).

    owner_of(parent) := parent.owner ?? parent.to_instance — the generalized scope
    guard. `owner` is unset today (claiming, Stage 2, populates it later), so this
    falls back to the named recipient and is forward-compatible: a direct message's
    owner IS its recipient, which is exactly the lanes-B guard reply.from==parent.to.

    Transition (ACTION categories only; info/non-action carry no obligation):
      {question, contract, review} -> resolved   (an answer satisfies)
      {task, blocker}              -> responded   (engaged; stays in the action lane,
                                                   deprioritized, until an explicit done)
    Never downgrades an already-resolved parent (idempotent re-reply). Broadcast
    parents (to_instance="*") never match a concrete replier -> never auto-ack.
    Best-effort: a failure here must never break the underlying send.

    RETURNS the new obligation state ("responded"|"resolved") iff it advanced an
    open/responded ACTION parent, else None. The caller uses a non-None return to
    PROMOTE this reply to push even if the reply's own category is badge-only
    (contract:reply-promotion-v0) — the advance conditions ARE the promotion
    conditions (action-lane parent + owner-guard + not-already-resolved).
    """
    if db is None or not parent_id or not replier:
        return None
    try:
        parent = db.messages.find_one(
            {"_id": parent_id},
            {"category": 1, "to_instance": 1, "owner": 1, "obligation": 1},
        )
        if not parent:
            return None
        if parent.get("category") not in ACTION_CATEGORIES:
            return None  # info / non-action messages carry no obligation
        owner = parent.get("owner") or parent.get("to_instance")
        if not owner or replier != owner:
            return None  # scope guard: only the addressed owner's own reply clears it
        if parent.get("obligation") == "resolved":
            return None  # terminal — never downgrade (follow-up chatter, not promoted)
        if parent["category"] in OBLIGATION_RESOLVE_ON_REPLY:
            update = {"obligation": "resolved", "responded_at": now, "resolved_at": now}
        else:  # task, blocker — engaged but not done
            update = {"obligation": "responded", "responded_at": now}
        db.messages.update_one({"_id": parent_id}, {"$set": update})
        # Stage-5 TTL: a RESOLVED action is terminal → start its 7d expiry.
        # "responded" is NOT terminal (stays in the action lane), so only the
        # resolve branch ages the message out.
        if update.get("obligation") == "resolved":
            _set_action_message_expiry(db, parent_id)
        return update["obligation"]
    except Exception:
        return None


def _set_action_message_expiry(db, message_id):
    """Stage-5 TTL: when an ACTION message reaches a terminal state (acked or
    obligation resolved), give it expire_at = created + 7d so it ages out. Until
    then an action carries expire_at=null and never expires (the load-bearing
    "unacked action never vanishes" property). Idempotent — only sets when
    expire_at is still null — and ACTION-only (info already got its 48h expiry at
    send). Best-effort: a failure must never break the calling status update.
    """
    if db is None or not message_id:
        return
    try:
        db.messages.update_one(
            {"_id": message_id, "expire_at": None,
             "category": {"$in": ACTION_CATEGORIES}},
            [{"$set": {"expire_at": {
                "$add": ["$created_at", MESSAGE_ACTION_TTL_DAYS * 24 * 3600 * 1000]
            }}}],
        )
    except Exception:
        pass


@mcp.tool()
async def memory_send_message(
    session_id: str,
    to_instance: str,
    message: str,
    priority: str = "normal",
    category: str = "info",
    to_project: str = None,
    reply_to: str = None,
    in_response_to: str = None,
    chain_depth: int = None,
    require_human: bool = False,
    human_interacted: bool = False,
    component: str = None,
    subject: str = None,
    ctx: Context = None
) -> str:
    """
    Send a note to another agent in the project.

    Notes are persisted to MongoDB and appear in the recipient's inbox on their
    next session start. Supports full lifecycle tracking: pending, delivered,
    received, completed, failed.

    CATEGORY IS LOAD-BEARING — it sets the OBLIGATION (what you owe / are
    owed), not visibility: since push-all-info-v0 EVERY message pushes (action
    categories push full/header and PERSIST until cleared; info pushes a
    header, carries NO obligation, ages out ~48h). Rules (relocated here from
    the session guidelines, design:guideline-trim-v0):
    (1) DON'T INFLATE — filing an FYI as task/question buys no visibility and
        pollutes the action lane with an obligation that never clears.
    (2) DON'T UNDER-CALL — if you need something BACK (answer, decision,
        work), use an action category so it persists until cleared.
    (3) REPLY, DON'T LET IT ROT — replying with in_response_to=<parent>
        auto-clears question/review/contract; task/blocker stay OPEN until
        explicitly marked done (memory_update_message_status).
    (4) BROADCASTS (to='*') are info-lane only and project-scoped; an
        action-category broadcast never clears — address named recipients for
        group actions.
    (5) SEND-BAR — every push costs the recipient a context line: don't
        message what you can memory_query yourself; no empty acks or status
        pings (silence is fine).
    (6) Set human_interacted=True ONLY when a human-typed prompt is driving
        this send; False on autopilot replies.

    Args:
        session_id: Your session ID
        to_instance: Target agent name (e.g., 'frontend', 'backend', or '*' for all)
        message: The note content
        priority: Note priority (urgent, normal, low)
        category: Note category - determines the obligation it creates:
            contract - request to change cross-team behavior/interface; needs ratify/amend/block
            task - work assignment; stays open until explicitly marked done
            question - needs an answer back; cleared by your reply
            info - FYI, no action needed (default); header push, ages out ~48h
            review - look at this and confirm or flag issues; cleared by your reply
            blocker - sender is STOPPED until resolved (highest urgency)
        to_project: Target project (defaults to your project; use for cross-project notes)
        reply_to: Note ID this is replying to (for threading conversations)
        in_response_to: Message ID this is a programmatic auto-response to.
            If set, server computes chain_depth = parent.chain_depth + 1.
        chain_depth: Override chain depth. Server takes the
            max of (parent_depth+1, caller-provided). Above the configured
            push-control depth_cap (default 12 per project) the message is
            persisted but its push notification is suppressed (it sits in the
            recipient's normal inbox, accessible via direct query). Per-sender
            hourly limits (push_budget=30, hard_ceiling=100) also gate the
            push. See design:push-control-v0.
        require_human: Force human review on the recipient side. Always True for
            messages whose body matches the destructive-keyword regex (DELETE,
            DROP, TRUNCATE, deploy, production, git push --force).
        human_interacted: Sender-asserted: True when the sender is replying to
            an in-progress human prompt at this moment (e.g., a plugin marks
            this on outbound sends made while processing a Tom-typed prompt).
            When True, refreshes the sender's per-agent recency window. Trusted
            in non-adversarial environments; default False on automated replies.
        component: Optional sub-group under the project this message belongs to
            (design:unified-messaging-v0 Stage 1 — ADDRESSING). Free-form,
            human-chosen (e.g. "camera-sync"). Stage 1 carries it as first-class
            metadata only — addressing is still by to_instance; component-based
            pub/sub fan-out + claiming arrive in Stages 2-3. null (default) =
            today's behavior, routes by to_instance. nimbus's direct-send world
            is the component=null degenerate case (nimbus-compat invariant).
        subject: Optional sender-authored header line (<=80ch primary triage
            signal — server-authoritative delivery §E3 / contract:message-lanes-v0).
            Required-BY-GUIDELINE, not hard-rejected: a missing subject on an
            action message renders "(no subject)" on the recipient side. A reply
            (in_response_to set) with no explicit subject defaults to
            "Re: <parent subject>". Surfaces in get_messages and the announce packet.
    """
    error = require_session(session_id)
    if error:
        return error

    if priority not in MESSAGE_PRIORITIES:
        return json.dumps({"error": f"Invalid priority. Must be one of: {MESSAGE_PRIORITIES}"})

    if category not in MESSAGE_CATEGORIES:
        return json.dumps({"error": f"Invalid category. Must be one of: {MESSAGE_CATEGORIES}"})

    session_info = active_sessions[session_id]
    from_project = normalize_project(session_info.get("project", ""))
    target_project = to_project or from_project
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    # ── Validate target against project registry ──
    # Normalize project name (single source of truth)
    normalized_project = normalize_project(target_project)
    registered_project = db.projects.find_one({"name": normalized_project})

    if to_instance != "*":
        # Resolve the recipient: a live agent name passes through; a
        # nickname/alias (e.g. "coordinator" -> "emailTriage") is redirected
        # to the canonical live agent; an unknown name returns None.
        resolved = resolve_agent_name(db, normalized_project, to_instance)
        if resolved:
            to_instance = resolved
        else:
            # Unknown recipient. Fail loud whenever we have a roster to
            # validate against — a REGISTERED project, OR an unregistered
            # project that nonetheless has registered agents (e.g.
            # emailtriage, which was silently voiding misrouted sends before
            # the registered_project gate was relaxed). Only a project with
            # no roster at all falls through (back-compat: a project being
            # set up before any agent has registered).
            has_roster = bool(registered_project) or db.registered_agents.find_one(
                {"project": normalized_project}, {"_id": 1}
            )
            if has_roster:
                # Agent not registered - hard reject with suggestions
                suggestions = _fuzzy_match_agent(db, normalized_project, to_instance)
                error_msg = f"Agent '{to_instance}' is not registered in project '{normalized_project}'."
                if suggestions:
                    error_msg += f" Did you mean: {', '.join(suggestions)}?"
                else:
                    # List all valid agents
                    all_agents = [a["name"] for a in db.registered_agents.find(
                        {"project": normalized_project}, {"name": 1}
                    )]
                    if all_agents:
                        error_msg += f" Valid agents: {', '.join(all_agents)}"
                return json.dumps({"error": error_msg})
            # No roster yet — allow message through (backward compatibility).
            # This lets messaging work before projects are fully set up.

    # ── Dedup check ──
    # Reject identical messages to same target within 5 minutes
    dedup_window = now - timedelta(minutes=5)
    existing_msg = db.messages.find_one({
        "to_instance": to_instance,
        "to_project": normalized_project,
        "from_instance": session_info["claude_instance"],
        "message": message,
        "created_at": {"$gte": dedup_window}
    })
    if existing_msg:
        return json.dumps({
            "error": "Duplicate message detected. An identical message was sent to this target within the last 5 minutes.",
            "existing_message_id": existing_msg["_id"]
        })

    # ── Human-sender rule (design:human-sender-rule-v0.1) ──
    # When the caller's session is user-tier (Path B soft-auth: only valid
    # api_keys produce role="user"), force chain_depth=0 regardless of any
    # in_response_to parent or caller-supplied value. A user send is by
    # definition the start of a fresh chain even when threaded for UI
    # continuity. Hard-cap check below is naturally bypassed (0 < cap).
    sent_by_human = session_info.get("role") == "user"

    if sent_by_human:
        final_depth = 0
    else:
        # ── Phase C1: chain depth math ──
        # Final depth is max(parent.chain_depth + 1, caller-provided chain_depth, 0).
        parent_depth = -1  # so parent_depth + 1 == 0 when no parent
        if in_response_to:
            parent = db.messages.find_one({"_id": in_response_to}, {"chain_depth": 1})
            if parent and isinstance(parent.get("chain_depth"), int):
                parent_depth = parent["chain_depth"]
        caller_depth = chain_depth if isinstance(chain_depth, int) else 0
        final_depth = max(parent_depth + 1, caller_depth, 0)

    # ── Push control v0 evaluation (design:push-control-v0 v1.1.0) ──
    # Replaces the legacy hard cap (5) + Phase D2 recency-bypass
    # block. The new gate has three layers, all per-sender and all invisible
    # to the agent: push depth cap (per-thread, flat, no reset), push budget
    # (per-sender hourly, soft), hard ceiling (per-sender hourly, suspend).
    # Per §6 the depth cap does NOT consult human-presence — the cap is
    # deliberately unconditional. The recency window survives only for the
    # read-side push-suppression-filter release (§3, applied in read_inbox).
    #
    # Hard-ceiling alerting + agent suspension are wired in Phase 1d. For
    # now a hard_trip just sets push_suppressed and emits an audit event;
    # the agent is not yet suspended (incoming Phase 1d).
    pc_eval = push_control.evaluate_send(
        db=db,
        sender_instance=session_info["claude_instance"],
        sender_project=from_project,
        chain_depth=final_depth,
        recipient_instance=to_instance,
        recipient_project=normalized_project,
        recency_bypass=False,
    )
    suppress_push = pc_eval["suppress"]
    push_suppress_reason = pc_eval["reason"]
    emission_count = pc_eval["emission_count"]
    hard_trip = pc_eval["hard_trip"]
    # Legacy field — no longer computed at send time. Kept on the message
    # doc for backward compat with downstream consumers that read it.
    recency_bypass = False

    if suppress_push:
        try:
            log_audit(
                "push_control.hard_trip" if hard_trip else "push_control.soft_trip",
                actor=session_info["claude_instance"],
                project=from_project,
                details={
                    "reason": push_suppress_reason,
                    "to_instance": to_instance,
                    "to_project": normalized_project,
                    "chain_depth": final_depth,
                    "emission_count": emission_count,
                    "config_depth_cap": pc_eval["effective_config"].get("depth_cap"),
                    "config_push_budget": pc_eval["effective_config"].get("push_budget"),
                    "config_hard_ceiling": pc_eval["effective_config"].get("hard_ceiling"),
                },
                session_id=session_id,
            )
        except Exception:
            pass

    # ── Phase 1d/1e/1g: hard-ceiling escalation ──
    # First crossing of the hard ceiling for this sender in this hour:
    # compute the incident window, write an alert, insert system@junto
    # recovery notices into both endpoints' inboxes, suspend the agent,
    # and fire an out-of-band webhook to claudeControl. All inside a
    # try/except so a malfunction in the escalation path never breaks
    # the underlying send.
    if hard_trip:
        try:
            push_control.handle_hard_trip(
                db=db,
                sender_instance=session_info["claude_instance"],
                sender_project=from_project,
                emission_count=emission_count,
                trigger="hard_ceiling",
                trip_time=now,
                cfg=pc_eval["effective_config"],
            )
        except Exception as e:
            log.error("push_control: hard_trip orchestration failed: %s", e)

    # ── Limit-watch (design:limit-watch-v0) ──
    # Persist the sender's hourly peak + fire once-per-hour budget_warn /
    # push_budget_breach alerts. `suppressed` counts only budget-driven
    # suppression — depth_cap suppression is conversation-shape, not volume,
    # and would pollute the tuning data.
    push_control.record_emission_history(
        db,
        session_info["claude_instance"],
        from_project,
        emission_count,
        bool(suppress_push and push_suppress_reason in ("push_budget", "hard_ceiling")),
        pc_eval["effective_config"],
    )

    # ── Phase C1.1: destructive content gate, chain-depth-gated ──
    # Auto-flag only when this is a relayed/automated message (chain_depth>0).
    # Depth-0 sends are deliberate (human-tier or new agent chain) — the caller
    # is presumed to know what they're doing and can pass require_human=True
    # explicitly. The gate is here to break runaway auto-reply loops, not to
    # police prose. backlog_6bcf2d646772.
    body_is_destructive = (
        final_depth > 0 and bool(_DESTRUCTIVE_KEYWORDS.search(message))
    )
    final_require_human = bool(require_human) or body_is_destructive

    # ── SUBJECT (server-authoritative delivery §E3 / build-plan task 1) ──
    # Sender-authored primary header signal. Required-by-guideline, NOT
    # hard-rejected. A reply with no explicit subject defaults to
    # "Re: <parent subject>" (one extra lookup only on a reply-without-subject;
    # the chain_depth branch above doesn't always fetch the parent).
    final_subject = subject.strip() if isinstance(subject, str) and subject.strip() else None
    if final_subject is None and in_response_to:
        _parent_subj = db.messages.find_one({"_id": in_response_to}, {"subject": 1})
        _ps = (_parent_subj or {}).get("subject")
        if _ps:
            final_subject = _ps if _ps.startswith("Re: ") else f"Re: {_ps}"

    # ── Build and store message ──
    message_id = f"msg_{uuid.uuid4().hex[:12]}"

    msg_doc = {
        "_id": message_id,
        "to_instance": to_instance,
        "to_project": normalized_project,
        "from_instance": session_info["claude_instance"],
        "from_project": from_project,
        "from_session": session_id,
        "message": message,
        "priority": priority,
        "category": category,
        "subject": final_subject,
        # Per-message read state (build-plan task 2): the recipients who have
        # READ this message (body-pull or ack). Empty = unread by everyone; the
        # source of truth for the unread filter, replacing the seen-watermark.
        "read_by": [],
        "reply_to": reply_to,
        "in_response_to": in_response_to,
        "chain_depth": final_depth,
        "require_human": final_require_human,
        "sent_by_human": sent_by_human,
        "human_interacted": bool(human_interacted),
        # Legacy field kept during transition. The instance-prefix variant is
        # forgeable; new code should read sent_by_human instead.
        "user_originated": sent_by_human or session_info.get("claude_instance", "").startswith("user-"),
        "status": "pending",
        # Obligation track (design:unified-messaging-v0 Stage 3 / lanes-B). ACTION
        # categories start "open"; info carries none. A reply from the owner
        # advances it via _advance_parent_obligation_on_reply.
        # BROADCASTS NEVER MINT OBLIGATIONS (backlog_ddfa1ae20a76, 2026-08-06):
        # an announcement has no addressee who owes a reply — obligations
        # require a named recipient. Before this, one action-category broadcast
        # doc rendered as a permanently-open obligation in EVERY project
        # member's action lane (23 measured phantoms). Enforcement half of
        # send-rule #4, which documented exactly this hazard as guideline.
        "obligation": (
            "open" if (category in ACTION_CATEGORIES and to_instance != "*") else None
        ),
        # Component (design:unified-messaging-v0 Stage 1 / ADDRESSING). Optional
        # sub-group under the project. Stage 1 = first-class metadata only;
        # component-routing/claiming land in Stages 2-3. null = route by
        # to_instance (today's behavior; nimbus's degenerate case).
        "component": component or None,
        # Ownership (design:unified-messaging-v0 Stage 2 / CLAIMING). owner stays
        # null until a GROUP message (broadcast to_instance="*" or component-
        # addressed) is claimed via memory_claim_message (atomic CAS on
        # owner:null). DIRECT sends leave owner null too — the obligation guard
        # reads `owner ?? to_instance`, so a direct message's implicit owner is
        # its recipient (nimbus-compat invariant). claimed_at stamps the win.
        "owner": None,
        "claimed_at": None,
        # Differential TTL (design:unified-messaging-v0 Stage 5 / lanes-C). info
        # ages in 48h; an ACTION message starts with NO expiry (unacked actions
        # must never silently vanish) and gets expire_at=created+7d only when it
        # reaches a terminal state (ack/resolve) via _set_action_message_expiry.
        # Broadcasts always get the info TTL regardless of category: with no
        # obligation to clear them (rule above), a no-expiry action-category
        # broadcast would be immortal-but-invisible. Announcements age out.
        "expire_at": (
            now + timedelta(hours=MESSAGE_INFO_TTL_HOURS)
            if (to_instance == "*" or category not in ACTION_CATEGORIES)
            else None
        ),
        "push_suppressed": suppress_push,
        "push_suppress_reason": push_suppress_reason,
        "emission_count": emission_count,
        "recency_bypass": recency_bypass,
        "created_at": now,
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }

    # Phase 1 #2 canary 13/13: §4.3.b transactional emission. The message
    # insert and its op_log entry land atomically. If commit fails, neither
    # write is observable to peers or readers — no half-state to reconcile.
    # First user of with_op_log(); pattern reusable for other Mongo-backed
    # mutations (agent heartbeats, locks).
    with with_op_log(db) as (session, append):
        db.messages.insert_one(msg_doc, session=session)
        append(
            op_type="message.sent",
            actor={
                "agent": session_info["claude_instance"],
                "project": from_project,
                "session_id": session_id,
            },
            ref={"collection": "messages", "doc_id": message_id},
            payload=msg_doc,
            intent_id=get_current_intent_id(),
        )

    # ── lanes-B: reply auto-acks the parent's obligation ──
    # A reply (in_response_to set) from the parent's owner advances the parent
    # off the action lane (resolved) or marks it engaged (responded). Owner
    # defaults to the named recipient (forward-compatible with Stage-2 claiming).
    # Best-effort — never blocks the send. A non-None return means this reply
    # advanced an open/responded action obligation → PROMOTE it to push even if
    # its own category is badge-only (contract:reply-promotion-v0).
    advanced_obligation = None
    if in_response_to:
        advanced_obligation = _advance_parent_obligation_on_reply(
            db, in_response_to, session_info["claude_instance"], now
        )

    # ── Phase D2: outbound recency bump ──
    # If the sender asserts human_interacted=True (and is not itself user-tier
    # — sent_by_human already implies a different identity), refresh the
    # sender's per-agent recency timestamp. This lets a confused-but-Tom-
    # supervised agent keep a chain alive past the cap.
    if human_interacted and not sent_by_human:
        _bump_human_interaction(db, from_project, session_info["claude_instance"], now)

    # ── Phase C2: notify inbox subscribers ──
    # Skip the push when suppressed by the chain-depth gate so a runaway loop
    # doesn't get a free auto-delivery channel. Pull-side (memory_get_messages
    # / read_inbox) still surfaces the message — coordinator alert covers
    # visibility.
    if not suppress_push:
        # Server-authoritative delivery §E: build the announce packet (None for
        # badge-only/info) and content-push it alongside the resource-updated.
        # contract:reply-promotion-v0: an obligation-closing reply is promoted to
        # push even if its own category is badge-only, so the requester isn't blind.
        announce_packet = _build_announce_packet(msg_doc, promoted=bool(advanced_obligation))
        await _notify_inbox_for_send(msg_doc["to_project"], to_instance, announce_packet)

    # Subscriber count is read from the in-process subscription map, which
    # _notify_inbox_for_send may have just pruned of dead sessions. Reading
    # AFTER notify gives the most accurate "live" count.
    live_subscribers = _live_subscribers_count(msg_doc["to_project"], to_instance)

    # effective_chain_depth: legacy field. Push-control v0 removed the
    # recency-bypass behavior on the depth cap, so this is now identical to
    # final_depth. Kept in the response shape for backward compat.
    effective_chain_depth = final_depth

    # Dangling-ref advisory (backlog_d03297e01f30): existence-check ID-shaped
    # refs in the outgoing body, warn in THIS response — a warning that lands
    # before the sender's next action is a gate; anything later is a feed.
    # Best-effort, additive field, never blocks the send.
    _ref_advisory = {}
    try:
        from shared_memory.clients import get_chroma as _get_chroma
        from shared_memory.write_lint import advisory_payload, find_unresolved_refs
        _unresolved = await find_unresolved_refs(
            message, db, await _get_chroma(), from_project
        )
        if _unresolved:
            _ref_advisory = advisory_payload(_unresolved)
    except Exception:
        pass

    response = {
        "status": "queued",
        "message_id": message_id,
        "to": to_instance,
        "to_project": msg_doc["to_project"],
        "from_project": from_project,
        "priority": priority,
        "category": category,
        "obligation": msg_doc["obligation"],
        "component": msg_doc["component"],
        "reply_to": reply_to,
        "in_response_to": in_response_to,
        "chain_depth": final_depth,
        "effective_chain_depth": effective_chain_depth,
        "sent_by_human": sent_by_human,
        "human_interacted": bool(human_interacted),
        "require_human": final_require_human,
        "destructive_match": body_is_destructive,
        "persisted": True,
        "push_suppressed": suppress_push,
        "push_suppress_reason": push_suppress_reason,
        "emission_count": emission_count,
        "recency_bypass": recency_bypass,
        "live_subscribers": 0 if suppress_push else live_subscribers,
    }
    if _ref_advisory:
        response.update(_ref_advisory)

    # Read-side idle-queue visibility (backlog_da56a6e0c46b). When the recipient
    # has NO live stream to receive this push, tell the sender what's already
    # waiting + how long they've been idle, so the sender can decide to escalate
    # (manually wake the agent) rather than sit blind on an unanswered ask. Only
    # on direct sends with a genuinely absent subscriber — a live recipient is
    # getting the push, no escalation question to answer.
    if live_subscribers == 0 and to_instance != "*":
        idle = _recipient_idle_snapshot(db, msg_doc["to_project"], to_instance)
        if idle is not None:
            response["recipient_idle"] = idle

    return json.dumps(response)


@mcp.tool()
async def memory_get_messages(
    session_id: str,
    include_delivered: bool = False,
    limit: int = 20,
    message_id: str = None,
    for_instance: str = None,
    cursor: str = None,
    updated_within_days: int = None,
    include_seen: bool = False,
    headers_only: bool = False,
    created_after: str = None,
    created_before: str = None,
    ctx: Context = None
) -> str:
    """
    Get pending notes for your agent.

    Returns notes addressed to you by other agents. Notes are scoped by project
    — you only see notes sent to your project.

    Args:
        session_id: Your session ID
        include_delivered: Include already delivered notes (default False)
        limit: Maximum notes to return (default 20)
        message_id: Fetch a specific note by ID. Admin/user-tier roles and
            project admins can fetch any message; other agents can only fetch
            messages addressed to themselves.
        for_instance: View notes for a different agent in your project. Admin/
            user-tier roles and project admins only. Passing for_instance equal
            to your own agent name is always allowed (self-read).
        cursor: Pagination cursor (created_at ISO string from a previous call's
            next_cursor). Returns the next page of OLDER messages (created_at < cursor).
        updated_within_days: Only return messages whose `created_at` is within
            the last N days. Omit (default None) to disable the recency filter.
            Server-side filter — cheaper than load-then-filter at the caller.
            Note: messages have a DIFFERENTIAL TTL (design:unified-messaging-v0
            Stage 5, see clients.py / config.py): info ages out in 48h, an
            unacked action never ages, and an acked/resolved action lasts 7d from
            creation. So for info, values > 2 effectively behave as 2.
        include_seen: Default False — a top read (no cursor) by the owning agent
            returns only messages newer than the agent's read-watermark
            (messages_seen_through), and advances that watermark when it has
            handed over the complete unseen set (has_more=False). This stops
            every `go` from redisplaying the full 7-day window. Set True for a
            full-window catch-up (skips the watermark filter and does not
            advance it). The watermark is per-recipient and ONLY consulted on
            the self, non-paginated path; cursor pagination and for_instance
            peeks always bypass it. The inbox:// resource (push delivery,
            control UI) is a separate path and is NOT affected by this filter.
            See design:message-read-watermark-v0.
        headers_only: Default False. True returns metadata WITHOUT the message
            body — a triage/reconcile scan that is READ-INERT (does NOT mark the
            returned messages read), so an agent can scan unread headers and then
            choose which bodies to pull. The go/park reconcile path
            (design:server-authoritative-delivery-v0 §E). Pull a body (by
            message_id or a normal non-headers read) or ack to mark read.
        created_after: ISO timestamp — only messages with created_at strictly
            AFTER this. Explicit lower bound (vs updated_within_days' rolling
            approximation). Build-plan task 3.
        created_before: ISO timestamp — only messages with created_at strictly
            BEFORE this. Explicit upper bound. Build-plan task 3.
    """
    error = require_session(session_id)
    if error:
        return error

    if updated_within_days is not None and updated_within_days < 1:
        return json.dumps({"error": "updated_within_days must be >= 1"})

    session_info = active_sessions[session_id]
    my_instance = session_info["claude_instance"]
    my_project = normalize_project(session_info.get("project", ""))
    my_role = session_info.get("role", "agent")

    db = get_mongo()
    if db is None:
        return json.dumps({"count": 0, "messages": [], "error": "MongoDB unavailable"})

    # Admin-equivalent for inbox reads: cross-project roles 'admin'/'user' OR
    # named project-admin. Mirrors _check_inbox_authz so the subscribe-side
    # and read-side authorization are consistent (a user-tier session that
    # can subscribe to inbox://X/Y can also read it via memory_get_messages).
    is_admin = (my_role in ("admin", "user")) or _is_project_admin(db, my_project, my_instance)

    # ── Direct message lookup by ID (admin/coordinator only) ──
    if message_id:
        doc = db.messages.find_one({"_id": message_id})
        if not doc:
            return json.dumps({"error": f"Message '{message_id}' not found"})
        # Non-admins can only see their own messages
        if not is_admin:
            msg_to = doc.get("to_instance", doc.get("to", ""))
            msg_project = doc.get("to_project", "")
            if msg_to != my_instance and msg_to != "*":
                return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
            if msg_project and msg_project != my_project:
                return json.dumps({"error": "Permission denied. Message belongs to a different project."})
        # Body-pull marks read (build-plan task 2): fetching your OWN message's
        # body by id is a canonical read. Skip when an admin peeks at a message
        # addressed to someone else (don't mark read on their behalf).
        if doc.get("to_instance", doc.get("to")) in (my_instance, "*"):
            _mark_messages_read(db, [doc["_id"]], my_instance)
        return json.dumps({"count": 1, "messages": [_message_entry(doc)]})

    # ── Querying for another agent's messages ──
    # Self-read (for_instance == my_instance) is always allowed.
    # Otherwise requires admin-equivalent role (see is_admin computed above).
    target_instance = my_instance
    if for_instance:
        if for_instance != my_instance and not is_admin:
            return json.dumps({"error": "Permission denied. Only admins/coordinators can view other agents' messages."})
        target_instance = for_instance

    # Build query - match by instance AND project
    # Broadcasts (*) only go to named/admin agents, not workers
    is_worker = target_instance.startswith("worker_")
    if is_worker:
        instance_match = {"to_instance": target_instance}  # Workers only get direct messages
    else:
        instance_match = {
            "$or": [
                {"to_instance": target_instance},
                {"to_instance": "*"}
            ]
        }
    project_match = {
        "$or": [
            {"to_project": my_project},
            {"to_project": {"$exists": False}},  # legacy messages without project
            {"to_project": ""},  # empty project = broadcast
        ]
    }
    query = {"$and": [instance_match, project_match]}

    if not include_delivered:
        query["$and"].append({"status": "pending"})

    # Cursor pagination: cursor = created_at ISO string from previous call's
    # next_cursor. Fetch the page of messages OLDER than that cursor.
    if cursor:
        cursor_dt = parse_timestamp(cursor)
        if cursor_dt is not None:
            query["$and"].append({"created_at": {"$lt": cursor_dt}})

    # Recency filter (server-side; saves load-then-filter cycles at callers)
    if updated_within_days is not None:
        recency_cutoff = utc_now() - timedelta(days=int(updated_within_days))
        query["$and"].append({"created_at": {"$gte": recency_cutoff}})

    # Explicit created_at bounds (build-plan task 3). Distinct from cursor (which
    # is a created_at < pagination key) and from updated_within_days (rolling).
    # Silently ignore an unparseable bound rather than reject the whole read.
    if created_after is not None:
        _after_dt = parse_timestamp(created_after)
        if _after_dt is not None:
            query["$and"].append({"created_at": {"$gt": _after_dt}})
    if created_before is not None:
        _before_dt = parse_timestamp(created_before)
        if _before_dt is not None:
            query["$and"].append({"created_at": {"$lt": _before_dt}})

    # ── Unread filter (build-plan task 2: per-message read state) ──
    # A top read (no cursor) by the OWNING agent defaults to UNREAD-only. Read
    # state is now PER-MESSAGE — read_by excludes the agent — replacing the
    # single auto-advancing messages_seen_through watermark (which conflated
    # "glanced at a new msg" with "older msgs read"). The watermark is DEMOTED to
    # a coarse FLOOR: messages at/before it are treated as already-read, so an
    # agent carrying a pre-task-2 watermark isn't re-flooded post-deploy. Cursor
    # pagination and for_instance peeks bypass (I2/I3); include_seen is the
    # full-window catch-up hatch (design:message-read-watermark-v0 invariants).
    is_owner_read = (
        not include_seen and cursor is None and target_instance == my_instance
    )
    if is_owner_read:
        query["$and"].append({"read_by": {"$ne": my_instance}})
        watermark = _get_messages_seen_through(db, my_project, my_instance)
        if watermark is not None:
            query["$and"].append({"created_at": {"$gt": watermark}})

    # Fetch limit+1 to detect "has more" without a separate count query.
    # Sort recency-primary (created_at DESC). Priority is NOT a DB sort key:
    # it is a STRING (low/normal/urgent), so ("priority",1) sorted it
    # alphabetically (urgent LAST) and, applied before limit, stranded urgent
    # behind a large backlog (design:inbox-surfacing-v0). created_at-primary
    # also makes cursor pagination consistent (the cursor filters created_at <
    # x). Within-page priority ordering is still applied below in Python.
    page_size = max(1, int(limit))
    db_cursor = db.messages.find(query).sort([
        ("created_at", -1)
    ]).limit(page_size + 1)

    raw_docs = list(db_cursor)
    has_more = len(raw_docs) > page_size
    raw_docs = raw_docs[:page_size]

    next_cursor = None
    if has_more and raw_docs:
        last_created = raw_docs[-1].get("created_at")
        if hasattr(last_created, "isoformat"):
            next_cursor = last_created.isoformat()
        elif last_created is not None:
            next_cursor = str(last_created)

    messages = []
    saw_human_message = False
    for doc in raw_docs:
        created_at = doc.get("created_at")
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        is_sent_by_human = bool(doc.get("sent_by_human", False))
        if is_sent_by_human:
            saw_human_message = True
        entry = {
            "id": doc["_id"],
            "from": doc.get("from_instance", doc.get("from", "?")),
            "from_project": doc.get("from_project", ""),
            "category": doc.get("category", "info"),
            "subject": doc.get("subject"),
            # headers_only (task 3) is a read-inert triage scan: omit the body so
            # the recipient triages from headers and pulls bodies it wants.
            "message": None if headers_only else doc.get("message", ""),
            "priority": doc.get("priority", "normal"),
            "status": doc.get("status", "pending"),
            "obligation": doc.get("obligation"),
            "component": doc.get("component"),
            "owner": doc.get("owner"),
            "created": created_at,
            "delivered": doc.get("status", "pending") != "pending",
            "chain_depth": doc.get("chain_depth", 0),
            "require_human": bool(doc.get("require_human", False)),
            "user_originated": bool(doc.get("user_originated", False)),
            "sent_by_human": is_sent_by_human,
            "human_interacted": bool(doc.get("human_interacted", False)),
            "push_suppressed": bool(doc.get("push_suppressed", False)),
            "push_suppress_reason": doc.get("push_suppress_reason"),
            "recency_bypass": bool(doc.get("recency_bypass", False)),
            "is_system_notice": bool(doc.get("is_system_notice", False)),
            "system_notice_kind": doc.get("system_notice_kind"),
        }
        if doc.get("reply_to"):
            entry["reply_to"] = doc["reply_to"]
        if doc.get("in_response_to"):
            entry["in_response_to"] = doc["in_response_to"]
        _add_lane_fields(entry)
        messages.append(entry)

    # ── Phase D2: inbound recency bump ──
    # Update the recipient's per-agent recency timestamp the moment the
    # server hands them a sent_by_human=True message — that's our closest
    # observable to "client received." Skip when an admin/coordinator is
    # peeking at someone else's inbox via for_instance.
    if saw_human_message and target_instance == my_instance:
        _bump_human_interaction(db, my_project, my_instance)

    # ── Mark returned messages read (build-plan task 2: per-message read) ──
    # Replaces the old watermark auto-advance. A body-returning owner read marks
    # ONLY the messages handed over (per-message), so a truncated page never marks
    # the unreturned tail read — the old has_more guard is no longer needed.
    # INERT for resource reads (read_inbox / I1), cursor pages (I2) and
    # include_seen catch-ups (I3): is_owner_read already excludes those. The
    # headers-only scan (build-plan task 3) will opt OUT of this marking so a pure
    # triage glance stays inert.
    if is_owner_read and not headers_only and raw_docs:
        _mark_messages_read(db, [d["_id"] for d in raw_docs], my_instance)

    # Lanes-A within-page display order (interface:lanes-a-server-wire-v0): tier
    # over recency over priority. This is the POST-FETCH re-sort, NOT the DB
    # selection key — selection stays created_at-primary above (design:inbox-
    # surfacing-v0 Fix A), so nothing strands past limit() and the created_at
    # cursor stays coherent. inbox's co-sign blocking condition (msg_83e884ecfac7).
    _sort_messages_by_lane(messages)

    # Lane badge counts over the FULL inbox (not page-limited — the plugin only
    # sees page-1 and can't count the tail). Action counts ignore the watermark
    # (a seen-but-unanswered ask still owes a reply); fyi counts unseen-only.
    lane_counts = _compute_lane_counts(
        db,
        [instance_match, project_match],
        watermark=_get_messages_seen_through(db, my_project, target_instance),
        reader=target_instance,
    )

    return json.dumps({
        "count": len(messages),
        "messages": messages,
        "lane_counts": lane_counts,
        "next_cursor": next_cursor,
        "has_more": has_more,
    })


@mcp.tool()
async def memory_update_message_status(
    session_id: str,
    message_id: str,
    status: str,
    ctx: Context = None
) -> str:
    """
    Update a message's lifecycle status.

    Call this to track message progress through the system.

    Args:
        session_id: Your session ID
        message_id: The message ID to update
        status: New DELIVERY status (delivered, received, completed, failed) OR an
            OBLIGATION verb (responded, resolved). The two are separate axes:
            delivery statuses write the `status` field; "responded"/"resolved"
            write the `obligation` field (lanes-B / design:unified-messaging-v0).
            "resolved" is the explicit low-friction done verb for a task/blocker
            that a reply only marked "responded".
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    now = utc_now()

    # ── Obligation track (lanes-B) — separate axis from delivery status ──
    # "resolved"/"responded" are not delivery statuses; route them to the
    # `obligation` field. "resolved" is the explicit done verb (clears a
    # task/blocker that a reply left "responded").
    if status in ("responded", "resolved"):
        obl_update = {"obligation": status, "responded_at": now}
        if status == "resolved":
            obl_update["resolved_at"] = now
        result = db.messages.update_one({"_id": message_id}, {"$set": obl_update})
        if result.matched_count == 0:
            return json.dumps({"error": f"Message not found: {message_id}"})
        # Stage-5 TTL: "resolved" is terminal → start the 7d expiry.
        if status == "resolved":
            _set_action_message_expiry(db, message_id)
        return json.dumps({
            "obligation": status,
            "message_id": message_id,
            "updated": True,
        })

    if status not in MESSAGE_STATUSES:
        return json.dumps({"error": f"Invalid status. Must be one of: {MESSAGE_STATUSES} (or obligation verbs: responded, resolved)"})

    update = {"status": status}

    # Set appropriate timestamp
    if status == "delivered":
        update["delivered_at"] = now
    elif status == "received":
        update["received_at"] = now
    elif status in ["completed", "failed"]:
        update["completed_at"] = now

    result = db.messages.update_one(
        {"_id": message_id},
        {"$set": update}
    )

    if result.matched_count == 0:
        return json.dumps({"error": f"Message not found: {message_id}"})

    # Per-message read state (build-plan task 2): an ack (received) is an explicit
    # "I've seen and handled this" → mark it read by the acking agent, the other
    # half of body-pull-marks-read. Keeps the unread filter / badge consistent
    # whether the agent pulled the body or just acked.
    if status == "received":
        caller_instance = active_sessions.get(session_id, {}).get("claude_instance")
        if caller_instance:
            _mark_messages_read(db, [message_id], caller_instance)

    # Stage-5 TTL: ack/completion is terminal for an action message → start its
    # 7d expiry. "delivered" is NOT terminal (delivered ≠ acted-on).
    if status in ("received", "completed", "failed"):
        _set_action_message_expiry(db, message_id)

    return json.dumps({
        "status": status,
        "message_id": message_id,
        "updated": True
    })


@mcp.tool()
async def memory_claim_message(
    session_id: str,
    message_id: str,
    ctx: Context = None
) -> str:
    """
    Claim ownership of a GROUP-addressed message (atomic, first-wins).

    design:unified-messaging-v0 Stage 2 / CLAIMING. When a message is addressed
    to a GROUP — a broadcast (to_instance="*") or a component — every subscriber
    sees the same single doc. Calling this claims it for YOU via an atomic
    compare-and-swap (find_one_and_update on owner:null): exactly one caller
    wins, the rest learn they lost. The winner becomes the message's `owner`, so
    the winner's reply auto-acks the obligation — the same `owner` the lanes-B
    guard reads (claiming + auto-ack are one mechanism, by design).

    DIRECT messages (addressed to a concrete agent) are NOT claimable — they
    already have an implicit owner (the recipient). Claiming one is rejected.

    Loser dedup is READ-SIDE in Stage 2: `owner` is surfaced on every message
    read, so a non-winner sees owner=<someone-else> and skips it. The active
    push-notify to other subscribers rides on Stage-3 component fan-out.

    Args:
        session_id: Your session ID.
        message_id: The group message to claim.

    Returns JSON {claimed, owner, message_id[, claimed_at][, note]}:
      claimed=True  → you won; owner is you.
      claimed=False → already held; owner is the agent who holds it.
    """
    error = require_session(session_id)
    if error:
        return error

    session_info = active_sessions[session_id]
    me = session_info["claude_instance"]
    my_project = normalize_project(session_info.get("project", ""))

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    doc = db.messages.find_one(
        {"_id": message_id},
        {"to_instance": 1, "to_project": 1, "component": 1, "owner": 1},
    )
    if not doc:
        return json.dumps({"error": f"Message not found: {message_id}"})

    # ── Project visibility: you can only claim within your own project ──
    msg_project = doc.get("to_project", "")
    if msg_project and msg_project != my_project:
        return json.dumps({"error": "Permission denied. Message belongs to a different project."})

    # ── Group-addressing gate ──
    # Only a GROUP message is claimable. Today the sole group form is a broadcast
    # (to_instance="*"); a component-scoped group send is "*" + a component tag.
    # A message with a CONCRETE recipient is direct — it has an implicit owner
    # (that recipient) and must not be hijacked, EVEN IF it carries a component
    # tag (the tag is metadata, not an address). When Stage 3 adds true
    # component-addressed sends (no concrete to_instance), extend this gate; the
    # owner:null CAS below already handles them unchanged.
    to_instance = doc.get("to_instance", "")
    is_group = to_instance == "*"
    if not is_group:
        return json.dumps({
            "error": (
                f"Message {message_id} is direct-addressed (to='{to_instance}') and "
                f"cannot be claimed — it already has an implicit owner. Claiming "
                f"applies only to group messages (broadcast to='*')."
            )
        })

    # ── Atomic compare-and-swap: first-wins on owner:null ──
    # find_one_and_update is the single round-trip CAS — the {_id, owner:None}
    # filter is the precondition; only one concurrent caller matches it. Returns
    # the post-update doc iff WE won, None if another caller already set owner.
    now = utc_now()
    won = db.messages.find_one_and_update(
        {"_id": message_id, "owner": None},
        {"$set": {"owner": me, "claimed_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if won is not None:
        try:
            log_audit("message.claimed", me, my_project,
                      {"message_id": message_id, "component": doc.get("component"),
                       "to_instance": to_instance})
        except Exception:
            pass
        return json.dumps({
            "claimed": True,
            "owner": me,
            "message_id": message_id,
            "claimed_at": now.isoformat() if hasattr(now, "isoformat") else now,
        })

    # Lost the race (or it was claimed earlier). Re-read to report who holds it.
    current = db.messages.find_one({"_id": message_id}, {"owner": 1})
    current_owner = current.get("owner") if current else None
    return json.dumps({
        "claimed": False,
        "owner": current_owner,
        "message_id": message_id,
        "note": ("You already own this." if current_owner == me
                 else f"Already claimed by {current_owner}."),
    })


@mcp.tool()
async def memory_acknowledge_message(
    session_id: str,
    message_id: str,
    ctx: Context = None
) -> str:
    """
    Acknowledge receipt of a message (shortcut for status=received).

    Call this after processing a message to mark it handled.

    Args:
        session_id: Your session ID
        message_id: The message ID to acknowledge
    """
    return await memory_update_message_status(session_id, message_id, "received", ctx)


@mcp.tool()
async def memory_heartbeat(
    session_id: str,
    status: str = "idle",
    current_task: str = None,
    ctx: Context = None
) -> str:
    """
    Send a heartbeat to update your agent status.

    Call this periodically to let the system know you're alive.
    Enables load balancing, stuck detection, and routing decisions.

    Args:
        session_id: Your session ID
        status: Current status (idle, busy, error)
        current_task: Description of current task (if busy)
    """
    error = require_session(session_id)
    if error:
        return error

    if status not in ["idle", "busy", "error"]:
        return json.dumps({"error": "Status must be: idle, busy, error"})

    session_info = active_sessions[session_id]
    instance = session_info["claude_instance"]
    now = utc_now()

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    db.agent_status.update_one(
        {"instance": instance},
        {
            "$set": {
                "instance": instance,
                "session_id": session_id,
                "status": status,
                "current_task": current_task,
                "last_heartbeat": now,
                "tmux_target": session_info.get("tmux_target")
            }
        },
        upsert=True
    )

    return json.dumps({
        "status": "ok",
        "instance": instance,
        "agent_status": status,
        "timestamp": now.isoformat()
    })


@mcp.tool()
async def memory_get_agent_status(
    session_id: str,
    instance: str = None,
    ctx: Context = None
) -> str:
    """
    Get status of Claude agents.

    Args:
        session_id: Your session ID
        instance: Specific instance to check (omit for all agents)
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    query = {"instance": instance} if instance else {}
    cursor = db.agent_status.find(query)

    agents = []
    now = utc_now()
    for doc in cursor:
        last_hb = parse_timestamp(doc.get("last_heartbeat"))
        stale = False
        if last_hb:
            age_seconds = (now - last_hb).total_seconds()
            stale = age_seconds > 300  # 5 minutes = stale

        agents.append({
            "instance": doc["instance"],
            "status": doc.get("status", "unknown"),
            "current_task": doc.get("current_task"),
            "tmux_target": doc.get("tmux_target"),
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
            "stale": stale
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    })


@mcp.tool()
async def memory_list_agents(
    session_id: str,
    project: str = None,
    query: str = None,
    ctx: Context = None
) -> str:
    """
    Discover registered Claude agents across all projects.

    Use this to find out who exists, what they do, and how to reach them.
    Agents auto-register when they call memory_start_session.

    Args:
        session_id: Your session ID
        project: Filter by project (omit for all projects)
        query: Search term to filter by name or role description
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    mongo_query = {}
    if project:
        mongo_query["project"] = project

    cursor = db.agent_directory.find(mongo_query).sort("last_seen", -1)

    # Roster join (backlog_77a8e2f540ce): lifecycle fields live on
    # registered_agents, not the directory — without them a roster review
    # can't see tier/spawned_by, and directory-only rows are indistinguishable
    # from registered ones (the 62-vs-23 confusion: two different collections).
    roster = {}
    _roster_q = {"project": project} if project else {}
    for _r in db.registered_agents.find(
        _roster_q, {"project": 1, "name": 1, "tier": 1, "spawned_by": 1,
                    "sunset": 1, "purpose": 1, "aliases": 1,
                    # authoritative role_description — see the roster-wins
                    # note at the row build below
                    "role_description": 1}
    ):
        roster[(_r.get("project"), _r.get("name"))] = _r

    agents = []
    now = utc_now()
    for doc in cursor:
        _reg_pre = roster.get((doc.get("project"), doc.get("instance")))
        # Search the AUTHORITATIVE role text, not the directory cache —
        # otherwise a query matches (or misses) on words the caller can no
        # longer see in the returned row.
        _role_for_match = (
            (_reg_pre or {}).get("role_description")
            or doc.get("role_description", "")
        )
        # If query provided, filter by instance name or role_description
        if query:
            q_lower = query.lower()
            instance_match = q_lower in doc.get("instance", "").lower()
            role_match = q_lower in _role_for_match.lower()
            project_match = q_lower in doc.get("project", "").lower()
            if not (instance_match or role_match or project_match):
                continue

        last_seen = parse_timestamp(doc.get("last_seen"))
        days_ago = None
        if last_seen:
            days_ago = round((now - last_seen).total_seconds() / 86400, 1)

        _reg = _reg_pre
        # ROSTER WINS on role_description (coordinator msg_af23da260f8e):
        # registered_agents is authoritative, agent_directory is the cache.
        # Reading roster-first heals pre-existing divergence with no backfill
        # — the write-through in memory_project keeps them aligned going
        # forward, this makes the DISCOVERY surface correct even for rows
        # that diverged before the write-through shipped.
        _role = _role_for_match
        # Surface the divergence rather than hiding it: a silently-corrected
        # read is how the two stores drifted unnoticed for weeks.
        _dir_role = doc.get("role_description", "")
        _stale_cache = bool(_reg and _dir_role and _dir_role != _role)
        agents.append({
            "project": doc.get("project"),
            "instance": doc.get("instance"),
            "role_description": _role,
            "last_seen": last_seen.isoformat() if last_seen else None,
            "days_ago": days_ago,
            "session_count": doc.get("session_count", 0),
            "last_task": doc.get("last_task", ""),
            # Lifecycle join (additive fields, backlog_77a8e2f540ce).
            # registered=False → directory-only row: never formally rostered,
            # invisible to standup, candidate for roster-hygiene review.
            "registered": _reg is not None,
            "tier": (_reg or {}).get("tier"),
            "spawned_by": (_reg or {}).get("spawned_by"),
            "sunset": (_reg or {}).get("sunset"),
            "purpose": (_reg or {}).get("purpose"),
            **({"directory_cache_stale": True} if _stale_cache else {}),
        })

    # Roster-only rows (registered_agents docs with no directory row — e.g.
    # seed-registered agents that never opened a session): append so ONE call
    # covers the full roster-review surface (backlog_77a8e2f540ce).
    _seen = {(a["project"], a["instance"]) for a in agents}
    for (_proj, _name), _r in roster.items():
        if (_proj, _name) in _seen:
            continue
        if query:
            q_lower = query.lower()
            if not (q_lower in (_name or "").lower()
                    or q_lower in (_r.get("role_description") or "").lower()
                    or q_lower in (_proj or "").lower()):
                continue
        agents.append({
            "project": _proj,
            "instance": _name,
            "role_description": _r.get("role_description", ""),
            "last_seen": None,
            "days_ago": None,
            "session_count": 0,
            "last_task": "",
            "registered": True,
            "roster_only": True,
            "tier": _r.get("tier"),
            "spawned_by": _r.get("spawned_by"),
            "sunset": _r.get("sunset"),
            "purpose": _r.get("purpose"),
        })

    return json.dumps({
        "count": len(agents),
        "agents": agents
    }, indent=2)


# ──────────────────────────────────────────────────────────────────────────
# Phase C2: inbox resource + subscriptions
# ──────────────────────────────────────────────────────────────────────────

# Default page size when reading an inbox resource. Same as memory_get_messages.
INBOX_DEFAULT_LIMIT = 20


def _format_inbox_message(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one Mongo message doc into the same payload memory_get_messages emits."""
    created_at = doc.get("created_at")
    if hasattr(created_at, "isoformat"):
        created_at = created_at.isoformat()
    entry = {
        "id": doc["_id"],
        "from": doc.get("from_instance", doc.get("from", "?")),
        "from_project": doc.get("from_project", ""),
        "to": doc.get("to_instance", doc.get("to", "?")),
        "to_project": doc.get("to_project", ""),
        "category": doc.get("category", "info"),
        "subject": doc.get("subject"),
        "message": doc.get("message", ""),
        "priority": doc.get("priority", "normal"),
        "status": doc.get("status", "pending"),
        "obligation": doc.get("obligation"),
        "component": doc.get("component"),
        "owner": doc.get("owner"),
        "created": created_at,
        "delivered": doc.get("status", "pending") != "pending",
        "chain_depth": doc.get("chain_depth", 0),
        "require_human": bool(doc.get("require_human", False)),
        "user_originated": bool(doc.get("user_originated", False)),
        "sent_by_human": bool(doc.get("sent_by_human", False)),
        "human_interacted": bool(doc.get("human_interacted", False)),
        "push_suppressed": bool(doc.get("push_suppressed", False)),
        "push_suppress_reason": doc.get("push_suppress_reason"),
        "recency_bypass": bool(doc.get("recency_bypass", False)),
        "is_system_notice": bool(doc.get("is_system_notice", False)),
        "system_notice_kind": doc.get("system_notice_kind"),
    }
    if doc.get("reply_to"):
        entry["reply_to"] = doc["reply_to"]
    if doc.get("in_response_to"):
        entry["in_response_to"] = doc["in_response_to"]
    _add_lane_fields(entry)
    return entry


@mcp.resource(
    "inbox://{project}/{agent}",
    name="inbox",
    description=(
        "Pending messages for an agent in a project. Reading returns the same "
        "payload shape as memory_get_messages plus a next_cursor for pagination "
        "(pass via memory_get_messages cursor= once supported). Subscribe to this "
        "URI to receive notifications/resources/updated when new messages arrive."
    ),
    mime_type="application/json",
)
async def read_inbox(project: str, agent: str) -> str:
    """Return pending messages for inbox://<project>/<agent> as JSON."""
    ok, reason = _check_inbox_authz(project, agent)
    if not ok:
        return json.dumps({
            "uri": inbox_uri(project, agent),
            "error": "permission denied",
            "detail": reason,
        })

    db = get_mongo()
    if db is None:
        return json.dumps({
            "uri": inbox_uri(project, agent),
            "count": 0,
            "messages": [],
            "error": "MongoDB unavailable",
        })

    # Match the targeting rules used by memory_get_messages:
    #  - workers only get direct messages (no broadcasts).
    #  - everyone else gets direct + "*" broadcasts.
    #  - project filter mirrors the legacy/empty-project tolerance.
    is_worker = agent.startswith("worker_")
    if is_worker:
        instance_match = {"to_instance": agent}
    else:
        instance_match = {"$or": [{"to_instance": agent}, {"to_instance": "*"}]}
    project_match = {
        "$or": [
            {"to_project": project},
            {"to_project": {"$exists": False}},
            {"to_project": ""},
        ]
    }
    query = {"$and": [instance_match, project_match, {"status": "pending"}]}

    # ── Push-suppression filter (design:push-control-v0 §3) ──
    # The inbox resource is what the channel-push delivery path reads. By
    # default, hide push_suppressed messages so a continuously-polling plugin
    # does not pump them as live channel notifications. They are released
    # only when the recipient's recency window is open (5min from any of:
    # memory_start_session for this agent, inbound sent_by_human=True,
    # outbound human_interacted=True). When closed, suppressed messages
    # remain visible to direct queries (memory_get_messages); the push
    # surface is the only one filtered.
    #
    # System notices (is_system_notice=True) are ALWAYS surfaced — per §8
    # they're meant to be found on the next normal inbox flush, sitting in
    # front of the suspect messages. They carry push_suppressed=True so
    # they never push, but they must not be hidden from reads.
    push_filter_active = not push_control.should_deliver_via_push_filter(
        db, project, agent
    )
    if push_filter_active:
        query["$and"].append({
            "$or": [
                {"push_suppressed": {"$ne": True}},
                {"is_system_notice": True},
            ]
        })

    # Recency-primary sort (created_at DESC) — see the get_messages sort note:
    # priority is a string, so DB-sorting on it stranded urgent behind a large
    # backlog (design:inbox-surfacing-v0). Within-page priority ordering is
    # applied in Python below; the inbox resource is the plugin's page-1 path.
    cursor = db.messages.find(query).sort([
        ("created_at", -1),
    ]).limit(INBOX_DEFAULT_LIMIT + 1)

    docs = list(cursor)
    has_more = len(docs) > INBOX_DEFAULT_LIMIT
    docs = docs[:INBOX_DEFAULT_LIMIT]

    next_cursor = None
    if has_more and docs:
        last_created = docs[-1].get("created_at")
        if hasattr(last_created, "isoformat"):
            next_cursor = last_created.isoformat()
        elif last_created is not None:
            next_cursor = str(last_created)

    messages = [_format_inbox_message(d) for d in docs]
    # Lanes-A within-page display order (post-fetch re-sort; DB selection above
    # stays created_at-primary). See get_messages for the layering rationale.
    _sort_messages_by_lane(messages)

    # Phase D2: bump recipient's recency timestamp the moment we hand them a
    # sent_by_human=True message via the inbox URI.
    if any(m.get("sent_by_human") for m in messages):
        _bump_human_interaction(db, project, agent)

    # Lane badge counts over the full inbox. fyi M-count is per-message UNREAD
    # (read_by excludes this agent) with the seen-watermark as a coarse floor —
    # and this resource read is READ-INERT (never writes read_by), so surfacing
    # the badge never zeroes M (build-plan task 2). action counts are read-state-
    # independent by design.
    lane_counts = _compute_lane_counts(
        db, [instance_match, project_match],
        watermark=_get_messages_seen_through(db, project, agent),
        reader=agent,
    )

    return json.dumps({
        "uri": inbox_uri(project, agent),
        "project": project,
        "agent": agent,
        "count": len(messages),
        "messages": messages,
        "lane_counts": lane_counts,
        "next_cursor": next_cursor,
        "has_more": has_more,
    })


def _parse_inbox_uri(uri_str: str) -> tuple:
    """Parse inbox://<project>/<agent> → (project, agent) or (None, None).

    Project component is normalized so case/separator variants of the URI
    resolve to the same subscription bucket.
    """
    m = re.match(r"^inbox://([^/]+)/([^/?#]+)", uri_str)
    if not m:
        return (None, None)
    return (normalize_project(m.group(1)), m.group(2))


# ── Server-authoritative content-push ──────────────────────────────────────
# design:server-authoritative-delivery-v0 §ANNOUNCE-PUSH (v0.5.1), ratified into
# contract:message-lanes-v0 §E. The server pushes a small content packet to each
# CONNECTED subscriber session instead of relying on the plugin to pull the
# inbox:// window on a payload-less resource-updated. The custom method below +
# the packet shape in _build_announce_packet ARE the frozen wire contract (§E3);
# the junto-inbox plugin registers a handler for this exact method.
ANNOUNCE_METHOD = "notifications/junto/announce"


def _announce_mode(category, priority, require_human, is_system_notice, obligation):
    """Render mode for the announce push, or None when the message must NOT push.

    INJECT (full body inline):  blocker | urgent | require_human | system_notice
    HEADER (one line, body-on-pull):  any other ACTION-lane OR fyi-lane (info) message
    None  (badge-only, no push):  ONLY a resolved (cleared) action

    info/fyi now pushes as a metadata-only HEADER (subject+from, body-on-pull) rather
    than badge-only — directed AND broadcast (push-all-info-v0, Tom 2026-06-24: "info
    has bitten us too often, push it like action; limit data to subject+from"). The
    fyi lane keeps its no-obligation, ~48h-TTL semantics — only the PUSH changes, not
    the lane (push ≠ obligation). A resolved action (lane "cleared") is the one case
    that must NOT re-push. classify_lane is the single source of truth for the lane
    (§E1); mode only adds the inject/header split on top.
    """
    lane, _tier = classify_lane(category, obligation)
    if lane == "cleared":
        return None  # a resolved action must not re-push
    # action OR fyi → push. Escalate to a full-body inject only for must-read-now
    # signals; everything else (incl. normal info) is a metadata-only header so the
    # context cost is one line (subject + from), body pulled on demand.
    if category == "blocker" or priority == "urgent" or require_human or is_system_notice:
        return "inject"
    return "header"


def _build_announce_packet(msg_doc, promoted: bool = False):
    """Build notifications/junto/announce params from a stored message doc, or
    None when the message is badge-only (must not be pushed).

    Frozen field set (contract:message-lanes-v0 §E3). Body is inlined ONLY for
    mode=="inject"; header mode is metadata-only (body-on-pull). All values are
    JSON primitives (created_at → ISO string) so the dict survives raw JSON-RPC
    serialization on the write stream.

    `promoted` (contract:reply-promotion-v0): when this reply advanced an open
    action obligation but its OWN mode would be None, promote it to push so the
    requester isn't left blind. Since push-all-info-v0 (2026-06-24) info pushes as
    a header natively, so the only mode==None case left is a CLEARED action reply —
    promotion now triggers rarely, but the safety net is kept. INJECT if the reply
    is itself urgent/blocker/require_human/system_notice, else HEADER. The reply
    still carries no NEW obligation — this only changes whether it pushes, not its
    lane membership (push ≠ new obligation).
    """
    is_system_notice = bool(msg_doc.get("is_system_notice", False))
    category = msg_doc.get("category", "info")
    priority = msg_doc.get("priority", "normal")
    require_human = bool(msg_doc.get("require_human", False))
    mode = _announce_mode(category, priority, require_human, is_system_notice, msg_doc.get("obligation"))
    if mode is None:
        if not promoted:
            return None
        # Obligation-closing reply whose own category doesn't push → promote it.
        mode = "inject" if (
            category == "blocker" or priority == "urgent" or require_human or is_system_notice
        ) else "header"
    created_at = msg_doc.get("created_at")
    packet = {
        "mode": mode,
        "from_agent": msg_doc.get("from_instance"),
        "from_project": msg_doc.get("from_project"),
        "category": msg_doc.get("category"),
        "priority": msg_doc.get("priority"),
        "msg_id": msg_doc.get("_id"),
        "chain_depth": msg_doc.get("chain_depth"),
        "in_response_to": msg_doc.get("in_response_to"),
        "obligation_state": msg_doc.get("obligation"),
        # SUBJECT is the primary triage line for a header push (info now pushes as
        # header → subject+from is all the recipient sees until they pull the body).
        "subject": msg_doc.get("subject"),
        "require_human": bool(msg_doc.get("require_human", False)),
        "is_system_notice": is_system_notice,
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }
    if mode == "inject":
        packet["body"] = msg_doc.get("message")
    return packet


async def _content_push(session: Any, packet: Dict[str, Any]) -> None:
    """Send one notifications/junto/announce content-push to a subscribed session.

    A custom JSON-RPC method is NOT in the typed ServerNotification union, so
    ServerSession.send_notification() would reject it. We build a raw
    JSONRPCNotification and write it to the session's stream via the low-level
    send_message escape hatch (mcp/server/session.py). SDK-coupling caveat:
    send_message is documented 'low-level experimental' — see
    learning_5dcf4824df37700f; the mcp SDK version is pinned. Raises on transport
    failure so the caller prunes the dead session.
    """
    notif = JSONRPCNotification(jsonrpc="2.0", method=ANNOUNCE_METHOD, params=packet)
    await session.send_message(SessionMessage(message=JSONRPCMessage(notif)))


# ── SSE notification-stream keepalive ──────────────────────────────────────
# A periodic no-op notification down each subscribed session's long-lived SSE
# GET stream. PRIMARY purpose: keep the stream warm so an idle-connection reaper
# on the path never silently drops it (the half-open-stream failure where the
# server keeps "delivering" into a dead socket while the client's separate-socket
# heartbeat keeps the session looking alive). SECONDARY: a genuinely dead socket
# eventually blocks the write → the per-send timeout fires → we prune the
# session. The plugin has no handler for this method, so it's ignored client-side
# (same "unknown method degrades to quiet" property as the announce push).
KEEPALIVE_METHOD = "notifications/junto/keepalive"
_keepalive_task = None


async def _send_keepalive(session: Any) -> None:
    """Write one keepalive notification to a session's stream. Raises on transport
    failure (and is cancellable by the wait_for timeout) so the caller prunes."""
    notif = JSONRPCNotification(jsonrpc="2.0", method=KEEPALIVE_METHOD, params={})
    await session.send_message(SessionMessage(message=JSONRPCMessage(notif)))


async def _keepalive_one(uri_str: str, session: Any) -> None:
    """Send a keepalive to one session under a per-send timeout; prune on failure.

    The timeout is load-bearing: a half-open socket's write can BLOCK (the SDK's
    notification stream is a zero-buffer memory stream, so send() waits for the
    SSE writer to drain). Without the timeout one stuck session would wedge the
    whole sweep. A timeout or transport error both mean "unusable" → drop it."""
    try:
        await asyncio.wait_for(_send_keepalive(session), timeout=SSE_KEEPALIVE_SEND_TIMEOUT)
    except Exception as e:
        log.debug("sse-keepalive: prune subscriber for %s: %s", uri_str, e)
        bucket = inbox_subscriptions.get(uri_str)
        if bucket is not None:
            bucket.discard(session)
            if not bucket:
                inbox_subscriptions.pop(uri_str, None)


async def _keepalive_sweep() -> None:
    """One pass: keepalive every subscribed session concurrently (snapshot first
    so prunes don't mutate what we're iterating). Concurrency means one slow/stuck
    session can't serialize-block the others."""
    tasks = [
        _keepalive_one(uri_str, session)
        for uri_str in list(inbox_subscriptions.keys())
        for session in list(inbox_subscriptions.get(uri_str, ()))
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _keepalive_loop() -> None:
    log.info("sse-keepalive: started (interval=%ss, send_timeout=%ss)",
             SSE_KEEPALIVE_SECONDS, SSE_KEEPALIVE_SEND_TIMEOUT)
    while True:
        try:
            await asyncio.sleep(SSE_KEEPALIVE_SECONDS)
            await _keepalive_sweep()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("sse-keepalive: loop error: %s", e)


def start_keepalive() -> None:
    """Start the SSE keepalive background task. Idempotent if already running."""
    global _keepalive_task
    if _keepalive_task is not None and not _keepalive_task.done():
        return
    _keepalive_task = asyncio.create_task(_keepalive_loop(), name="sse_keepalive")
    log.info("sse-keepalive: background task created")


def stop_keepalive() -> None:
    """Cancel the keepalive task on shutdown."""
    global _keepalive_task
    if _keepalive_task is not None and not _keepalive_task.done():
        _keepalive_task.cancel()
    _keepalive_task = None


async def _notify_inbox_for_send(
    to_project: str, to_instance: str, packet: Dict[str, Any] = None
) -> None:
    """Dispatch inbox notifications after memory_send_message.

    Direct messages → notify the named agent's inbox URI.
    Broadcasts (to_instance='*') → notify every subscribed inbox URI in the
    target project, since each subscriber will see the broadcast in their
    own get_messages/inbox view.

    `packet` (server-authoritative delivery §E): when provided AND the message is
    push-eligible, each session gets a content-push in ADDITION to the
    resource-updated. None (wake-all / sync / scheduler / cleared-action paths) =
    resource-updated only. Since push-all-info-v0 (2026-06-24) info pushes as a
    header, so broadcasts (info-lane by convention, §B) now DO carry a packet and
    content-push to every subscriber in the target project — project-scoped, never
    cross-project (the prefix below pins it to to_project).
    """
    if not to_project:
        return
    if to_instance == "*":
        prefix = f"inbox://{to_project}/"
        targets = []
        for uri in list(inbox_subscriptions.keys()):
            if not uri.startswith(prefix):
                continue
            p, a = _parse_inbox_uri(uri)
            if p and a and a != "*":
                targets.append((p, a))
        for project, agent in targets:
            await _notify_inbox(project, agent, packet)
        return
    await _notify_inbox(to_project, to_instance, packet)


async def _notify_inbox(project: str, agent: str, packet: Dict[str, Any] = None) -> None:
    """Notify every session subscribed to inbox://<project>/<agent>.

    Always fires notifications/resources/updated (the pre-cutover plugin pulls the
    inbox:// window on it). When `packet` is provided, ADDITIONALLY content-pushes
    the announce packet (server-authoritative delivery §E2) — a post-cutover
    plugin acts on this and ignores the resource-updated; a pre-cutover plugin
    ignores the unknown announce method. Keeping BOTH during the transition means
    a server-ahead-of-plugin (or plugin-ahead-of-server) state degrades to quiet,
    never a flood (§E7). Drops sessions whose send fails (dead transport).
    Best-effort: a failure here must not break message insert.
    """
    if not project or not agent:
        return
    uri_str = inbox_uri(project, agent)
    sessions = list(inbox_subscriptions.get(uri_str, ()))
    if not sessions:
        return

    try:
        url = AnyUrl(uri_str)
    except Exception:  # pragma: no cover — defensive
        log.warning("inbox: cannot construct AnyUrl for %s", uri_str)
        return

    async def _push_one(session: Any) -> None:
        await session.send_resource_updated(url)
        if packet is not None:
            await _content_push(session, packet)

    dead: List[Any] = []
    for session in sessions:
        try:
            # Per-send timeout (backlog_940b9f9c66e1): a half-open socket's
            # write BLOCKS forever on the zero-buffer notification stream —
            # the same failure the keepalive sweep guards against. Without
            # this, one zombie subscriber wedges the loop and every LIVE
            # subscriber after it in the list never receives the push
            # (observed as delivered:false during active sessions on the
            # work box). Timeout or transport error both mean "unusable".
            await asyncio.wait_for(_push_one(session), timeout=NOTIFY_SEND_TIMEOUT)
        except Exception as e:  # session closed / write end gone / stuck
            log.debug("inbox: drop dead subscriber for %s: %s", uri_str, e)
            dead.append(session)

    if dead:
        bucket = inbox_subscriptions.get(uri_str)
        if bucket is not None:
            for s in dead:
                bucket.discard(s)
            if not bucket:
                inbox_subscriptions.pop(uri_str, None)


# Subscribe / unsubscribe handlers — registered on the lowlevel server because
# FastMCP exposes a resource decorator but not a subscribe handler. The lowlevel
# `subscribe_resource()` overrides the request handler for SubscribeRequest.
# Inside the handler, mcp._mcp_server.request_context.session is the
# ServerSession that issued the SubscribeRequest — that's the one we need to
# notify on future updates.

@mcp._mcp_server.subscribe_resource()
async def _on_subscribe(uri: AnyUrl) -> None:
    uri_str = str(uri)
    if not uri_str.startswith("inbox://"):
        # Only inbox resources are subscribable for now. No-op for others
        # (the lowlevel server still returns success, matching MCP semantics).
        return
    project, agent = _parse_inbox_uri(uri_str)
    if not project or not agent:
        raise ValueError(f"malformed inbox URI: {uri_str}")

    ok, reason = _check_inbox_authz(project, agent)
    if not ok:
        caller = _resolve_caller_identity()
        log_audit(
            "inbox.subscribe.denied",
            actor=(caller or {}).get("claude_instance", "unknown"),
            project=project,
            details={"uri": uri_str, "agent": agent, "reason": reason,
                     "caller_project": (caller or {}).get("project", "")},
            session_id=(caller or {}).get("session_id", ""),
        )
        # Surface a real error so the client doesn't think they subscribed.
        log.warning("inbox: subscribe denied for %s — %s", uri_str, reason)
        raise PermissionError(f"subscribe denied: {reason}")

    try:
        session = mcp._mcp_server.request_context.session
    except LookupError:
        log.warning("inbox: subscribe outside request context for %s", uri_str)
        return
    inbox_subscriptions.setdefault(uri_str, set()).add(session)
    caller = _resolve_caller_identity()
    log_audit(
        "inbox.subscribe",
        actor=(caller or {}).get("claude_instance", "unknown"),
        project=project,
        details={"uri": uri_str, "agent": agent,
                 "subscriber_count": len(inbox_subscriptions[uri_str])},
        session_id=(caller or {}).get("session_id", ""),
    )
    log.info(
        "inbox: subscribed %s/%s (subscribers=%d)",
        project, agent, len(inbox_subscriptions[uri_str]),
    )


@mcp._mcp_server.unsubscribe_resource()
async def _on_unsubscribe(uri: AnyUrl) -> None:
    uri_str = str(uri)
    if not uri_str.startswith("inbox://"):
        return
    try:
        session = mcp._mcp_server.request_context.session
    except LookupError:
        return
    bucket = inbox_subscriptions.get(uri_str)
    if bucket is None:
        return
    bucket.discard(session)
    remaining = len(bucket)
    if not bucket:
        inbox_subscriptions.pop(uri_str, None)
    project, agent = _parse_inbox_uri(uri_str)
    caller = _resolve_caller_identity()
    log_audit(
        "inbox.unsubscribe",
        actor=(caller or {}).get("claude_instance", "unknown"),
        project=project or "",
        details={"uri": uri_str, "agent": agent or "", "remaining": remaining},
        session_id=(caller or {}).get("session_id", ""),
    )
    log.info("inbox: unsubscribed %s (remaining=%d)", uri_str, remaining)
