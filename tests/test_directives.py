"""Tests for fleet directives — code-seed, targeting, bundle surfacing, ack.

The primitive that propagates cross-server "here's what you need to do" notices:
code-seeded (travels with deploy to the air-gapped work box), surfaced as a
get_session banner, ack-to-clear, target-filtered.
"""

import asyncio

import pytest

import shared_memory.directives as dr
import shared_memory.tools.directives as tdr
from shared_memory.state import active_sessions


# --- in-memory Mongo fake ----------------------------------------------------
class _Col:
    def __init__(self):
        self.docs = {}
        self._auto = 0

    def _match(self, d, q):
        return all(d.get(k) == v for k, v in q.items())

    def find_one(self, q, projection=None):
        for d in self.docs.values():
            if self._match(d, q):
                return dict(d)
        return None

    def find(self, q, projection=None):
        return [dict(d) for d in self.docs.values() if self._match(d, q)]

    def update_one(self, q, update, upsert=False):
        for d in self.docs.values():
            if self._match(d, q):
                d.update(update.get("$set", {}))
                return
        if upsert:
            doc = {}
            doc.update(q)
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            _id = doc.get("_id")
            if _id is None:
                self._auto += 1
                _id = f"auto{self._auto}"
                doc["_id"] = _id
            self.docs[_id] = doc

    def create_index(self, *a, **k):
        pass


class _DB:
    def __init__(self):
        self.directives = _Col()
        self.directive_acks = _Col()


@pytest.fixture
def db(monkeypatch):
    fake = _DB()
    monkeypatch.setattr(tdr, "get_mongo", lambda: fake)
    # one code directive targeting coordinators
    monkeypatch.setattr(dr, "FLEET_DIRECTIVES", [{
        "key": "rollout-x",
        "title": "Do X",
        "body": "Go do X in the launcher.",
        "target": {"projects": None, "agents": ["coordinator"]},
        "ref": "interface:x-v0",
        "severity": "action",
        "expires_at": None,
    }])
    active_sessions["s_coord"] = {"claude_instance": "coordinator", "project": "junto", "role": "agent"}
    active_sessions["s_worker"] = {"claude_instance": "memory", "project": "junto", "role": "agent"}
    yield fake
    for s in ("s_coord", "s_worker"):
        active_sessions.pop(s, None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- seed --------------------------------------------------------------------
def test_seed_insert_unchanged_deactivate(db):
    r1 = dr.seed_directives(db)
    assert r1["inserted"] == 1 and r1["unchanged"] == 0
    # second boot, no change → zero writes
    r2 = dr.seed_directives(db)
    assert r2["unchanged"] == 1 and r2["inserted"] == 0
    assert db.directives.find_one({"key": "rollout-x"})["active"] is True


def test_seed_deactivates_retired(db, monkeypatch):
    dr.seed_directives(db)
    # retire it in "code"
    monkeypatch.setattr(dr, "FLEET_DIRECTIVES", [])
    r = dr.seed_directives(db)
    assert r["deactivated"] == 1
    assert db.directives.find_one({"key": "rollout-x"})["active"] is False


# --- targeting + surfacing ---------------------------------------------------
def test_pending_targets_coordinator_only(db):
    dr.seed_directives(db)
    coord = dr.get_pending_directives(db, "junto", "coordinator")
    assert [d["key"] for d in coord] == ["rollout-x"]
    # a non-coordinator agent is not targeted
    worker = dr.get_pending_directives(db, "junto", "memory")
    assert worker == []


def test_pending_excludes_expired(db, monkeypatch):
    monkeypatch.setattr(dr, "FLEET_DIRECTIVES", [{
        "key": "old", "title": "t", "body": "b",
        "target": {}, "ref": None, "severity": "info",
        "expires_at": "2000-01-01T00:00:00+00:00",
    }])
    dr.seed_directives(db)
    assert dr.get_pending_directives(db, "junto", "anyone") == []


def test_pending_untargeted_reaches_all(db, monkeypatch):
    monkeypatch.setattr(dr, "FLEET_DIRECTIVES", [{
        "key": "all-hands", "title": "t", "body": "b",
        "target": {}, "ref": None, "severity": "info", "expires_at": None,
    }])
    dr.seed_directives(db)
    assert [d["key"] for d in dr.get_pending_directives(db, "anyproj", "anyagent")] == ["all-hands"]


# --- ack ---------------------------------------------------------------------
def test_ack_clears_for_acker_only(db):
    import json
    dr.seed_directives(db)
    # coordinator acks
    res = json.loads(_run(tdr.memory_ack_directive(session_id="s_coord", key="rollout-x", note="done")))
    assert res["status"] == "acked"
    # no longer surfaces for coordinator@junto
    assert dr.get_pending_directives(db, "junto", "coordinator") == []
    # but a coordinator in ANOTHER project still sees it (per-(key,project,agent) ack)
    assert [d["key"] for d in dr.get_pending_directives(db, "nimbus", "coordinator")] == ["rollout-x"]


def test_ack_rejects_untargeted_agent(db):
    import json
    dr.seed_directives(db)
    res = json.loads(_run(tdr.memory_ack_directive(session_id="s_worker", key="rollout-x")))
    assert "error" in res


def test_ack_unknown_key(db):
    import json
    dr.seed_directives(db)
    res = json.loads(_run(tdr.memory_ack_directive(session_id="s_coord", key="nope")))
    assert "error" in res
