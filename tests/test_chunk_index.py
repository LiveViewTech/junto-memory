"""Chunk index (interface:chunk-index-v0): chunking, stamp currency, the
fail-open scan, the incremental sweep diff, and /recall lane selection."""

import asyncio

import pytest

import shared_memory.__main__ as m
import shared_memory.auth as auth
import shared_memory.chunk_index as ci
import shared_memory.helpers as helpers
import shared_memory.query_config as qc
from shared_memory.tools.query import calculate_relevance


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _async_return(value):
    async def _f(*a, **kw):
        return value
    return _f


# --- chunking ------------------------------------------------------------------
def test_short_doc_is_one_chunk():
    assert ci.chunk_text("a short note") == ["a short note"]
    assert ci.chunk_text("") == []


def test_long_doc_windows_overlap_and_reach_the_end():
    text = " ".join(f"word{i}" for i in range(600))
    parts = ci.chunk_text(text)
    assert len(parts) >= 3
    assert parts[0].startswith("word0")
    assert parts[-1].endswith("word599")
    # stride < size → consecutive windows share text
    assert parts[1].split()[0] in parts[0]


# --- stamp ---------------------------------------------------------------------
def test_is_current_requires_complete_and_matching_stamp():
    good = {**ci.stamp("0.40.11"), "complete": True}
    assert ci.is_current(good, "0.40.11")
    assert not ci.is_current(good, "0.41.0")                       # Ollama upgraded
    assert not ci.is_current({**good, "complete": False}, "0.40.11")
    assert not ci.is_current({**good, "model": "nomic-embed-text"}, "0.40.11")
    assert not ci.is_current(good, None)                           # embedder down
    assert not ci.is_current(None, "0.40.11")


# --- scan ----------------------------------------------------------------------
class FakeChunkCol:
    def __init__(self, rows, meta=None):
        self.rows = rows          # [(chunk_id, doc_id, cosine_distance)]
        self.metadata = meta or {**ci.stamp("v1"), "complete": True}
        self.seen = None

    async def query(self, query_embeddings, n_results, where=None, include=None):
        self.seen = {"n": n_results, "where": where}
        rows = [r for r in self.rows if ci._matches(self._meta(r), where)][:n_results]
        return {"ids": [[r[0] for r in rows]],
                "metadatas": [[self._meta(r) for r in rows]],
                "distances": [[r[2] for r in rows]]}

    @staticmethod
    def _meta(r):
        return {"doc_id": r[1], "status": r[3] if len(r) > 3 else "active", "type": "learning"}


class FakeSource:
    name = "proj_x"

    def __init__(self, docs):
        self.docs = docs          # {id: (body, meta)}

    async def get(self, ids=None, include=None, limit=None, offset=None):
        ids = [i for i in ids if i in self.docs]
        return {"ids": ids, "documents": [self.docs[i][0] for i in ids],
                "metadatas": [self.docs[i][1] for i in ids]}


class FakeChunkClient:
    def __init__(self, col):
        self.col = col

    async def get_collection(self, name, embedding_function=None):
        if self.col is None:
            raise ValueError("missing")
        return self.col


@pytest.fixture
def lane(monkeypatch):
    def install(chunk_col):
        monkeypatch.setattr(ci, "get_chunk_client", _async_return(FakeChunkClient(chunk_col)))
        monkeypatch.setattr(ci, "ollama_version", _async_return("v1"))
    return install


def test_scan_collapses_chunks_to_docs_and_reads_source(lane):
    col = FakeChunkCol([("a#2", "a", 0.10), ("a#0", "a", 0.20), ("b#0", "b", 0.30),
                        ("gone#0", "gone", 0.35), ("c#1", "c", 0.40)])
    lane(col)
    src = FakeSource({"a": ("A body", {"status": "active"}), "b": ("B body", {"status": "active"}),
                      "c": ("C body", {"status": "active"})})
    got = _run(ci.scan(src, [0.1], 2, {"status": "active"}))
    # unfiltered, deep over-fetch first (Chroma's where pre-filter is slow)
    assert col.seen == {"n": 2 * ci.OVERFETCH * ci.UNFILTERED_FACTOR, "where": None}
    assert got["ids"][0] == ["a", "b"]                 # best chunk per doc, top n
    assert got["documents"][0] == ["A body", "B body"]  # SOURCE body, not the chunk
    # synthetic distance → calculate_relevance gives the chunk cosine
    assert calculate_relevance(got["distances"][0][0]) == pytest.approx(0.90)


