"""Mechanism B — pending-agent GC (design:identity-lifecycle-v0 v0.2.0).

Reaps PENDING-tier roster rows whose agent is genuinely gone, so auto-
registered one-shots (probes, temp helpers, mis-boots) stop accumulating as
addressable ghosts that silently swallow mis-routed messages.

Parameters as ratified by coordinator@nimbus (msg_7bfb4018d26e) + memory:
- PENDING TIER ONLY. named/admin/worker rows are never touched — the
  pending/named boundary is the destructive gate, which is why the fleet's
  legitimate long-livers were promoted to named before this shipped
  (2026-07-12, Tom-approved).
- NO immediate-reap on clean end_session (coordinator Flag 1, taken further:
  trigger-1 dropped entirely). ALL reaping goes through the grace scan, so a
  parked-not-dead pending agent is structurally safe. Correctness > promptness.
- GRACE: last_seen older than JUNTO_PENDING_REAP_GRACE_HOURS (default 48).
- LIVE-SESSION GUARD: never reap while any active session belongs to the
  agent (covers the long-running-but-quiet case).
- OBLIGATION GUARD (v1 = BLOCK, not orphan-resolve): never reap an agent
  with open inbound obligations — reaping would strand the waiter forever.
- SCAN, THROTTLED: runs at session start (the cleanup_stale_sessions moment),
  scoped to pending rows only, and does real work at most once per
  THROTTLE_MINUTES via a claimed last_swept marker.
- REAP = delete the registered_agents + agent_directory rows (addressability
  and roster presence). Artifacts (messages, learnings, specs, audit) are
  NEVER touched — agent ≠ output. A reaped name can freely re-register later.
- DEFAULT OFF (JUNTO_PENDING_REAP_ENABLED) — enable deliberately after the
  roster's long-livers are promoted.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

META_COLLECTION = "maintenance_meta"
META_ID = "pending_reap"
THROTTLE_MINUTES = 15


def reap_enabled() -> bool:
    return os.environ.get("JUNTO_PENDING_REAP_ENABLED", "false").lower() in (
        "1", "true", "yes",
    )


def _grace_hours() -> float:
    try:
        return float(os.environ.get("JUNTO_PENDING_REAP_GRACE_HOURS", "48"))
    except ValueError:
        return 48.0


def _as_utc(value) -> datetime:
    """Normalize mongo datetimes (naive UTC) / ISO strings to aware UTC.
    Unparseable/missing → datetime.min (ancient ⇒ grace-eligible)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.min.replace(tzinfo=timezone.utc)


def maybe_reap_pending_agents(db, active_sessions: Dict[str, dict]) -> List[dict]:
    """Throttled scan; returns the reaped [(project, name), ...] descriptors.
    Best-effort by design — callers wrap it; any exception here must never
    block a session start."""
    if db is None or not reap_enabled():
        return []

    now = datetime.now(timezone.utc)

    # Throttle: claim the sweep FIRST (upsert last_swept), so concurrent
    # session starts don't run duplicate scans.
    meta = db[META_COLLECTION].find_one({"_id": META_ID})
    if meta and _as_utc(meta.get("last_swept")) > now - timedelta(minutes=THROTTLE_MINUTES):
        return []
    db[META_COLLECTION].update_one(
        {"_id": META_ID}, {"$set": {"last_swept": now}}, upsert=True
    )

    live = {
        (info.get("project"), info.get("claude_instance"))
        for info in active_sessions.values()
    }
    cutoff = now - timedelta(hours=_grace_hours())

    reaped: List[dict] = []
    for row in list(db.registered_agents.find({"tier": "pending"})):
        proj, name = row.get("project"), row.get("name")
        if (proj, name) in live:
            continue
        # Grace check on last_seen, falling back to created_at: no current
        # path creates a pending row without last_seen (auto-register always
        # stamps it; add_agent rejects tier="pending"), but if one ever
        # appears, its AGE — not the field's absence — decides eligibility.
        # Both missing → ancient → eligible (that shape is precisely the
        # legacy seed-ghost class this reaper exists for).
        if _as_utc(row.get("last_seen") or row.get("created_at")) >= cutoff:
            continue
        # Open inbound obligations: someone is still waiting on this agent.
        # Reaping now would orphan their question/task/blocker forever.
        if db.messages.count_documents(
            {"to_instance": name, "to_project": proj, "obligation": "open"}
        ) > 0:
            continue

        db.registered_agents.delete_one({"project": proj, "name": name, "tier": "pending"})
        db.agent_directory.delete_many({"project": proj, "instance": name})
        reaped.append({"project": proj, "agent": name})
        try:
            from shared_memory.audit import log_audit
            log_audit("agent.reaped", name, proj, {
                "tier": "pending",
                "last_seen": str(row.get("last_seen")),
                "grace_hours": _grace_hours(),
            })
        except Exception:
            pass

    if reaped:
        logger.info("pending-agent GC reaped %d ghost(s): %s", len(reaped), reaped)
    return reaped


