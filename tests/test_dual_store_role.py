"""Dual-store role_description consistency (coordinator msg_af23da260f8e).

role_description lives in TWO mongo stores: `registered_agents` (the roster,
written by memory_project) and `agent_directory` (the discovery cache, written
at start_session). Before 2026-08-07 an admin's update_agent wrote only the
roster — so `memory_list_agents`, the surface strangers read to route work,
kept serving the OLD text while the correct value sat where only an admin
registry-get would ever see it. Selectively divergent, silent in both
directions: the writer saw success, the readers saw nothing change.

Contract pinned here: registered_agents is AUTHORITATIVE, agent_directory is a
CACHE. Writes propagate; reads prefer the roster (which heals rows that
diverged before the write-through existed) and flag the divergence rather than
silently correcting it.
"""

import json

import pytest

from shared_memory.state import active_sessions
from shared_memory.tools import messaging as messaging_mod
from shared_memory.tools import projects as projects_mod


class _Cursor(list):
    """List that also answers pymongo's chained .sort(key, direction)."""

    def sort(self, key, direction=1):
        return self


class _DirCol:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.update_calls = []

    def find(self, filt=None, projection=None):
        return _Cursor(self.rows)

    def update_one(self, filt, update, upsert=False):
        self.update_calls.append({"filter": filt, "update": update})
        for r in self.rows:
            if all(r.get(k) == v for k, v in filt.items()):
                r.update(update.get("$set", {}))

    def find_one(self, filt, projection=None):
        for r in self.rows:
            if all(r.get(k) == v for k, v in filt.items()):
                return r
        return None


class _RegCol(_DirCol):
    pass


class _ProjCol:
    def __init__(self, admins):
        self.admins = admins

    def find_one(self, filt, projection=None):
        return {"name": filt.get("name"), "admins": self.admins}

    def update_one(self, *a, **k):
        pass


class _DB:
    def __init__(self, reg_rows, dir_rows, admins=("coordinator",)):
        self.registered_agents = _RegCol(reg_rows)
        self.agent_directory = _DirCol(dir_rows)
        self.projects = _ProjCol(list(admins))


OLD = "Marketing-site relaunch (WP Engine), launch pages + pricing"
NEW = "Marketing site + storefront presence. Owns the WP Engine relaunch."


def _rows(dir_role=OLD, reg_role=OLD):
    reg = [{"project": "nimbus", "name": "wordpress-team", "tier": "named",
            "role_description": reg_role, "spawned_by": None,
            "sunset": None, "purpose": None, "aliases": ["marketing-team"]}]
    dirs = [{"project": "nimbus", "instance": "wordpress-team",
             "role_description": dir_role, "last_seen": "2026-08-07T09:43:00+00:00",
             "session_count": 12, "last_task": ""}]
    return reg, dirs


@pytest.mark.asyncio
async def test_update_agent_writes_through_to_directory(monkeypatch):
    """The regression: a roster write must reach the discovery cache."""
    reg, dirs = _rows()
    db = _DB(reg, dirs)
    active_sessions["s1"] = {"claude_instance": "coordinator", "project": "nimbus"}
    monkeypatch.setattr(projects_mod, "get_mongo", lambda: db)

    out = json.loads(
        await projects_mod.memory_project(
            session_id="s1", action="update_agent", name="nimbus",
            agent="wordpress-team", role_description=NEW,
        )
    )
    assert out["status"] == "updated"
    assert db.registered_agents.rows[0]["role_description"] == NEW
    assert db.agent_directory.rows[0]["role_description"] == NEW, (
        "discovery cache still serving the stale role — the defect"
    )


@pytest.mark.asyncio
async def test_update_without_role_does_not_touch_directory(monkeypatch):
    reg, dirs = _rows()
    db = _DB(reg, dirs)
    active_sessions["s1"] = {"claude_instance": "coordinator", "project": "nimbus"}
    monkeypatch.setattr(projects_mod, "get_mongo", lambda: db)

    await projects_mod.memory_project(
        session_id="s1", action="update_agent", name="nimbus",
        agent="wordpress-team", purpose="marketing",
    )
    assert db.agent_directory.update_calls == []


@pytest.mark.asyncio
async def test_list_agents_prefers_roster_and_flags_stale_cache(monkeypatch):
    """Pre-existing divergence heals on READ, and is reported, not hidden."""
    reg, dirs = _rows(dir_role=OLD, reg_role=NEW)
    db = _DB(reg, dirs)
    active_sessions["s2"] = {"claude_instance": "coordinator", "project": "nimbus"}
    monkeypatch.setattr(messaging_mod, "get_mongo", lambda: db)

    out = json.loads(
        await messaging_mod.memory_list_agents(session_id="s2", project="nimbus")
    )
    row = next(a for a in out["agents"] if a["instance"] == "wordpress-team")
    assert row["role_description"] == NEW, "discovery surface must serve the authoritative role"
    assert row.get("directory_cache_stale") is True


@pytest.mark.asyncio
async def test_list_agents_query_matches_authoritative_text(monkeypatch):
    """A query must match words the caller can actually see in the result."""
    reg, dirs = _rows(dir_role=OLD, reg_role=NEW)
    db = _DB(reg, dirs)
    active_sessions["s3"] = {"claude_instance": "coordinator", "project": "nimbus"}
    monkeypatch.setattr(messaging_mod, "get_mongo", lambda: db)

    out = json.loads(
        await messaging_mod.memory_list_agents(
            session_id="s3", project="nimbus", query="storefront"
        )
    )
    assert [a["instance"] for a in out["agents"]] == ["wordpress-team"]


@pytest.mark.asyncio
async def test_no_false_stale_flag_when_consistent(monkeypatch):
    reg, dirs = _rows(dir_role=NEW, reg_role=NEW)
    db = _DB(reg, dirs)
    active_sessions["s4"] = {"claude_instance": "coordinator", "project": "nimbus"}
    monkeypatch.setattr(messaging_mod, "get_mongo", lambda: db)

    out = json.loads(
        await messaging_mod.memory_list_agents(session_id="s4", project="nimbus")
    )
    row = next(a for a in out["agents"] if a["instance"] == "wordpress-team")
    assert "directory_cache_stale" not in row
