"""Verification state (Memento #1, interface:verification-state-v0).

memory_verify writes the state with role + no-self-verify guards; memory_query
and /recall read it: rejected docs drop out, verified docs lead their band but
never jump one, and every result carries `verification`.
"""

import json

import pytest

from shared_memory import __main__ as main_mod
from shared_memory import query_config as qc
from shared_memory.state import active_sessions
from shared_memory.tools import lifecycle as lifecycle_mod
from shared_memory.tools import query as q


# --- memory_verify ------------------------------------------------------------
class _DocCol:
    name = "proj_nimbus"

    def __init__(self, docs):
        self.docs = docs

    async def get(self, ids, include=None):
        found = [i for i in ids if i in self.docs]
        return {"ids": found, "metadatas": [dict(self.docs[i]) for i in found],
                "documents": ["body" for _ in found]}

    async def update(self, ids, metadatas):
        for i, m in zip(ids, metadatas):
            self.docs[i] = m


class _Many:
    def __init__(self):
        self.calls = []

    def update_many(self, f, u):
        self.calls.append((f, u))
        return type("R", (), {"modified_count": 1})()

    def insert_one(self, d):
        self.calls.append(d)


class _DB:
    def __init__(self):
        self.verification_events = _Many()
        self.learning_facets = _Many()
        self.learning_claims = _Many()


@pytest.fixture
def ver(monkeypatch):
    col = _DocCol({
        "learning_a": {"status": "active", "claude_instance": "author"},
    })
    db = _DB()

    async def _chroma():
        return object()

    async def _proj(c, p):
        return col

    async def _shared(c, n):
        return _DocCol({})

    monkeypatch.setattr(lifecycle_mod, "get_chroma", _chroma)
    monkeypatch.setattr(lifecycle_mod, "get_project_collection", _proj)
    monkeypatch.setattr(lifecycle_mod, "get_shared_collection", _shared)
    monkeypatch.setattr(lifecycle_mod, "get_mongo", lambda: db)

    def _sess(name, role):
        active_sessions["sv"] = {"claude_instance": name, "project": "nimbus", "role": role,
                                 "last_activity": "2099-01-01T00:00:00+00:00"}
    yield {"col": col, "db": db, "sess": _sess}
    active_sessions.pop("sv", None)


async def test_admin_verifies_and_audit_is_written(ver):
    ver["sess"]("reviewer", "admin")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "verified"))
    assert out["old_verification"] == "unreviewed" and out["new_verification"] == "verified"
    meta = ver["col"].docs["learning_a"]
    assert meta["verification"] == "verified" and meta["verified_by"] == "reviewer"
    event = ver["db"].verification_events.calls[0]
    assert event["from"] == "unreviewed" and event["to"] == "verified" and event["by_role"] == "admin"
    assert out["facets_synced"] is True


async def test_plain_agent_cannot_verify(ver):
    ver["sess"]("someone", "agent")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "verified"))
    assert "Permission denied" in out["error"]
    assert "verification" not in ver["col"].docs["learning_a"]


async def test_librarian_may_verify_as_agent_role(ver):
    ver["sess"]("librarian", "agent")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "verified"))
    assert out["status"] == "updated"


async def test_no_self_verify_even_as_owner(ver):
    # Keyless deployments resolve every session to owner; this is the guard that bites.
    ver["sess"]("author", "owner")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "verified"))
    assert "authored this doc" in out["error"]


async def test_human_may_verify_own_doc(ver):
    ver["sess"]("author", "user")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "verified"))
    assert out["status"] == "updated"


async def test_reject_needs_reason_and_redacts_it(ver):
    ver["sess"]("reviewer", "admin")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "rejected"))
    assert "reason is required" in out["error"]
    out = json.loads(await lifecycle_mod.memory_verify(
        "sv", "learning_a", "rejected", reason="wrong; real creds are MONGO_PASSWORD=T0mTest99"))
    assert out["new_verification"] == "rejected"
    assert "T0mTest99" not in ver["col"].docs["learning_a"]["verification_reason"]


async def test_invalid_verdict(ver):
    ver["sess"]("reviewer", "admin")
    out = json.loads(await lifecycle_mod.memory_verify("sv", "learning_a", "maybe"))
    assert "Invalid verdict" in out["error"]


# --- memory_query read side ------------------------------------------------------
class _QCol:
    def __init__(self, name, rows):
        self.name, self.rows = name, rows   # rows: (id, dist, extra_meta)

    async def query(self, query_texts=None, query_embeddings=None, n_results=3, where=None):
        rows = self.rows[:n_results]
        metas = [{"title": f"T-{r[0]}", "status": "active", "type": "learning",
                  "updated": "2026-10-01T00:00:00+00:00", **r[2]} for r in rows]
        return {"ids": [[r[0] for r in rows]], "documents": [["body" for _ in rows]],
                "metadatas": [metas], "distances": [[r[1] for r in rows]]}


@pytest.fixture
def mq(monkeypatch):
    holder = {}

    async def _ret(v):
        return v

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(q, "require_session", lambda sid: None)
    monkeypatch.setitem(q.active_sessions, "s", {"claude_instance": "t"})
    monkeypatch.setattr(q, "get_chroma", lambda: _ret(object()))
    monkeypatch.setattr(q, "get_mongo", lambda: None)
    monkeypatch.setattr(q, "get_project_collection", lambda c, p: _ret(holder["proj"]))
    monkeypatch.setattr(q, "get_shared_collection", lambda c, n: _ret(_QCol(n, [])))
    monkeypatch.setattr(q, "embed_query_once", lambda col, text: _ret([0.1]))
    monkeypatch.setattr(q, "update_access_stats", _noop)
    cfg = {"default_expand": True, "default_expand_top": 0, "default_snippet_length": 200,
           "lexical_max": 0, "chunk_index": False, "rerank": False, "rerank_min": 0.5}
    monkeypatch.setattr(qc, "get_effective_config", lambda db, project=None: cfg)
    return holder


def _res(out):
    return json.loads(out)["results"]


async def test_query_drops_rejected_and_tags_every_row(mq):
    mq["proj"] = _QCol("proj_x", [
        ("a", 0.2, {}), ("b", 0.25, {"verification": "rejected"}), ("c", 0.3, {"verification": "verified"}),
    ])
    rows = _res(await q.memory_query("s", "x", project="x", limit=5))
    assert [r["id"] for r in rows] == ["c", "a"]
    assert {r["id"]: r["verification"] for r in rows} == {"c": "verified", "a": "unreviewed"}


async def test_include_inactive_shows_rejected_with_warning(mq):
    mq["proj"] = _QCol("proj_x", [("b", 0.2, {"verification": "rejected"})])
    rows = _res(await q.memory_query("s", "x", project="x", limit=5, include_inactive=True))
    assert rows[0]["id"] == "b" and "REJECTED" in rows[0]["warning"]


async def test_verified_never_jumps_a_band(mq):
    # a: relevance 0.9 (band 0). v: relevance 0.6 (band 1), verified.
    mq["proj"] = _QCol("proj_x", [("a", 0.2, {}), ("v", 0.8, {"verification": "verified"})])
    rows = _res(await q.memory_query("s", "x", project="x", limit=5))
    assert [r["id"] for r in rows] == ["a", "v"]


# --- /recall ----------------------------------------------------------------------
def test_recall_excludes_rejected():
    assert main_mod._recall_excluded("learning_z", {"verification": "rejected"}, None) is True
    assert main_mod._recall_excluded("learning_z", {"verification": "verified"}, None) is False
