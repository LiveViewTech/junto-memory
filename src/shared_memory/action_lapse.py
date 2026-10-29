"""Stale action obligations lapse (design:action-lapse-v0).

Why: under unified-messaging Stage 5 an open action message never ages, and
replies often omit in_response_to, so the action lane fills with sediment that
hides real asks (2026-10-12: 1,954 open on nimbus, 1,051 older than 60d).

Every SWEEP_INTERVAL_SECONDS, per project with push_control `action_lapse_days`
> 0, pending action messages older than that whose obligation is still
open/responded/unset become obligation="lapsed": out of the action lane, NOT
deleted (expire_at untouched), revivable with memory_update_message_status(id,
"open") or by the owner replying. Human-touched threads are exempt. Each
affected recipient gets ONE digest notice per sweep.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Dict, List, Optional

from shared_memory.config import ACTION_CATEGORIES

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 6 * 3600
FIRST_SWEEP_DELAY_SECONDS = 300   # let a fresh boot settle before the first pass
DIGEST_MAX_ITEMS = 30
PREVIEW_CHARS = 80
DIGEST_SUBJECT = "Lapsed: {n} unanswered action message(s) older than {days}d left your action lane"

_sweep_task: Optional[asyncio.Task] = None


def _candidates(db, project: str, cutoff, responded_cutoff=None) -> List[Dict]:
    """Open/unset obligations older than `cutoff`. Responded ones (answered,
    never closed — the bulk of nimbus sediment, learning_7260ac9541c520d5)
    lapse once the REPLY is older than `responded_cutoff` (created_at when
    responded_at is missing); without one they share `cutoff`."""
    rc = responded_cutoff or cutoff
    return list(db.messages.find(
        {
            "to_project": project,
            "to_instance": {"$ne": "*"},
            "category": {"$in": ACTION_CATEGORIES},
            "status": "pending",
            "sent_by_human": {"$ne": True},
            "user_originated": {"$ne": True},
            "require_human": {"$ne": True},
            "$or": [
                {"obligation": {"$in": ["open", None]}, "created_at": {"$lt": cutoff}},
                {"obligation": "responded", "responded_at": {"$lt": rc}},
                {"obligation": "responded", "responded_at": None, "created_at": {"$lt": rc}},
            ],
        },
        {"_id": 1, "to_instance": 1, "from_instance": 1, "from_project": 1,
         "subject": 1, "message": 1, "obligation": 1, "created_at": 1, "category": 1},
    ))


def _human_touched(db, ids: List[str], since) -> set:
    """Ids among `ids` that got a human reply at or after `since`."""
    if not ids:
        return set()
    return set(db.messages.distinct(
        "in_response_to",
        {"in_response_to": {"$in": ids}, "sent_by_human": True, "created_at": {"$gte": since}},
    ))


def _preview(doc) -> str:
    text = (doc.get("subject") or doc.get("message") or "").replace("\n", " ").strip()
    return text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")


def build_digest(items: List[Dict], days: int) -> str:
    """Per-sender counts, then the newest DIGEST_MAX_ITEMS grouped by sender."""
    by_sender = defaultdict(list)
    for d in items:
        by_sender["%s@%s" % (d.get("from_instance", "?"), d.get("from_project", "?"))].append(d)
    lines = [
        "%d action message(s) addressed to you were left open (unanswered for over %d days, "
        "or answered but never marked done) and are now obligation=lapsed. They are NOT deleted and no longer count in your action "
        "lane. If one is still real, revive it with memory_update_message_status(<id>, \"open\") "
        "or just reply with in_response_to=<id>. (design:action-lapse-v0)" % (len(items), days),
        "",
        "By sender: " + ", ".join("%s %d" % (s, len(v)) for s, v in
                                  sorted(by_sender.items(), key=lambda kv: -len(kv[1]))),
    ]
    newest = sorted(items, key=lambda d: d.get("created_at"), reverse=True)[:DIGEST_MAX_ITEMS]
    shown = defaultdict(list)
    for d in newest:
        shown["%s@%s" % (d.get("from_instance", "?"), d.get("from_project", "?"))].append(d)
    lines.append("")
    lines.append("Newest %d:" % len(newest))
    for sender, docs in shown.items():
        lines.append("%s:" % sender)
        for d in docs:
            when = d["created_at"].strftime("%m-%d") if hasattr(d.get("created_at"), "strftime") else "?"
            lines.append("  %s %s [%s] %s" % (d["_id"], when, d.get("category"), _preview(d)))
    if len(items) > len(newest):
        lines.append("(+%d older not listed)" % (len(items) - len(newest)))
    return "\n".join(lines)


async def _send_digest(db, project: str, agent: str, body: str, n: int, days: int) -> str:
    from shared_memory.helpers import utc_now
    from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send

    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    from shared_memory.config import MESSAGE_INFO_TTL_HOURS
    doc = {
        "_id": message_id,
        "to_instance": agent,
        "to_project": project,
        "from_instance": "system",
        "from_project": project,
        "from_session": None,
        "subject": DIGEST_SUBJECT.format(n=n, days=days),
        "message": body,
        "priority": "low",
        "category": "info",
        "obligation": None,
        "reply_to": None,
        "in_response_to": None,
        "chain_depth": 0,
        "require_human": False,
        "sent_by_human": False,
        "human_interacted": False,
        "user_originated": False,
        "status": "pending",
        "push_suppressed": False,
        "push_suppress_reason": None,
        "emission_count": 0,
        "recency_bypass": False,
        "is_system_notice": True,
        "system_notice_kind": "action_lapse_digest",
        "created_at": now,
        "expire_at": now + timedelta(hours=MESSAGE_INFO_TTL_HOURS),
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }
    db.messages.insert_one(doc)
    try:
        await _notify_inbox_for_send(project, agent, _build_announce_packet(doc))
    except Exception as e:
        log.warning("action-lapse: digest push failed for %s/%s: %s", agent, project, e)
    return message_id


async def sweep_project(db, project: str, days: int, now=None,
                        responded_days: int = 0) -> Dict[str, int]:
    """Lapse one project's stale actions. Returns {agent: lapsed_count}."""
    from shared_memory.helpers import utc_now

    now = now or utc_now()
    cutoff = now - timedelta(days=days)
    responded_cutoff = now - timedelta(days=responded_days) if responded_days > 0 else None
    cands = _candidates(db, project, cutoff, responded_cutoff)
    exempt = _human_touched(db, [d["_id"] for d in cands], cutoff)
    by_agent = defaultdict(list)
    for d in cands:
        if d["_id"] not in exempt:
            by_agent[d["to_instance"]].append(d)
    result = {}
    for agent, docs in by_agent.items():
        lapsed = []
        for d in docs:
            # Guard on the obligation we read, so a concurrent reply wins.
            r = db.messages.update_one(
                {"_id": d["_id"], "obligation": d.get("obligation"), "status": "pending"},
                {"$set": {"obligation": "lapsed", "lapsed_at": now,
                          "lapsed_from": d.get("obligation") or "open"}},
            )
            if r.modified_count:
                lapsed.append(d)
        if lapsed:
            try:
                await _send_digest(db, project, agent, build_digest(lapsed, days), len(lapsed), days)
            except Exception as e:
                log.error("action-lapse: digest failed for %s/%s: %s", agent, project, e)
            result[agent] = len(lapsed)
    return result


