"""Idle→park nudge (design:idle-park-nudge-v0; coordinator msg_177ffff64ec1, Tom 2026-10-10).

Why: Claude Code's prompt cache expires ~60 min after an agent's last turn. A
big-context agent that is typed into after that pays a full-price cache
rebuild. CC 2.1.296 tried to solve this with idle compaction; the fleet turned
that off (learning_c92c3e1080238427). Instead, the server nudges an agent that
has gone quiet for `idle_park_minutes` (default 50, i.e. still cache-warm) to
park, so the park turn runs cheap and the operator returns to a parked agent
(/clear + go) rather than a stale 800K context.

Mechanics:
- ONE timer per (project, agent name) — not per session; agents often run
  2-3 sessions. In-memory only: a server restart clears every timer, and the
  agent's next real call re-arms it (a restart is not a reason to nudge).
- Armed/reset by every REAL tool call, observed at the CallToolRequest
  chokepoint (app.py). Also reset (never armed) by the agent's TURNS: the
  statusline stamps written_at on every render, renders happen only during
  turns, and the box reporter forwards it via memory_report_context
  (note_turn). Without this a turn spent talking to the user (no junto
  calls) looked idle and got nudged mid-conversation (coordinator@nimbus
  msg_00be0936015b, 2026-10-15). Background traffic does NOT count: the junto-inbox
  plugin's heartbeat / health / emission-stats / start_session (reconnects)
  and the per-box context reporter (memory_report_context, every 60s). A call
  with no resolvable session is ignored.
- Fires ONCE per idle period. The agent's own reply turn to the nudge makes
  junto calls; calls within REARM_GRACE_SECONDS of the nudge do not re-arm, so
  an agent that reads the nudge and decides not to park is not re-nudged every
  hour while it stays idle. Real activity after the grace re-arms.
- memory_end_session disarms the timer (parked agents are never nudged).
- Only agents with a LARGE context are nudged: the context gauge
  (agent_status.context_tokens, fed by memory_report_context) must be >=
  `idle_park_min_context` (default 200K). Measured from transcripts
  2026-10-10: a warm park costs ~0.45-0.9x context in input-token equivalents,
  /clear+go ~80-210K, a cold resume ~2x context, and a cold park ~2.5-2.85x.
  Below ~200K the nudge doesn't pay; it also stops a post-go agent being
  nudged again. An agent with NO reported context is still nudged (unknown
  size; e.g. boxes without the reporter).
- Opt-out: push_control config `idle_park_minutes=0` (per project or server
  default) disables; `idle_park_optout` is a comma-separated agent list for
  always-on agents.
- Policy text: the default body advises parking at a clean stop. A project
  with an active project-scoped guideline `park_on_idle_nudge` gets that
  guideline's live text in the body instead (nudge_text), so its own idle
  policy (e.g. nimbus keep-alive) wins without agents overriding the nudge.
  Projects without one fall back to push_control `idle_park_policy` (project
  override or server default), so a many-project server sets it once.
- Every body ends with the agent's local time and a DAY/NIGHT verdict in its
  own zone (agent_timezone: agent_directory.timezone → idle_park_timezone →
  America/Chicago), so policies need not hardcode `TZ=... date`.
- Parked agents: after memory_end_session the agent's calls don't arm a timer
  until it starts a new session itself (note_session_start; a plugin rebind
  doesn't count) or does real work (UNPARK_TOOLS), so a parked agent is not
  nudged for reading or answering a message.
- Auto-reset agents (idle_park_autoreset) get a direct "park now, you'll be
  /clear+go'd" body with their reported context size; it overrides the
  project policy text (Tom 2026-10-17). Since 2026-10-26 only at night
  (21-05 America/Chicago); by day they get the project policy like everyone.
- Self-wake skip: no nudge while the agent has its own pending reminder due
  within SELF_WAKE_WINDOW_SECONDS (it has already armed a wake). The timer is
  still marked nudged, so the reminder's turn re-arms it as usual.

Delivery: an is_system_notice message (kind idle_park_nudge) → INJECT push via
the normal inbox path, so the full body reaches the agent's channel.

Phase 2 — auto-reset (design:idle-park-autoreset-v0, Tom 2026-10-11): when a
nudged agent parks (memory_end_session while its timer is still in the nudged
state) and is listed in `idle_park_autoreset`, an `idle_park_resets` doc goes
pending. The per-box context reporter receives it in the memory_report_context
response, runs a safe-inject gate on the agent's tmux pane, types /clear + go,
and reports the outcome back on its next memory_report_context call.
"""

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Tools the junto-inbox plugin / context reporter call on their own schedule.
# These must never reset the idle timer, or it would never fire.
BACKGROUND_TOOLS = frozenset({
    "memory_heartbeat",
    "memory_health",
    "memory_start_session",
    "memory_report_context",
    "memory_get_emission_stats",
})

