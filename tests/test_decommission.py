"""Mechanism C — temp-agent decommission (identity-lifecycle, named → retired).

Pins: the decommission one-shot (key revocation by naming convention, state-
spec archive, terminal tier flip, directory-row drop, artifact preservation,
live-session refusal), the retired-identity session gate, retired exclusion
from recipient resolution, and the standup sunset-watch rollup.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared_memory.state import active_sessions
from shared_memory.tools import projects as projects_mod


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _Col:
    def __init__(self):
        self.find_one_result = None
        self.find_results = []
        self.update_calls = []
        self.delete_many_calls = []
        self.find_filters = []

    def find_one(self, filt, *a, **k):
        return self.find_one_result

    def find(self, filt=None, *a, **k):
        self.find_filters.append(filt)
        return list(self.find_results)

    def update_one(self, filt, update, upsert=False):
        self.update_calls.append({"filter": filt, "update": update})

    def delete_many(self, filt):
        self.delete_many_calls.append(filt)


class _DB:
    def __init__(self):
        self.projects = _Col()
        self.registered_agents = _Col()
        self.agent_directory = _Col()
        self.api_keys = _Col()


class _SpecColl:
    """Chroma-shaped fake for the state-spec archive path."""

    def __init__(self, has_spec=True):
        self.has_spec = has_spec
        self.update_calls = []

    async def get(self, ids, include=None):
        if self.has_spec:
            return {"ids": list(ids), "metadatas": [{"status": "active", "spec_name": "state:x"}]}
        return {"ids": [], "metadatas": []}

    async def update(self, ids, metadatas):
        self.update_calls.append({"ids": ids, "metadatas": metadatas})


@pytest.fixture
def _wired(monkeypatch):
    db = _DB()
    db.projects.find_one_result = {"name": "junto", "admins": ["human", "tester"]}
    spec_coll = _SpecColl()

    monkeypatch.setattr(projects_mod, "get_mongo", lambda: db)

    import shared_memory.clients as clients_mod
    import shared_memory.helpers as helpers_mod

    async def _fake_get_chroma():
        return object()

    async def _fake_get_project_collection(client, project):
        return spec_coll

    monkeypatch.setattr(clients_mod, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(helpers_mod, "get_project_collection", _fake_get_project_collection)

    import shared_memory.auth as auth_mod
    revoked = []
    monkeypatch.setattr(auth_mod, "revoke_api_key", lambda n: revoked.append(n) or True)

    active_sessions.clear()
    active_sessions["sess_dc"] = {
        "claude_instance": "tester", "project": "junto", "role": "owner",
        "allowed_projects": [], "started_at": "2026-07-12T00:00:00+00:00",
    }
    yield db, spec_coll, revoked
    active_sessions.clear()


async def _decommission(agent="beta-tracker"):
    return json.loads(await projects_mod.memory_project(
        session_id="sess_dc", action="decommission", name="junto", agent=agent
    ))


# ---------------------------------------------------------------------------
# decommission action
# ---------------------------------------------------------------------------

async def test_decommission_happy_path(_wired):
    db, spec_coll, revoked = _wired
    db.registered_agents.find_one_result = {
        "project": "junto", "name": "beta-tracker", "tier": "named",
        "purpose": "cowork beta tracking", "sunset": "2026-07-01",
    }
    db.api_keys.find_results = [{"name": "beta-tracker"}, {"name": "beta-tracker-sync"}]

    resp = await _decommission()

    assert resp["status"] == "decommissioned"
    assert resp["previous_tier"] == "named"
    assert set(resp["keys_revoked"]) == {"beta-tracker", "beta-tracker-sync"}
    assert set(revoked) == {"beta-tracker", "beta-tracker-sync"}
    assert resp["state_spec_archived"] is True
    # terminal tier flip recorded
    tier_sets = [c for c in db.registered_agents.update_calls
                 if c["update"]["$set"].get("tier") == "retired"]
    assert len(tier_sets) == 1
    assert tier_sets[0]["update"]["$set"]["retired_by"] == "tester"
    # directory rows dropped; spec archived with reason
    assert db.agent_directory.delete_many_calls == [
        {"project": "junto", "instance": "beta-tracker"}]
    assert spec_coll.update_calls[0]["metadatas"][0]["status"] == "archived"
    # pulled from admins list
    assert any("$pull" in c["update"] for c in db.projects.update_calls)


async def test_decommission_refused_while_live(_wired):
    db, _, _ = _wired
    db.registered_agents.find_one_result = {
        "project": "junto", "name": "beta-tracker", "tier": "named"}
    active_sessions["sess_live"] = {
        "claude_instance": "beta-tracker", "project": "junto"}
    resp = await _decommission()
    assert "LIVE session" in resp["error"]
    assert db.registered_agents.update_calls == []


async def test_decommission_refused_when_already_retired(_wired):
    db, _, _ = _wired
    db.registered_agents.find_one_result = {
        "project": "junto", "name": "beta-tracker", "tier": "retired",
        "retired_at": "2026-07-01"}
    resp = await _decommission()
    assert "already retired" in resp["error"]


async def test_decommission_survives_missing_spec_and_keys(_wired):
    db, spec_coll, _ = _wired
    spec_coll.has_spec = False
    db.registered_agents.find_one_result = {
        "project": "junto", "name": "beta-tracker", "tier": "pending"}
    db.api_keys.find_results = []
    resp = await _decommission()
    assert resp["status"] == "decommissioned"
    assert resp["keys_revoked"] == []
    assert resp["state_spec_archived"] is False


# ---------------------------------------------------------------------------
# retired exclusions
# ---------------------------------------------------------------------------

def test_resolve_agent_name_excludes_retired():
    class _RosterDB:
        class registered_agents:
            captured = []

            @classmethod
            def find_one(cls, filt, *a, **k):
                cls.captured.append(filt)
                return None

    projects_mod.resolve_agent_name(_RosterDB, "junto", "beta-tracker")
    for filt in _RosterDB.registered_agents.captured:
        assert filt["tier"] == {"$ne": "retired"}


async def test_retired_identity_cannot_start_session(monkeypatch):
    from tests.test_service_principal import (
        _FakeChromaCollection, _FakeMongo,
    )
    from shared_memory.tools import sessions as sessions_mod

    db = _FakeMongo()
    db.projects.find_one = lambda *a, **k: {"name": "junto", "admins": ["human"]}
    db.registered_agents.find_one = lambda *a, **k: {
        "project": "junto", "name": "beta-tracker", "tier": "retired",
        "retired_at": "2026-07-12", "retired_by": "tester",
    }
    monkeypatch.setattr(sessions_mod, "get_mongo", lambda: db)

    async def _fake_get_chroma():
        return object()

    async def _fake_coll(*a, **k):
        return _FakeChromaCollection()

    monkeypatch.setattr(sessions_mod, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(sessions_mod, "get_project_collection", _fake_coll)
    monkeypatch.setattr(sessions_mod, "get_shared_collection", _fake_coll)

    active_sessions.clear()
    raw = await sessions_mod.memory_start_session(
        project="junto", claude_instance="beta-tracker")
    resp = json.loads(raw)
    active_sessions.clear()

    assert resp.get("retired") is True
    assert "RETIRED" in resp["error"]
    assert "session_id" not in resp


# ---------------------------------------------------------------------------
# standup sunset watch + retired filter
# ---------------------------------------------------------------------------

async def test_standup_sunset_watch_and_retired_filter(monkeypatch):
    from shared_memory.tools import standup as standup_mod

    soon = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()

    class _RosterCol:
        find_filters = []

        @classmethod
        def find(cls, filt):
            cls.find_filters.append(filt)
            return [
                {"name": "ending-soon", "tier": "named", "sunset": soon,
                 "purpose": "beta tracking"},
                {"name": "long-lived", "tier": "named", "sunset": far},
                {"name": "no-sunset", "tier": "named"},
            ]

    class _StandupDB:
        registered_agents = _RosterCol

        class projects:
            @staticmethod
            def find_one(filt):
                return {"name": "junto", "owner": "memory"}

    monkeypatch.setattr(standup_mod, "get_mongo", lambda: _StandupDB)

    async def _fake_get_chroma():
        return object()

    class _EmptyColl:
        async def get(self, *a, **k):
            return {"metadatas": [], "documents": []}

    async def _fake_coll(*a, **k):
        return _EmptyColl()

    monkeypatch.setattr(standup_mod, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(standup_mod, "get_project_collection", _fake_coll)

    active_sessions.clear()
    active_sessions["sess_su"] = {
        "claude_instance": "tester", "project": "junto", "role": "owner",
        "allowed_projects": [], "started_at": "2026-07-12T00:00:00+00:00",
        "last_activity": "2026-07-12T00:00:00+00:00",
    }
    raw = await standup_mod.memory_standup(session_id="sess_su", project="junto")
    resp = json.loads(raw)
    active_sessions.clear()

    # roster query excluded retired tier
    assert _RosterCol.find_filters[0].get("tier") == {"$ne": "retired"}
    # only the <=14d sunset surfaces
    watch = resp["sunset_watch"]
    assert [w["agent"] for w in watch] == ["ending-soon"]
    assert watch[0]["overdue"] is False
    assert 0 < watch[0]["days_left"] <= 14
    assert resp["summary"]["sunset_watch"] == 1
