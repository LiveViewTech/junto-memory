"""Tests for the memory_get_by_id surface bundle:

- authored_by on responses (backlog_601d268dbe6b — the surface-gap
  false-clear trap) + facets on learning rows
- unique 12-char prefix resolution (backlog_fa06355f851b)
- msg_* message dispatch with get_messages-parity auth (backlog_f6f950b3b4ce)
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import query


class FakeCollection:
    def __init__(self, name, docs):
        # docs: {doc_id: (meta_dict, content_str)}
        self.name = name
        self.docs = docs
        self.updated = []

    async def get(self, ids=None, include=None, **kwargs):
        if ids:
            hits = [i for i in ids if i in self.docs]
            return {
                "ids": hits,
                "metadatas": [dict(self.docs[i][0]) for i in hits],
                "documents": [self.docs[i][1] for i in hits],
            }
        return {"ids": list(self.docs)}

    async def update(self, ids, metadatas):
        self.updated.append(list(ids))


class FakeChroma:
    def __init__(self, collections):
        self.collections = collections

    async def list_collections(self):
        return self.collections


class FakeMessages:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, q):
        return self.docs.get(q.get("_id"))


class FakeDb:
    def __init__(self, messages=None):
        self.messages = FakeMessages(messages or {})


def _install_session(session_id="sess_test", instance="tester", project="test",
                     role="agent"):
    active_sessions[session_id] = {
        "claude_instance": instance,
        "project": project,
        "role": role,
        "allowed_projects": [project],
        "started_at": "2026-07-23T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_sessions():
    active_sessions.clear()
    yield
    active_sessions.clear()


def _patch_chroma(monkeypatch, collections):
    async def _fake_get_chroma():
        return FakeChroma(collections)
    monkeypatch.setattr(query, "get_chroma", _fake_get_chroma)


LEARNING_ID = "learning_7cc0b78f0e53b4bb"
BARE_ID = "34e6c10ceecf9b59"


def _std_collections():
    return [FakeCollection("proj_test", {
        LEARNING_ID: (
            {"title": "A learning", "type": "learning", "status": "active",
             "project": "test", "tags": "[]", "created": "2026-07-01",
             "updated": "2026-07-01", "claude_instance": "authorbot"},
            "learning body",
        ),
        BARE_ID: (
            {"title": "A memory", "type": "context", "status": "active",
             "project": "test", "tags": "[]", "created": "2026-07-02",
             "updated": "2026-07-02"},
            "memory body",
        ),
    })]


@pytest.mark.asyncio
async def test_exact_hit_carries_authored_by_and_facets(monkeypatch):
    _install_session()
    _patch_chroma(monkeypatch, _std_collections())
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())

    import shared_memory.facets as facets_mod
    monkeypatch.setattr(
        facets_mod, "get_facets_for_ids",
        lambda db, ids: {LEARNING_ID: {"claim": "c", "operation": "diagnose"}},
    )

    out = json.loads(await query.memory_get_by_id("sess_test", LEARNING_ID))
    assert out["found"] is True
    assert out["authored_by"] == "authorbot"
    assert out["facets"]["operation"] == "diagnose"
    assert out["content"] == "learning body"


@pytest.mark.asyncio
async def test_exact_hit_null_authored_by_no_facets_for_non_learning(monkeypatch):
    _install_session()
    _patch_chroma(monkeypatch, _std_collections())
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())

    out = json.loads(await query.memory_get_by_id("sess_test", BARE_ID))
    assert out["found"] is True
    assert out["authored_by"] is None
    assert "facets" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [
    "7cc0b78f0e53",                # bare 12-char tail
    "learning_7cc0b78f0e53",       # typed prefix
])
async def test_prefix_resolves_unique(monkeypatch, prefix):
    _install_session()
    _patch_chroma(monkeypatch, _std_collections())
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())

    out = json.loads(await query.memory_get_by_id("sess_test", prefix))
    assert out["found"] is True
    assert out["id"] == LEARNING_ID
    assert out["resolved_from_prefix"] == prefix


@pytest.mark.asyncio
async def test_prefix_ambiguous_returns_candidates(monkeypatch):
    _install_session()
    cols = [FakeCollection("proj_test", {
        "learning_aaaabbbbcccc0001": ({"title": "x", "tags": "[]"}, ""),
        "learning_aaaabbbbcccc0002": ({"title": "y", "tags": "[]"}, ""),
    })]
    _patch_chroma(monkeypatch, cols)

    out = json.loads(await query.memory_get_by_id("sess_test", "aaaabbbbcccc"))
    assert out["found"] is False
    assert "Ambiguous prefix" in out["error"]
    assert len(out["candidates"]) == 2


@pytest.mark.asyncio
async def test_short_prefix_not_resolved(monkeypatch):
    _install_session()
    _patch_chroma(monkeypatch, _std_collections())

    out = json.loads(await query.memory_get_by_id("sess_test", "7cc0b78f"))
    assert out["found"] is False
    assert "not found" in out["error"].lower()


@pytest.mark.asyncio
async def test_typed_prefix_does_not_match_other_type(monkeypatch):
    _install_session()
    _patch_chroma(monkeypatch, _std_collections())

    # tail is 12+ chars but the type prefix is wrong — must NOT resolve
    out = json.loads(await query.memory_get_by_id("sess_test", "spec_7cc0b78f0e53"))
    assert out["found"] is False


def _msg_doc(to_instance="tester", to_project="test"):
    return {
        "_id": "msg_abc123def456",
        "from_instance": "sender",
        "from_project": "test",
        "to_instance": to_instance,
        "to_project": to_project,
        "category": "info",
        "subject": "subj",
        "message": "hello",
        "priority": "normal",
        "status": "pending",
        "created_at": None,
        "created": None,
    }


@pytest.mark.asyncio
async def test_msg_dispatch_own_message(monkeypatch):
    _install_session()
    marked = []
    import shared_memory.tools.messaging as messaging_mod
    import shared_memory.tools.projects as projects_mod
    monkeypatch.setattr(messaging_mod, "_mark_messages_read",
                        lambda db, ids, inst: marked.append((list(ids), inst)))
    monkeypatch.setattr(projects_mod, "_is_project_admin", lambda db, p, i: False)
    monkeypatch.setattr(query, "get_mongo",
                        lambda: FakeDb({"msg_abc123def456": _msg_doc()}))

    out = json.loads(await query.memory_get_by_id("sess_test", "msg_abc123def456"))
    assert out["found"] is True
    assert out["collection"] == "messages"
    assert out["type"] == "message"
    assert out["message"]["message"] == "hello"
    assert out["message"]["lane"]  # lane fields stamped
    assert marked == [((["msg_abc123def456"]), "tester")]


@pytest.mark.asyncio
async def test_msg_dispatch_denied_for_other_recipient(monkeypatch):
    _install_session()
    import shared_memory.tools.projects as projects_mod
    monkeypatch.setattr(projects_mod, "_is_project_admin", lambda db, p, i: False)
    monkeypatch.setattr(
        query, "get_mongo",
        lambda: FakeDb({"msg_abc123def456": _msg_doc(to_instance="someone_else")}))

    out = json.loads(await query.memory_get_by_id("sess_test", "msg_abc123def456"))
    assert out["found"] is False
    assert "Permission denied" in out["error"]


@pytest.mark.asyncio
async def test_msg_dispatch_admin_can_view_others(monkeypatch):
    _install_session(role="admin")
    marked = []
    import shared_memory.tools.messaging as messaging_mod
    monkeypatch.setattr(messaging_mod, "_mark_messages_read",
                        lambda db, ids, inst: marked.append(inst))
    monkeypatch.setattr(
        query, "get_mongo",
        lambda: FakeDb({"msg_abc123def456": _msg_doc(to_instance="someone_else")}))

    out = json.loads(await query.memory_get_by_id("sess_test", "msg_abc123def456"))
    assert out["found"] is True
    assert marked == []  # admin peek must not mark read on recipient's behalf


@pytest.mark.asyncio
async def test_msg_not_found(monkeypatch):
    _install_session()
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())

    out = json.loads(await query.memory_get_by_id("sess_test", "msg_nonexistent1"))
    assert out["found"] is False
    assert "not found" in out["error"].lower()
