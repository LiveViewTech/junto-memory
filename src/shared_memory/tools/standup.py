"""Standup rollup — auto-compiled team snapshot + Tom decision queue.

Read-only aggregation over data junto already stores (agent_state specs, the
registered-agent roster, the live-session map). Lets a human or agent read one
compact page of who's-doing-what / what's-blocked / what-needs-Tom without an
agent being awake to assemble it. backlog_0382aab1447f.
"""

import json
import re
from typing import Optional

from mcp.server.fastmcp import Context

from shared_memory.app import mcp
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.helpers import (
    get_project_collection,
    normalize_project,
    parse_timestamp,
    require_session,
    utc_now,
)
from shared_memory.state import active_sessions


def _extract_section(content: str, heading: str) -> Optional[str]:
    """Text under a '## <heading>' markdown section up to the next '## ' (or
    EOF). Case-insensitive on the heading; returns None if absent or empty."""
    if not content:
        return None
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s+|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if not m:
        return None
    return m.group(1).strip() or None


def _is_empty_marker(text: Optional[str]) -> bool:
    """True if a section value is effectively 'nothing' (None / 'None' / etc.)."""
    if not text:
        return True
    return text.strip().lower() in ("none", "none.", "n/a", "na", "-", "—")


@mcp.tool()
async def memory_standup(
    session_id: str,
    project: str,
    stale_hours: int = 24,
    include_idle: bool = False,
    ctx: Context = None,
) -> str:
    """
    Auto-compiled team standup for a project — no agent required to produce it.

    Reads every agent_state spec (state:<name>), the registered-agent roster,
    and the live-session map, then returns a compact snapshot:
      - agents[]: current_task, blockers, state_version, state_updated,
        state_age_hours, stale flag, status (working/parked/idle), tier,
        sunset, purpose
      - tom_decision_queue[]: aggregated "## Open Tom Decisions" items harvested
        from each state spec (the convention proposed in backlog_0382aab1447f)
      - blocked_agents[], stale_specs[], and a counts summary

    Read-only — pure aggregation, writes nothing.

    Args:
        session_id: Your session ID
        project: Project to roll up (e.g., "junto", "nimbus")
        stale_hours: Flag state specs not refreshed within this many hours
            (default 24).
        include_idle: When False (default), agents with no state spec and no
            live session are collapsed into an idle_agents count + name list
            instead of a full row each — this keeps the page compact on
            projects with many orphaned/pending registrations. Set True to emit
            a full row for every registered agent.
    """
    error = require_session(session_id)
    if error:
        return error

    project = normalize_project(project)
    now = utc_now()
    stale_cutoff = None
    if stale_hours and stale_hours > 0:
        from datetime import timedelta
        stale_cutoff = now - timedelta(hours=int(stale_hours))

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    # Roster: registered agents (name -> doc). Retired identities are
    # terminal (identity-lifecycle Mechanism C) — excluded from the standup
    # entirely; their archived state specs are already filtered by the
    # status=="active" check below.
    roster = {}
    try:
        for a in db.registered_agents.find(
            {"project": project, "tier": {"$ne": "retired"}}
        ):
            roster[a.get("name")] = a
    except Exception:
        pass

    # Live sessions for this project -> instance -> latest last_activity
    alive = {}
    for info in active_sessions.values():
        if info.get("project") != project:
            continue
        inst = info.get("claude_instance")
        if not inst:
            continue
        la = parse_timestamp(info.get("last_activity"))
        if inst not in alive or (la and (alive[inst] is None or la > alive[inst])):
            alive[inst] = la

    # agent_state specs from chroma (get-all + filter in Python — matches the
    # robust pattern memory_list_specs uses rather than relying on a chroma
    # `where` clause).
    state_specs = {}  # agent -> {version, updated, content}
    try:
        chroma = await get_chroma()
        col = await get_project_collection(chroma, project)
        res = await col.get(include=["metadatas", "documents"])
        metas = res.get("metadatas") or []
        docs = res.get("documents") or []
        for i, meta in enumerate(metas):
            if not meta or meta.get("type") != "spec" or meta.get("status") != "active":
                continue
            if meta.get("spec_type") != "agent_state":
                continue
            spec_name = meta.get("spec_name") or ""
            if not spec_name.startswith("state:"):
                continue
            agent = spec_name.split(":", 1)[1]
            state_specs[agent] = {
                "version": meta.get("spec_version"),
                "updated": meta.get("updated") or meta.get("created"),
                "content": docs[i] if i < len(docs) else "",
            }
    except Exception:
        pass

    # Union of everyone we know about: roster ∪ anyone with a state spec.
    all_agents = sorted(set(roster.keys()) | set(state_specs.keys()))

    agents_out = []
    idle_names = []
    blocked = []
    stale = []
    tom_queue = []
    counts = {"working": 0, "parked": 0, "idle": 0}

    for name in all_agents:
        reg = roster.get(name, {})
        spec = state_specs.get(name)

        current_task = blockers = version = updated = None
        age_hours = None
        is_stale = False

        if spec:
            version = spec["version"]
            updated = spec["updated"]
            content = spec["content"] or ""
            current_task = _extract_section(content, "Current Task")
            blk = _extract_section(content, "Blockers")
            blockers = None if _is_empty_marker(blk) else blk
            decisions = _extract_section(content, "Open Tom Decisions")
            if not _is_empty_marker(decisions):
                tom_queue.append({"agent": name, "items": decisions})
            ut = parse_timestamp(updated)
            if ut:
                age_hours = round((now - ut).total_seconds() / 3600, 1)
                if stale_cutoff and ut < stale_cutoff:
                    is_stale = True
                    stale.append({"agent": name, "age_hours": age_hours, "version": version})

        if name in alive:
            status = "working"
        elif spec:
            status = "parked"
        else:
            status = "idle/no-state"

        if status == "working":
            counts["working"] += 1
        elif status == "parked":
            counts["parked"] += 1
        else:
            counts["idle"] += 1

        # Collapse idle/no-state agents (orphaned/pending registrations) to a
        # name list unless explicitly requested — they have nothing to report
        # and would otherwise dominate the page on busy projects.
        if status == "idle/no-state" and not include_idle:
            idle_names.append(name)
            continue

        if blockers:
            blocked.append({"agent": name, "blockers": blockers})

        agents_out.append({
            "agent": name,
            "tier": reg.get("tier"),
            "role": reg.get("role_description") or "",
            "status": status,
            "current_task": current_task,
            "blockers": blockers,
            "state_version": version,
            "state_updated": updated,
            "state_age_hours": age_hours,
            "stale": is_stale,
            "sunset": reg.get("sunset"),
            "purpose": reg.get("purpose"),
        })

    # Sunset watch (identity-lifecycle Mechanism C): temp agents whose sunset
    # date is past or within 14 days — surfaced so retirement is a standup
    # line, not memory-of-a-human. Decommission stays deliberate (operator
    # runs memory_project action="decommission"); this only reminds.
    sunset_watch = []
    for a in roster.values():
        s = a.get("sunset")
        if not s:
            continue
        st = parse_timestamp(s) if isinstance(s, str) else s
        if st is None:
            continue
        if st.tzinfo is None:
            from datetime import timezone as _tz
            st = st.replace(tzinfo=_tz.utc)
        days_left = (st - now).total_seconds() / 86400
        if days_left <= 14:
            sunset_watch.append({
                "agent": a.get("name"),
                "sunset": str(s),
                "days_left": round(days_left, 1),
                "overdue": days_left < 0,
                "purpose": a.get("purpose"),
            })
    sunset_watch.sort(key=lambda w: w["days_left"])

    project_doc = None
    try:
        project_doc = db.projects.find_one({"name": project})
    except Exception:
        pass

    return json.dumps({
        "project": project,
        "owner": (project_doc or {}).get("owner"),
        "generated_at": now.isoformat(),
        "agent_count": len(all_agents),
        "summary": {
            "working": counts["working"],
            "parked": counts["parked"],
            "idle": counts["idle"],
            "blocked": len(blocked),
            "stale_specs": len(stale),
            "open_tom_decisions": len(tom_queue),
            "sunset_watch": len(sunset_watch),
        },
        "sunset_watch": sunset_watch,
        "tom_decision_queue": tom_queue,
        "blocked_agents": blocked,
        "stale_specs": stale,
        "agents": agents_out,
        "idle_agents": {
            "count": len(idle_names),
            "names": idle_names,
            "note": "registered but no state spec and no live session; pass include_idle=true for full rows",
        },
    }, indent=2, default=str)
