"""Push control — server-side gating per `design:push-control-v0` v1.1.0.

Three control layers, all invisible to agents:
  1. Push depth cap   — per-thread, flat ceiling (no reset)
  2. Push budget      — per-sender, hourly, soft (suppress push only)
  3. Hard ceiling     — per-sender, hourly, hard (suspend agent + alert)

Send always persists; push is the controlled action. A message that is "sent
but not pushed" sits in the receiver's normal inbox and is filtered out of
the channel-push delivery path (inbox resource read) until the recipient's
recency window opens.

Counters live in-process — no Mongo persistence. They reset on server restart
which is acceptable: a confused agent that survives a restart starts fresh,
and an unconfused agent never trips anyway.

Config is owner-tier operator config, stored in `push_control_config` Mongo
collection. Per-project overrides on top of a server-level default.

Alerts are durable in the `alerts` Mongo collection. The alert channel is
out-of-band by construction (separate collection, separate transport via
webhook) so it survives suspension of the message bus that just tripped.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from shared_memory.audit import log_audit
from shared_memory.helpers import normalize_project, utc_now

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Defaults (numbers locked by v1.1.0 §13, validated against n=383 archive)
# ──────────────────────────────────────────────────────────────────────────

DEFAULT_DEPTH_CAP = 12
DEFAULT_PUSH_BUDGET = 30           # per sender per rolling hour
DEFAULT_HARD_CEILING = 100         # per sender per rolling hour
DEFAULT_RECOVERY_BEHAVIOR = "annotated"   # annotated | quarantine | leave_it
DEFAULT_INCIDENT_PAD_MESSAGES = 12         # min(pad_msgs, pad_seconds)
DEFAULT_INCIDENT_PAD_SECONDS = 300
DEFAULT_WARN_FRACTION = 0.8        # budget_warn alert at this fraction of push_budget
EMISSION_HISTORY_TTL_DAYS = 90     # hourly peak docs age out after this

VALID_RECOVERY_BEHAVIORS = {"annotated", "quarantine", "leave_it"}

# Recognized config keys for memory_admin push_control_set_config.
# Unknown keys are rejected to avoid silent typos in operator commands.
CONFIG_KEYS = {
    "depth_cap": int,
    "push_budget": int,
    "hard_ceiling": int,
    "recovery_behavior": str,
    "incident_pad_messages": int,
    "incident_pad_seconds": int,
    "webhook_url": str,
    "webhook_token": str,
    "warn_fraction": float,
}

# Default scope key — sentinel id for the server-level default in
# push_control_config. Real project names are normalized lowercase and never
# start with "__", so this won't collide.
DEFAULT_SCOPE = "__default__"


# ──────────────────────────────────────────────────────────────────────────
# In-process emission counter
# ──────────────────────────────────────────────────────────────────────────
# Map (sender_instance, sender_project) -> (hour_iso_bucket, count). Single
# entry per agent: when the hour rolls over, the entry is overwritten on the
# next send. No background cleanup needed.

_emission_counters: Dict[Tuple[str, str], Tuple[str, int]] = {}


def _current_hour_bucket() -> str:
    """Bucket key for the current UTC hour, e.g. '2026-05-19T15'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def increment_emission(sender_instance: str, sender_project: str) -> int:
    """Increment the sender's current-hour emission count. Returns new count.

    System sources (sender_instance == 'system') are NOT counted — per
    §6 "System-generated messages are excluded from budget accounting."
    Returns 0 in that case without storing anything.
    """
    if not sender_instance or sender_instance == "system":
        return 0
    project = normalize_project(sender_project) or ""
    key = (sender_instance, project)
    bucket = _current_hour_bucket()
    hour, count = _emission_counters.get(key, (bucket, 0))
    if hour != bucket:
        # Hour rolled, reset
        count = 0
        hour = bucket
    count += 1
    _emission_counters[key] = (hour, count)
    return count


def get_emission_count(sender_instance: str, sender_project: str) -> int:
    """Read the sender's current-hour emission count (without incrementing).

    Stale entries from previous hours return 0.
    """
    if not sender_instance:
        return 0
    project = normalize_project(sender_project) or ""
    key = (sender_instance, project)
    bucket = _current_hour_bucket()
    hour, count = _emission_counters.get(key, (bucket, 0))
    if hour != bucket:
        return 0
    return count


def reset_emission_counters() -> None:
    """Clear all in-process counters. Test/admin escape hatch."""
    _emission_counters.clear()


def snapshot_emission_counters() -> List[Dict[str, Any]]:
    """Read-only snapshot of all current-hour counters (debug/diagnostics).

    Returns a list of {instance, project, hour, count} for the current
    hour bucket only — stale buckets are filtered out.
    """
    bucket = _current_hour_bucket()
    out: List[Dict[str, Any]] = []
    for (instance, project), (hour, count) in _emission_counters.items():
        if hour != bucket:
            continue
        out.append({
            "instance": instance,
            "project": project,
            "hour": hour,
            "count": count,
        })
    return out