# Work a parked agent only does when it has resumed working: it unparks the
# agent and arms its timer. Agents woken while parked often keep working on
# the inbox plugin's session and never call memory_start_session, so without
# this they were never nudged again (server-team/billing-team sat at ~700K all
# night 10-26, coordinator@nimbus msg_df405eec7915). Reads, acks and
# memory_send_message stay out: a parked agent woken to answer one message
# should not be nudged for it.
UNPARK_TOOLS = frozenset({
    "memory_update_work",
    "memory_lock_files",
    "memory_define_spec",
    "memory_record_learning",
    "memory_store",
    "memory_register_function",
    "memory_add_backlog_item",
    "memory_update_backlog_item",
    "memory_complete_backlog_item",
    "memory_batch_backlog",
    "memory_delegate",
})

SWEEP_INTERVAL_SECONDS = 60
REARM_GRACE_SECONDS = 600
RESET_EXPIRY_HOURS = 6
RESET_OUTCOMES = frozenset({"done", "failed", "deferred"})
# An agent with its own reminder due within this window has already armed a
# wake (e.g. a nimbus keep-alive), so the nudge is skipped. Without this the
# 50-min nudge always lands before a +55m keep-alive and resets its count, so
# the chain never times out (learning_222e630bf8b35d24).
SELF_WAKE_WINDOW_SECONDS = 3600

NUDGE_SUBJECT = "Idle {minutes} min — prompt cache expires at ~60 min idle"
# Default advice, used when the project has no idle-policy guideline.
NUDGE_BODY = (
    "Idle {minutes} min: park (memory_get_skill('parking')) if you're at a "
    "clean stopping point — your prompt cache expires at ~60 min idle, and "
    "parking while it's warm is cheap. If you're mid-task or the user is "
    "actively working with you, ignore this. (One nudge per idle period; "
    "opt out via push_control config idle_park_optout.)"
)
# Agents on idle_park_autoreset get a direct "park now" instead: the box will
# /clear + go them after they park, and that only happens if they park while
# the nudge is fresh. It overrides the project's keep-alive policy (Tom,
# 2026-10-17) — at night only since 2026-10-26 (see _is_night). Must start with "Idle": the reset gate anchors on
# "[SYSTEM NOTICE] Idle" (contrib/context-report NUDGE_MARKER).
NUDGE_AUTORESET_BODY = (
    "Idle {minutes} min{context}. You're on auto-reset: PARK NOW "
    "(memory_get_skill('parking')): refresh your state spec, then "
    "memory_end_session. Once you've parked, this box will /clear and `go` you "
    "automatically, so you come back with a fresh context and your state spec. "
    "This overrides any keep-alive rule for you. Skip it only if the user is "
    "actively working with you or you're mid-way through an irreversible step."
)

# A project sets its own idle policy with a project-scoped guideline of this
# name. The nudge carries that guideline's LIVE text instead of the default
# advice, so agents act on the current rule even when the copy cached at
# session start / plugin bind is stale (coordinator@nimbus, msg_9f1cf850e7d6).
POLICY_GUIDELINE = "park_on_idle_nudge"
NUDGE_POLICY_BODY = (
    "Idle {minutes} min. Your project's idle policy (guideline "
    "{guideline}, live text — it supersedes any cached copy):\n\n{rule}"
)

# (project, agent) -> {"last_real": monotonic, "nudged_at": monotonic|None}
_timers: Dict[Tuple[str, str], Dict[str, Optional[float]]] = {}

_sweep_task: Optional[asyncio.Task] = None

# (project, agent) pairs that parked (memory_end_session). Their tool calls
# don't arm a timer until the agent itself starts a new session: a parked
# agent's plugin stays attached, and its calls used to re-arm the timer, so
# parked agents were nudged every ~2h onto an expired cache — a full context
# rebuild to reply "still parked" (coordinator@nimbus, msg_9684a5d90e59).
_parked: set = set()

