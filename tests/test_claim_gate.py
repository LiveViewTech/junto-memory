"""Write-time contradiction gate — classifier stage (claim_gate.py).

Pins the interface:claim-extraction-v0 v1.0.2 implementation:
  - Stage-2 label parsing (substring, earliest-hit wins, no-match → CONSISTENT)
  - Stage-1 non-empty validation + whitespace collapse + 3000-char head
  - claim cache honoring RECIPE_VERSION (stale rows lazily re-extracted)
  - orchestrator fail-quiet posture (disabled / endpoint down / empty
    extraction all degrade to threshold-only, never raise)
  - memory_record_learning CONTRADICTION note wiring
"""

import json

import pytest

from shared_memory import claim_gate


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """httpx.AsyncClient stand-in: pops canned completions in call order."""

    def __init__(self, completions):
        self.completions = list(completions)
        self.requests = []

    async def post(self, url, json=None):
        self.requests.append({"url": url, "json": json})
        return _FakeResponse(self.completions.pop(0))


class _FakeClaimsCol:
    def __init__(self):
        self.rows = {}
        self.update_calls = 0

    def find_one(self, filt):
        return self.rows.get(filt["_id"])

    def update_one(self, filt, update, upsert=False):
        self.update_calls += 1
        row = self.rows.setdefault(filt["_id"], {"_id": filt["_id"]})
        row.update(update["$set"])


class _FakeDB:
    def __init__(self):
        self._col = _FakeClaimsCol()

    def __getitem__(self, name):
        assert name == claim_gate.CLAIMS_COLLECTION
        return self._col


class _FakeGetCollection:
    """Chroma-shaped fake for .get(ids=..., include=...)."""

    def __init__(self, docs_by_id):
        self.docs_by_id = docs_by_id

    async def get(self, ids, include=None):
        return {"ids": ids, "documents": [self.docs_by_id.get(i) for i in ids]}


@pytest.fixture
def _gate_on(monkeypatch):
    monkeypatch.setenv("JUNTO_CLAIM_GATE_ENABLED", "true")


# ---------------------------------------------------------------------------
# classify_pair — Stage-2 parsing
# ---------------------------------------------------------------------------

async def test_classify_parses_each_label():
    for label in ("CONTRADICTS", "CONSISTENT", "UNRELATED"):
        client = _FakeClient([f" {label}. "])
        got = await claim_gate.classify_pair(client, "at", "ac", "bt", "bc")
        assert got == label


async def test_classify_substring_and_case():
    client = _FakeClient(["I think it contradicts the prior."])
    assert await claim_gate.classify_pair(client, "a", "a", "b", "b") == "CONTRADICTS"


async def test_classify_earliest_hit_wins():
    client = _FakeClient(["UNRELATED (not CONSISTENT)"])
    assert await claim_gate.classify_pair(client, "a", "a", "b", "b") == "UNRELATED"


async def test_classify_no_match_fails_quiet_to_consistent():
    client = _FakeClient(["cannot determine"])
    assert await claim_gate.classify_pair(client, "a", "a", "b", "b") == "CONSISTENT"


async def test_classify_sends_title_anchored_claims():
    client = _FakeClient(["CONSISTENT"])
    await claim_gate.classify_pair(client, "Title A", "claim a", "Title B", "claim b")
    user_msg = client.requests[0]["json"]["messages"][1]["content"]
    assert "CLAIM A (earlier):\nTitle A\nclaim a" in user_msg
    assert "CLAIM B (later):\nTitle B\nclaim b" in user_msg
    assert client.requests[0]["json"]["max_tokens"] == claim_gate.CLASSIFY_MAX_TOKENS
    assert client.requests[0]["json"]["temperature"] == 0


# ---------------------------------------------------------------------------
# extract_claim — Stage-1 validation
# ---------------------------------------------------------------------------

async def test_extract_collapses_whitespace():
    client = _FakeClient(["  The gate\n  is a no-op.  "])
    assert await claim_gate.extract_claim(client, "t", "c") == "The gate is a no-op."


async def test_extract_empty_returns_none():
    client = _FakeClient(["   "])
    assert await claim_gate.extract_claim(client, "t", "c") is None


async def test_extract_truncates_content_head():
    client = _FakeClient(["claim"])
    await claim_gate.extract_claim(client, "title", "x" * 10000)
    user_msg = client.requests[0]["json"]["messages"][1]["content"]
    assert len(user_msg) == len("title\n\n") + claim_gate.CONTENT_HEAD_CHARS


# ---------------------------------------------------------------------------
# claim cache — recipe-version invalidation
# ---------------------------------------------------------------------------

def test_cache_hit_requires_matching_recipe_version():
    db = _FakeDB()
    db._col.rows["learning_x"] = {
        "_id": "learning_x", "claim": "old", "recipe_version": "0.9.0",
    }
    assert claim_gate._cached_claim(db, "learning_x") is None  # stale → miss

    claim_gate._cache_claim(db, "learning_x", "fresh")
    assert claim_gate._cached_claim(db, "learning_x") == "fresh"
    assert db._col.rows["learning_x"]["recipe_version"] == claim_gate.RECIPE_VERSION


async def test_prior_claim_cache_hit_skips_extraction(_gate_on):
    db = _FakeDB()
    claim_gate._cache_claim(db, "learning_p", "cached claim")
    client = _FakeClient([])  # any HTTP call would pop from empty → IndexError
    coll = _FakeGetCollection({})
    got = await claim_gate._prior_claim(db, client, coll, "learning_p", "T")
    assert got == "cached claim"
    assert client.requests == []


# ---------------------------------------------------------------------------
# classify_against_priors — orchestrator
# ---------------------------------------------------------------------------

