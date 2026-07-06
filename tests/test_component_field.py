"""Component-addressing regression (design:unified-messaging-v0 Stage 1).

Stage 1 adds:
  - an optional first-class `component` field on messages (sub-group under a
    project), carried as metadata only — addressing is still by to_instance,
    so this is purely additive (nimbus-compat: component=null is today's world)
  - `subscribed_components` declared at start_session; the session-start
    response surfaces `component_peers` — OTHER agents recently active in any
    of the caller's components, read from the persistent agent_directory

These tests pin that the four message serializers surface `component`, and that
the peer-discovery helper filters correctly (self, window, dedup, shared-only).
"""

import json
from datetime import timedelta

from shared_memory.helpers import utc_now


# ── Minimal Mongo query matcher (only the shapes these paths produce) ──
def _match(doc, query):
    for key, cond in query.items():
        if key == "$and":
            if not all(_match(doc, c) for c in cond):
                return False
        elif key == "$or":
            if not any(_match(doc, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            val = doc.get(key)
            for op, operand in cond.items():
                if op == "$gt" and not (val is not None and val > operand):
                    return False
                elif op == "$lt" and not (val is not None and val < operand):
                    return False
                elif op == "$gte" and not (val is not None and val >= operand):
                    return False
                elif op == "$ne" and val == operand:
                    return False
                elif op == "$in" and val not in operand and not (
                    isinstance(val, list) and any(v in operand for v in val)
                ):
                    return False
                elif op == "$exists":
                    if (key in doc) != operand:
                        return False
        else:
            if doc.get(key) != cond:
                return False
    return True


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, spec):
        for field, direction in reversed(spec):
            self._docs.sort(
                key=lambda d: (d.get(field) is None, d.get(field)),
                reverse=(direction < 0),
            )
        return self

    def limit(self, n):
        return iter(self._docs[:n])

    def __iter__(self):
        return iter(self._docs)


class _FakeMessages:
    def __init__(self, docs):
        self._docs = [dict(d) for d in docs]

    def find(self, query, projection=None):
        return _FakeCursor([d for d in self._docs if _match(d, query)])

    def count_documents(self, query):
        return sum(1 for d in self._docs if _match(d, query))

    def find_one(self, filt, projection=None):
        for d in self._docs:
            if d.get("_id") == filt.get("_id"):
                return dict(d)
        return None


class _FakeAgentDirectory:
    def __init__(self, rows=None):
        # rows: list of dicts (each an agent_directory record)
        self.rows = list(rows or [])

    def find_one(self, filt, projection=None):
        for r in self.rows:
            if r.get("project") == filt.get("project") and r.get("instance") == filt.get("instance"):
                return dict(r)
        return None

    def find(self, query, projection=None):
        return [dict(r) for r in self.rows if _match(r, query)]


class _FakeDB:
    def __init__(self, messages=None, directory=None):
        self.messages = _FakeMessages(messages or [])
        self.agent_directory = directory or _FakeAgentDirectory()


def _msg(_id, to_instance="memory", to_project="junto", component=None, **extra):
    d = {
        "_id": _id,
        "to_instance": to_instance,
        "to_project": to_project,
        "from_instance": "peer",
        "from_project": "junto",
        "message": _id,
        "category": "info",
        "priority": "normal",
        "status": "pending",
        "obligation": None,
        "component": component,
        "created_at": utc_now(),
    }
    d.update(extra)
    return d


def _setup(monkeypatch, fake_db, instance="memory", project="junto", role="agent"):
    from shared_memory.state import active_sessions
    from shared_memory.tools import messaging as m

    sid = f"_test_component_{instance}_{id(fake_db)}"
    active_sessions[sid] = {"role": role, "claude_instance": instance, "project": project}
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    monkeypatch.setattr(m, "_is_project_admin", lambda *a, **k: False)
    return sid, m, active_sessions


# ── Message serializers surface `component` ──

async def test_get_messages_list_surfaces_component(monkeypatch):
    fake_db = _FakeDB([_msg("m1", component="camera-sync")])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        entry = next(x for x in res["messages"] if x["id"] == "m1")
        assert entry["component"] == "camera-sync"
    finally:
        sessions.pop(sid, None)


async def test_get_messages_list_component_defaults_none(monkeypatch):
    fake_db = _FakeDB([_msg("m1")])  # component unset
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, include_seen=True))
        entry = next(x for x in res["messages"] if x["id"] == "m1")
        assert entry["component"] is None
    finally:
        sessions.pop(sid, None)