# Session task strings that belong to a client, not an agent turn. A start
# from one of these doesn't count as the agent coming back.
CLIENT_TASKS = frozenset({"junto-inbox channel plugin"})

# (project, agent, nudged_at ISO) for agents that ended their session while
# nudged. Drained into idle_park_resets by drain_parked() (sweep or report).
_parked_after_nudge: Deque[Tuple[str, str, Optional[str]]] = deque()


def _identity_for_session(session_id) -> Optional[Tuple[str, str]]:
    from shared_memory.state import active_sessions

    if not isinstance(session_id, str):
        return None
    info = active_sessions.get(session_id)
    if not info:
        return None
    project = info.get("project")
    agent = info.get("claude_instance")
    if not project or not agent:
        return None
    return project, agent


def note_tool_call(tool_name: str, session_id, now: Optional[float] = None) -> None:
    """Record a tool call against its agent's idle timer (CallToolRequest hook)."""
    if tool_name in BACKGROUND_TOOLS:
        return
    ident = _identity_for_session(session_id)
    if ident is None:
        return
    now = time.monotonic() if now is None else now
    if tool_name == "memory_end_session":
        _parked.add(ident)
        timer = _timers.pop(ident, None)
        if timer is not None and timer.get("nudged_at") is not None:
            _parked_after_nudge.append((ident[0], ident[1], timer.get("nudged_wall")))
        return
    if ident in _parked:
        if tool_name not in UNPARK_TOOLS:
            return
        _parked.discard(ident)
    timer = _timers.get(ident)
    if timer is None:
        _timers[ident] = {"last_real": now, "nudged_at": None}
        return
    nudged_at = timer.get("nudged_at")
    if nudged_at is not None and now - nudged_at < REARM_GRACE_SECONDS:
        # The agent's response turn to the nudge — don't re-arm off it.
        return
    timer["last_real"] = now
    timer["nudged_at"] = None
    timer["nudged_wall"] = None


def note_session_start(project: str, agent: str, task) -> None:
    """The agent started a session itself: a parked agent can be nudged again.

    A client start (the inbox plugin rebinding) doesn't unpark.
    """
    if (task or "").strip().lower() in CLIENT_TASKS:
        return
    _parked.discard((project, agent))