def test_scan_filters_on_chunk_meta_then_rechecks_source(lane):
    col = FakeChunkCol([("old#0", "old", 0.05, "superseded"), ("a#0", "a", 0.1),
                        ("lag#0", "lag", 0.15), ("b#0", "b", 0.2)])
    lane(col)
    # "lag" was superseded at the source after the last sweep
    src = FakeSource({"a": ("A", {"status": "active"}), "lag": ("L", {"status": "superseded"}),
                      "b": ("B", {"status": "active"}), "old": ("O", {"status": "superseded"})})
    got = _run(ci.scan(src, [0.1], 5, {"status": "active"}))
    assert got["ids"][0] == ["a", "b"]


def test_scan_requeries_with_where_when_filter_starves(lane, monkeypatch):
    monkeypatch.setattr(ci, "UNFILTERED_FACTOR", 1)
    monkeypatch.setattr(ci, "OVERFETCH", 1)
    rows = [(f"x{i}#0", f"x{i}", 0.01 * i, "superseded") for i in range(3)] + [("a#0", "a", 0.5)]
    col = FakeChunkCol(rows)
    lane(col)
    got = _run(ci.scan(FakeSource({"a": ("A", {"status": "active"})}), [0.1], 1, {"status": "active"}))
    assert got["ids"][0] == ["a"] and col.seen["where"] == {"status": "active"}


def test_matches_handles_the_shapes_callers_build():
    meta = {"status": "active", "type": "learning"}
    assert ci._matches(meta, {"$and": [{"status": "active"}, {"type": {"$in": ["learning"]}}]})
    assert ci._matches(meta, {"status": "active", "type": {"$in": ["learning", "spec"]}})
    assert not ci._matches(meta, {"type": {"$in": ["spec"]}})
    with pytest.raises(ci._Unsupported):
        ci._matches(meta, {"n": {"$gt": 1}})


def test_scan_drops_docs_deleted_since_the_sweep(lane):
    lane(FakeChunkCol([("gone#0", "gone", 0.1), ("a#0", "a", 0.2)]))
    got = _run(ci.scan(FakeSource({"a": ("A", {})}), [0.1], 5, None))
    assert got["ids"][0] == ["a"]


def test_scan_returns_none_when_index_stale_or_missing(lane):
    lane(FakeChunkCol([("a#0", "a", 0.1)], meta={**ci.stamp("v0"), "complete": True}))
    assert _run(ci.scan(FakeSource({"a": ("A", {})}), [0.1], 5, None)) is None
    lane(None)
    assert _run(ci.scan(FakeSource({"a": ("A", {})}), [0.1], 5, None)) is None


# --- incremental sweep ---------------------------------------------------------
class MemCol:
    """In-memory Chroma-ish collection for the sweep diff."""

    def __init__(self, name, metadata=None):
        self.name, self.metadata = name, metadata or {}
        self.rows = {}            # id -> (doc, meta)

    async def count(self):
        return len(self.rows)

    async def get(self, ids=None, limit=None, offset=0, include=None):
        keys = list(self.rows)[offset:offset + (limit or len(self.rows))]
        return {"ids": keys, "documents": [self.rows[k][0] for k in keys],
                "metadatas": [self.rows[k][1] for k in keys]}

    async def upsert(self, ids, embeddings, metadatas):
        for i, md in zip(ids, metadatas):
            self.rows[i] = (None, md)

    async def update(self, ids, metadatas):
        for i, md in zip(ids, metadatas):
            self.rows[i] = (None, md)

    async def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


