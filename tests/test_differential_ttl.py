"""Differential-TTL regression (design:unified-messaging-v0 Stage 5 / lanes-C).

Replaces the old flat "every message expires at created+7d" with a per-doc
expire_at field driving a Mongo TTL index (expireAfterSeconds=0):

  - info / non-action       → expire_at = created + 48h at send (ages fast)
  - ACTION, unacked          → expire_at = null → NEVER ages (load-bearing: an
                               open task/question must not silently vanish)
  - ACTION, acked/resolved   → expire_at = created + 7d (set at the terminal
                               transition, not at send)

Plus the boot-time migration: drop the legacy created_at TTL index, backfill
expire_at = created+7d on existing docs (preserves old behavior — no early
deletion, no immortal docs), create the expire_at TTL index + a plain created_at
index for the recency sort.
"""

import json
from datetime import timedelta

from shared_memory.config import MESSAGE_ACTION_TTL_DAYS
from shared_memory.helpers import utc_now


class _Res:
    def __init__(self, matched):
        self.matched_count = matched


def _match_doc(d, filt):
    for k, v in filt.items():
        if k == "_id":
            continue
        if isinstance(v, dict):
            if "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
            elif "$exists" in v:
                if (k in d) != v["$exists"]:
                    return False
            else:
                return False
        else:
            if d.get(k) != v:
                return False
    return True


def _eval(expr, doc):
    if isinstance(expr, dict) and "$add" in expr:
        base, ms = expr["$add"]
        val = doc.get(base[1:]) if isinstance(base, str) and base.startswith("$") else base
        return val + timedelta(milliseconds=ms)
    return expr


class _FakeMessages:
    def __init__(self, docs):
        self.docs = {d["_id"]: dict(d) for d in docs}

    def find_one(self, filt, projection=None):
        d = self.docs.get(filt.get("_id"))
        if d is None or not _match_doc(d, filt):
            return None
        return dict(d)

    def update_one(self, filt, upd):
        d = self.docs.get(filt.get("_id"))
        if d is None or not _match_doc(d, filt):
            return _Res(0)
        if isinstance(upd, list):  # aggregation pipeline
            for stage in upd:
                for k, v in (stage.get("$set") or {}).items():
                    d[k] = _eval(v, d)
        else:
            for k, v in (upd.get("$set") or {}).items():
                d[k] = v
        return _Res(1)


class _FakeDB:
    def __init__(self, docs):
        self.messages = _FakeMessages(docs)


def _amsg(_id, category="task", expire_at=None, created_at=None, obligation="open"):
    return {
        "_id": _id,
        "to_instance": "alice",
        "to_project": "junto",
        "category": category,
        "status": "pending",
        "obligation": obligation,
        "expire_at": expire_at,
        "created_at": created_at or utc_now(),
    }


def _exp(db, _id):
    return db.messages.docs[_id].get("expire_at")


# ── _set_action_message_expiry ──

def test_set_expiry_on_unacked_action():
    from shared_memory.tools import messaging as m
    created = utc_now()
    db = _FakeDB([_amsg("a", category="task", created_at=created)])
    m._set_action_message_expiry(db, "a")
    exp = _exp(db, "a")
    assert exp is not None
    assert exp == created + timedelta(days=MESSAGE_ACTION_TTL_DAYS)


def test_set_expiry_is_idempotent():
    """Once expire_at is set, a second call must not move it (filter guards on
    expire_at:null)."""
    from shared_memory.tools import messaging as m
    created = utc_now()
    already = created + timedelta(days=99)
    db = _FakeDB([_amsg("a", category="task", expire_at=already, created_at=created)])
    m._set_action_message_expiry(db, "a")
    assert _exp(db, "a") == already  # untouched


def test_set_expiry_skips_info():
    """info is not an ACTION category — the helper's category guard excludes it
    (info already got its 48h expiry at send)."""
    from shared_memory.tools import messaging as m
    db = _FakeDB([_amsg("i", category="info", expire_at=None)])
    m._set_action_message_expiry(db, "i")
    assert _exp(db, "i") is None  # not matched → not set