def note_turn(agent: str, written_at, now: Optional[float] = None, now_wall=None) -> bool:
    """Reset `agent`'s idle timer from a statusline render time (memory_report_context).

    Only existing timers move: agent_status carries no project, so a render can't
    arm a new one, and it resets every project's timer for that name (a name clash
    only means fewer nudges). Renders inside the nudge's re-arm grace are the
    agent answering the nudge and don't count. Returns True if a timer moved.
    """
    from datetime import datetime, timezone

    if not agent or not isinstance(written_at, str):
        return False
    try:
        at_wall = datetime.fromisoformat(written_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if at_wall.tzinfo is None:
        at_wall = at_wall.replace(tzinfo=timezone.utc)
    now = time.monotonic() if now is None else now
    now_wall = datetime.now(timezone.utc) if now_wall is None else now_wall
    at = now - max(0.0, (now_wall - at_wall).total_seconds())  # future stamps (clock skew) = now
    moved = False
    for (_project, name), timer in _timers.items():
        if name != agent or at <= timer["last_real"]:
            continue
        nudged_at = timer.get("nudged_at")
        if nudged_at is not None:
            if at < nudged_at + REARM_GRACE_SECONDS:
                continue
            timer["nudged_at"] = None
            timer["nudged_wall"] = None
        timer["last_real"] = at
        moved = True
    return moved


def due_agents(minutes_for, now: Optional[float] = None):
    """Return (project, agent, idle_minutes) for timers due to fire.

    `minutes_for(project, agent)` returns the threshold in minutes, or 0/None
    when the agent is opted out.
    """
    now = time.monotonic() if now is None else now
    due = []
    for (project, agent), timer in list(_timers.items()):
        if timer.get("nudged_at") is not None:
            continue
        minutes = minutes_for(project, agent)
        if not minutes:
            continue
        if now - timer["last_real"] >= minutes * 60:
            due.append((project, agent, minutes))
    return due


def mark_nudged(project: str, agent: str, now: Optional[float] = None) -> None:
    from shared_memory.helpers import utc_now

    timer = _timers.get((project, agent))
    if timer is not None:
        timer["nudged_at"] = time.monotonic() if now is None else now
        timer["nudged_wall"] = utc_now().isoformat()


def _threshold_from_config(db, project: str, agent: str) -> int:
    from shared_memory.push_control import get_effective_config

    cfg = get_effective_config(db, project)
    minutes = cfg.get("idle_park_minutes") or 0
    optout = cfg.get("idle_park_optout") or ""
    if agent in {a.strip() for a in str(optout).split(",") if a.strip()}:
        return 0
    if not minutes:
        return 0
    min_context = int(cfg.get("idle_park_min_context") or 0)
    if min_context:
        tokens = _reported_context_tokens(db, agent)
        if tokens is not None and tokens < min_context:
            return 0
    return int(minutes)


def _reported_context_tokens(db, agent: str) -> Optional[int]:
    """Last context size from the gauge (agent_status is keyed by instance only)."""
    try:
        doc = db.agent_status.find_one({"instance": agent}, {"context_tokens": 1})
    except Exception:
        return None
    tokens = (doc or {}).get("context_tokens")
    return tokens if isinstance(tokens, int) and tokens > 0 else None


def _csv(value) -> set:
    return {a.strip() for a in str(value or "").split(",") if a.strip()}


def drain_parked(db) -> int:
    """Turn queued nudged-parks into pending idle_park_resets docs (opt-in only).

    One pending doc per (project, agent): a second park while one is pending
    is a no-op. Returns the number of docs created.
    """
    from shared_memory.helpers import utc_now
    from shared_memory.push_control import get_effective_config

    created = 0
    while _parked_after_nudge:
        project, agent, nudged_wall = _parked_after_nudge.popleft()
        try:
            cfg = get_effective_config(db, project)
            if agent not in _csv(cfg.get("idle_park_autoreset")):
                continue
            res = db.idle_park_resets.update_one(
                {"project": project, "agent": agent, "status": "pending"},
                {"$setOnInsert": {
                    "_id": f"reset_{uuid.uuid4().hex[:12]}",
                    "project": project,
                    "agent": agent,
                    "status": "pending",
                    "requested_at": utc_now(),
                    "nudge_at": nudged_wall,
                    "attempts": 0,
                }},
                upsert=True,
            )
            if res.upserted_id is not None:
                created += 1
                log.info("idle-park: reset pending for %s/%s (%s)", agent, project, res.upserted_id)
        except Exception as e:
            log.error("idle-park: could not record reset for %s/%s: %s", agent, project, e)
    return created


def pending_resets_for(db, instances) -> List[dict]:
    """Pending resets for these agent names; expires ones older than RESET_EXPIRY_HOURS."""
    from shared_memory.helpers import utc_now

    drain_parked(db)
    names = [i for i in instances if isinstance(i, str) and i]
    if not names:
        return []
    cutoff = utc_now() - timedelta(hours=RESET_EXPIRY_HOURS)
    db.idle_park_resets.update_many(
        {"agent": {"$in": names}, "status": "pending", "requested_at": {"$lt": cutoff}},
        {"$set": {"status": "expired", "finished_at": utc_now()}},
    )
    out = []
    for doc in db.idle_park_resets.find({"agent": {"$in": names}, "status": "pending"}):
        requested = doc.get("requested_at")
        out.append({
            "reset_id": doc["_id"],
            "project": doc.get("project"),
            "agent": doc.get("agent"),
            "requested_at": requested.isoformat() if hasattr(requested, "isoformat") else requested,
        })
    return out


def apply_reset_results(db, results) -> Dict[str, list]:
    """Record reporter outcomes. done/failed close a pending reset; deferred only logs the attempt."""
    from shared_memory.helpers import utc_now

    applied, rejected = [], []
    for r in results or []:
        if not isinstance(r, dict):
            rejected.append(str(r)[:40])
            continue
        reset_id = r.get("reset_id")
        outcome = r.get("outcome")
        detail = str(r.get("detail") or "")[:500]
        if not reset_id or outcome not in RESET_OUTCOMES:
            rejected.append(str(reset_id or r)[:40])
            continue
        now = utc_now()
        if outcome == "deferred":
            update = {"$set": {"last_attempt_at": now, "last_detail": detail}, "$inc": {"attempts": 1}}
        else:
            update = {"$set": {"status": outcome, "finished_at": now, "last_detail": detail},
                      "$inc": {"attempts": 1}}
        res = db.idle_park_resets.update_one({"_id": reset_id, "status": "pending"}, update)
        if res.matched_count:
            applied.append(reset_id)
            if outcome != "deferred":
                log.info("idle-park: reset %s %s (%s)", reset_id, outcome, detail)
        else:
            rejected.append(reset_id)
    return {"applied": applied, "rejected": rejected}


def _project_idle_policy(db, project: str) -> Optional[str]:
    """Live text of the project's park_on_idle_nudge guideline, or None."""
    scope = project.lower().replace("-", "_").replace(" ", "_")
    try:
        doc = db.guidelines.find_one(
            {"name": POLICY_GUIDELINE, "scope": scope, "active": True}, {"rule": 1})
    except Exception:
        return None
    rule = (doc or {}).get("rule")
    return rule.strip() if isinstance(rule, str) and rule.strip() else None


def _on_autoreset(db, project: str, agent: Optional[str]) -> bool:
    if not agent:
        return False
    try:
        from shared_memory.push_control import get_effective_config
        return agent in _csv(get_effective_config(db, project).get("idle_park_autoreset"))
    except Exception:
        return False


# Auto-reset agents get PARK NOW only at night (Tom's local time). By day they
# follow the project's keep-alive policy like everyone else, so Tom can work
# with them without a park + go every hour (Tom, 2026-10-26). Matches the
# NIGHT window in the park_on_idle_nudge guidelines (21-05 America/Chicago).
NIGHT_TZ = "America/Chicago"
NIGHT_START_HOUR = 21
NIGHT_END_HOUR = 6


def _is_night(now: Optional[datetime] = None, tz: Optional[str] = None) -> bool:
    try:
        from zoneinfo import ZoneInfo
        local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz or NIGHT_TZ))
    except Exception:
        return True  # no tz data: keep the pre-2026-10-26 always-park behavior
    return local.hour >= NIGHT_START_HOUR or local.hour < NIGHT_END_HOUR