# ─────────────────────────────────────────────────────────────────────────────
# Assistant-class escheatment (design:assistant-escheat-v0, RATIFIED 2026-08-07)
#
# Human-companion chats (claude.ai app/web/cowork) register as
# `assistant<Project>-<topic>`; they send/receive/own like agents but die
# silently like workers. On idle past the windows: owned specs TRANSFER to
# the escheat target (14d), open action mail RE-HOMES as an ATOMIC MOVE (7d
# — new annotated doc + resolve original, cross-referenced; a copy that
# leaves the original open DOUBLES the deadlock and looks successful doing
# it). After both sweeps the ordinary pending-reaper takes the row —
# obligations were MOVED, so the guard passes with no changes.
# ─────────────────────────────────────────────────────────────────────────────

ESCHEAT_META_ID = "assistant_escheat"

_LEGACY_ASSISTANT_PREFIXES = ("tom-assistant", "claude-web", "claude-chat")


def _spec_escheat_days() -> float:
    try:
        return float(os.environ.get("ASSISTANT_SPEC_ESCHEAT_IDLE_DAYS", "14"))
    except ValueError:
        return 14.0


def _msg_escheat_days() -> float:
    try:
        return float(os.environ.get("ASSISTANT_MSG_ESCHEAT_IDLE_DAYS", "7"))
    except ValueError:
        return 7.0


def _resolve_escheat_target(db, project: str) -> str:
    """Escheat fallback chain (§9a): coordinator → first project admin →
    None (terminal: NO escheat, row is deliberately never reaped — the
    preserved deadlock is the right failure direction)."""
    coord = db.registered_agents.find_one(
        {"project": project, "name": "coordinator", "tier": {"$ne": "retired"}},
        {"name": 1},
    )
    if coord:
        return coord["name"]
    proj = db.projects.find_one({"name": project}, {"admins": 1})
    admins = (proj or {}).get("admins") or []
    return admins[0] if admins else None


def classify_assistant(db, project: str, instance: str):
    """Return the escheat target when `instance` is assistant-shaped in
    `project`, else None. Patterns (§2): `assistant<project>-<topic>`
    (case-insensitive on the project token, hyphens/underscores folded) plus
    the grandfathered legacy prefixes."""
    if not instance:
        return None
    low = instance.lower()
    proj_token = (project or "").replace("_", "").replace("-", "")
    is_assistant = False
    if low.startswith("assistant") and "-" in instance:
        head = low.split("-", 1)[0]  # e.g. "assistantnimbus"
        if head == f"assistant{proj_token}" or head == "assistant":
            is_assistant = True
    if any(low.startswith(p) for p in _LEGACY_ASSISTANT_PREFIXES):
        is_assistant = True
    if not is_assistant:
        return None
    return _resolve_escheat_target(db, project)