async def test_get_messages_by_id_surfaces_component(monkeypatch):
    fake_db = _FakeDB([_msg("m1", component="auth")])
    sid, m, sessions = _setup(monkeypatch, fake_db)
    try:
        res = json.loads(await m.memory_get_messages(session_id=sid, message_id="m1"))
        assert res["messages"][0]["component"] == "auth"
        # consistency fix: obligation now present on the single-lookup path too
        assert "obligation" in res["messages"][0]
    finally:
        sessions.pop(sid, None)


def test_format_inbox_message_surfaces_component():
    from shared_memory.tools.messaging import _format_inbox_message
    entry = _format_inbox_message(_msg("m1", component="camera-sync"))
    assert entry["component"] == "camera-sync"
    entry2 = _format_inbox_message(_msg("m2"))  # unset
    assert entry2["component"] is None


def test_get_pending_messages_surfaces_component(monkeypatch):
    from shared_memory.tools import messaging as m
    fake_db = _FakeDB([_msg("m1", to_instance="memory", component="auth")])
    monkeypatch.setattr(m, "get_mongo", lambda: fake_db)
    out = m.get_pending_messages_for_instance("memory", project="junto")
    assert out and out[0]["component"] == "auth"


# ── Peer-discovery helper ──

def _dir_row(instance, components, ago_minutes=1, project="junto", task=""):
    return {
        "project": project,
        "instance": instance,
        "subscribed_components": components,
        "last_seen": utc_now() - timedelta(minutes=ago_minutes),
        "last_task": task,
    }


def test_component_peers_returns_shared_only():
    from shared_memory.tools.sessions import _active_component_peers
    db = _FakeDB(directory=_FakeAgentDirectory([
        _dir_row("alice", ["camera-sync", "auth"], task="wiring lens"),
        _dir_row("bob", ["billing"]),  # no overlap
    ]))
    peers = _active_component_peers(db, "junto", "memory", ["camera-sync"])
    assert len(peers) == 1
    assert peers[0]["agent"] == "alice"
    assert peers[0]["components"] == ["camera-sync"]  # only the SHARED component
    assert peers[0]["task"] == "wiring lens"


def test_component_peers_excludes_self():
    from shared_memory.tools.sessions import _active_component_peers
    db = _FakeDB(directory=_FakeAgentDirectory([
        _dir_row("memory", ["camera-sync"]),
    ]))
    assert _active_component_peers(db, "junto", "memory", ["camera-sync"]) == []


def test_component_peers_filters_by_window():
    from shared_memory.tools.sessions import _active_component_peers
    db = _FakeDB(directory=_FakeAgentDirectory([
        _dir_row("stale", ["camera-sync"], ago_minutes=120),  # outside 15-min window
    ]))
    assert _active_component_peers(db, "junto", "memory", ["camera-sync"]) == []


def test_component_peers_empty_subscriptions_is_noop():
    from shared_memory.tools.sessions import _active_component_peers
    db = _FakeDB(directory=_FakeAgentDirectory([_dir_row("alice", ["camera-sync"])]))
    assert _active_component_peers(db, "junto", "memory", []) == []
    assert _active_component_peers(db, "junto", "memory", None) == []


def test_component_peers_none_db_is_noop():
    from shared_memory.tools.sessions import _active_component_peers
    assert _active_component_peers(None, "junto", "memory", ["camera-sync"]) == []
