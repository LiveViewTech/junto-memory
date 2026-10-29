"""Mechanism B — pending-agent GC (identity_gc.py) + the two projects.py
ghost-class fixes (no coordinator manufacture at create; narrowed remove guard).

Pins the ratified parameters (coordinator@nimbus msg_7bfb4018d26e):
pending-tier only, grace window, live-session guard, open-obligation BLOCK,
throttled scan, default-disabled.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared_memory import identity_gc


def _ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _Col:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.deleted = []
        self.delete_many_calls = []
        self.find_filters = []
        self.meta = {}

    def find(self, filt=None):
        self.find_filters.append(filt)
        return list(self.rows)

    def find_one(self, filt):
        return self.meta.get(filt.get("_id")) if "_id" in filt else None

    def update_one(self, filt, update, upsert=False):
        if "_id" in filt:
            row = self.meta.setdefault(filt["_id"], {"_id": filt["_id"]})
            row.update(update.get("$set", {}))

    def delete_one(self, filt):
        self.deleted.append(filt)

    def delete_many(self, filt):
        self.delete_many_calls.append(filt)


class _Messages:
    def __init__(self, open_for=None):
        self.open_for = open_for or set()  # {(project, name)}

    def count_documents(self, filt):
        return 1 if (filt["to_project"], filt["to_instance"]) in self.open_for else 0


class _DB:
    def __init__(self, pending_rows, open_obligations=None):
        self.registered_agents = _Col(pending_rows)
        self.agent_directory = _Col()
        self.messages = _Messages(open_obligations)
        self._meta_col = _Col()

    def __getitem__(self, name):
        assert name == identity_gc.META_COLLECTION
        return self._meta_col


@pytest.fixture
def _reap_on(monkeypatch):
    monkeypatch.setenv("JUNTO_PENDING_REAP_ENABLED", "true")


def _row(name, project="junto", last_seen=None):
    return {"project": project, "name": name, "tier": "pending",
            "last_seen": last_seen}


# ---------------------------------------------------------------------------
# GC behavior
# ---------------------------------------------------------------------------

def test_disabled_is_noop(monkeypatch):
    monkeypatch.delenv("JUNTO_PENDING_REAP_ENABLED", raising=False)
    db = _DB([_row("ghost", last_seen=_ago(1000))])
    assert identity_gc.maybe_reap_pending_agents(db, {}) == []
    assert db.registered_agents.deleted == []
    assert db._meta_col.meta == {}  # not even the sweep marker


def test_reaps_stale_pending_only(_reap_on):
    db = _DB([
        _row("ghost", last_seen=_ago(100)),          # stale → reap
        _row("recent", last_seen=_ago(1)),            # inside grace → keep
        _row("never-seen", last_seen=None),            # no last_seen → ancient → reap
    ])
    reaped = identity_gc.maybe_reap_pending_agents(db, {})
    assert {r["agent"] for r in reaped} == {"ghost", "never-seen"}
    # scan was scoped to pending tier
    assert db.registered_agents.find_filters == [{"tier": "pending"}]
    # directory rows cleaned for each reaped agent
    assert {c["instance"] for c in db.agent_directory.delete_many_calls} == {"ghost", "never-seen"}


def test_live_session_guard(_reap_on):
    db = _DB([_row("quiet-but-live", last_seen=_ago(100))])
    sessions = {"sid1": {"project": "junto", "claude_instance": "quiet-but-live"}}
    assert identity_gc.maybe_reap_pending_agents(db, sessions) == []
    assert db.registered_agents.deleted == []


def test_open_obligation_blocks_reap(_reap_on):
    db = _DB(
        [_row("owes-answer", last_seen=_ago(100))],
        open_obligations={("junto", "owes-answer")},
    )
    assert identity_gc.maybe_reap_pending_agents(db, {}) == []
    assert db.registered_agents.deleted == []


def test_throttle_skips_recent_sweep(_reap_on):
    db = _DB([_row("ghost", last_seen=_ago(100))])
    db._meta_col.meta[identity_gc.META_ID] = {
        "_id": identity_gc.META_ID,
        "last_swept": datetime.now(timezone.utc) - timedelta(minutes=2),
    }
    assert identity_gc.maybe_reap_pending_agents(db, {}) == []
    assert db.registered_agents.deleted == []


def test_fresh_row_without_last_seen_protected_by_created_at(_reap_on):
    """Coordinator's None-last_seen edge (msg_35b9ba284875): a hypothetical
    just-created pending row with no last_seen must ride created_at through
    the grace window, not be treated as ancient."""
    fresh = _row("just-added", last_seen=None)
    fresh["created_at"] = _ago(1)  # created an hour ago, inside grace
    db = _DB([fresh])
    assert identity_gc.maybe_reap_pending_agents(db, {}) == []
    assert db.registered_agents.deleted == []


def test_as_utc_handles_naive_iso_and_none():
    naive = datetime(2026, 7, 1, 12, 0, 0)
    assert identity_gc._as_utc(naive).tzinfo is not None
    assert identity_gc._as_utc("2026-07-01T12:00:00Z").tzinfo is not None
    ancient = identity_gc._as_utc(None)
    assert ancient < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# projects.py ghost-class fixes
# ---------------------------------------------------------------------------

class _ProjCol:
    def __init__(self, existing=None):
        self.existing = existing
        self.inserted = []
        self.updated = []

    def find_one(self, filt):
        return self.existing

    def insert_one(self, doc):
        self.inserted.append(doc)

    def update_one(self, filt, update, upsert=False):
        self.updated.append({"filter": filt, "update": update, "upsert": upsert})

    def delete_one(self, filt):
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append(filt)

        class _R:
            deleted_count = 1
        return _R()


class _ProjDB:
    def __init__(self):
        self.projects = _ProjCol()
        self.registered_agents = _ProjCol()


@pytest.fixture
def _proj(monkeypatch):
    from shared_memory.state import active_sessions
    from shared_memory.tools import projects as projects_mod

    db = _ProjDB()
    monkeypatch.setattr(projects_mod, "get_mongo", lambda: db)
    active_sessions["sess_pj"] = {
        "claude_instance": "tester", "project": "junto", "role": "owner",
        "allowed_projects": [], "started_at": "2026-07-12T00:00:00+00:00",
    }
    yield db, projects_mod
    active_sessions.clear()


async def test_create_manufactures_no_coordinator_row(_proj):
    db, projects_mod = _proj
    raw = await projects_mod.memory_project(
        session_id="sess_pj", action="create", name="brand_new"
    )
    resp = json.loads(raw)
    assert resp["status"] == "created"
    assert resp["auto_registered"] == []
    # admins LIST still names coordinator (powers apply when a real one registers)
    assert "coordinator" in resp["admins"]
    # but NO roster row was written
    assert db.registered_agents.inserted == []
    assert db.registered_agents.updated == []


async def test_remove_agent_allows_never_sessioned_coordinator(_proj):
    db, projects_mod = _proj
    db.projects.existing = {"name": "junto", "admins": ["human", "tester"]}
    db.registered_agents.existing = {
        "project": "junto", "name": "coordinator", "session_count": 0,
    }
    raw = await projects_mod.memory_project(
        session_id="sess_pj", action="remove_agent", name="junto", agent="coordinator"
    )
    resp = json.loads(raw)
    assert resp.get("status") == "removed", resp


async def test_remove_agent_still_protects_real_coordinator(_proj):
    db, projects_mod = _proj
    db.projects.existing = {"name": "junto", "admins": ["human", "tester"]}
    db.registered_agents.existing = {
        "project": "junto", "name": "coordinator", "session_count": 41,
    }
    raw = await projects_mod.memory_project(
        session_id="sess_pj", action="remove_agent", name="junto", agent="coordinator"
    )
    resp = json.loads(raw)
    assert "error" in resp
    assert "session history" in resp["error"]
