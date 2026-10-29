"""get_active_work must not report the inbox plugin's boilerplate task for
every agent (backlog_df486507318c): fall back to the identity's other
session's task, then its newest work-item title."""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.helpers import utc_now_iso
from shared_memory.state import active_sessions
from shared_memory.tools import query

PLUGIN = "junto-inbox channel plugin"


class _WorkCol:
    def __init__(self, items):
        self.items = items

    async def get(self, where=None, include=None):
        rows = [m for m in self.items if not where or m.get("project") == where.get("project")]
        return {"documents": ["x"] * len(rows), "metadatas": rows}


def _sess(inst, task, project="nimbus"):
    now = utc_now_iso()
    return {"claude_instance": inst, "project": project, "task": task, "role": "agent",
            "allowed_projects": [project], "started": now, "last_activity": now}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    active_sessions.clear()
    active_sessions["me"] = _sess("memory", "", project="junto")
    now = utc_now_iso()
    work = _WorkCol([
        {"title": "ship release 62acd907", "claude_instance": "server-team", "project": "nimbus", "updated": now},
    ])

    async def _get_chroma():
        return object()

    async def _get_shared(client, kind):
        return work

    monkeypatch.setattr(query, "get_chroma", _get_chroma)
    monkeypatch.setattr(query, "get_shared_collection", _get_shared)
    yield
    active_sessions.clear()


async def _tasks():
    out = json.loads(await query.memory_get_active_work(session_id="me"))
    return {r["session_id"]: r["task"] for r in out["currently_active"]}


async def test_plugin_task_replaced_by_own_session_task():
    active_sessions["p1"] = _sess("frames-team", PLUGIN)
    active_sessions["o1"] = _sess("frames-team", "playout modes gate")
    t = await _tasks()
    assert t["p1"] == "playout modes gate"
    assert t["o1"] == "playout modes gate"


async def test_plugin_task_falls_back_to_newest_work_item():
    active_sessions["p2"] = _sess("server-team", PLUGIN)
    assert (await _tasks())["p2"] == "ship release 62acd907"


async def test_plugin_task_with_nothing_better_is_blank():
    active_sessions["p3"] = _sess("analyst", PLUGIN)
    assert (await _tasks())["p3"] == ""


async def test_real_task_untouched():
    active_sessions["s1"] = _sess("pipeline", "deploy jobs")
    assert (await _tasks())["s1"] == "deploy jobs"
