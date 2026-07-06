"""Tests for contract:reply-promotion-v0 — an obligation-closing reply pushes even
when the reply's OWN category is badge-only (e.g. an info-tagged answer to a
question), so the requester isn't left blind. Push ≠ new obligation: promotion
only changes whether the reply pushes, not its lane membership.
"""

from datetime import datetime, timezone

import shared_memory.tools.messaging as m


class _FakeColl:
    def __init__(self, doc):
        self.doc = doc
        self.updates = []

    def find_one(self, q, proj=None):
        return self.doc

    def update_one(self, q, u):
        self.updates.append(u)
        if isinstance(u, dict) and "$set" in u:
            self.doc.update(u["$set"])

        class _R:
            matched_count = 1

        return _R()


class _FakeDB:
    def __init__(self, doc):
        self.messages = _FakeColl(doc)


NOW = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)


# ── _advance_parent_obligation_on_reply now returns the advanced state ──
# (a non-None return is exactly the promotion signal)

def test_advance_question_returns_resolved():
    db = _FakeDB({"_id": "p1", "category": "question", "to_instance": "alice", "obligation": "open"})
    assert m._advance_parent_obligation_on_reply(db, "p1", "alice", NOW) == "resolved"


def test_advance_task_returns_responded():
    db = _FakeDB({"_id": "p1", "category": "task", "to_instance": "alice", "obligation": "open"})
    assert m._advance_parent_obligation_on_reply(db, "p1", "alice", NOW) == "responded"


def test_advance_already_resolved_returns_none():
    db = _FakeDB({"_id": "p1", "category": "question", "to_instance": "alice", "obligation": "resolved"})
    assert m._advance_parent_obligation_on_reply(db, "p1", "alice", NOW) is None


def test_advance_non_owner_returns_none():
    db = _FakeDB({"_id": "p1", "category": "question", "to_instance": "alice", "obligation": "open"})
    assert m._advance_parent_obligation_on_reply(db, "p1", "bob", NOW) is None


def test_advance_info_parent_returns_none():
    db = _FakeDB({"_id": "p1", "category": "info", "to_instance": "alice", "obligation": None})
    assert m._advance_parent_obligation_on_reply(db, "p1", "alice", NOW) is None


# ── _build_announce_packet promotion ──

def _info_reply_doc():
    return {
        "_id": "r1", "category": "info", "priority": "normal",
        "from_instance": "alice", "from_project": "junto", "to_instance": "bob",
        "chain_depth": 1, "in_response_to": "p1", "obligation": None,
        "created_at": NOW, "message": "the answer",
    }


def test_info_reply_pushes_natively_since_push_all_info():
    # push-all-info-v0 (2026-06-24) subsumes promotion for info: an info reply now
    # pushes as a header whether or not it closes an obligation. Promotion remains
    # only for the residual mode==None case (a cleared-action reply).
    pkt = m._build_announce_packet(_info_reply_doc(), promoted=False)
    assert pkt is not None
    assert pkt["mode"] == "header"
    assert "body" not in pkt


def test_info_reply_promoted_pushes_as_header():
    pkt = m._build_announce_packet(_info_reply_doc(), promoted=True)
    assert pkt is not None
    assert pkt["mode"] == "header"        # one-line, body-on-pull
    assert "body" not in pkt
    assert pkt["msg_id"] == "r1"


def test_promoted_urgent_reply_injects_with_body():
    doc = _info_reply_doc()
    doc["priority"] = "urgent"
    pkt = m._build_announce_packet(doc, promoted=True)
    assert pkt["mode"] == "inject"
    assert pkt["body"] == "the answer"    # inject inlines the body


def test_action_reply_pushes_regardless_of_promoted_flag():
    # A correctly-tagged action reply already pushes; promotion is a no-op for it.
    doc = _info_reply_doc()
    doc["category"] = "question"
    doc["obligation"] = "open"
    pkt = m._build_announce_packet(doc, promoted=False)
    assert pkt is not None
    assert pkt["mode"] == "header"