class MemChroma:
    def __init__(self):
        self.cols = {}

    async def get_collection(self, name, embedding_function=None):
        if name not in self.cols:
            raise ValueError(name)
        return self.cols[name]

    async def create_collection(self, name, embedding_function=None, metadata=None):
        self.cols[name] = MemCol(name, dict(metadata or {}))
        return self.cols[name]

    async def delete_collection(self, name):
        if name not in self.cols:
            raise ValueError(name)
        del self.cols[name]


async def _modify(self, name=None, metadata=None):
    store = _modify.store
    store.cols.pop(self.name)
    self.name, self.metadata = name, metadata
    store.cols[name] = self


def test_sync_builds_then_diffs(monkeypatch):
    cchroma, src_chroma = MemChroma(), MemChroma()
    src = MemCol("proj_x")
    src.rows = {"a": ("alpha body", {"status": "active", "type": "learning"}),
                "b": ("beta body", {"status": "active", "type": "spec"})}
    src_chroma.cols["proj_x"] = src
    _modify.store = cchroma
    monkeypatch.setattr(MemCol, "modify", _modify, raising=False)
    monkeypatch.setattr(ci, "get_chunk_client", _async_return(cchroma))

    async def fake_embed(texts, timeout=600.0):
        return [[0.0] for _ in texts]
    monkeypatch.setattr(ci, "embed", fake_embed)

    s = _run(ci.sync_source(src_chroma, "proj_x", "v1"))
    assert s["rebuilt"] and s["added"] == 2
    live = cchroma.cols["c__proj_x"]
    assert ci.is_current(live.metadata, "v1") and set(live.rows) == {"a#0", "b#0"}

    # no change → no work
    s = _run(ci.sync_source(src_chroma, "proj_x", "v1"))
    assert not s["rebuilt"] and (s["added"], s["changed"], s["meta"], s["deleted"]) == (0, 0, 0, 0)

    # body edit, status flip, delete, new doc
    src.rows["a"] = ("alpha body EDITED", {"status": "active", "type": "learning"})
    src.rows["b"] = ("beta body", {"status": "superseded", "type": "spec"})
    src.rows["c"] = ("gamma", {"status": "active", "type": "learning"})
    del src.rows["a"]
    src.rows["a2"] = ("new", {})
    s = _run(ci.sync_source(src_chroma, "proj_x", "v1"))
    assert (s["added"], s["meta"], s["deleted"]) == (2, 1, 1)
    assert set(live.rows) == {"b#0", "c#0", "a2#0"}
    assert live.rows["b#0"][1]["status"] == "superseded"

    # stamp change (Ollama upgrade) → full rebuild via __next swap
    s = _run(ci.sync_source(src_chroma, "proj_x", "v2"))
    assert s["rebuilt"]
    assert ci.is_current(cchroma.cols["c__proj_x"].metadata, "v2")
    assert "c__proj_x__next" not in cchroma.cols


# --- /recall lane selection ----------------------------------------------------
class FakeCollection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queried = False

    async def query(self, query_texts=None, n_results=5, where=None, query_embeddings=None):
        self.queried = True
        rows = self.rows[:n_results]
        return {"ids": [[r[0] for r in rows]], "metadatas": [[r[1] for r in rows]],
                "distances": [[r[2] for r in rows]], "documents": [[r[3] for r in rows]]}


def _meta(**kw):
    return {"type": "learning", "title": "T", "status": "active",
            "updated": "2026-10-25T00:00:00+00:00", **kw}


@pytest.fixture
def recall(monkeypatch):
    proj = FakeCollection([("learning_minilm00000000", _meta(), 0.4, "minilm body")])
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(m, "get_chroma", _async_return(object()))
    monkeypatch.setattr(m, "get_mongo", lambda: object())
    monkeypatch.setattr(helpers, "get_project_collection", _async_return(proj))
    monkeypatch.setattr(helpers, "get_shared_collection", _async_return(FakeCollection()))
    import shared_memory.facets as facets
    monkeypatch.setattr(facets, "get_facets_for_ids", lambda db, ids: {})
    cfg = {"recall_floor": 0.6, "chunk_recall_floor": 0.5, "chunk_index": False}
    monkeypatch.setattr(qc, "get_effective_config", lambda db, project=None: cfg)
    monkeypatch.setattr(ci, "embed_query", _async_return([0.1]))
    return {"proj": proj, "cfg": cfg}