def valid_timezone(tz) -> bool:
    """True for an IANA zone name zoneinfo can load (e.g. 'America/Denver')."""
    if not isinstance(tz, str) or not tz.strip():
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz.strip())
        return True
    except Exception:
        return False


def agent_timezone(db, project: str, agent: Optional[str]) -> str:
    """The agent's local zone for day/night decisions (Tom 2026-10-27: offices
    in several locations, so per agent). Order: the agent's own
    agent_directory.timezone (memory_start_session(timezone=...)) → push_control
    idle_park_timezone (project, then server default) → NIGHT_TZ."""
    from shared_memory.helpers import normalize_project
    try:
        if db is not None and agent:
            doc = db.agent_directory.find_one(
                {"project": normalize_project(project), "instance": agent}, {"timezone": 1})
            tz = (doc or {}).get("timezone")
            if valid_timezone(tz):
                return tz.strip()
    except Exception:
        pass
    try:
        from shared_memory.push_control import get_effective_config
        tz = get_effective_config(db, project).get("idle_park_timezone")
        if valid_timezone(tz):
            return tz.strip()
    except Exception:
        pass
    return NIGHT_TZ


def _clock_line(tz: str, now: Optional[datetime] = None) -> str:
    """One line telling the agent its local time and the server's day/night
    verdict, so idle policies need not hardcode a zone (`TZ=... date`)."""
    when = "NIGHT" if _is_night(now, tz) else "DAY"
    try:
        from zoneinfo import ZoneInfo
        hhmm = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except Exception:
        hhmm = "??:??"
    return (f"Your local time: {hhmm} ({tz}) — {when} "
            f"(night = {NIGHT_START_HOUR:02d}:00–{NIGHT_END_HOUR:02d}:00).")


def _server_idle_policy(db, project: str) -> Optional[str]:
    """push_control idle_park_policy (project override, else server default):
    the idle policy for projects without their own park_on_idle_nudge
    guideline — one setting covers a server with many projects."""
    try:
        from shared_memory.push_control import get_effective_config
        rule = get_effective_config(db, project).get("idle_park_policy")
    except Exception:
        return None
    return rule.strip() if isinstance(rule, str) and rule.strip() else None


