"""memory_report_context: self-report only for non-admins under JUNTO_REQUIRE_KEY
(interface:context-self-report-v0)."""

import asyncio
import json

import shared_memory.auth as auth
import shared_memory.idle_park as idle_park
import shared_memory.tools.messaging as msg
from shared_memory.state import active_sessions


class _Status:
    def __init__(self):
        self.set = {}

    def update_one(self, q, u, upsert=False):
        self.set[q["instance"]] = u["$set"]


class _Db:
    def __init__(self):
        self.agent_status = _Status()


def _run(monkeypatch, require_key, role):
    db = _Db()
    monkeypatch.setattr(msg, "get_mongo", lambda: db)
    monkeypatch.setattr(auth, "REQUIRE_KEY", require_key)
    monkeypatch.setattr("shared_memory.context_stamp.record_history", lambda *a, **k: None)
    monkeypatch.setattr(idle_park, "pending_resets_for", lambda db, names: [])
    active_sessions["s_rc"] = {"project": "work", "claude_instance": "eric", "role": role}
    try:
        out = json.loads(asyncio.run(msg.memory_report_context("s_rc", reports=[
            {"instance": "eric", "context_tokens": 300_000, "context_window": 1_000_000},
            {"instance": "roy", "context_tokens": 900_000, "context_window": 1_000_000},
        ])))
    finally:
        active_sessions.pop("s_rc", None)
    return out, db


def test_keyed_agent_reports_only_itself(monkeypatch):
    out, db = _run(monkeypatch, True, "agent")
    assert out["reported"] == 1 and set(db.agent_status.set) == {"eric"}
    assert out["skipped"] == ["roy (self-report only)"]


def test_keyed_admin_and_unkeyed_fleet_report_others(monkeypatch):
    out, db = _run(monkeypatch, True, "admin")
    assert out["reported"] == 2
    out, db = _run(monkeypatch, False, "agent")
    assert out["reported"] == 2
