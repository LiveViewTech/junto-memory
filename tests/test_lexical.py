"""Lexical identifier lane (lexical.py)."""

import asyncio
import uuid

import pytest

from shared_memory.lexical import identifier_tokens, lexical_scan


@pytest.mark.parametrize("text,expected", [
    ("why does cleanup_stale_sessions expire nimbusSupport", ["cleanup_stale_sessions", "nimbusSupport"]),
    ("see msg_0a08d37ec788 and BuyCart.aspx.cs", ["msg_0a08d37ec788", "BuyCart.aspx.cs"]),
    ("src/shared_memory/helpers.py:541", ["src/shared_memory/helpers.py:541"]),
    ("how do sessions expire after a restart", []),
    ("https://example.com/a_b_c", []),
    ("version 2026 release", []),
])
def test_identifier_tokens(text, expected):
    assert identifier_tokens(text) == expected


def test_identifier_tokens_capped_longest_first():
    toks = identifier_tokens("a_bcdef b_cdefgh c_defghi d_efghij")
    assert len(toks) == 3


class _AsyncCol:
    """Async face over a sync in-process chromadb collection."""

    def __init__(self, col):
        self._col = col

    async def query(self, **kw):
        return self._col.query(**kw)


@pytest.fixture
def col():
    chromadb = pytest.importorskip("chromadb")
    client = chromadb.EphemeralClient()
    c = client.create_collection(f"lex_{uuid.uuid4().hex[:8]}")
    c.add(ids=["d1", "d2", "d3"],
          documents=["The idle sweep lives in cleanup_stale_sessions and drops sessions.",
                     "Billing flow: StripeProvider handles refunds.",
                     "Sessions expire when the agent goes quiet."],
          metadatas=[{"title": "t1"}, {"title": "t2"}, {"title": "t3"}])
    yield _AsyncCol(c)


def _vec(c, text):
    return [float(x) for x in c._col._embedding_function([text])[0]]


def test_lexical_scan_finds_exact_identifier(col):
    q = "where is cleanup_stale_sessions"
    hits = asyncio.run(lexical_scan(col, q, _vec(col, q), None, 2))
    assert [h["id"] for h in hits] == ["d1"]
    assert hits[0]["token"] == "cleanup_stale_sessions"


def test_lexical_scan_excludes_ids_and_noops_on_prose(col):
    q = "where is cleanup_stale_sessions"
    assert asyncio.run(lexical_scan(col, q, _vec(col, q), None, 2, exclude_ids={"d1"})) == []
    p = "how do sessions expire"
    assert asyncio.run(lexical_scan(col, p, _vec(col, p), None, 2)) == []


def test_ttl_change_uses_collmod():
    import os
    url = os.environ.get("JUNTO_TEST_MONGO_URL")
    if not url:
        pytest.skip("JUNTO_TEST_MONGO_URL not set")
    from pymongo import MongoClient

    from shared_memory import recall_metrics as rm
    client = MongoClient(url, serverSelectionTimeoutMS=3000)
    db = client[f"rm_{uuid.uuid4().hex[:8]}"]
    try:
        db.recall_events.create_index("ts", expireAfterSeconds=7 * 24 * 3600)   # the old 7d index
        rm._ensure_ttl(db, "recall_events")
        ttl = [i for i in db.recall_events.list_indexes() if i["key"] == {"ts": 1}][0]
        assert ttl["expireAfterSeconds"] == rm.TTL_SECONDS
        rm._indexed = False
        rm.log_recall_event(db, "junto", "a", ["d1"], 0.6, scores=[0.7], query="q" * 5000, matches=[None])
        row = db.recall_events.find_one()
        assert len(row["query"]) == rm.QUERY_LOG_CHARS and row["matches"] == [None]
        rm.log_query_event(db, "junto", "a", "find x", ["d1"], scores=[0.5])
        assert db.query_events.count_documents({}) == 1
    finally:
        rm._indexed = False
        client.drop_database(db.name)
        client.close()
