"""Cross-encoder rerank for memory_query (interface:rerank-v0)."""

import asyncio
import json

import pytest

import shared_memory.query_config as qc
import shared_memory.rerank as rr
import shared_memory.tools.query as q


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _row(i, sim, mn, key, text="t"):
    return {"id": i, "relevance": f"{sim:.0%}", "_sim": sim, "_min": mn, "_cap_key": key, "_text": text}


# --- apply ----------------------------------------------------------------------
def test_apply_with_scores_filters_and_orders_by_score():
    rows = [_row("a", .9, .3, "project"), _row("b", .2, .3, "project"), _row("c", .5, .4, "context")]
    out = rr.apply(rows, [0.4, 0.95, 0.7], 0.5, {"project": 3, "context": 2})
    assert [r["id"] for r in out] == ["b", "c"]          # a dropped (<0.5); b kept despite low sim
    assert out[0]["relevance"] == "95%"


def test_apply_without_scores_is_exactly_todays_selection():
    rows = [_row("p1", .9, .3, "project"), _row("p2", .8, .3, "project"), _row("p3", .7, .3, "project"),
            _row("p4", .6, .3, "project"), _row("low", .2, .3, "project"),
            _row("c1", .5, .4, "context"), _row("c2", .45, .4, "context"), _row("c3", .44, .4, "context")]
    out = rr.apply(rows, None, 0.5, {"project": 3, "context": 2})
    assert [r["id"] for r in out] == ["p1", "p2", "p3", "c1", "c2"]


def test_score_fails_open(monkeypatch):
    monkeypatch.setattr(rr, "RERANK_URL", "http://127.0.0.1:9")   # nothing listens
    monkeypatch.setattr(rr, "RERANK_TIMEOUT_S", 0.5)
    assert _run(rr.score("q", ["a"])) is None
    assert _run(rr.score("q", [])) == []


# --- memory_query end to end (fakes) -------------------------------------------------
class FakeCol:
    def __init__(self, name, rows):
        self.name, self.rows, self.asked = name, rows, []   # rows: (id, doc, dist)

    async def query(self, query_texts=None, query_embeddings=None, n_results=3, where=None):
        self.asked.append(n_results)
        rows = self.rows[:n_results]
        meta = lambda i: {"title": f"T-{i}", "status": "active", "type": "learning"}  # noqa: E731
        return {"ids": [[r[0] for r in rows]], "documents": [[r[1] for r in rows]],
                "metadatas": [[meta(r[0]) for r in rows]], "distances": [[r[2] for r in rows]]}


@pytest.fixture
def mq(monkeypatch):
    # dist d -> relevance 1 - d/2
    proj = FakeCol("proj_x", [(f"p{i}", f"body {i}", 0.2 + 0.2 * i) for i in range(12)])
    pat, ctx = FakeCol("shared_patterns", []), FakeCol("shared_context", [])
    bumped = []

    async def _ret(v):
        return v
    monkeypatch.setattr(q, "require_session", lambda sid: None)
    monkeypatch.setitem(q.active_sessions, "s", {"claude_instance": "t"})
    monkeypatch.setattr(q, "get_chroma", lambda: _ret(object()))
    monkeypatch.setattr(q, "get_mongo", lambda: None)
    monkeypatch.setattr(q, "get_project_collection", lambda c, p: _ret(proj))
    monkeypatch.setattr(q, "get_shared_collection", lambda c, n: _ret(pat if n == "patterns" else ctx))
    monkeypatch.setattr(q, "embed_query_once", lambda col, text: _ret([0.1]))

    async def _bump(col, i):
        bumped.append(i)
    monkeypatch.setattr(q, "update_access_stats", _bump)
    cfg = {"default_expand": True, "default_expand_top": 0, "default_snippet_length": 200,
           "lexical_max": 0, "chunk_index": False, "rerank": False, "rerank_min": 0.5}
    monkeypatch.setattr(qc, "get_effective_config", lambda db, project=None: cfg)
    return {"proj": proj, "cfg": cfg, "bumped": bumped, "mp": monkeypatch}


def _ids(out):
    return [r["id"] for r in json.loads(out)["results"]]


def test_flag_off_is_unchanged(mq):
    out = _run(q.memory_query("s", "x", project="x", limit=3))
    assert _ids(out) == ["p0", "p1", "p2"] and mq["proj"].asked == [3]
    assert mq["bumped"] == ["p0", "p1", "p2"]


def test_flag_on_reranks_a_wider_pool_and_bumps_only_served(mq):
    mq["cfg"]["rerank"] = True
    seen = {}

    async def fake_score(query, texts):
        seen["n"] = len(texts)
        # favour p7 and p9 (low vector similarity), everything else below rerank_min
        return [0.9 if t.startswith("T-p7") else 0.8 if t.startswith("T-p9") else 0.1 for t in texts]
    mq["mp"].setattr(rr, "score", fake_score)
    out = _run(q.memory_query("s", "x", project="x", limit=3))
    assert mq["proj"].asked == [rr.pool_size(3)] and seen["n"] == 10
    assert set(_ids(out)) == {"p7", "p9"}
    assert sorted(mq["bumped"]) == ["p7", "p9"]
    assert all(not k.startswith("_") for r in json.loads(out)["results"] for k in r)


def test_flag_on_service_down_gives_todays_results(mq):
    mq["cfg"]["rerank"] = True

    async def down(query, texts):
        return None
    mq["mp"].setattr(rr, "score", down)
    out = _run(q.memory_query("s", "x", project="x", limit=3))
    assert _ids(out) == ["p0", "p1", "p2"]
    assert mq["bumped"] == ["p0", "p1", "p2"]
