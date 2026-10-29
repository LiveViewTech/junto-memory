"""memory_change_status scope resolution (coordinator blocker msg_065a525bff50).

Pins the fix for the read-scoping defect family (learning_c5ea0608b8fe4c46):
  1. project omitted → the SESSION's project collection is searched FIRST
     (the old code searched shared-only, so in-project agents could not
     retire their own project's docs).
  2. not-found responses carry `searched` + a project-parameter hint
     (the bare error is what turned a malformed call into a fleet-wide
     "tool is broken" escalation).
  3. explicit project= behavior unchanged.
"""

import json

import pytest

from shared_memory.state import active_sessions
from shared_memory.tools import lifecycle as lifecycle_mod


class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or {}
        self.updates = []

    async def get(self, ids, include=None):
        found = [i for i in ids if i in self.docs]
        return {
            "ids": found,
            "metadatas": [dict(self.docs[i]) for i in found],
            "documents": ["body" for _ in found],
        }

    async def update(self, ids, metadatas):
        self.updates.append((ids, metadatas))
        for i, m in zip(ids, metadatas):
            self.docs[i] = m


@pytest.fixture
def scoped(monkeypatch):
    proj_col = _FakeCollection({"learning_abc": {"status": "active"}})
    shared_cols = {"patterns": _FakeCollection(), "context": _FakeCollection()}

    async def fake_get_chroma():
        return object()

    async def fake_get_project_collection(chroma, project):
        assert project == "nimbus"
        return proj_col

    async def fake_get_shared_collection(chroma, name):
        return shared_cols[name]

    monkeypatch.setattr(lifecycle_mod, "get_chroma", fake_get_chroma)
    monkeypatch.setattr(lifecycle_mod, "get_project_collection",
                        fake_get_project_collection)
    monkeypatch.setattr(lifecycle_mod, "get_shared_collection",
                        fake_get_shared_collection)
    active_sessions["sess1"] = {"claude_instance": "t", "project": "nimbus",
                                "last_activity": "2099-01-01T00:00:00+00:00"}
    yield proj_col
    active_sessions.pop("sess1", None)


async def test_omitted_project_defaults_to_session_project(scoped):
    out = json.loads(await lifecycle_mod.memory_change_status(
        "sess1", "learning_abc", "superseded"))
    assert out["status"] == "updated"
    assert out["old_status"] == "active"
    assert scoped.docs["learning_abc"]["status"] == "superseded"


async def test_not_found_carries_searched_and_hint(scoped):
    out = json.loads(await lifecycle_mod.memory_change_status(
        "sess1", "learning_missing", "superseded"))
    assert "not found" in out["error"].lower()
    assert out["searched"][0] == "nimbus"
    assert "shared:patterns" in out["searched"]
    assert "project parameter" in out["hint"]


async def test_explicit_project_unchanged(scoped):
    out = json.loads(await lifecycle_mod.memory_change_status(
        "sess1", "learning_abc", "deprecated", project="nimbus"))
    assert out["status"] == "updated"
