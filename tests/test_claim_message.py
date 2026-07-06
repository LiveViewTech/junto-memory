"""Message-claiming regression (design:unified-messaging-v0 Stage 2 / CLAIMING).

A GROUP message (broadcast to_instance="*") is one doc seen by many agents.
memory_claim_message lets exactly one of them take ownership via an atomic
compare-and-swap (find_one_and_update on owner:null). The winner becomes the
message `owner`, which is the SAME owner the lanes-B obligation guard reads — so
the winner's reply auto-acks, and an UNCLAIMED group message never auto-acks
(its implicit owner is "*", which no concrete replier matches). Pins:

  - first claim wins, second loses and learns the current owner (first-wins CAS)
  - re-claim by the holder is a no-op that reports self-ownership
  - DIRECT messages (concrete to_instance) are NOT claimable — even when they
    carry a component tag (the tag is metadata, not an address; claiming one
    would hijack the recipient's obligation)
  - cross-project claims are denied; unknown ids error
  - claiming gives a group message an owner so the claimer's reply RESOLVES it,
    where before-claim the same reply did nothing (Stage 2 ⇄ Stage 0 unify)
"""

import json

from shared_memory.helpers import utc_now


class _FakeMessages:
    def __init__(self, docs):
        self.docs = {d["_id"]: dict(d) for d in docs}

    def find_one(self, filt, projection=None):
        d = self.docs.get(filt.get("_id"))
        if d is None:
            return None
        for k, v in filt.items():
            if k == "_id":
                continue
            if d.get(k) != v:
                return None
        return dict(d)

    def find_one_and_update(self, filt, upd, return_document=None):
        d = self.docs.get(filt.get("_id"))
        if d is None:
            return None
        # CAS precondition: every non-_id filter key must match the live doc.
        for k, v in filt.items():
            if k == "_id":
                continue
            if d.get(k) != v:
                return None
        for k, v in (upd.get("$set") or {}).items():
            d[k] = v
        return dict(d)  # ReturnDocument.AFTER

    def update_one(self, filt, upd):
        d = self.docs.get(filt.get("_id"))
        if d is None:
            return None
        for k, v in (upd.get("$set") or {}).items():
            d[k] = v
        return None


class _FakeDB:
    def __init__(self, docs):
        self.messages = _FakeMessages(docs)


def _gmsg(_id, category="question", to_instance="*", to_project="junto",
          component=None, owner=None, obligation="open"):
    return {
        "_id": _id,
        "to_instance": to_instance,
        "to_project": to_project,
        "from_instance": "asker",
        "from_project": "junto",
        "message": _id,
        "category": category,
        "priority": "normal",
        "status": "pending",
        "obligation": obligation,
        "component": component,
        "owner": owner,
        "claimed_at": None,
        "created_at": utc_now(),
    }


def _session(monkeypatch, db, instance="alice", project="junto"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m
    sid = f"_test_claim_{instance}_{id(db)}"
    active_sessions[sid] = {"role": "agent", "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: db)
    return sid, m, active_sessions


async def test_claim_broadcast_succeeds(monkeypatch):
    db = _FakeDB([_gmsg("g1")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="g1"))
        assert res["claimed"] is True
        assert res["owner"] == "alice"
        assert res.get("claimed_at")
        assert db.messages.docs["g1"]["owner"] == "alice"
        assert db.messages.docs["g1"]["claimed_at"] is not None
    finally:
        sessions.pop(sid, None)


async def test_second_claim_loses_and_sees_owner(monkeypatch):
    db = _FakeDB([_gmsg("g1")])
    sid_a, m, sessions = _session(monkeypatch, db, instance="alice")
    sid_b = None
    try:
        await m.memory_claim_message(session_id=sid_a, message_id="g1")
        from shared_memory.state import active_sessions
        sid_b = f"_test_claim_bob_{id(db)}"
        active_sessions[sid_b] = {"role": "agent", "claude_instance": "bob", "project": "junto"}
        res = json.loads(await m.memory_claim_message(session_id=sid_b, message_id="g1"))
        assert res["claimed"] is False
        assert res["owner"] == "alice"  # loser learns who holds it
        assert "Already claimed by alice" in res["note"]
    finally:
        sessions.pop(sid_a, None)
        if sid_b:
            sessions.pop(sid_b, None)


async def test_reclaim_by_holder_is_noop(monkeypatch):
    db = _FakeDB([_gmsg("g1")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        await m.memory_claim_message(session_id=sid, message_id="g1")
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="g1"))
        assert res["claimed"] is False
        assert res["owner"] == "alice"
        assert "already own" in res["note"].lower()
    finally:
        sessions.pop(sid, None)


async def test_direct_message_not_claimable(monkeypatch):
    db = _FakeDB([_gmsg("d1", to_instance="carol")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="d1"))
        assert "error" in res
        assert "direct-addressed" in res["error"]
        assert db.messages.docs["d1"]["owner"] is None  # untouched
    finally:
        sessions.pop(sid, None)


async def test_component_tagged_direct_message_not_claimable(monkeypatch):
    """A component TAG on a directed message does not make it claimable — that
    would hijack the concrete recipient's obligation."""
    db = _FakeDB([_gmsg("d1", to_instance="carol", component="camera-sync")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="d1"))
        assert "error" in res and "direct-addressed" in res["error"]
        assert db.messages.docs["d1"]["owner"] is None
    finally:
        sessions.pop(sid, None)


async def test_cross_project_claim_denied(monkeypatch):
    db = _FakeDB([_gmsg("g1", to_project="nimbus")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice", project="junto")
    try:
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="g1"))
        assert "error" in res and "different project" in res["error"]
    finally:
        sessions.pop(sid, None)


async def test_claim_unknown_id_errors(monkeypatch):
    db = _FakeDB([])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        res = json.loads(await m.memory_claim_message(session_id=sid, message_id="nope"))
        assert "error" in res and "not found" in res["error"].lower()
    finally:
        sessions.pop(sid, None)


# ── Stage 2 ⇄ Stage 0: claiming gives a group message an owner that can clear ──

async def test_claim_then_reply_resolves_where_unclaimed_did_not(monkeypatch):
    db = _FakeDB([_gmsg("g1", category="question")])
    sid, m, sessions = _session(monkeypatch, db, instance="alice")
    try:
        # BEFORE claim: owner is null → guard falls back to to_instance="*";
        # bob's reply matches no concrete owner → obligation stays open.
        m._advance_parent_obligation_on_reply(db, "g1", "bob", utc_now())
        assert db.messages.docs["g1"]["obligation"] == "open"

        # bob claims it → owner=bob.
        from shared_memory.state import active_sessions
        sid_b = f"_test_claim_bob2_{id(db)}"
        active_sessions[sid_b] = {"role": "agent", "claude_instance": "bob", "project": "junto"}
        try:
            won = json.loads(await m.memory_claim_message(session_id=sid_b, message_id="g1"))
            assert won["claimed"] is True and won["owner"] == "bob"
        finally:
            sessions.pop(sid_b, None)

        # AFTER claim: bob's reply now matches owner=bob → resolves.
        m._advance_parent_obligation_on_reply(db, "g1", "bob", utc_now())
        assert db.messages.docs["g1"]["obligation"] == "resolved"
    finally:
        sessions.pop(sid, None)
