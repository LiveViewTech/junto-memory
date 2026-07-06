"""Push control read-side tools — `design:push-control-v0` v1.1.0 §5, §7.

Exposes two MCP tools for the claudeControl dashboard:
  - memory_list_alerts: list recent alerts, filterable by ack state.
  - memory_get_emission_stats: per-agent current-hour emission count +
    config thresholds, for the per-agent counter display.

Write-side operations (config CRUD, alert ack, agent unsuspend) live under
`memory_admin` sub-actions in tools/admin.py — they are owner/operator-tier
and gated behind the existing admin auth surface.
"""

import json
from datetime import timedelta

from mcp.server.fastmcp import Context

from shared_memory import push_control
from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import normalize_project, require_session, utc_now


@mcp.tool()
async def memory_list_alerts(
    session_id: str,
    unacknowledged_only: bool = False,
    project: str = None,
    limit: int = 50,
    ctx: Context = None,
) -> str:
    """List push-control alerts, newest first.

    Designed for claudeControl's alert dashboard. Default behavior returns
    the most recent 50 alerts regardless of ack state — claudeControl uses
    `unacknowledged_only=True` for the active-incident feed.

    Args:
        session_id: Your session ID.
        unacknowledged_only: Return only alerts with acknowledged=False.
            Use this for the polling fallback that catches missed webhooks.
        project: Filter to alerts for agents in this project.
        limit: Max alerts to return (default 50, capped at 200).

    Auth: any agent or user-tier session can call. There is no per-agent
    filtering — alerts are operator-facing data, not message-bus content.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    limit = max(1, min(int(limit), 200))
    alerts = push_control.list_alerts(
        db=db,
        unacknowledged_only=bool(unacknowledged_only),
        project=project,
        limit=limit,
    )

    # Trim sample bodies for transport (full bodies are in the alert doc,
    # accessible via direct Mongo if needed). 200 chars per sample is plenty
    # for dashboard preview.
    for a in alerts:
        for s in a.get("sample_messages", []) or []:
            body = s.get("message", "")
            if isinstance(body, str) and len(body) > 200:
                s["message"] = body[:200] + "…"
            ts = s.get("created_at")
            if hasattr(ts, "isoformat"):
                s["created_at"] = ts.isoformat()

    return json.dumps({
        "count": len(alerts),
        "alerts": alerts,
    }, default=str)


@mcp.tool()
async def memory_get_emission_stats(
    session_id: str,
    agent: str = None,
    project: str = None,
    history_days: int = 0,
    ctx: Context = None,
) -> str:
    """Current-hour emission count + thresholds for one or all agents.

    Designed for the claudeControl per-agent counter dashboard (§5). Returns
    a list of `{instance, project, hour, count, depth_cap, push_budget,
    hard_ceiling, suspended}` entries.

    Args:
        session_id: Your session ID.
        agent: Filter to one specific agent (omit for all current-hour senders).
        project: Filter to senders in one project (omit for all).
        history_days: When > 0, also return limit-watch history from the
            emission_history collection (design:limit-watch-v0): per-sender
            hourly peaks for the last N days (capped at 90 — the collection
            TTL), as `history` (raw hourly rows, newest first, capped 500)
            plus `history_summary` (per-sender: hours_active, max_peak,
            hours_warned, hours_breached, total_suppressed). This is the
            limit-TUNING view: peaks near push_budget across many hours mean
            the budget is throttling real traffic; empty history means the
            limits aren't being approached at all.

    Auth: any agent or user-tier session can call. Counters are not
    sensitive — they describe message volume, not message content.
    """
    error = require_session(session_id)
    if error:
        return error

    db = get_mongo()

    snapshot = push_control.snapshot_emission_counters()
    out = []
    for entry in snapshot:
        if agent and entry["instance"] != agent:
            continue
        if project and entry["project"] != project:
            continue
        # Resolve the agent's effective config (depth_cap/push_budget/hard_ceiling
        # are project-scoped; suspension is per-agent). PyMongo Database does
        # not implement __bool__, so `is not None` is required.
        cfg = push_control.get_effective_config(db, entry["project"]) if db is not None else {}
        suspended = push_control.is_agent_suspended(db, entry["project"], entry["instance"])
        out.append({
            **entry,
            "depth_cap": cfg.get("depth_cap"),
            "push_budget": cfg.get("push_budget"),
            "hard_ceiling": cfg.get("hard_ceiling"),
            "suspended": suspended,
            # Quick-render fields for the dashboard
            "over_push_budget": entry["count"] > (cfg.get("push_budget") or 9_999_999),
            "over_hard_ceiling": entry["count"] >= (cfg.get("hard_ceiling") or 9_999_999),
        })

    # ── (A) Synthetic zero-row for the idle case (design:autopilot-removal-v0 §5) ──
    # When an explicit agent= + project= query matches no current-hour emission
    # row (the common IDLE statusline case), the in-process counters carry no
    # caps — caps ride on the row. Return a zero-row resolved from project config
    # so the idle chip still shows live "0/budget (ceiling)" + a real suspended
    # flag, instead of an empty list (which would force the client to hardcode
    # caps or blank the denominator). `suspended` is resolved from the suspension
    # store, NOT assumed false: a suspended agent stops sending and naturally
    # falls to 0 current-hour emissions, so the idle case INCLUDES suspended
    # agents — hardcoding false here would hide a live suspension on the chip.
    # Gated on BOTH agent and project being explicit: config is project-scoped,
    # so an agent-only query can't resolve which project's caps to synthesize.
    if not out and agent and project and db is not None:
        cfg = push_control.get_effective_config(db, project)
        out.append({
            "instance": agent,
            "project": normalize_project(project),
            "hour": push_control._current_hour_bucket(),
            "count": 0,
            "depth_cap": cfg.get("depth_cap"),
            "push_budget": cfg.get("push_budget"),
            "hard_ceiling": cfg.get("hard_ceiling"),
            "suspended": push_control.is_agent_suspended(db, project, agent),
            "over_push_budget": False,
            "over_hard_ceiling": False,
        })

    # Stable sort: most-emitting agents first.
    out.sort(key=lambda x: -x["count"])

    payload = {
        "count": len(out),
        "stats": out,
    }

    # ── Limit-watch history (design:limit-watch-v0) ──
    if history_days and int(history_days) > 0 and db is not None:
        days = min(int(history_days), 90)
        cutoff = utc_now() - timedelta(days=days)
        hq = {"hour_start": {"$gte": cutoff}}
        if agent:
            hq["instance"] = agent
        if project:
            hq["project"] = normalize_project(project)
        try:
            rows = list(
                db.emission_history.find(hq, {"_id": 0})
                .sort("hour_start", -1)
                .limit(500)
            )
        except Exception as e:
            rows = []
            payload["history_error"] = str(e)

        summary = {}
        for r in rows:
            k = (r.get("instance"), r.get("project"))
            s = summary.setdefault(
                k,
                {
                    "instance": r.get("instance"),
                    "project": r.get("project"),
                    "hours_active": 0,
                    "max_peak": 0,
                    "hours_warned": 0,
                    "hours_breached": 0,
                    "total_suppressed": 0,
                },
            )
            s["hours_active"] += 1
            s["max_peak"] = max(s["max_peak"], int(r.get("peak_count", 0)))
            s["hours_warned"] += 1 if r.get("warn_alerted") else 0
            s["hours_breached"] += 1 if r.get("breach_alerted") else 0
            s["total_suppressed"] += int(r.get("suppressed", 0))

        payload["history_days"] = days
        payload["history"] = rows
        payload["history_truncated"] = len(rows) == 500
        payload["history_summary"] = sorted(
            summary.values(), key=lambda s: -s["max_peak"]
        )

    return json.dumps(payload, default=str)