async def maybe_escheat_assistants(db, chroma, active_sessions: Dict[str, dict]) -> List[dict]:
    """Throttled escheat sweep over assistant-class rows. Returns action
    descriptors. Best-effort by design — callers wrap it; nothing here may
    block a session start."""
    if db is None or not reap_enabled():
        return []

    now = datetime.now(timezone.utc)
    meta = db[META_COLLECTION].find_one({"_id": ESCHEAT_META_ID})
    if meta and _as_utc(meta.get("last_swept")) > now - timedelta(minutes=THROTTLE_MINUTES):
        return []
    db[META_COLLECTION].update_one(
        {"_id": ESCHEAT_META_ID}, {"$set": {"last_swept": now}}, upsert=True
    )

    live = {
        (info.get("project"), info.get("claude_instance"))
        for info in active_sessions.values()
    }
    actions: List[dict] = []

    # Candidate rows: classified assistants + legacy-prefix rows not yet
    # classified (grandfathering at sweep time — §2; they won't re-register).
    seen_keys = set()
    candidates = list(db.registered_agents.find({"agent_class": "assistant"}))
    legacy_or = [{"name": {"$regex": f"^{p}"}} for p in _LEGACY_ASSISTANT_PREFIXES]
    for row in db.registered_agents.find({"$or": legacy_or, "agent_class": {"$exists": False}}):
        candidates.append(row)

    msg_cutoff = now - timedelta(days=_msg_escheat_days())
    spec_cutoff = now - timedelta(days=_spec_escheat_days())

    for row in candidates:
        proj, name = row.get("project"), row.get("name")
        if not proj or not name or (proj, name) in seen_keys:
            continue
        seen_keys.add((proj, name))
        if (proj, name) in live:
            continue
        if row.get("tier") == "retired":
            continue
        last_seen = _as_utc(row.get("last_seen") or row.get("created_at"))

        target = row.get("escheat_to") or _resolve_escheat_target(db, proj)
        if not target or target == name:
            continue  # §9a terminal: no escheat, row deliberately not reaped

        # Grandfather stamp for legacy rows entering the class now.
        if row.get("agent_class") != "assistant":
            db.registered_agents.update_one(
                {"project": proj, "name": name},
                {"$set": {"agent_class": "assistant", "escheat_to": target}},
            )

        # ── Mail sweep (7d): ATOMIC MOVE, idempotent via escheat_moved_to ──
        if last_seen < msg_cutoff:
            open_msgs = list(db.messages.find({
                "to_instance": name, "to_project": proj,
                "obligation": "open",
                "escheat_moved_to": {"$exists": False},
            }))
            for m in open_msgs:
                try:
                    new_id = f"msg_{uuid_hex12()}"
                    prov_line = (
                        f"[escheat] re-homed from '{name}'@{proj} "
                        f"(idle since {last_seen.date()}); original {m['_id']}. "
                        f"Dispersal or retire-unread per "
                        f"design:assistant-escheat-v0 §7."
                    )
                    new_doc = dict(m)
                    new_doc["_id"] = new_id
                    new_doc["to_instance"] = target
                    new_doc["escheat_original"] = m["_id"]
                    new_doc["escheat_from"] = name
                    new_doc["message"] = f"{prov_line}\n\n{m.get('message', '')}"
                    new_doc["created_at"] = now
                    new_doc["status"] = "pending"
                    db.messages.insert_one(new_doc)
                    db.messages.update_one(
                        {"_id": m["_id"]},
                        {"$set": {
                            "obligation": "resolved",
                            "resolved_at": now,
                            "escheat_moved_to": new_id,
                        }},
                    )
                    actions.append({"escheat_msg": m["_id"], "to": target, "new": new_id})
                except Exception:
                    logger.warning("escheat mail move failed for %s", m.get("_id"))

        # ── Spec sweep (14d): transfer with provenance-at-transfer (§7a) ──
        if chroma is not None and last_seen < spec_cutoff:
            try:
                from shared_memory.helpers import get_project_collection
                coll = await get_project_collection(chroma, proj)
                got = await coll.get(where={"spec_owner": name}, include=["metadatas"])
                dir_row = db.agent_directory.find_one(
                    {"project": proj, "instance": name},
                    {"role_description": 1, "last_task": 1},
                ) or {}
                for sid, smeta in zip(got.get("ids") or [], got.get("metadatas") or []):
                    smeta = smeta or {}
                    if smeta.get("spec_type") == "agent_state":
                        continue
                    # §9b: re-target to the most recent actual editor within
                    # the window when one exists; coordinator otherwise.
                    editor = smeta.get("updated_by")
                    spec_target = target
                    if editor and editor != name:
                        upd = _as_utc(smeta.get("updated"))
                        if upd > spec_cutoff:
                            spec_target = editor
                    smeta["spec_owner"] = spec_target
                    smeta["owner_transferred_from"] = name
                    smeta["owner_transferred_by"] = "escheat-sweep"
                    smeta["owner_transferred_at"] = now.isoformat()
                    smeta["prior_owner_role"] = dir_row.get("role_description", "")
                    smeta["prior_owner_last_task"] = dir_row.get("last_task", "")
                    await coll.update(ids=[sid], metadatas=[smeta])
                    actions.append({
                        "escheat_spec": smeta.get("spec_name") or sid,
                        "from": name, "to": spec_target,
                    })
            except Exception:
                logger.warning("escheat spec sweep failed for %s/%s", proj, name)

    for a in actions:
        try:
            from shared_memory.audit import log_audit
            log_audit("agent.escheat", a.get("from", ""), "", a)
        except Exception:
            pass
    if actions:
        logger.info("assistant escheat: %d action(s): %s", len(actions), actions)
    return actions


def uuid_hex12() -> str:
    import uuid as _uuid
    return _uuid.uuid4().hex[:12]
