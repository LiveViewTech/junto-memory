"""Unit tests for design:guideline-trim-v0 server pieces: shared-skill
resolution fallback and the guidelines corpus version."""

import shared_memory.tools.guidelines as gl
import shared_memory.tools.skills as sk


class FakeCol:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, q):
        for d in self.docs:
            if all(d.get(k) == v for k, v in q.items()):
                return d
        return None


def test_resolve_falls_back_to_shared(monkeypatch):
    col = FakeCol([{"_id": "skill_x", "project": "", "name": "parking"}])
    monkeypatch.setitem(sk.active_sessions, "s1", {"project": "nimbus"})
    doc = sk._resolve(col, "s1", "parking", None)
    assert doc is not None and doc["_id"] == "skill_x"


def test_resolve_prefers_project_over_shared(monkeypatch):
    col = FakeCol([
        {"_id": "skill_shared", "project": "", "name": "parking"},
        {"_id": "skill_local", "project": "nimbus", "name": "parking"},
    ])
    monkeypatch.setitem(sk.active_sessions, "s1", {"project": "nimbus"})
    assert sk._resolve(col, "s1", "parking", None)["_id"] == "skill_local"


def test_guidelines_version_default_zero(monkeypatch):
    monkeypatch.setattr(gl, "get_mongo", lambda: None)
    assert gl.get_guidelines_version() == 0


def test_guidelines_version_reads_meta():
    class Meta:
        def find_one(self, q):
            return {"_id": "version", "value": 7}

    class DB:
        guidelines_meta = Meta()

    assert gl.get_guidelines_version(DB()) == 7