# ── auto-ack (_advance_parent_obligation_on_reply) drives the expiry ──

def test_resolved_autoack_sets_expiry():
    from shared_memory.tools import messaging as m
    created = utc_now()
    db = _FakeDB([_amsg("q", category="question", created_at=created)])
    m._advance_parent_obligation_on_reply(db, "q", "alice", utc_now())
    assert db.messages.docs["q"]["obligation"] == "resolved"
    assert _exp(db, "q") == created + timedelta(days=MESSAGE_ACTION_TTL_DAYS)


def test_responded_autoack_does_not_set_expiry():
    """A task only goes 'responded' (still in the action lane) → must NOT age."""
    from shared_memory.tools import messaging as m
    db = _FakeDB([_amsg("t", category="task")])
    m._advance_parent_obligation_on_reply(db, "t", "alice", utc_now())
    assert db.messages.docs["t"]["obligation"] == "responded"
    assert _exp(db, "t") is None


# ── memory_update_message_status terminal transitions ──

def _session(monkeypatch, db, instance="alice", project="junto"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m
    sid = f"_test_ttl_{instance}_{id(db)}"
    active_sessions[sid] = {"role": "agent", "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: db)
    return sid, m, active_sessions


async def test_received_sets_expiry(monkeypatch):
    created = utc_now()
    db = _FakeDB([_amsg("t", category="task", created_at=created)])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        await m.memory_update_message_status(session_id=sid, message_id="t", status="received")
        assert _exp(db, "t") == created + timedelta(days=MESSAGE_ACTION_TTL_DAYS)
    finally:
        sessions.pop(sid, None)


async def test_resolved_verb_sets_expiry(monkeypatch):
    created = utc_now()
    db = _FakeDB([_amsg("t", category="task", obligation="responded", created_at=created)])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        await m.memory_update_message_status(session_id=sid, message_id="t", status="resolved")
        assert _exp(db, "t") == created + timedelta(days=MESSAGE_ACTION_TTL_DAYS)
    finally:
        sessions.pop(sid, None)


async def test_delivered_does_not_set_expiry(monkeypatch):
    """'delivered' ≠ acted-on → an action message stays unaged."""
    db = _FakeDB([_amsg("t", category="task")])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        await m.memory_update_message_status(session_id=sid, message_id="t", status="delivered")
        assert _exp(db, "t") is None
    finally:
        sessions.pop(sid, None)


# ── migration helper ──

class _FakeColl:
    def __init__(self, existing):
        self._idx = dict(existing)
        self.dropped, self.created, self.updated = [], [], []

    def index_information(self):
        return dict(self._idx)

    def drop_index(self, name):
        self.dropped.append(name)
        self._idx.pop(name, None)

    def create_index(self, keys, expireAfterSeconds=None):
        self.created.append((keys, expireAfterSeconds))

    def update_many(self, filt, upd):
        self.updated.append((filt, upd))


def test_migration_drops_legacy_and_builds_new():
    from shared_memory.clients import _migrate_messages_ttl
    coll = _FakeColl({"created_at_1": {"key": [("created_at", 1)], "expireAfterSeconds": 604800}})
    _migrate_messages_ttl(coll)
    assert "created_at_1" in coll.dropped
    assert ("expire_at", 0) in coll.created
    assert ("created_at", None) in coll.created
    assert len(coll.updated) == 1
    filt, upd = coll.updated[0]
    assert filt == {"expire_at": {"$exists": False}}
    assert isinstance(upd, list)  # aggregation-pipeline backfill


def test_migration_idempotent_when_already_migrated():
    """Second boot: legacy index already gone, expire_at index present. Must not
    try to drop a missing legacy TTL index."""
    from shared_memory.clients import _migrate_messages_ttl
    coll = _FakeColl({"expire_at_1": {"key": [("expire_at", 1)], "expireAfterSeconds": 0},
                      "created_at_1": {"key": [("created_at", 1)]}})  # plain, no TTL
    _migrate_messages_ttl(coll)
    assert coll.dropped == []  # created_at_1 has no expireAfterSeconds → not a legacy TTL
    assert ("expire_at", 0) in coll.created