def nudge_text(db, project: str, minutes: int, agent: Optional[str] = None,
               now: Optional[datetime] = None) -> Tuple[str, str]:
    """(subject, body) for an idle nudge to `agent` in `project`."""
    subject = NUDGE_SUBJECT.format(minutes=minutes)
    tz = agent_timezone(db, project, agent)
    clock = "\n\n" + _clock_line(tz, now)
    if _is_night(now, tz) and _on_autoreset(db, project, agent):
        tokens = _reported_context_tokens(db, agent)
        context = f" (your context: ~{tokens // 1000}K tokens)" if tokens else ""
        return subject, NUDGE_AUTORESET_BODY.format(minutes=minutes, context=context) + clock
    rule = _project_idle_policy(db, project)
    if rule:
        return subject, NUDGE_POLICY_BODY.format(
            minutes=minutes, guideline=POLICY_GUIDELINE, rule=rule) + clock
    rule = _server_idle_policy(db, project)
    if rule:
        return subject, NUDGE_POLICY_BODY.format(
            minutes=minutes, guideline="push_control idle_park_policy", rule=rule) + clock
    return subject, NUDGE_BODY.format(minutes=minutes) + clock


def _has_pending_self_wake(db, project: str, agent: str, now_wall=None) -> bool:
    """True if the agent has a pending reminder due within SELF_WAKE_WINDOW_SECONDS."""
    from datetime import datetime, timedelta, timezone

    now_wall = now_wall or datetime.now(timezone.utc)
    try:
        doc = db.scheduled_messages.find_one({
            "agent_instance": agent,
            "agent_project": project,
            "status": "pending",
            "deliver_at": {"$lte": now_wall + timedelta(seconds=SELF_WAKE_WINDOW_SECONDS)},
        }, {"_id": 1})
    except Exception:
        return False
    return doc is not None


async def _send_nudge(db, project: str, agent: str, minutes: int) -> Optional[str]:
    from shared_memory.helpers import utc_now
    from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send

    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    subject, body = nudge_text(db, project, minutes, agent)
    doc = {
        "_id": message_id,
        "to_instance": agent,
        "to_project": project,
        "from_instance": "system",
        "from_project": project,
        "from_session": None,
        "subject": subject,
        "message": body,
        "priority": "normal",
        "category": "info",
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
        "system_notice_kind": "idle_park_nudge",
        "created_at": utc_now(),
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }
    db.messages.insert_one(doc)
    try:
        await _notify_inbox_for_send(project, agent, _build_announce_packet(doc))
    except Exception as e:
        # Message is persisted; pull-side delivery still works.
        log.warning("idle-park: push failed for %s/%s: %s", agent, project, e)
    return message_id


async def sweep_once(db) -> int:
    """Nudge every due agent once. Returns the number of nudges sent."""
    if db is None:
        return 0
    drain_parked(db)
    sent = 0
    for project, agent, minutes in due_agents(lambda p, a: _threshold_from_config(db, p, a)):
        # Mark first: a failed insert must not turn into a nudge every minute.
        mark_nudged(project, agent)
        if _has_pending_self_wake(db, project, agent):
            log.info("idle-park: skipped %s/%s, own reminder due within %ss",
                     agent, project, SELF_WAKE_WINDOW_SECONDS)
            continue
        try:
            msg_id = await _send_nudge(db, project, agent, minutes)
            sent += 1
            log.info("idle-park: nudged %s/%s after %s min idle (%s)", agent, project, minutes, msg_id)
        except Exception as e:
            log.error("idle-park: nudge failed for %s/%s: %s", agent, project, e)
    return sent


async def _sweep_loop() -> None:
    from shared_memory.clients import get_mongo

    log.info("idle-park: started (sweep=%ss, rearm_grace=%ss)", SWEEP_INTERVAL_SECONDS, REARM_GRACE_SECONDS)
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await sweep_once(get_mongo())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("idle-park: loop error: %s", e)


def start_idle_park() -> None:
    """Start the sweep task. Idempotent if already running."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="idle_park_sweep")
    log.info("idle-park: background task created")


def build_call_tool_handler_with_idle_park(orig_handler):
    """Wrap the CallToolRequest handler to feed note_tool_call."""

    async def wrapped(req):
        try:
            args = getattr(req.params, "arguments", None)
            session_id = args.get("session_id") if isinstance(args, dict) else None
            note_tool_call(getattr(req.params, "name", ""), session_id)
        except Exception as e:  # bookkeeping must never break a tool call
            log.debug("idle-park: note_tool_call failed: %s", e)
        return await orig_handler(req)

    return wrapped