def record_emission_history(
    db,
    sender_instance: str,
    sender_project: str,
    count: int,
    suppressed: bool,
    cfg: Dict[str, Any],
) -> None:
    """Persist the sender's hourly peak + fire once-per-hour proximity alerts.

    Limit-watch (design:limit-watch-v0): the in-process counters are wiped on
    restart and remember only the current hour, so limit-tuning ("are 30/100
    right?") has no data. This writes one emission_history doc per
    (sender, hour) — $max peak_count, $inc sends/suppressed — and writes a
    durable alert the FIRST time the sender crosses (a) warn_fraction of
    push_budget ("budget_warn") and (b) push_budget itself
    ("push_budget_breach", the moment silent soft-containment starts). Dedup
    is durable: a flag on the history doc, flipped with a guarded update, so
    each fires at most once per sender-hour even across server restarts.
    Counts at/above hard_ceiling are handle_hard_trip's job — skipped here.
    Best-effort: never raises, never blocks the send path. System senders
    (count==0) are excluded, matching increment_emission.
    """
    if db is None or not sender_instance or sender_instance == "system" or count <= 0:
        return
    try:
        project = normalize_project(sender_project) or ""
        bucket = _current_hour_bucket()
        hour_start = datetime.strptime(bucket, "%Y-%m-%dT%H").replace(
            tzinfo=timezone.utc
        )
        key = {"instance": sender_instance, "project": project, "hour": bucket}
        db.emission_history.update_one(
            key,
            {
                "$max": {"peak_count": count},
                "$inc": {"sends": 1, "suppressed": 1 if suppressed else 0},
                "$setOnInsert": {"hour_start": hour_start},
            },
            upsert=True,
        )

        budget = int(cfg.get("push_budget", DEFAULT_PUSH_BUDGET))
        ceiling = int(cfg.get("hard_ceiling", DEFAULT_HARD_CEILING))
        try:
            warn_fraction = float(cfg.get("warn_fraction", DEFAULT_WARN_FRACTION))
        except (TypeError, ValueError):
            warn_fraction = DEFAULT_WARN_FRACTION
        warn_at = max(1, int(budget * warn_fraction))

        trigger = None
        if budget < count < ceiling:
            flag, trigger = "breach_alerted", "push_budget_breach"
            explainer = (
                f"{sender_instance}@{project} crossed push_budget: {count} sends "
                f"this hour vs budget {budget} (hard_ceiling {ceiling}). Pushes "
                f"are now silently soft-suppressed. If this traffic is "
                f"legitimate, the budget may need raising — see emission_history."
            )
        elif warn_at <= count <= budget:
            flag, trigger = "warn_alerted", "budget_warn"
            explainer = (
                f"{sender_instance}@{project} at {count}/{budget} sends this "
                f"hour ({int(warn_fraction * 100)}% warn threshold, hard_ceiling "
                f"{ceiling}). Approaching soft suppression."
            )
        if trigger:
            res = db.emission_history.update_one(
                {**key, flag: {"$ne": True}}, {"$set": {flag: True}}
            )
            if getattr(res, "modified_count", 0) == 1:
                write_alert(
                    db,
                    agent_instance=sender_instance,
                    agent_project=project,
                    trigger=trigger,
                    prior_hour_message_count=count,
                    window_start=hour_start,
                    window_end=utc_now(),
                    recipient_set=[],
                    shape="proximity",
                    shape_explainer=explainer,
                    sample_messages=[],
                )
    except Exception as e:
        log.error("push_control: record_emission_history failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────
# Config storage (Mongo, owner-tier read/write through memory_admin)
# ──────────────────────────────────────────────────────────────────────────

def init_push_control_indexes(db) -> None:
    """Register indexes on push_control_config and alerts collections.

    Called once from clients.py during MongoDB initialization. Idempotent —
    pymongo create_index() no-ops on an existing index with the same key spec.
    """
    if db is None:
        return

    cfg = db.push_control_config
    cfg.create_index("scope", unique=True)
    # Future: if we ever want to query overrides by project quickly.
    cfg.create_index("project")

    alerts = db.alerts
    alerts.create_index("agent_instance")
    alerts.create_index("agent_project")
    alerts.create_index([("acknowledged", 1), ("created_at", -1)])
    alerts.create_index("created_at")
    # Trigger lookups for dashboards.
    alerts.create_index([("trigger", 1), ("created_at", -1)])

    # Limit-watch hourly peaks (design:limit-watch-v0). One doc per
    # (sender, hour); TTL ages them out so the collection stays bounded.
    hist = db.emission_history
    hist.create_index(
        [("instance", 1), ("project", 1), ("hour", 1)], unique=True
    )
    hist.create_index(
        "hour_start", expireAfterSeconds=EMISSION_HISTORY_TTL_DAYS * 24 * 3600
    )
    hist.create_index([("project", 1), ("hour_start", -1)])


def _default_config_dict() -> Dict[str, Any]:
    return {
        "depth_cap": DEFAULT_DEPTH_CAP,
        "push_budget": DEFAULT_PUSH_BUDGET,
        "hard_ceiling": DEFAULT_HARD_CEILING,
        "recovery_behavior": DEFAULT_RECOVERY_BEHAVIOR,
        "incident_pad_messages": DEFAULT_INCIDENT_PAD_MESSAGES,
        "incident_pad_seconds": DEFAULT_INCIDENT_PAD_SECONDS,
        "webhook_url": None,
        "webhook_token": None,
        "warn_fraction": DEFAULT_WARN_FRACTION,
    }


def _read_config_doc(db, scope: str) -> Dict[str, Any]:
    """Read one config doc by scope id. Returns {} if missing or db unavailable."""
    if db is None:
        return {}
    try:
        doc = db.push_control_config.find_one({"scope": scope}) or {}
    except Exception:
        return {}
    return doc


def get_effective_config(db, project: Optional[str] = None) -> Dict[str, Any]:
    """Return effective config for a project: server default + project override.

    Per §13: server-level default applies everywhere; per-project override
    replaces individual keys. Returns a fully-populated dict, never None.

    Includes a `_sources` field per key indicating where the value came from
    ("default" or "project") for transparency in get_config display.
    """
    base = _default_config_dict()
    sources: Dict[str, str] = {k: "code_default" for k in base}

    db_default = _read_config_doc(db, DEFAULT_SCOPE)
    for k in list(base.keys()):
        if k in db_default and db_default[k] is not None:
            base[k] = db_default[k]
            sources[k] = "server_default"

    if project:
        norm = normalize_project(project)
        override = _read_config_doc(db, norm)
        for k in list(base.keys()):
            if k in override and override[k] is not None:
                base[k] = override[k]
                sources[k] = f"project:{norm}"

    base["_sources"] = sources
    base["_project"] = normalize_project(project) if project else None
    return base


def set_config_value(db, project: Optional[str], key: str, value: Any, actor: str) -> Dict[str, Any]:
    """Upsert a single config key in the appropriate scope.

    project=None writes to the server default; otherwise to the project
    override. Validates key and coerces the value to the declared type.
    Returns {ok: True, scope, key, value} on success or {error: ...}.
    """
    if key not in CONFIG_KEYS:
        return {"error": f"unknown config key '{key}'; valid keys: {sorted(CONFIG_KEYS)}"}

    declared_type = CONFIG_KEYS[key]
    if value is not None:
        try:
            value = declared_type(value)
        except (TypeError, ValueError):
            return {"error": f"value for {key!r} must be {declared_type.__name__}"}

    # Domain validation
    if key == "recovery_behavior":
        if value not in VALID_RECOVERY_BEHAVIORS:
            return {"error": f"recovery_behavior must be one of {sorted(VALID_RECOVERY_BEHAVIORS)}"}
    if key in ("depth_cap", "push_budget", "hard_ceiling",
               "incident_pad_messages", "incident_pad_seconds") and isinstance(value, int):
        if value < 1:
            return {"error": f"{key} must be >= 1"}
    if key == "warn_fraction" and isinstance(value, float):
        if not (0.0 < value < 1.0):
            return {"error": "warn_fraction must be between 0 and 1 (exclusive)"}
    if key == "hard_ceiling" and isinstance(value, int):
        # Sanity: hard_ceiling should be >= push_budget. Read the would-be effective
        # push_budget after this write would land.
        eff = get_effective_config(db, project)
        eff_push_budget = eff["push_budget"]
        if value < eff_push_budget:
            return {"error": f"hard_ceiling ({value}) must be >= push_budget ({eff_push_budget})"}

    if db is None:
        return {"error": "MongoDB unavailable"}

    scope = DEFAULT_SCOPE if project is None else normalize_project(project)
    now = utc_now()
    try:
        db.push_control_config.update_one(
            {"scope": scope},
            {"$set": {
                "scope": scope,
                "project": None if scope == DEFAULT_SCOPE else scope,
                key: value,
                "updated_at": now,
                "updated_by": actor,
            }},
            upsert=True,
        )
    except Exception as e:
        return {"error": f"write failed: {e}"}

    try:
        log_audit(
            "push_control.config_set",
            actor=actor,
            project=scope if scope != DEFAULT_SCOPE else "",
            details={"key": key, "value": value, "scope": scope},
        )
    except Exception:
        pass

    return {"ok": True, "scope": scope, "key": key, "value": value}


def reset_config(db, project: Optional[str], key: Optional[str], actor: str) -> Dict[str, Any]:
    """Drop a per-project override.

    project=None is invalid here (you can't 'reset' the server default — set
    it explicitly). key=None drops ALL overrides for the project (delete doc).
    key=<name> unsets that one field.
    """
    if project is None:
        return {"error": "reset_config requires a project (server default cannot be reset)"}
    if db is None:
        return {"error": "MongoDB unavailable"}

    scope = normalize_project(project)
    now = utc_now()
    if key is None:
        try:
            result = db.push_control_config.delete_one({"scope": scope})
        except Exception as e:
            return {"error": f"delete failed: {e}"}
        try:
            log_audit(
                "push_control.config_reset_all",
                actor=actor,
                project=scope,
                details={"scope": scope, "deleted_count": result.deleted_count},
            )
        except Exception:
            pass
        return {"ok": True, "scope": scope, "deleted": result.deleted_count > 0}

    if key not in CONFIG_KEYS:
        return {"error": f"unknown config key '{key}'"}

    try:
        result = db.push_control_config.update_one(
            {"scope": scope},
            {"$unset": {key: ""}, "$set": {"updated_at": now, "updated_by": actor}},
        )
    except Exception as e:
        return {"error": f"unset failed: {e}"}

    try:
        log_audit(
            "push_control.config_reset_key",
            actor=actor,
            project=scope,
            details={"scope": scope, "key": key, "matched": result.matched_count},
        )
    except Exception:
        pass

    return {"ok": True, "scope": scope, "key": key, "unset": result.matched_count > 0}


# ──────────────────────────────────────────────────────────────────────────
# Agent suspension
# ──────────────────────────────────────────────────────────────────────────

def is_agent_suspended(db, project: str, agent: str) -> bool:
    """True iff the agent's registered_agents doc has suspended=True.

    Missing doc or missing field treated as False (the only failure-safe
    default for a safety mechanism). Read is cheap and on the hot send path,
    so kept as a single find_one with projection.
    """
    if db is None or not project or not agent:
        return False
    try:
        doc = db.registered_agents.find_one(
            {"project": normalize_project(project), "name": agent},
            {"suspended": 1},
        )
    except Exception:
        return False
    if not doc:
        return False
    return bool(doc.get("suspended", False))


def set_agent_suspended(
    db,
    project: str,
    agent: str,
    suspended: bool,
    reason: str = "",
    actor: str = "system",
) -> bool:
    """Flip the suspended flag on registered_agents. Best-effort; True on success."""
    if db is None or not project or not agent:
        return False
    norm = normalize_project(project)
    now = utc_now()
    update: Dict[str, Any] = {
        "suspended": bool(suspended),
        "suspended_at": now if suspended else None,
        "suspended_reason": reason if suspended else "",
        "suspended_by": actor if suspended else "",
    }
    try:
        result = db.registered_agents.update_one(
            {"project": norm, "name": agent},
            {"$set": update},
        )
    except Exception as e:
        log.warning("push_control: set_agent_suspended failed: %s", e)
        return False

    try:
        log_audit(
            "push_control.agent_suspended" if suspended else "push_control.agent_unsuspended",
            actor=actor,
            project=norm,
            details={"agent": agent, "reason": reason},
        )
    except Exception:
        pass

    return result.matched_count > 0


# ──────────────────────────────────────────────────────────────────────────
# Suppression evaluation (send-side gate)
# ──────────────────────────────────────────────────────────────────────────

def evaluate_send(
    db,
    sender_instance: str,
    sender_project: str,
    chain_depth: int,
    recipient_instance: str,
    recipient_project: str,
    recency_bypass: bool,
) -> Dict[str, Any]:
    """Decide whether to push this send.

    Returns a dict with:
      - suppress: bool — true if any soft or hard limit says don't push
      - reason: str|None — one of None / 'depth_cap' / 'push_budget' /
        'hard_ceiling' / 'agent_suspended'
      - emission_count: int — sender's current-hour count AFTER this send
                              (incremented inside this call)
      - hard_trip: bool — true iff this send is the one that crossed the
                          hard ceiling for the first time this hour
      - effective_config: dict — the config that applied (sender's project)

    The caller is responsible for actually persisting the message (we do not);
    we just compute the gating decision and bump the in-process counter.

    System-source sends (sender_instance == 'system') are unconditionally
    *not suppressed* and *not counted* per §6 — they are non-pushing-by-
    construction at a higher layer (the caller chooses not to push them).
    """
    # System sends bypass entirely — caller decides delivery semantics.
    if sender_instance == "system":
        return {
            "suppress": False,
            "reason": None,
            "emission_count": 0,
            "hard_trip": False,
            "effective_config": get_effective_config(db, sender_project),
        }

    cfg = get_effective_config(db, sender_project)

    # If sender is suspended, suppress unconditionally. No counter increment —
    # the agent is already in the bounded state.
    if is_agent_suspended(db, sender_project, sender_instance):
        return {
            "suppress": True,
            "reason": "agent_suspended",
            "emission_count": get_emission_count(sender_instance, sender_project),
            "hard_trip": False,
            "effective_config": cfg,
        }

    # Recipient suspended? Don't push to a suspended agent either —
    # per §7 suspension stops both directions.
    if recipient_instance and recipient_instance != "*":
        if is_agent_suspended(db, recipient_project, recipient_instance):
            # Count the send (it still happened) but suppress the push.
            new_count = increment_emission(sender_instance, sender_project)
            return {
                "suppress": True,
                "reason": "recipient_suspended",
                "emission_count": new_count,
                "hard_trip": False,
                "effective_config": cfg,
            }

    # Increment first — an emission is an emission whether it was pushed
    # (§11 "yes — counted at the time the server decides not to push").
    new_count = increment_emission(sender_instance, sender_project)

    # Layer 1: depth cap. Per §6 the cap does NOT consult human-presence,
    # but the existing messaging.py recency_bypass logic still applies to
    # the *legacy* hard cap of 5; we preserve it here only as an explicit
    # override the caller can pass. For the new depth_cap (12), no bypass.
    if chain_depth > cfg["depth_cap"]:
        return {
            "suppress": True,
            "reason": "depth_cap",
            "emission_count": new_count,
            "hard_trip": False,
            "effective_config": cfg,
        }

    # Layer 3: hard ceiling — checked before push_budget so we set hard_trip
    # only on the very first crossing in this hour bucket.
    hard_ceiling = cfg["hard_ceiling"]
    push_budget = cfg["push_budget"]
    if new_count >= hard_ceiling:
        # hard_trip is True only at the precise crossing — subsequent sends
        # in the same hour are post-trip noise.
        hard_trip = (new_count == hard_ceiling)
        return {
            "suppress": True,
            "reason": "hard_ceiling",
            "emission_count": new_count,
            "hard_trip": hard_trip,
            "effective_config": cfg,
        }

    # Layer 2: push budget (soft).
    if new_count > push_budget:
        return {
            "suppress": True,
            "reason": "push_budget",
            "emission_count": new_count,
            "hard_trip": False,
            "effective_config": cfg,
        }

    return {
        "suppress": False,
        "reason": None,
        "emission_count": new_count,
        "hard_trip": False,
        "effective_config": cfg,
    }


# ──────────────────────────────────────────────────────────────────────────
# Recipient-side push filter (delivery-time)
# ──────────────────────────────────────────────────────────────────────────

def recency_window_open(db, project: str, agent: str) -> bool:
    """Is the recipient's recency window currently open?

    Mirrors messaging.py:_has_recent_human_interaction without importing it
    (avoid circular import — messaging.py imports from us once we wire 1c in).
    Lazily re-implements: read agent_directory.last_human_interaction, compare
    age to HUMAN_RECENCY_WINDOW_SECONDS (5 min).
    """
    if db is None or not project or not agent:
        return False
    try:
        doc = db.agent_directory.find_one(
            {"project": normalize_project(project), "instance": agent},
            {"last_human_interaction": 1},
        )
    except Exception:
        return False
    if not doc:
        return False
    last = doc.get("last_human_interaction")
    if last is None:
        return False
    # Normalize naive timestamps to UTC (helpers.parse_timestamp would do this
    # but is not in our imports to keep them tight).
    if isinstance(last, datetime):
        last_dt = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
    else:
        try:
            last_dt = datetime.fromisoformat(str(last))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return False
    age = (utc_now() - last_dt).total_seconds()
    return age <= 300  # HUMAN_RECENCY_WINDOW_SECONDS


def should_deliver_via_push_filter(db, project: str, agent: str) -> bool:
    """Should the inbox-resource read return push_suppressed messages?

    True iff recency window is open. When False (closed), the read_inbox
    handler filters push_suppressed=true messages from the response so the
    plugin-poll surface does not pump them as live channel notifications.
    When True (open), they are released — the human is presumed to be at
    the terminal and will see them as part of inbox triage.

    This is the §3 release mechanism — reuses existing recency-bypass
    infrastructure (agent_directory.last_human_interaction).
    """
    return recency_window_open(db, project, agent)


# ──────────────────────────────────────────────────────────────────────────
# Alert write (durable, out-of-band from message bus)
# ──────────────────────────────────────────────────────────────────────────

def write_alert(
    db,
    agent_instance: str,
    agent_project: str,
    trigger: str,
    prior_hour_message_count: int,
    window_start: datetime,
    window_end: datetime,
    recipient_set: List[str],
    shape: str,
    shape_explainer: str,
    sample_messages: List[Dict[str, Any]],
    peer_notice_inserted: bool = False,
) -> Optional[str]:
    """Insert an alert doc into the alerts collection. Returns alert_id or None.

    Alert schema (per §7 required fields):
      _id, agent, agent_instance, agent_project, trigger,
      prior_hour_message_count, window_start, window_end, recipient_set,
      shape, shape_explainer, sample_messages, peer_notice_inserted,
      acknowledged, acknowledged_at, acknowledged_by, created_at,
      webhook_fired_at, webhook_status
    """
    if db is None:
        log.error("push_control: write_alert called with db=None")
        return None

    alert_id = f"alert_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    norm_project = normalize_project(agent_project)
    doc = {
        "_id": alert_id,
        "agent": f"{agent_instance}@{norm_project}",
        "agent_instance": agent_instance,
        "agent_project": norm_project,
        "trigger": trigger,
        "prior_hour_message_count": prior_hour_message_count,
        "window_start": window_start,
        "window_end": window_end,
        "recipient_set": recipient_set,
        "shape": shape,
        "shape_explainer": shape_explainer,
        "sample_messages": sample_messages,
        "peer_notice_inserted": peer_notice_inserted,
        "acknowledged": False,
        "acknowledged_at": None,
        "acknowledged_by": None,
        "created_at": now,
        "webhook_fired_at": None,
        "webhook_status": None,
    }
    try:
        db.alerts.insert_one(doc)
    except Exception as e:
        log.error("push_control: alert insert failed: %s", e)
        return None
    try:
        log_audit(
            "push_control.alert_fired",
            actor="system",
            project=norm_project,
            details={
                "alert_id": alert_id,
                "agent": doc["agent"],
                "trigger": trigger,
                "count": prior_hour_message_count,
            },
        )
    except Exception:
        pass
    return alert_id


def acknowledge_alert(db, alert_id: str, actor: str) -> Dict[str, Any]:
    """Mark an alert acknowledged by `actor`. Does NOT unsuspend the agent.

    Per §7 'Ack ≠ unsuspend' — operator may ack and continue investigating.
    """
    if db is None:
        return {"error": "MongoDB unavailable"}
    try:
        result = db.alerts.update_one(
            {"_id": alert_id, "acknowledged": False},
            {"$set": {
                "acknowledged": True,
                "acknowledged_at": utc_now(),
                "acknowledged_by": actor,
            }},
        )
    except Exception as e:
        return {"error": str(e)}
    if result.matched_count == 0:
        return {"error": f"alert {alert_id} not found or already acknowledged"}
    try:
        log_audit(
            "push_control.alert_acknowledged",
            actor=actor,
            project="",
            details={"alert_id": alert_id},
        )
    except Exception:
        pass
    return {"ok": True, "alert_id": alert_id, "acknowledged_by": actor}


def list_alerts(
    db,
    unacknowledged_only: bool = False,
    project: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List recent alerts, newest first."""
    if db is None:
        return []
    query: Dict[str, Any] = {}
    if unacknowledged_only:
        query["acknowledged"] = False
    if project:
        query["agent_project"] = normalize_project(project)
    out: List[Dict[str, Any]] = []
    try:
        cursor = db.alerts.find(query).sort("created_at", -1).limit(max(1, int(limit)))
    except Exception as e:
        log.warning("push_control: list_alerts failed: %s", e)
        return []
    for doc in cursor:
        # Stringify datetimes for transport.
        for fld in ("created_at", "window_start", "window_end",
                    "acknowledged_at", "webhook_fired_at"):
            v = doc.get(fld)
            if hasattr(v, "isoformat"):
                doc[fld] = v.isoformat()
        out.append(doc)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Incident analysis (for hard-trip alerts and recovery notices)
# ──────────────────────────────────────────────────────────────────────────

def compute_incident_window(
    db,
    sender_instance: str,
    sender_project: str,
    trigger: str,
    trip_time: datetime,
    cfg: Dict[str, Any],
) -> Tuple[datetime, datetime, List[Dict[str, Any]]]:
    """Determine the incident window per §8.

    For depth-cap trip: window is the whole thread (not implemented in v0 —
    we treat it the same as budget for now and color the look-back; the
    thread-coloring path comes when depth-cap recovery is wired up).

    For budget/ceiling trip: look-back from the first soft-limit trip,
    sized `min(depth_cap msgs, 5 minutes real time)` of sender's output.

    Returns (window_start, window_end, sample_messages).
    """
    if db is None:
        return trip_time, trip_time, []

    norm_project = normalize_project(sender_project)
    pad_msgs = int(cfg.get("incident_pad_messages", DEFAULT_INCIDENT_PAD_MESSAGES))
    pad_seconds = int(cfg.get("incident_pad_seconds", DEFAULT_INCIDENT_PAD_SECONDS))

    seconds_floor = trip_time - timedelta(seconds=pad_seconds)
    try:
        cursor = (
            db.messages.find(
                {
                    "from_instance": sender_instance,
                    "from_project": norm_project,
                    "created_at": {"$lte": trip_time},
                },
                {
                    "_id": 1, "to_instance": 1, "to_project": 1,
                    "message": 1, "created_at": 1, "chain_depth": 1,
                },
            )
            .sort("created_at", -1)
            .limit(pad_msgs)
        )
        recent = list(cursor)
    except Exception as e:
        log.warning("push_control: compute_incident_window read failed: %s", e)
        return seconds_floor, trip_time, []

    # Filter to seconds_floor — whichever bound is tighter wins.
    in_window: List[Dict[str, Any]] = []
    for d in recent:
        ts = d.get("created_at")
        if isinstance(ts, datetime):
            ts_dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            continue
        if ts_dt >= seconds_floor:
            in_window.append(d)
    in_window.reverse()  # oldest → newest

    if in_window:
        window_start = in_window[0]["created_at"]
        if isinstance(window_start, datetime) and window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
    else:
        window_start = seconds_floor

    samples: List[Dict[str, Any]] = []
    # Take up to 5 representative messages, prefer evenly spaced through the window.
    if in_window:
        step = max(1, len(in_window) // 5)
        for d in in_window[::step][:5]:
            body = d.get("message", "")
            if isinstance(body, str) and len(body) > 500:
                body = body[:500] + "…"
            samples.append({
                "id": d["_id"],
                "to": f"{d.get('to_instance', '?')}@{d.get('to_project', '?')}",
                "chain_depth": int(d.get("chain_depth", 0)),
                "created_at": d.get("created_at"),
                "message": body,
            })

    return window_start, trip_time, samples


def classify_incident_shape(samples: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Return (shape, explainer) per §7 — 'identical_repeating' vs 'varied'.

    Heuristic: if ≥75% of samples have identical message body, shape is
    'identical_repeating' (→ plumbing bug / delivery loop). Otherwise
    'varied' (→ likely model-side confusion).
    """
    if not samples:
        return "varied", "no samples available"
    bodies = [s.get("message", "") for s in samples]
    counts: Dict[str, int] = {}
    for b in bodies:
        counts[b] = counts.get(b, 0) + 1
    most_common_count = max(counts.values()) if counts else 0
    ratio = most_common_count / len(bodies)
    if ratio >= 0.75 and most_common_count >= 2:
        return "identical_repeating", (
            f"{most_common_count}/{len(bodies)} sample messages have identical body"
        )
    distinct = len(counts)
    return "varied", f"{distinct} distinct bodies across {len(bodies)} samples"


def recipient_set_from_samples(samples: List[Dict[str, Any]]) -> List[str]:
    """Distinct recipients in the incident window, sorted for stable output."""
    seen = set()
    for s in samples:
        to = s.get("to")
        if to:
            seen.add(to)
    return sorted(seen)


# ──────────────────────────────────────────────────────────────────────────
# Recovery notice insertion (§8)
# ──────────────────────────────────────────────────────────────────────────

def _format_recovery_notice_body(
    suspended_instance: str,
    suspended_project: str,
    trigger: str,
    count: int,
    cfg: Dict[str, Any],
    shape: str,
    shape_explainer: str,
    window_start: datetime,
    window_end: datetime,
    recipient_set: List[str],
) -> str:
    """Plain-language self-contained notice body per §8.

    An agent with no concept of "spiral" or "push control" must understand
    this from the text alone — no jargon, no rule, just facts + guidance.
    """
    def _fmt(dt):
        if hasattr(dt, "isoformat"):
            return dt.isoformat()
        return str(dt)

    suspended = f"{suspended_instance}@{suspended_project}"
    threshold_word = {
        "hard_ceiling": f"the per-sender hourly ceiling ({cfg.get('hard_ceiling', DEFAULT_HARD_CEILING)})",
        "push_budget": f"the per-sender hourly push budget ({cfg.get('push_budget', DEFAULT_PUSH_BUDGET)})",
        "depth_cap": f"the per-thread depth cap ({cfg.get('depth_cap', DEFAULT_DEPTH_CAP)})",
    }.get(trigger, "a push-control threshold")

    shape_para = ""
    if shape == "identical_repeating":
        shape_para = (
            "The messages were near-identical — a strong signal this was a "
            "delivery loop or replay bug in the message plumbing rather than "
            "model-side confusion."
        )
    elif shape == "varied":
        shape_para = (
            "The messages varied in content — more consistent with model-side "
            "confusion than a plumbing replay."
        )

    recipients_line = ""
    if recipient_set:
        recipients_line = "Recipients in the incident window: " + ", ".join(recipient_set) + ".\n"

    body = (
        f"SYSTEM NOTICE — push control activated for {suspended}.\n\n"
        f"Between {_fmt(window_start)} and {_fmt(window_end)}, {suspended} "
        f"emitted {count} messages in a one-hour window, exceeding "
        f"{threshold_word}. Push notifications for {suspended} have been "
        f"suspended pending operator review.\n\n"
        f"Incident shape: {shape}. {shape_explainer}\n"
        f"{shape_para}\n\n"
        f"{recipients_line}"
        f"\nThe messages that follow this notice in your inbox arrived during "
        f"the suspected malfunction. They may reflect state from before it, "
        f"or repeat work already done. Evaluate each for current relevance "
        f"against your state spec before acting on it.\n\n"
        f"This notice was inserted by the system at incident close. "
        f"{suspended} has been suspended pending fresh-session recovery. "
        f"No action is required from you unless the messages below "
        f"specifically request it."
    )
    return body


def _insert_one_notice(
    db,
    recipient_instance: str,
    recipient_project: str,
    from_project: str,
    body: str,
    position: datetime,
) -> Optional[str]:
    """Insert one system@junto notice into a single recipient's inbox.

    Notice is non-pushing by construction (push_suppressed=True). It is
    inserted with a created_at set to `position`, which the caller chooses
    to be slightly earlier than the first incident-window message so that
    on a newest-first inbox sort, the notice immediately precedes them.

    Returns the inserted notice id or None on failure.
    """
    if db is None or not recipient_instance:
        return None
    notice_id = f"msg_{uuid.uuid4().hex[:12]}"
    norm_recipient_project = normalize_project(recipient_project) or recipient_project
    norm_from_project = normalize_project(from_project) or from_project
    doc = {
        "_id": notice_id,
        "to_instance": recipient_instance,
        "to_project": norm_recipient_project,
        "from_instance": "system",
        "from_project": norm_from_project,
        "from_session": None,
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
        # Non-pushing by construction (§8).
        "push_suppressed": True,
        "push_suppress_reason": "system_notice",
        "emission_count": 0,
        "recency_bypass": False,
        "is_system_notice": True,
        "system_notice_kind": "push_control.recovery",
        "created_at": position,
        "delivered_at": None,
        "received_at": None,
        "completed_at": None,
    }
    try:
        db.messages.insert_one(doc)
    except Exception as e:
        log.warning("push_control: notice insert failed for %s@%s: %s",
                    recipient_instance, recipient_project, e)
        return None
    return notice_id


def insert_recovery_notice(
    db,
    suspended_instance: str,
    suspended_project: str,
    samples: List[Dict[str, Any]],
    shape: str,
    shape_explainer: str,
    window_start: datetime,
    window_end: datetime,
    trigger: str,
    count: int,
    cfg: Dict[str, Any],
) -> List[str]:
    """Insert system@junto recovery notices into BOTH endpoints' inboxes.

    Per §8: at incident-close, insert a synthetic notice into the inbox of
    BOTH endpoints of the spiral — the suspended agent AND every peer in
    the incident window — positioned immediately ahead of the incident
    messages. The peer is not collateral; per §8 it is a primary recipient
    because it is most exposed to acting on spiral garbage.

    Returns list of inserted notice IDs (one per recipient).
    """
    if db is None or not suspended_instance:
        return []

    norm_project = normalize_project(suspended_project)
    recipient_set = recipient_set_from_samples(samples)

    # Position the notice one second before the earliest incident message
    # so on newest-first sort it sits immediately ahead of them.
    if isinstance(window_start, datetime):
        ws = window_start if window_start.tzinfo else window_start.replace(tzinfo=timezone.utc)
    else:
        ws = utc_now()
    notice_position = ws - timedelta(seconds=1)

    body = _format_recovery_notice_body(
        suspended_instance=suspended_instance,
        suspended_project=norm_project,
        trigger=trigger,
        count=count,
        cfg=cfg,
        shape=shape,
        shape_explainer=shape_explainer,
        window_start=ws,
        window_end=window_end,
        recipient_set=recipient_set,
    )

    inserted: List[str] = []

    # Notice to the suspended agent itself
    self_id = _insert_one_notice(
        db,
        recipient_instance=suspended_instance,
        recipient_project=norm_project,
        from_project=norm_project,
        body=body,
        position=notice_position,
    )
    if self_id:
        inserted.append(self_id)

    # Notice to each peer (distinct instance@project pairs in incident window).
    peers = set()
    for s in samples:
        to = s.get("to", "")
        if isinstance(to, str) and "@" in to:
            inst, proj = to.split("@", 1)
            inst = inst.strip()
            proj = proj.strip()
            if not inst or not proj:
                continue
            # Don't notice self twice — already inserted above.
            if inst == suspended_instance and normalize_project(proj) == norm_project:
                continue
            peers.add((inst, normalize_project(proj)))

    for peer_instance, peer_project in peers:
        peer_id = _insert_one_notice(
            db,
            recipient_instance=peer_instance,
            recipient_project=peer_project,
            from_project=norm_project,
            body=body,
            position=notice_position,
        )
        if peer_id:
            inserted.append(peer_id)

    if inserted:
        try:
            log_audit(
                "push_control.recovery_notice",
                actor="system",
                project=norm_project,
                details={
                    "suspended_agent": f"{suspended_instance}@{norm_project}",
                    "notice_count": len(inserted),
                    "peer_count": len(peers),
                    "trigger": trigger,
                    "shape": shape,
                },
            )
        except Exception:
            pass

    return inserted


# ──────────────────────────────────────────────────────────────────────────
# Webhook fire (out-of-band alert delivery)
# ──────────────────────────────────────────────────────────────────────────

async def fire_alert_webhook(
    db,
    alert_id: str,
    webhook_url: str,
    webhook_token: Optional[str],
    payload: Dict[str, Any],
) -> None:
    """Async POST the alert payload to the configured webhook.

    Best-effort: failures do not lose the alert (already persisted to
    `alerts` collection). Updates the alert row with `webhook_fired_at` +
    `webhook_status` on completion. Short timeout (10s) — claudeControl is
    expected to respond fast; if it does not, the polling fallback at
    `memory_list_alerts(unacknowledged=True)` catches the alert anyway.
    """
    if not webhook_url or not alert_id:
        return

    headers = {"Content-Type": "application/json"}
    if webhook_token:
        headers["Authorization"] = f"Bearer {webhook_token}"

    status: str
    try:
        import httpx  # lazy import — only needed on the rare hard-trip path
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
            status = f"http_{resp.status_code}"
    except Exception as e:
        status = f"failed:{type(e).__name__}:{str(e)[:160]}"

    if db is not None:
        try:
            db.alerts.update_one(
                {"_id": alert_id},
                {"$set": {
                    "webhook_fired_at": utc_now(),
                    "webhook_status": status,
                }},
            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Hard-trip orchestrator
# ──────────────────────────────────────────────────────────────────────────

def handle_hard_trip(
    db,
    sender_instance: str,
    sender_project: str,
    emission_count: int,
    trigger: str,
    trip_time: datetime,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Execute the full hard-ceiling response.

    Steps (per §7-§8):
      1. Compute incident window from message archive.
      2. Classify shape (identical_repeating vs varied).
      3. Write durable alert to `alerts` collection.
      4. Insert system@junto recovery notices into both endpoints' inboxes.
      5. Flip agent.suspended=True on the registered_agents doc.
      6. Schedule async webhook POST to claudeControl.

    Returns a dict with the resulting alert_id, notice IDs, and webhook
    schedule status. Caller logs the trip; this function is the
    one-shot orchestrator.
    """
    norm_project = normalize_project(sender_project)
    result: Dict[str, Any] = {
        "alert_id": None,
        "notice_ids": [],
        "webhook_scheduled": False,
        "suspended": False,
    }

    try:
        window_start, window_end, samples = compute_incident_window(
            db=db,
            sender_instance=sender_instance,
            sender_project=norm_project,
            trigger=trigger,
            trip_time=trip_time,
            cfg=cfg,
        )
    except Exception as e:
        log.error("push_control.handle_hard_trip: window compute failed: %s", e)
        window_start, window_end, samples = trip_time, trip_time, []

    shape, shape_explainer = classify_incident_shape(samples)
    recipient_set = recipient_set_from_samples(samples)

    # 1. Insert recovery notices first (so peer_notice_inserted on alert is accurate)
    try:
        notice_ids = insert_recovery_notice(
            db=db,
            suspended_instance=sender_instance,
            suspended_project=norm_project,
            samples=samples,
            shape=shape,
            shape_explainer=shape_explainer,
            window_start=window_start,
            window_end=window_end,
            trigger=trigger,
            count=emission_count,
            cfg=cfg,
        )
        result["notice_ids"] = notice_ids
    except Exception as e:
        log.error("push_control.handle_hard_trip: notice insert failed: %s", e)
        notice_ids = []

    # 2. Write alert (durable record)
    try:
        alert_id = write_alert(
            db=db,
            agent_instance=sender_instance,
            agent_project=norm_project,
            trigger=trigger,
            prior_hour_message_count=emission_count,
            window_start=window_start,
            window_end=window_end,
            recipient_set=recipient_set,
            shape=shape,
            shape_explainer=shape_explainer,
            sample_messages=samples,
            peer_notice_inserted=bool(notice_ids),
        )
        result["alert_id"] = alert_id
    except Exception as e:
        log.error("push_control.handle_hard_trip: alert write failed: %s", e)
        alert_id = None

    # 3. Suspend the agent
    try:
        suspended_ok = set_agent_suspended(
            db=db,
            project=norm_project,
            agent=sender_instance,
            suspended=True,
            reason=f"{trigger}: {emission_count} sends in current hour",
            actor="system",
        )
        result["suspended"] = suspended_ok
    except Exception as e:
        log.error("push_control.handle_hard_trip: suspend failed: %s", e)

    # 4. Schedule async webhook (fire-and-forget; alert persists either way)
    webhook_url = cfg.get("webhook_url")
    if alert_id and webhook_url:
        payload = {
            "alert_id": alert_id,
            "agent": f"{sender_instance}@{norm_project}",
            "agent_instance": sender_instance,
            "agent_project": norm_project,
            "trigger": trigger,
            "prior_hour_message_count": emission_count,
            "window_start": window_start.isoformat() if hasattr(window_start, "isoformat") else str(window_start),
            "window_end": window_end.isoformat() if hasattr(window_end, "isoformat") else str(window_end),
            "recipient_set": recipient_set,
            "shape": shape,
            "shape_explainer": shape_explainer,
            "peer_notice_inserted": bool(notice_ids),
            "fired_at": utc_now().isoformat(),
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(fire_alert_webhook(
                db=db,
                alert_id=alert_id,
                webhook_url=webhook_url,
                webhook_token=cfg.get("webhook_token"),
                payload=payload,
            ))
            result["webhook_scheduled"] = True
        except RuntimeError:
            # Not in an event loop (e.g., called from sync test). Skip.
            log.warning("push_control: webhook scheduling skipped — no event loop")

    return result
