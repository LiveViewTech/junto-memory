"""Service-principal session mode (design:identity-lifecycle-v0 Mechanism A).

Pins the three properties that make a principal "invisible by construction":
  1. principal=True with no VALIDATED key is rejected (identity = key issuance;
     revocation is the only lifecycle exit, so keyless principals can't exist).
  2. A principal session writes NO registered_agents row and NO agent_directory
     row (including the recency bump), while a normal session writes both.
  3. Session-keyed enumerators filter principals: memory_get_active_work and
     the other_claudes briefing block.
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import query as query_mod
from shared_memory.tools import sessions as sessions_mod


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeCollection:
    def __init__(self):
        self.update_calls = []

    def find_one(self, *a, **k):
        return None

    def find(self, *a, **k):
        return []

    def update_one(self, filt, update, upsert=False):
        self.update_calls.append({"filter": filt, "update": update})


class _FakeMongo:
    def __init__(self):
        self._cols = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._cols.setdefault(name, _FakeCollection())


class _FakeChromaCollection:
    async def query(self, *a, **k):
        return {"documents": [[]], "metadatas": [[]], "ids": [[]], "distances": [[]]}

    async def get(self, *a, **k):
        return {"documents": [], "metadatas": [], "ids": []}


@pytest.fixture
def _wired(monkeypatch):
    """Stub every external surface memory_start_session touches."""
    db = _FakeMongo()
    chroma_coll = _FakeChromaCollection()

    # A registered project row makes the roster path realistic: normal
    # unregistered agents take the auto-register-as-pending branch (the one
    # principals must skip).
    db.projects.find_one = lambda *a, **k: {"name": "junto", "admins": ["human"]}

    monkeypatch.setattr(sessions_mod, "get_mongo", lambda: db)

    async def _fake_get_chroma():
        return object()

    async def _fake_coll(*a, **k):
        return chroma_coll

    monkeypatch.setattr(sessions_mod, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(sessions_mod, "get_project_collection", _fake_coll)
    monkeypatch.setattr(sessions_mod, "get_shared_collection", _fake_coll)
    monkeypatch.setattr(sessions_mod, "get_relevant_locks_for_session", lambda *a: [])
    monkeypatch.setattr(sessions_mod, "get_pending_signals", lambda *a: [])
    monkeypatch.setattr(sessions_mod, "get_blocking_others", lambda *a: [])

    async def _empty_async(*a, **k):
        return []

    monkeypatch.setattr(sessions_mod, "get_recent_modifications", _empty_async)
    monkeypatch.setattr(sessions_mod, "get_interface_updates", _empty_async)

    active_sessions.clear()
    yield db
    active_sessions.clear()


@pytest.fixture
def _auth_on(monkeypatch):
    """AUTH_ENABLED=true soft-auth posture; 'smk_valid' is the only good key."""
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_mod, "REQUIRE_KEY", False)
    monkeypatch.setattr(auth_mod, "TUNNEL_REQUIRES_KEY", False)
    monkeypatch.setattr(auth_mod, "get_header_api_key", lambda: None)

    def _validate(key):
        if key == "smk_valid":
            return {"role": "agent", "projects": ["junto"], "name": "sub-principal"}
        return None

    monkeypatch.setattr(auth_mod, "validate_api_key", _validate)


async def _start(**kw):
    raw = await sessions_mod.memory_start_session(project="junto", **kw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 1. key gate
# ---------------------------------------------------------------------------

async def test_principal_rejected_without_key(_wired, _auth_on):
    resp = await _start(claude_instance="sub-w1", principal=True)
    assert "error" in resp
    assert "validated API key" in resp["error"]
    assert not active_sessions  # no session materialized


async def test_principal_rejected_when_auth_disabled(_wired, monkeypatch):
    # AUTH_ENABLED=false → no key is ever validated → principals impossible.
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    resp = await _start(claude_instance="sub-w1", principal=True)
    assert "error" in resp


# ---------------------------------------------------------------------------
# 2. invisibility: no registry rows
# ---------------------------------------------------------------------------

async def test_principal_writes_no_registry_rows(_wired, _auth_on):
    db = _wired
    resp = await _start(
        claude_instance="sub-w1", principal=True, api_key="smk_valid"
    )
    assert "session_id" in resp, resp
    assert db.registered_agents.update_calls == []
    assert db.agent_directory.update_calls == []  # incl. the recency bump
    assert active_sessions[resp["session_id"]]["is_principal"] is True
    assert active_sessions[resp["session_id"]]["role"] == "agent"


async def test_normal_session_still_registers(_wired, _auth_on):
    db = _wired
    resp = await _start(claude_instance="regular", api_key="smk_valid")
    assert "session_id" in resp, resp
    # auto-register (pending tier) + directory upsert(s) both happened
    assert any(
        c["update"]["$set"].get("tier") == "pending"
        for c in db.registered_agents.update_calls
    )
    assert db.agent_directory.update_calls  # directory row + recency bump
    assert active_sessions[resp["session_id"]]["is_principal"] is False


# ---------------------------------------------------------------------------
# 3. enumeration filters
# ---------------------------------------------------------------------------

async def test_get_active_work_filters_principals(_wired, _auth_on, monkeypatch):
    principal = await _start(
        claude_instance="sub-w1", principal=True, api_key="smk_valid"
    )
    normal = await _start(claude_instance="regular", api_key="smk_valid")

    async def _fake_get_chroma():
        return object()

    async def _fake_shared(client, kind):
        return _FakeChromaCollection()

    monkeypatch.setattr(query_mod, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(query_mod, "get_shared_collection", _fake_shared)

    caller = await _start(claude_instance="observer", api_key="smk_valid")
    raw = await query_mod.memory_get_active_work(session_id=caller["session_id"])
    work = json.loads(raw)

    instances = [w["claude_instance"] for w in work["currently_active"]]
    assert "regular" in instances
    assert "sub-w1" not in instances
    assert normal["session_id"] != principal["session_id"]


async def test_other_claudes_briefing_excludes_principals(_wired, _auth_on):
    await _start(claude_instance="sub-w1", principal=True, api_key="smk_valid")
    await _start(claude_instance="regular", api_key="smk_valid")
    resp = await _start(claude_instance="observer", api_key="smk_valid")

    others = resp.get("other_claudes", [])
    assert any(o.startswith("regular@") for o in others)
    assert not any(o.startswith("sub-w1@") for o in others)