_EMPTY = {"ids": [[]], "metadatas": [[]], "distances": [[]], "documents": [[]]}


def _chunk_hit(score):
    return {"ids": [["learning_chunk000000000"]], "metadatas": [[_meta()]],
            "distances": [[2.0 * (1 - score)]], "documents": [["chunk-lane body"]]}


def test_recall_flag_off_never_touches_the_chunk_lane(recall, monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("chunk lane used with flag off")
    monkeypatch.setattr(ci, "scan", boom)
    status, p = _run(m._recall_payload({"project": "junto", "query": "q"}, None))
    assert status == 200 and [s["id"] for s in p["snippets"]] == ["learning_minilm00000000"]


def test_recall_flag_on_uses_chunk_floor(recall, monkeypatch):
    recall["cfg"]["chunk_index"] = True
    # 0.55 is below the MiniLM floor (0.6) but above the chunk floor (0.5)
    proj = recall["proj"]
    monkeypatch.setattr(ci, "scan", lambda col, v, n, w: _async_return(
        _chunk_hit(0.55) if col is proj else _EMPTY)())
    status, p = _run(m._recall_payload({"project": "junto", "query": "q"}, None))
    assert status == 200
    assert [s["id"] for s in p["snippets"]] == ["learning_chunk000000000"]
    assert not recall["proj"].queried


def test_recall_flag_on_falls_back_when_lane_unusable(recall, monkeypatch):
    recall["cfg"]["chunk_index"] = True
    monkeypatch.setattr(ci, "scan", _async_return(None))
    status, p = _run(m._recall_payload({"project": "junto", "query": "q"}, None))
    assert status == 200 and [s["id"] for s in p["snippets"]] == ["learning_minilm00000000"]


def test_recall_explicit_threshold_beats_chunk_floor(recall, monkeypatch):
    recall["cfg"]["chunk_index"] = True
    monkeypatch.setattr(ci, "scan", lambda col, v, n, w: _async_return(_chunk_hit(0.55))())
    status, p = _run(m._recall_payload({"project": "junto", "query": "q", "threshold": 0.7}, None))
    assert status == 200 and p["count"] == 0


# --- chunker 3 (title-prefixed) ---------------------------------------------------
def test_chunker3_prefixes_title_and_hashes_it(monkeypatch):
    body = " ".join(f"word{i}" for i in range(600))
    meta = {"title": "Mizar box weights"}
    monkeypatch.setattr(ci, "CHUNKER", 2)
    plain, h2 = ci.doc_texts(body, meta), ci.doc_hash(body, meta)
    assert h2 == ci.body_hash(body)                 # chunker 2 hashes unchanged
    monkeypatch.setattr(ci, "CHUNKER", 3)
    titled = ci.doc_texts(body, meta)
    assert len(titled) == len(plain)
    assert all(t == f"Mizar box weights\n\n{p}" for t, p in zip(titled, plain))
    assert ci.doc_hash(body, meta) != ci.doc_hash(body, {"title": "renamed"})
    assert ci.doc_texts(body, {}) == plain           # no title → no prefix


class _QCfg:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, q, proj=None):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in q.items())), None)


class _QDb:
    def __init__(self, docs):
        self.query_config = _QCfg(docs)


def test_sweep_runs_only_when_some_scope_enables_chunk_index(monkeypatch):
    monkeypatch.setattr(qc, "DEFAULT_CHUNK_INDEX", False)
    assert not ci.any_scope_enabled(None)
    assert not ci.any_scope_enabled(_QDb([{"scope": "junto", "chunk_index": False}]))
    assert ci.any_scope_enabled(_QDb([{"scope": "nimbus", "chunk_index": True}]))
    monkeypatch.setattr(qc, "DEFAULT_CHUNK_INDEX", True)
    assert ci.any_scope_enabled(None)
