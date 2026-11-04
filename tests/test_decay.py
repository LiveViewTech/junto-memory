"""Shelf-life decay (Memento #2, interface:ranking-decay-v0)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from shared_memory import query_config as qc
from shared_memory.decay import DECAY_FLOOR, decay_factor
from shared_memory.tools import query as q


def test_half_lives():
    assert decay_factor("volatile", None, 21) == pytest.approx(0.5)
    assert decay_factor("durable", None, 90) == pytest.approx(0.5)
    assert decay_factor("durable", "unreviewed", 45) == pytest.approx(0.5 ** 0.5)


def test_verified_durable_never_decays_but_verified_volatile_does():
    assert decay_factor("durable", "verified", 3650) == 1.0
    assert decay_factor("volatile", "verified", 21) == pytest.approx(0.5)


def test_no_facet_or_no_age_means_no_decay():
    assert decay_factor(None, None, 400) == 1.0
    assert decay_factor("volatile", None, None) == 1.0
    assert decay_factor("volatile", None, -3) == 1.0


def test_floor():
    assert decay_factor("volatile", None, 3650) == DECAY_FLOOR


def test_flag_registered_and_off_by_default():
    assert qc.CONFIG_KEYS["shelf_life_decay"] is bool
    assert qc._default_config_dict()["shelf_life_decay"] is False


# --- memory_query: flag on sinks an old volatile row below a fresh one ---------
class _QCol:
    def __init__(self, rows):
        self.name, self.rows = "proj_x", rows   # (id, dist, updated)

    async def query(self, query_texts=None, query_embeddings=None, n_results=3, where=None):
        rows = self.rows[:n_results]
        metas = [{"title": r[0], "status": "active", "type": "learning", "updated": r[2]} for r in rows]
        return {"ids": [[r[0] for r in rows]], "documents": [["body" for _ in rows]],
                "metadatas": [metas], "distances": [[r[1] for r in rows]]}


class _Facets:
    def __init__(self, rows):
        self.rows = rows

    def find(self, f):
        return [dict(r) for r in self.rows if r["_id"] in f["_id"]["$in"]]


class _DB:
    def __init__(self, rows):
        self.learning_facets = _Facets(rows)

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def mq(monkeypatch):
    async def _ret(v):
        return v

    async def _noop(*a, **k):
        return None
    # learning_old: relevance 0.9 (band 0), volatile, ~1 year old. learning_new: 0.65 (band 1), fresh.
    proj = _QCol([("learning_old", 0.2, "2025-10-30T00:00:00+00:00"),
                  ("learning_new", 0.7, "2026-10-29T00:00:00+00:00")])
    db = _DB([{"_id": "learning_old", "shelf_life": "volatile"},
              {"_id": "learning_new", "shelf_life": "volatile"}])
    monkeypatch.setattr(q, "require_session", lambda sid: None)
    monkeypatch.setitem(q.active_sessions, "s", {"claude_instance": "t"})
    monkeypatch.setattr(q, "get_chroma", lambda: _ret(object()))
    monkeypatch.setattr(q, "get_mongo", lambda: db)
    monkeypatch.setattr(q, "get_project_collection", lambda c, p: _ret(proj))
    monkeypatch.setattr(q, "get_shared_collection", lambda c, n: _ret(_QCol([])))
    monkeypatch.setattr(q, "embed_query_once", lambda col, text: _ret([0.1]))
    monkeypatch.setattr(q, "update_access_stats", _noop)
    cfg = {"default_expand": True, "default_expand_top": 0, "default_snippet_length": 200,
           "lexical_max": 0, "chunk_index": False, "rerank": False, "rerank_min": 0.5,
           "shelf_life_decay": False}
    monkeypatch.setattr(qc, "get_effective_config", lambda db, project=None: cfg)
    return cfg


async def test_flag_off_keeps_todays_order(mq):
    rows = json.loads(await q.memory_query("s", "x", project="x", limit=5))["results"]
    assert [r["id"] for r in rows] == ["learning_old", "learning_new"]
    assert all("decay" not in r for r in rows)


async def test_flag_on_never_moves_a_row_across_bands(mq):
    # v2: the 10-30 replay showed band-crossing hurt memory_query
    # (learning_9fb54b43c3819d1d). The stale 90% row stays in band 0.
    mq["shelf_life_decay"] = True
    rows = json.loads(await q.memory_query("s", "x", project="x", limit=5))["results"]
    assert [r["id"] for r in rows] == ["learning_old", "learning_new"]
    old = rows[0]
    assert old["relevance"] == "90%" and old["decay"] == DECAY_FLOOR
    assert "_decay" not in old


async def test_flag_on_sinks_stale_row_within_its_band(mq, monkeypatch):
    # Same band (both ~80%). The volatile row is NEWER but past its half-life
    # (40 days > 21); the durable row is older but inside its 90-day half-life.
    now = datetime.now(timezone.utc)
    proj = _QCol([("learning_vol", 0.2, (now - timedelta(days=40)).isoformat()),
                  ("learning_dur", 0.2, (now - timedelta(days=60)).isoformat())])
    db = _DB([{"_id": "learning_vol", "shelf_life": "volatile"},
              {"_id": "learning_dur", "shelf_life": "durable"}])

    async def _ret(v):
        return v
    monkeypatch.setattr(q, "get_project_collection", lambda c, p: _ret(proj))
    monkeypatch.setattr(q, "get_mongo", lambda: db)
    rows = json.loads(await q.memory_query("s", "x", project="x", limit=5))["results"]
    assert [r["id"] for r in rows] == ["learning_vol", "learning_dur"]   # off: newest first
    mq["shelf_life_decay"] = True
    rows = json.loads(await q.memory_query("s", "x", project="x", limit=5))["results"]
    assert [r["id"] for r in rows] == ["learning_dur", "learning_vol"]
