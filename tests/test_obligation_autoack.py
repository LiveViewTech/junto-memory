"""lanes-B / obligation-track regression (design:unified-messaging-v0 Stage 3).

A reply auto-acks its parent's OBLIGATION (a second axis, separate from the
delivery `status` field). Pins the contract:

  - owner_of(parent) := parent.owner ?? parent.to_instance  (generalized scope
    guard; `owner` is unset until Stage-2 claiming, so it falls back to the named
    recipient — forward-compatible, and identical to lanes-B reply.from==parent.to)
  - {question, contract, review} -> resolved   (an answer satisfies)
  - {task, blocker}              -> responded   (engaged; stays in the action lane)
  - info / non-action parents carry no obligation and are never touched
  - a reply from a NON-owner (or into a broadcast) never auto-acks (scope guard)
  - an already-resolved parent is never downgraded
  - the explicit "resolved"/"responded" verb on memory_update_message_status
    writes the obligation field, NOT the delivery status
"""

import json

from shared_memory.helpers import utc_now


class _FakeResult:
    def __init__(self, matched):
        self.matched_count = matched


class _FakeMessages:
    def __init__(self, docs):
        self.docs = {d["_id"]: dict(d) for d in docs}

    def find_one(self, filt, projection=None):
        d = self.docs.get(filt.get("_id"))
        return dict(d) if d else None

    def update_one(self, filt, upd):
        d = self.docs.get(filt.get("_id"))
        if d is None:
            return _FakeResult(0)
        for k, v in (upd.get("$set") or {}).items():
            d[k] = v
        return _FakeResult(1)

    def insert_one(self, doc, session=None):
        self.docs[doc["_id"]] = dict(doc)


class _FakeDB:
    def __init__(self, docs):
        self.messages = _FakeMessages(docs)


def _parent(_id, category, to_instance="alice", owner=None, obligation="open"):
    d = {
        "_id": _id,
        "category": category,
        "to_instance": to_instance,
        "obligation": obligation,
    }
    if owner is not None:
        d["owner"] = owner
    return d


def _obl(db, _id):
    return db.messages.docs[_id].get("obligation")


# ── Auto-ack helper: category transitions ──

def test_question_reply_by_owner_resolves():
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "question")])
    m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
    assert _obl(db, "p") == "resolved"
    assert db.messages.docs["p"].get("resolved_at") is not None


def test_contract_and_review_reply_by_owner_resolve():
    from shared_memory.tools import messaging as m
    for cat in ("contract", "review"):
        db = _FakeDB([_parent("p", cat)])
        m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
        assert _obl(db, "p") == "resolved", cat


def test_task_and_blocker_reply_by_owner_only_responds():
    from shared_memory.tools import messaging as m
    for cat in ("task", "blocker"):
        db = _FakeDB([_parent("p", cat)])
        m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
        assert _obl(db, "p") == "responded", cat
        assert db.messages.docs["p"].get("resolved_at") is None, cat


def test_info_parent_carries_no_obligation():
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "info", obligation=None)])
    m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
    assert _obl(db, "p") is None


# ── Scope guard ──

def test_third_party_reply_does_not_ack():
    """A reply whose sender is NOT the parent's owner must not clear it."""
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "question", to_instance="alice")])
    m._advance_parent_obligation_on_reply(db, "p", "bob", utc_now())
    assert _obl(db, "p") == "open"


def test_broadcast_parent_never_auto_acks():
    """to_instance='*' yields owner='*', which no concrete replier matches."""
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "task", to_instance="*")])
    m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
    assert _obl(db, "p") == "open"


# ── No-downgrade ──

def test_resolved_parent_is_not_downgraded():
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "task", obligation="resolved")])
    m._advance_parent_obligation_on_reply(db, "p", "alice", utc_now())
    assert _obl(db, "p") == "resolved"


# ── Stage-2 forward-compat: owner field overrides to_instance ──

def test_owner_field_overrides_to_instance():
    """When claiming populates `owner` (group message), the claimer's reply clears
    it and the original (component) recipient name is irrelevant."""
    from shared_memory.tools import messaging as m
    db = _FakeDB([_parent("p", "question", to_instance="camera-sync", owner="claimerX")])
    # non-claimer reply: no-op
    m._advance_parent_obligation_on_reply(db, "p", "someoneElse", utc_now())
    assert _obl(db, "p") == "open"
    # claimer reply: resolves
    m._advance_parent_obligation_on_reply(db, "p", "claimerX", utc_now())
    assert _obl(db, "p") == "resolved"


# ── Explicit resolve/responded verb on memory_update_message_status ──

def _session(monkeypatch, db, instance="alice", project="junto"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m
    sid = f"_test_obl_{instance}_{id(db)}"
    active_sessions[sid] = {"role": "agent", "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: db)
    return sid, m, active_sessions


async def test_resolved_verb_writes_obligation_not_status(monkeypatch):
    db = _FakeDB([_parent("p", "task", obligation="responded")])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        res = json.loads(
            await m.memory_update_message_status(session_id=sid, message_id="p", status="resolved")
        )
        assert res["obligation"] == "resolved"
        assert _obl(db, "p") == "resolved"
        assert db.messages.docs["p"].get("resolved_at") is not None
        # delivery status must be untouched by the obligation verb
        assert "status" not in db.messages.docs["p"] or db.messages.docs["p"]["status"] != "resolved"
    finally:
        sessions.pop(sid, None)


async def test_responded_verb_writes_obligation(monkeypatch):
    db = _FakeDB([_parent("p", "blocker", obligation="open")])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        res = json.loads(
            await m.memory_update_message_status(session_id=sid, message_id="p", status="responded")
        )
        assert res["obligation"] == "responded"
        assert _obl(db, "p") == "responded"
    finally:
        sessions.pop(sid, None)


async def test_delivery_status_still_works(monkeypatch):
    db = _FakeDB([_parent("p", "task", obligation="open")])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        res = json.loads(
            await m.memory_update_message_status(session_id=sid, message_id="p", status="received")
        )
        assert res["status"] == "received"
        assert db.messages.docs["p"]["status"] == "received"
        # obligation axis untouched by a delivery-status write
        assert _obl(db, "p") == "open"
    finally:
        sessions.pop(sid, None)


async def test_unknown_status_still_rejected(monkeypatch):
    db = _FakeDB([_parent("p", "task")])
    sid, m, sessions = _session(monkeypatch, db)
    try:
        res = json.loads(
            await m.memory_update_message_status(session_id=sid, message_id="p", status="bogus")
        )
        assert "error" in res
    finally:
        sessions.pop(sid, None)