def _priors():
    return [{"id": "learning_p1", "title": "P1", "score": 0.9, "updated": "u"}]


async def test_orchestrator_disabled_is_passthrough(monkeypatch):
    monkeypatch.delenv("JUNTO_CLAIM_GATE_ENABLED", raising=False)
    priors = _priors()
    got, contradicted = await claim_gate.classify_against_priors(
        None, None, "learning_new", "t", "d", priors
    )
    assert got is priors and contradicted == []
    assert "relationship" not in got[0]


async def test_orchestrator_happy_path_annotates_and_flags(_gate_on, monkeypatch):
    # call order: extract(new), extract(prior p1), classify(p1 vs new)
    client = _FakeClient(["new claim", "prior claim", "CONTRADICTS"])
    monkeypatch.setattr(claim_gate, "httpx", None, raising=False)

    import types

    fake_httpx = types.SimpleNamespace(
        Timeout=lambda *a, **k: None,
        AsyncClient=lambda timeout=None: _AsyncCM(client),
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    db = _FakeDB()
    coll = _FakeGetCollection({"learning_p1": "# P1\n\nprior body"})
    priors, contradicted = await claim_gate.classify_against_priors(
        db, coll, "learning_new", "New T", "new body", _priors()
    )
    assert priors[0]["relationship"] == "CONTRADICTS"
    assert contradicted == ["learning_p1"]
    # both claims cached under the current recipe
    assert db._col.rows["learning_new"]["claim"] == "new claim"
    assert db._col.rows["learning_p1"]["claim"] == "prior claim"


async def test_orchestrator_endpoint_failure_degrades_quietly(_gate_on, monkeypatch):
    import types

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("ollama down")

        async def __aexit__(self, *a):
            return False

    fake_httpx = types.SimpleNamespace(
        Timeout=lambda *a, **k: None, AsyncClient=lambda timeout=None: _Boom()
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    priors = _priors()
    got, contradicted = await claim_gate.classify_against_priors(
        _FakeDB(), None, "learning_new", "t", "d", priors
    )
    assert got == priors and contradicted == []
    assert "relationship" not in got[0]


async def test_orchestrator_empty_extraction_skips(_gate_on, monkeypatch):
    import types

    client = _FakeClient([""])  # extract(new) comes back empty
    fake_httpx = types.SimpleNamespace(
        Timeout=lambda *a, **k: None,
        AsyncClient=lambda timeout=None: _AsyncCM(client),
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    priors = _priors()
    got, contradicted = await claim_gate.classify_against_priors(
        _FakeDB(), None, "learning_new", "t", "d", priors
    )
    assert got == priors and contradicted == []


async def test_orchestrator_caps_classified_priors(_gate_on, monkeypatch):
    import types

    # extract(new) + 4 priors would need 4 extract+classify pairs; cap is 3.
    completions = ["new claim"]
    for _ in range(claim_gate.MAX_CLASSIFY_PRIORS):
        completions += ["prior claim", "CONSISTENT"]
    client = _FakeClient(completions)
    fake_httpx = types.SimpleNamespace(
        Timeout=lambda *a, **k: None,
        AsyncClient=lambda timeout=None: _AsyncCM(client),
    )
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)

    docs = {f"learning_p{i}": f"# P{i}\n\nbody" for i in range(1, 6)}
    priors = [
        {"id": f"learning_p{i}", "title": f"P{i}", "score": 0.9, "updated": "u"}
        for i in range(1, 6)
    ]
    got, _ = await claim_gate.classify_against_priors(
        _FakeDB(), _FakeGetCollection(docs), "learning_new", "t", "d", priors
    )
    annotated = [p for p in got if "relationship" in p]
    assert len(annotated) == claim_gate.MAX_CLASSIFY_PRIORS
    assert len(got) == 5  # un-classified tail still surfaced


class _AsyncCM:
    """async-context-manager wrapper handing back the fake client."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# memory_record_learning wiring — CONTRADICTION note
# ---------------------------------------------------------------------------

async def test_record_learning_contradiction_note(monkeypatch):
    from shared_memory import auth as auth_mod
    from shared_memory.state import active_sessions
    from shared_memory.tools import storage
    from tests.test_write_time_gate import _FakeQueryCollection, _meta

    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    active_sessions["sess_cg"] = {
        "claude_instance": "tester", "project": "test", "role": "agent",
        "allowed_projects": ["test"], "started_at": "2026-07-12T00:00:00+00:00",
    }
    coll = _FakeQueryCollection(
        ["learning_prior"], [_meta("mesh gate is a no-op")], [0.1]
    )

    async def _fake_get_chroma():
        return object()

    async def _fake_get_coll(client, project):
        return coll

    monkeypatch.setattr(storage, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(storage, "get_project_collection", _fake_get_coll)
    monkeypatch.setattr(storage, "get_mongo", lambda: None)
    monkeypatch.setattr(storage, "emit_op_log_from_context", lambda **kw: None)

    async def _fake_fetch_embedding(collection, doc_id):
        return None

    monkeypatch.setattr(storage, "fetch_embedding_for_op_log", _fake_fetch_embedding)

    async def _fake_classify(db, collection, new_doc_id, new_title, new_details, priors):
        priors[0]["relationship"] = "CONTRADICTS"
        return priors, [priors[0]["id"]]

    monkeypatch.setattr(storage.claim_gate, "classify_against_priors", _fake_classify)

    raw = await storage.memory_record_learning(
        session_id="sess_cg", title="gate works", details="it fired", project="test"
    )
    resp = json.loads(raw)
    active_sessions.clear()

    assert resp["status"] == "recorded"
    assert resp["similar_prior"][0]["relationship"] == "CONTRADICTS"
    assert "CONTRADICTION" in resp["note"]
    assert "learning_prior" in resp["note"]