async def sweep_once(db) -> Dict[str, Dict[str, int]]:
    """One pass over every project that has candidate messages."""
    if db is None:
        return {}
    from shared_memory.push_control import get_effective_config

    out = {}
    projects = db.messages.distinct(
        "to_project",
        {"category": {"$in": ACTION_CATEGORIES}, "status": "pending",
         "obligation": {"$in": ["open", "responded", None]}},
    )
    for project in projects:
        if not project:
            continue
        try:
            cfg = get_effective_config(db, project)
            days = int(cfg.get("action_lapse_days") or 0)
            responded_days = int(cfg.get("action_lapse_responded_days") or 0)
        except Exception:
            days, responded_days = 0, 0
        if days <= 0:
            continue
        res = await sweep_project(db, project, days, responded_days=responded_days)
        if res:
            out[project] = res
            log.info("action-lapse: %s lapsed %d message(s) across %d agent(s)",
                     project, sum(res.values()), len(res))
    return out


async def _sweep_loop() -> None:
    from shared_memory.clients import get_mongo

    log.info("action-lapse: started (sweep=%ss)", SWEEP_INTERVAL_SECONDS)
    delay = FIRST_SWEEP_DELAY_SECONDS
    while True:
        try:
            await asyncio.sleep(delay)
            delay = SWEEP_INTERVAL_SECONDS
            await sweep_once(get_mongo())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("action-lapse: loop error: %s", e)


def start_action_lapse() -> None:
    """Start the sweep task. Idempotent if already running."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="action_lapse_sweep")
    log.info("action-lapse: background task created")
