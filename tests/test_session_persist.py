"""Sessions survive a restart: snapshot to Mongo, restore once at start."""

import os
import uuid
from datetime import timedelta

import pytest

from shared_memory import session_persist as sp
from shared_memory.helpers import utc_now
from shared_memory.state import active_sessions


@pytest.fixture
def db():
    url = os.environ.get("JUNTO_TEST_MONGO_URL")
    if not url:
        pytest.skip("JUNTO_TEST_MONGO_URL not set (use a throwaway mongo, not production)")
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("test mongo not reachable")
    name = f"sp_test_{uuid.uuid4().hex[:8]}"
    try:
        yield client[name]
    finally:
        client.drop_database(name)
        client.close()


@pytest.fixture(autouse=True)
def _clean():
    active_sessions.clear()
    yield
    active_sessions.clear()


def _mk(sid, instance="agent", age=timedelta(0)):
    ts = (utc_now() - age).isoformat()
    active_sessions[sid] = {"project": "junto", "claude_instance": instance,
                            "task": "", "started": ts, "last_activity": ts,
                            "role": "agent", "allowed_projects": None}


def test_snapshot_then_restore_round_trips(db):
    _mk("s1")
    _mk("s2", instance="other")
    assert sp.snapshot_once(db) == 2
    saved = dict(active_sessions)
    active_sessions.clear()                      # the restart
    assert sp.restore_once(db) == 2
    assert active_sessions == saved


def test_snapshot_drops_ended_sessions(db):
    _mk("s1")
    _mk("s2")
    sp.snapshot_once(db)
    active_sessions.pop("s2")
    sp.snapshot_once(db)
    assert {r["_id"] for r in db[sp.COLLECTION].find()} == {"s1"}


def test_ephemeral_reporters_not_persisted(db):
    _mk("r1", instance="ctx-reporter-sage")
    _mk("s1")
    assert sp.snapshot_once(db) == 1


def test_restore_skips_hard_ttl_expired_and_keeps_live_ones(db):
    _mk("old", age=timedelta(days=15))
    _mk("new")
    sp.snapshot_once(db)
    active_sessions.clear()
    _mk("new", instance="already-here")          # in-process wins over the row
    assert sp.restore_once(db) == 0
    assert "old" not in active_sessions
    assert active_sessions["new"]["claude_instance"] == "already-here"


def test_restore_without_mongo_raises_so_loop_never_starts():
    with pytest.raises(RuntimeError):
        sp.restore_once(None)
