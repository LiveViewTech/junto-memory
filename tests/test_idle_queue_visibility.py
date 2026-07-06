"""Tests for read-side idle-queue visibility (backlog_da56a6e0c46b).

When memory_send_message finds NO live subscriber for a direct recipient, the
response carries a `recipient_idle` block: what's already waiting for that agent
(open/responded action obligations + waiting FYIs) and how long they've been idle
(agent_directory.last_seen). It lets the sender decide to escalate (manually wake
the agent) instead of sitting blind on an unanswered ask — coordinator's concrete
ask in msg_5982f608ec7b. The counts mirror the recipient's own next get_messages.
"""

from datetime import datetime, timedelta, timezone

import shared_memory.tools.messaging as m

NOW = datetime(2026, 6, 16, 0, 0, 0, tzinfo=timezone.utc)


def _clause_value(query, field):
    """Pull a field's clause out of the {"$and": [...]} query _compute_lane_counts
    builds, so the fake can decide which count to return."""
    for part in query.get("$and", []):
        if field in part:
            return part[field]
    return None


class _FakeMessages:
    """count_documents returns per-lane counts by inspecting the extra clause;
    find() backs the FYI-oldest probe (only hit when fyi_waiting > 0)."""

    def __init__(self, action_open, action_responded, fyi_waiting, oldest_created=None):
        self.action_open = action_open
        self.action_responded = action_responded
        self.fyi_waiting = fyi_waiting
        self.oldest_created = oldest_created

    def count_documents(self, query):
        category = _clause_value(query, "category")
        obligation = _clause_value(query, "obligation")
        if category == "info":
            return self.fyi_waiting
        # action lane: distinguish open vs responded by the obligation clause
        if obligation == "responded":
            return self.action_responded
        return self.action_open

    def find(self, query):
        docs = [{"created_at": self.oldest_created}] if self.oldest_created else []

        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            def sort(self, *_a, **_k):
                return self

            def limit(self, *_a, **_k):
                return iter(self._docs)

        return _Cursor(docs)


class _FakeAgentDir:
    def __init__(self, doc):
        self.doc = doc

    def find_one(self, query, projection=None):
        return self.doc


class _FakeDB:
    def __init__(self, messages, agent_doc):
        self.messages = messages
        self.agent_directory = _FakeAgentDir(agent_doc)


def test_snapshot_returns_counts_and_idle_hours(monkeypatch):
    monkeypatch.setattr(m, "utc_now", lambda: NOW)
    last_seen = (NOW - timedelta(hours=3)).isoformat()
    db = _FakeDB(
        _FakeMessages(action_open=2, action_responded=1, fyi_waiting=0),
        {"messages_seen_through": None, "last_seen": last_seen},
    )
    snap = m._recipient_idle_snapshot(db, "junto", "infra-team")
    assert snap["queued_action_open"] == 2
    assert snap["queued_action_responded"] == 1
    assert snap["queued_fyi_waiting"] == 0
    assert snap["idle_hours"] == 3.0
    assert snap["last_seen"] == last_seen


def test_snapshot_none_for_broadcast():
    db = _FakeDB(_FakeMessages(0, 0, 0), {})
    assert m._recipient_idle_snapshot(db, "junto", "*") is None


def test_snapshot_none_for_missing_project():
    db = _FakeDB(_FakeMessages(0, 0, 0), {})
    assert m._recipient_idle_snapshot(db, "", "infra-team") is None


def test_snapshot_none_for_missing_db():
    assert m._recipient_idle_snapshot(None, "junto", "infra-team") is None


def test_snapshot_unknown_agent_has_null_idle(monkeypatch):
    monkeypatch.setattr(m, "utc_now", lambda: NOW)
    # No agent_directory record → last_seen/idle_hours null, counts still computed.
    db = _FakeDB(_FakeMessages(action_open=1, action_responded=0, fyi_waiting=0), None)
    snap = m._recipient_idle_snapshot(db, "junto", "never-seen")
    assert snap["queued_action_open"] == 1
    assert snap["last_seen"] is None
    assert snap["idle_hours"] is None
