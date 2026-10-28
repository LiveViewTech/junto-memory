"""archive_by_tag -> restore_by_tag must return a doc to its pre-archive
status. previous_status was read after status was overwritten, so every
restore set status back to "archived" (a silent no-op)."""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import lifecycle


class _FakeCollection:
    def __init__(self, name, docs):
        self.name = name
        self.docs = docs  # id -> metadata

    async def get(self, where=None, include=None):
        items = [(i, m) for i, m in self.docs.items()
                 if not where or all(m.get(k) == v for k, v in where.items())]
        return {"ids": [i for i, _ in items], "metadatas": [dict(m) for _, m in items]}

    async def update(self, ids, metadatas):
        for i, m in zip(ids, metadatas):
            self.docs[i] = m


class _FakeChroma:
    def __init__(self, cols):
        self.cols = cols

    async def list_collections(self):
        return self.cols


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    monkeypatch.setattr(lifecycle, "emit_op_log_from_context", lambda **kw: None)
    active_sessions.clear()
    active_sessions["sess_test"] = {
        "claude_instance": "tester", "project": "test", "role": "agent",
        "allowed_projects": ["test"], "started_at": "2026-10-25T00:00:00+00:00",
    }
    yield
    active_sessions.clear()


@pytest.mark.parametrize("start_status", ["active", "deprecated"])
async def test_restore_returns_pre_archive_status(monkeypatch, start_status):
    col = _FakeCollection("proj_test", {
        "learning_1": {"title": "t", "status": start_status, "tags": json.dumps(["v5"])},
    })
    chroma = _FakeChroma([col])

    async def _get_chroma():
        return chroma

    monkeypatch.setattr(lifecycle, "get_chroma", _get_chroma)

    await lifecycle.memory_archive_by_tag(session_id="sess_test", tag="v5", project="test")
    assert col.docs["learning_1"]["status"] == "archived"
    assert col.docs["learning_1"]["previous_status"] == start_status

    await lifecycle.memory_restore_by_tag(session_id="sess_test", tag="v5", project="test")
    assert col.docs["learning_1"]["status"] == start_status
