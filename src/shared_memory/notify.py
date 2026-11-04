"""Human notifications (interface:human-notify-v0).

Junto queues a message for a PERSON; a notify runner on that person's own
machine (logged into their own Claude account) claims it and sends it
through their approved Claude connector (Gmail, Slack, ...) with a headless
`claude -p`. The server never talks to Slack or mail itself.

Collections:
  humans         {_id: name, channels: {gmail: addr, slack: id, ...},
                  preferred, quiet_hours "22:00-07:00", timezone,
                  verified: {channel: iso}, runner: {host, last_seen},
                  created_by, updated_at}
  notifications  {_id, to, channel, subject, body, urgency, status
                  queued|claimed|sent|failed, attempts, lease_until,
                  sender: {agent, project, kind}, provider_id, error,
                  created_at, sent_at, test}

Open by design (Tom 2026-10-30): any session may notify any registered
person, and the sender writes the content. Destinations are only ever the
person's registered channels, and each person's runner enforces its OWN
local allowlist on top, so a registry edit cannot redirect mail.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from shared_memory.helpers import utc_now

HUMANS = "humans"
NOTIFICATIONS = "notifications"
BODY_CAP = 3000
DAILY_CAP = 20                # immediate sends per recipient per UTC day
DEDUP_WINDOW = timedelta(hours=1)
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
CHANNELS = ("gmail", "slack", "email")
URGENCIES = ("normal", "urgent")

_indexed = False


def _ensure_indexes(db) -> None:
    global _indexed
    if _indexed or db is None:
        return
    db[NOTIFICATIONS].create_index([("to", 1), ("status", 1), ("created_at", 1)])
    db[NOTIFICATIONS].create_index("created_at", expireAfterSeconds=90 * 24 * 3600)
    _indexed = True


def parse_target(to: str) -> Optional[str]:
    """'human:tom' -> 'tom'; anything else -> None."""
    if isinstance(to, str) and to.startswith("human:") and len(to) > 6:
        return to[6:].strip().lower()
    return None


def register_human(db, name: str, actor: str, actor_role: str, channels: Dict[str, str],
                   preferred: Optional[str] = None, quiet_hours: Optional[str] = None,
                   timezone: Optional[str] = None) -> Dict[str, Any]:
    name = (name or "").strip().lower()
    if not name:
        return {"error": "name required"}
    bad = [c for c in channels if c not in CHANNELS]
    if bad:
        return {"error": f"unknown channel(s) {bad}; use {list(CHANNELS)}"}
    existing = db[HUMANS].find_one({"_id": name})
    if existing and existing.get("created_by") not in (actor, None) and actor_role not in ("admin", "owner", "user"):
        return {"error": f"'{name}' was registered by {existing.get('created_by')}; only they, a human or an admin can change it"}
    if timezone:
        try:
            ZoneInfo(timezone)
        except Exception:
            return {"error": f"unknown timezone {timezone!r}"}
    now = utc_now()
    update: Dict[str, Any] = {"updated_at": now}
    for ch, dest in channels.items():
        if dest:
            update[f"channels.{ch}"] = dest.strip()
            # A changed destination must be re-verified.
            if not existing or (existing.get("channels") or {}).get(ch) != dest.strip():
                update[f"verified.{ch}"] = None
    if preferred:
        update["preferred"] = preferred
    if quiet_hours is not None:
        update["quiet_hours"] = quiet_hours or None
    if timezone:
        update["timezone"] = timezone
    db[HUMANS].update_one({"_id": name}, {"$set": update,
                                          "$setOnInsert": {"created_by": actor, "created_at": now}}, upsert=True)
    return {"status": "registered", "human": public_human(db[HUMANS].find_one({"_id": name}))}


def public_human(h: Optional[Dict]) -> Optional[Dict]:
    if not h:
        return None
    return {"name": h["_id"], "channels": sorted((h.get("channels") or {}).keys()),
            "preferred": h.get("preferred"), "verified": {k: v for k, v in (h.get("verified") or {}).items() if v},
            "quiet_hours": h.get("quiet_hours"), "timezone": h.get("timezone"),
            "runner": h.get("runner")}


def _pick_channel(h: Dict, requested: str) -> Optional[str]:
    chans = h.get("channels") or {}
    if requested and requested != "auto":
        return requested if requested in chans else None
    if h.get("preferred") in chans:
        return h["preferred"]
    return next(iter(sorted(chans)), None)


def enqueue(db, to: str, subject: str, body: str, sender: Dict[str, Any], urgency: str = "normal",
            channel: str = "auto", session_id: Optional[str] = None, test: bool = False) -> Dict[str, Any]:
    _ensure_indexes(db)
    name = parse_target(to) or (to or "").strip().lower()
    h = db[HUMANS].find_one({"_id": name})
    if not h:
        return {"error": f"no registered human '{name}'. Register with memory_notify(action='register')."}
    if urgency not in URGENCIES:
        return {"error": f"urgency must be one of {list(URGENCIES)}"}
    ch = _pick_channel(h, channel)
    if not ch:
        return {"error": f"'{name}' has no channel {channel!r}; registered: {sorted((h.get('channels') or {}).keys())}"}
    if not (subject or "").strip() or not (body or "").strip():
        return {"error": "subject and body are required"}

    from shared_memory.redact import redact_fields
    (subject, body), redacted = redact_fields(subject, body, tool="memory_notify", session_id=session_id,
                                              fields=["subject", "body"])
    truncated = len(body) > BODY_CAP
    now = utc_now()
    nid = f"ntf_{uuid.uuid4().hex[:12]}"
    full_body = body if truncated else None
    if truncated:
        body = body[:BODY_CAP] + f"\n\n[truncated; full text: memory_notify(action='status', notification_id='{nid}')]"

    if not test:
        dup = db[NOTIFICATIONS].find_one({
            "to": name, "subject": subject, "sender.agent": sender.get("agent"),
            "status": {"$ne": "failed"}, "created_at": {"$gte": now - DEDUP_WINDOW}})
        if dup:
            return {"status": "duplicate", "notification_id": dup["_id"],
                    "note": "same sender + subject to this person within the last hour; not queued again"}
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if db[NOTIFICATIONS].count_documents({"to": name, "created_at": {"$gte": day_start},
                                              "test": {"$ne": True}}) >= DAILY_CAP:
            return {"error": f"daily cap ({DAILY_CAP}) reached for '{name}'; batch into one message or a digest"}

    db[NOTIFICATIONS].insert_one({
        "_id": nid, "to": name, "channel": ch, "subject": subject, "body": body, "full_body": full_body,
        "urgency": urgency, "status": "queued", "attempts": 0, "lease_until": None, "sender": sender,
        "provider_id": None, "error": None, "created_at": now, "sent_at": None, "test": bool(test),
    })
    out = {"status": "queued", "notification_id": nid, "to": name, "channel": ch}
    if not (h.get("verified") or {}).get(ch):
        out["warning"] = (f"'{name}' has not verified channel '{ch}' yet (no successful test send). "
                          "It is queued anyway; it goes out when their notify runner is running.")
    if not (h.get("runner") or {}).get("last_seen"):
        out["warning"] = (out.get("warning", "") + f" No notify runner has checked in for '{name}' yet.").strip()
    if redacted:
        out["redacted"] = redacted
    if truncated:
        out["truncated"] = True
    return out


def _in_quiet_hours(h: Dict, now: datetime) -> bool:
    qh = h.get("quiet_hours")
    if not qh or "-" not in qh:
        return False
    try:
        start, end = (datetime.strptime(x.strip(), "%H:%M").time() for x in qh.split("-", 1))
        local = now.astimezone(ZoneInfo(h.get("timezone") or "America/Chicago")).time()
    except Exception:
        return False
    return (start <= local < end) if start < end else (local >= start or local < end)


def claim(db, name: str, runner_host: str, limit: int = 5) -> Dict[str, Any]:
    """Runner side: lease up to `limit` due notifications for `name`."""
    _ensure_indexes(db)
    name = (name or "").strip().lower()
    h = db[HUMANS].find_one({"_id": name})
    if not h:
        return {"error": f"no registered human '{name}'"}
    now = utc_now()
    db[HUMANS].update_one({"_id": name}, {"$set": {"runner": {"host": runner_host, "last_seen": now}}})
    q: Dict[str, Any] = {"to": name, "$or": [{"status": "queued"},
                                             {"status": "claimed", "lease_until": {"$lt": now}}]}
    if _in_quiet_hours(h, now):
        q["urgency"] = "urgent"
    rows = []
    for n in db[NOTIFICATIONS].find(q).sort("created_at", 1).limit(max(1, min(limit, 20))):
        res = db[NOTIFICATIONS].update_one(
            {"_id": n["_id"], "status": n["status"], "lease_until": n.get("lease_until")},
            {"$set": {"status": "claimed", "lease_until": now + LEASE, "claimed_by": runner_host},
             "$inc": {"attempts": 1}})
        if res.modified_count:
            rows.append({"notification_id": n["_id"], "channel": n["channel"],
                         "destination": (h.get("channels") or {}).get(n["channel"]),
                         "subject": n["subject"], "body": n["body"], "urgency": n["urgency"],
                         "test": n.get("test", False), "sender": n.get("sender")})
    return {"human": name, "notifications": rows, "quiet_hours_active": "urgency" in q}


def report(db, notification_id: str, ok: bool, provider_id: Optional[str] = None,
           error: Optional[str] = None) -> Dict[str, Any]:
    n = db[NOTIFICATIONS].find_one({"_id": notification_id})
    if not n:
        return {"error": f"unknown notification {notification_id}"}
    now = utc_now()
    if ok:
        db[NOTIFICATIONS].update_one({"_id": n["_id"]}, {"$set": {
            "status": "sent", "sent_at": now, "provider_id": provider_id, "lease_until": None, "error": None}})
        if n.get("test"):
            db[HUMANS].update_one({"_id": n["to"]}, {"$set": {f"verified.{n['channel']}": now.isoformat()}})
        return {"status": "sent", "notification_id": n["_id"]}
    final = n.get("attempts", 0) >= MAX_ATTEMPTS
    db[NOTIFICATIONS].update_one({"_id": n["_id"]}, {"$set": {
        "status": "failed" if final else "queued", "error": (error or "unknown")[:500], "lease_until": None}})
    return {"status": "failed" if final else "requeued", "notification_id": n["_id"], "final": final,
            "sender": n.get("sender"), "to": n["to"], "subject": n["subject"], "error": error}


def status(db, to: Optional[str] = None, notification_id: Optional[str] = None,
           limit: int = 20) -> Dict[str, Any]:
    if notification_id:
        n = db[NOTIFICATIONS].find_one({"_id": notification_id})
        return {"notification": _public_ntf(n, full=True)} if n else {"error": "not found"}
    q = {"to": (parse_target(to) or to).lower()} if to else {}
    rows = [_public_ntf(n) for n in db[NOTIFICATIONS].find(q).sort("created_at", -1).limit(max(1, min(limit, 100)))]
    return {"notifications": rows}


def _public_ntf(n: Dict, full: bool = False) -> Dict:
    out = {k: n.get(k) for k in ("_id", "to", "channel", "subject", "urgency", "status", "attempts",
                                 "provider_id", "error", "sender", "test")}
    out["created_at"] = n.get("created_at").isoformat() if n.get("created_at") else None
    out["sent_at"] = n.get("sent_at").isoformat() if n.get("sent_at") else None
    if full:
        out["body"] = n.get("full_body") or n.get("body")
    return out


def list_humans(db) -> List[Dict]:
    return [public_human(h) for h in db[HUMANS].find().sort("_id", 1)]
