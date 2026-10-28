"""Write-time contradiction gate — threshold stage (backlog_6471d8348393).

memory_record_learning surfaces near-prior learnings so the author can catch a
duplicate or contradiction before it silently lands. THRESHOLD-ONLY v0:
embedding similarity, advisory, NEVER blocks the write. These tests pin:
  - _find_similar_learnings filtering (threshold / status / self-exclusion / sort)
  - the surfaced `similar_prior` + `note` in the tool response
  - the fail-safe: an advisory-lookup error must not block recording
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import storage


class _FakeQueryCollection:
    """Chroma-shaped fake: query() returns the configured id/meta/dist rows;
    add() records what was written. Set raise_on_query to simulate an outage."""

    def __init__(self, ids=None, metas=None, dists=None):
        self._ids = ids or []
        self._metas = metas or []
        self._dists = dists or []
        self.added_ids: list[str] = []
        self.raise_on_query = False
        self.query_calls: list[dict] = []

    async def query(self, query_texts, n_results, where=None):
        self.query_calls.append({"where": where, "n_results": n_results})
        if self.raise_on_query:
            raise RuntimeError("chroma down")
        return {
            "ids": [self._ids],
            "metadatas": [self._metas],
            "distances": [self._dists],
            "documents": [["body" for _ in self._ids]],
        }

    async def add(self, ids, documents, metadatas):
        self.added_ids.extend(ids)


def _meta(title, status="active", updated="2026-07-01T00:00:00+00:00"):
    return {"title": title, "type": "learning", "status": status, "updated": updated}


# score = 1 - clamp(dist,0,2)/2 → dist 0.2==0.9, 0.1==0.95, 1.4==0.3, 0.8==0.6
@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_sessions():
    active_sessions.clear()
    yield
    active_sessions.clear()


def _install_session(session_id="sess_test", project="test"):
    active_sessions[session_id] = {
        "claude_instance": "tester",
        "project": project,
        "role": "agent",
        "allowed_projects": [project],
        "started_at": "2026-07-09T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# _find_similar_learnings unit tests
# ---------------------------------------------------------------------------

async def test_find_similar_filters_threshold_status_and_self():
    ids = ["learning_a", "learning_b", "learning_c", "learning_self"]
    metas = [
        _meta("A near"),                       # score 0.9  -> keep
        _meta("B superseded", status="superseded"),  # excluded by status
        _meta("C far"),                        # score 0.3  -> below threshold
        _meta("self"),                         # excluded as the new doc itself
    ]
    dists = [0.2, 0.1, 1.4, 0.0]
    coll = _FakeQueryCollection(ids, metas, dists)

    hits = await storage._find_similar_learnings(
        coll, "text", exclude_id="learning_self", threshold=0.6
    )

    assert [h["id"] for h in hits] == ["learning_a"]
    assert hits[0]["score"] == 0.9
    assert hits[0]["title"] == "A near"
    # single-key where filter only (multi-key silently returns nothing here)
    assert coll.query_calls[0]["where"] == {"type": "learning"}


async def test_find_similar_sorts_by_score_desc():
    ids = ["learning_lo", "learning_hi"]
    metas = [_meta("lo"), _meta("hi")]
    dists = [0.8, 0.1]  # scores 0.6, 0.95
    coll = _FakeQueryCollection(ids, metas, dists)

    hits = await storage._find_similar_learnings(coll, "t", exclude_id="none", threshold=0.6)

    assert [h["id"] for h in hits] == ["learning_hi", "learning_lo"]


async def test_find_similar_empty_result():
    coll = _FakeQueryCollection([], [], [])
    hits = await storage._find_similar_learnings(coll, "t", exclude_id="none")
    assert hits == []


# ---------------------------------------------------------------------------
# memory_record_learning end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def _patch_collection(monkeypatch):
    coll = _FakeQueryCollection()

    async def _fake_get_chroma():
        return object()

    async def _fake_get_project_collection(client, project):
        return coll

    async def _fake_get_shared_collection(client, kind):
        return coll

    monkeypatch.setattr(storage, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(storage, "get_project_collection", _fake_get_project_collection)
    monkeypatch.setattr(storage, "get_shared_collection", _fake_get_shared_collection)
    monkeypatch.setattr(storage, "get_mongo", lambda: None)
    # op-log emit + embedding fetch are best-effort side channels — stub them out
    monkeypatch.setattr(storage, "emit_op_log_from_context", lambda **kw: None)

    async def _fake_fetch_embedding(collection, doc_id):
        return None

    monkeypatch.setattr(storage, "fetch_embedding_for_op_log", _fake_fetch_embedding)
    return coll


async def test_record_learning_surfaces_similar_prior(_patch_collection):
    _install_session()
    _patch_collection._ids = ["learning_prior"]
    _patch_collection._metas = [_meta("mesh gate is a no-op")]
    _patch_collection._dists = [0.1]  # score 0.95, above threshold

    raw = await storage.memory_record_learning(
        session_id="sess_test",
        title="mesh gate does nothing",
        details="observed the gate had no effect",
        project="test",
    )
    resp = json.loads(raw)

    assert resp["status"] == "recorded"
    assert resp["similar_prior"][0]["id"] == "learning_prior"
    assert "review for duplication or contradiction" in resp["note"]
    # the learning was still written
    assert resp["id"] in _patch_collection.added_ids


async def test_record_learning_no_similar_below_threshold(_patch_collection):
    _install_session()
    _patch_collection._ids = ["learning_unrelated"]
    _patch_collection._metas = [_meta("something else entirely")]
    _patch_collection._dists = [1.6]  # score 0.2, below threshold

    raw = await storage.memory_record_learning(
        session_id="sess_test", title="new topic", details="body", project="test"
    )
    resp = json.loads(raw)

    assert resp["status"] == "recorded"
    assert "similar_prior" not in resp
    assert "note" not in resp


async def test_record_learning_survives_lookup_failure(_patch_collection):
    """Fail-safe: an advisory-lookup outage must NOT block recording."""
    _install_session()
    _patch_collection.raise_on_query = True

    raw = await storage.memory_record_learning(
        session_id="sess_test", title="still records", details="body", project="test"
    )
    resp = json.loads(raw)

    assert resp["status"] == "recorded"
    assert "similar_prior" not in resp
    assert resp["id"] in _patch_collection.added_ids  # write happened despite the failure
