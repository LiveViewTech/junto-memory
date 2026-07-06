"""Unit tests for memory_sync_push — Phase 2 replication write endpoint.

Covers `design:local-first-junto-v0-mvp` v0.5.0 §5.1 (push contract), §4.4
(schema gate), §4.3.a (sequence-skip tolerance), §4.6 (intent-id dedupe),
§7.2 (mutable-spec fast-forward conflict + auto-file backlog), §7.4
(state-spec multi-instance detection).

Strategy: tests target the async `_push_ops` core via fakes that mimic
just enough of Mongo + Chroma. End-to-end the tool wrapper is exercised
via asyncio.run with monkeypatched state. The integration test that
hits real Mongo+Chroma lives in tests/test_sync_push_integration.py.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from shared_memory import auth, op_log, state
from shared_memory.config import ORIGIN_SERVER_ID
from shared_memory.tools import sync as sync_tool


# ───────────────────────────────────────────────────────────────────
# Fakes
# ───────────────────────────────────────────────────────────────────


class _DupKeyError(Exception):
    """Stand-in for pymongo.errors.DuplicateKeyError."""


class _FakeMongoCollection:
    """Minimal in-memory Mongo collection that honors the (origin, seq)
    unique constraint we lean on for race-safe dedupe in _push_ops."""

    def __init__(self, unique_keys: Optional[List[tuple]] = None):
        self._docs: List[Dict[str, Any]] = []
        # Each unique-key spec is a tuple of dotted-field names whose
        # combined values must be unique across the collection.
        self._unique_keys = unique_keys or []

    def _doc_key(self, doc: Dict[str, Any], spec: tuple) -> tuple:
        return tuple(_resolve(doc, k) for k in spec)

    def _check_unique(self, doc: Dict[str, Any]) -> None:
        for spec in self._unique_keys:
            new_key = self._doc_key(doc, spec)
            if any(self._doc_key(d, spec) == new_key for d in self._docs):
                raise _DupKeyError(f"duplicate {spec}: {new_key}")

    def insert_one(self, doc: Dict[str, Any], session=None) -> None:
        self._check_unique(doc)
        self._docs.append(dict(doc))

    def find_one(
        self, query: Dict[str, Any], session=None
    ) -> Optional[Dict[str, Any]]:
        for d in self._docs:
            if _match(d, query):
                return dict(d)
        return None

    def find(
        self,
        query: Optional[Dict[str, Any]] = None,
        projection: Optional[Dict[str, Any]] = None,
        session=None,
    ):
        rows = [dict(d) for d in self._docs if _match(d, query or {})]
        # The fake ignores projection (returns full docs) — callers that
        # depend on field-narrowing for correctness shouldn't; the real
        # driver respects it for wire-size, not for semantics.
        return _FakeCursor(rows)

    def update_one(
        self, query: Dict[str, Any], update: Dict[str, Any], session=None
    ) -> None:
        for d in self._docs:
            if _match(d, query):
                _apply_update(d, update)
                return

    def find_one_and_update(
        self,
        query: Dict[str, Any],
        update: Dict[str, Any],
        upsert: bool = False,
        return_document=None,
        session=None,
    ) -> Optional[Dict[str, Any]]:
        for d in self._docs:
            if _match(d, query):
                _apply_update(d, update)
                return dict(d)
        if upsert:
            base = dict(query)
            _apply_update(base, update)
            self._docs.append(base)
            return dict(base)
        return None

    def distinct(self, field: str) -> List[Any]:
        out = set()
        for d in self._docs:
            v = _resolve(d, field)
            if v is not None:
                out.add(v)
        return sorted(out)

    def count_documents(self, query: Dict[str, Any]) -> int:
        return sum(1 for d in self._docs if _match(d, query))


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def sort(self, spec):
        for field, direction in reversed(spec):
            self._rows.sort(key=lambda r: r.get(field), reverse=(direction < 0))
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def __iter__(self):
        return iter(self._rows)


def _match(row: Dict[str, Any], q: Dict[str, Any]) -> bool:
    for k, v in q.items():
        if k == "$or":
            if not any(_match(row, sub) for sub in v):
                return False
            continue
        actual = _resolve(row, k)
        if isinstance(v, dict):
            for op, arg in v.items():
                if op == "$gt":
                    if not (isinstance(actual, (int, float)) and actual > arg):
                        return False
                elif op == "$lt":
                    if not (isinstance(actual, (int, float)) and actual < arg):
                        return False
                elif op == "$in":
                    if actual not in arg:
                        return False
                elif op == "$exists":
                    present = _has_path(row, k)
                    if present is not arg:
                        return False
                else:  # pragma: no cover
                    raise NotImplementedError(f"fake doesn't support {op}")
        else:
            if actual != v:
                return False
    return True


def _resolve(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _has_path(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def _apply_update(doc: Dict[str, Any], update: Dict[str, Any]) -> None:
    for op, payload in update.items():
        if op == "$set":
            for k, v in payload.items():
                _set_dotted(doc, k, v)
        elif op == "$inc":
            for k, v in payload.items():
                cur = _resolve(doc, k)
                _set_dotted(doc, k, (cur or 0) + v)
        else:
            raise NotImplementedError(f"fake doesn't support {op}")


def _set_dotted(doc, dotted, value):
    parts = dotted.split(".")
    cur = doc
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


class _FakeMongoDB:
    def __init__(self, *, with_op_log_unique: bool = True):
        self._cols: Dict[str, _FakeMongoCollection] = {}
        # op_log enforces (origin, seq) uniqueness — same as the real index.
        self._cols[op_log.OPLOG_COLLECTION] = _FakeMongoCollection(
            unique_keys=[("origin", "seq")] if with_op_log_unique else []
        )
        # op_log_meta is keyed by _id (origin).
        self._cols[op_log.OPLOG_META_COLLECTION] = _FakeMongoCollection(
            unique_keys=[("_id",)]
        )

    def __getitem__(self, name: str) -> _FakeMongoCollection:
        if name not in self._cols:
            self._cols[name] = _FakeMongoCollection()
        return self._cols[name]

    def __getattr__(self, name: str) -> _FakeMongoCollection:
        # mongo.messages style attribute access — only triggers when name
        # isn't a real attribute on the instance. _cols is set in __init__,
        # so attribute lookup for collection names funnels here.
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


class _FakeChromaCollection:
    """Minimal async Chroma collection that captures writes for assertion."""

    def __init__(self, name: str):
        self.name = name
        self._rows: Dict[str, Dict[str, Any]] = {}

    async def add(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        for i, doc_id in enumerate(ids):
            if doc_id in self._rows:
                raise ValueError(f"duplicate id: {doc_id}")
            self._rows[doc_id] = {
                "id": doc_id,
                "document": documents[i] if documents else None,
                "metadata": (metadatas[i] if metadatas else {}) or {},
                "embedding": embeddings[i] if embeddings else None,
            }

    async def upsert(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        for i, doc_id in enumerate(ids):
            row = self._rows.setdefault(doc_id, {"id": doc_id})
            if documents is not None:
                row["document"] = documents[i]
            if metadatas is not None:
                row["metadata"] = metadatas[i] or {}
            if embeddings is not None:
                row["embedding"] = embeddings[i]

    async def update(
        self,
        ids: List[str],
        documents: Optional[List[str]] = None,
        metadatas: Optional[List[Dict[str, Any]]] = None,
        embeddings: Optional[List[List[float]]] = None,
    ) -> None:
        for i, doc_id in enumerate(ids):
            row = self._rows.get(doc_id)
            if row is None:
                continue
            if documents is not None and documents[i] is not None:
                row["document"] = documents[i]
            if metadatas is not None:
                row["metadata"] = metadatas[i] or {}
            if embeddings is not None:
                row["embedding"] = embeddings[i]

    async def delete(self, ids: List[str]) -> None:
        for doc_id in ids:
            self._rows.pop(doc_id, None)

    async def get(
        self,
        ids: Optional[List[str]] = None,
        include: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
    ):
        if ids:
            found_ids = [i for i in ids if i in self._rows]
        else:
            found_ids = list(self._rows.keys())
        result: Dict[str, Any] = {"ids": found_ids}
        if not include or "documents" in include:
            result["documents"] = [self._rows[i].get("document") for i in found_ids]
        if not include or "metadatas" in include:
            result["metadatas"] = [self._rows[i].get("metadata") for i in found_ids]
        if include and "embeddings" in include:
            result["embeddings"] = [self._rows[i].get("embedding") for i in found_ids]
        return result


class _FakeChroma:
    def __init__(self):
        self._collections: Dict[str, _FakeChromaCollection] = {}

    async def get_or_create_collection(self, name: str, metadata=None):
        if name not in self._collections:
            self._collections[name] = _FakeChromaCollection(name)
        return self._collections[name]


# ───────────────────────────────────────────────────────────────────
# Op-row builders
# ───────────────────────────────────────────────────────────────────


PEER_ORIGIN = "lan-spg-office"
EMBEDDING_384 = [0.01] * 384  # canonical 384-d MiniLM vector


def _op(
    op_type: str,
    seq: int,
    *,
    origin: str = PEER_ORIGIN,
    intent_id: Optional[str] = None,
    actor: Optional[Dict[str, Any]] = None,
    ref: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    schema_version: int = 1,
    op_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "_id": op_id or f"op_{op_type.replace('.','_')}_{seq}",
        "seq": seq,
        "ts": "2026-05-15T20:00:00+00:00",
        "origin": origin,
        "intent_id": intent_id,
        "actor": actor or {"agent": "peer-agent", "project": "junto", "session_id": "s"},
        "op_type": op_type,
        "ref": ref or {"collection": "proj_junto", "doc_id": f"doc_{seq}"},
        "payload": payload or {},
        "schema_version": schema_version,
    }


def _learning_op(seq: int, **overrides):
    op = _op(
        "learning.recorded",
        seq,
        ref={"collection": "shared_patterns", "doc_id": f"learning_{seq:016x}"},
        payload={
            "title": f"L{seq}",
            "details": "body",
            "tags": ["a"],
            "created": "2026-05-15T20:00:00+00:00",
            "embedding": EMBEDDING_384,
        },
    )
    op.update(overrides)
    return op


def _store_op(seq: int, **overrides):
    op = _op(
        "store.created",
        seq,
        ref={"collection": "shared_patterns", "doc_id": f"store_{seq:016x}"},
        payload={
            "title": f"S{seq}",
            "content": "body",
            "memory_type": "context",
            "tags": [],
            "files_related": [],
            "interface_name": None,
            "interface_version": None,
            "interface_owner": None,
            "interface_schema": None,
            "expires_at": None,
            "content_hash": "h",
            "created": "2026-05-15T20:00:00+00:00",
            "embedding": EMBEDDING_384,
        },
    )
    op.update(overrides)
    return op


def _function_op(seq: int, **overrides):
    op = _op(
        "function.registered",
        seq,
        ref={"collection": "proj_junto", "doc_id": f"func_{seq:012x}"},
        payload={
            "name": f"f{seq}",
            "file": "x.py:1",
            "purpose": "p",
            "gotchas": None,
            "prefer_over": None,
            "requires": None,
            "has_code": False,
            "code": None,
            "registered_at": "2026-05-15T20:00:00+00:00",
            "embedding": EMBEDDING_384,
        },
    )
    op.update(overrides)
    return op


def _spec_defined_op(seq: int, *, name: str = "design:x", **overrides):
    spec_doc_id = f"spec_{name.replace(':', '_').replace('/', '_')}"
    op = _op(
        "spec.defined",
        seq,
        ref={"collection": "shared_patterns", "doc_id": spec_doc_id},
        payload={
            "spec_name": name,
            "version": "1.0.0",
            "previous_version": None,
            "owner": "peer-agent",
            "spec_type": "design",
            "content": "spec body",
            "tags": [],
            "json_schema": None,
            "project": None,
            "updated_at": "2026-05-15T20:00:00+00:00",
            "embedding": EMBEDDING_384,
        },
    )
    op.update(overrides)
    return op


def _spec_updated_op(
    seq: int,
    *,
    name: str = "design:x",
    version: str = "1.1.0",
    previous_version: str = "1.0.0",
    spec_type: str = "design",
    project: Optional[str] = None,
    owner: str = "peer-agent",
    origin_server_id: Optional[str] = None,
    **overrides,
):
    spec_doc_id = f"spec_{name.replace(':', '_').replace('/', '_')}"
    collection = f"proj_{project}" if project else "shared_patterns"
    payload = {
        "spec_name": name,
        "version": version,
        "previous_version": previous_version,
        "owner": owner,
        "spec_type": spec_type,
        "content": f"v{version} body",
        "tags": [],
        "json_schema": None,
        "project": project,
        "updated_at": "2026-05-15T20:00:00+00:00",
        "embedding": EMBEDDING_384,
    }
    if origin_server_id is not None:
        payload["origin_server_id"] = origin_server_id
    op = _op(
        "spec.updated",
        seq,
        ref={"collection": collection, "doc_id": spec_doc_id},
        payload=payload,
    )
    op.update(overrides)
    return op


def _state_spec_op(
    seq: int,
    *,
    agent: str = "remote-agent",
    project: str = "junto",
    version: str = "1.0.0",
    previous_version: Optional[str] = None,
    origin_server_id: str = PEER_ORIGIN,
    op_type: str = "spec.defined",
    **overrides,
):
    name = f"state:{agent}"
    spec_doc_id = f"spec_{name.replace(':', '_')}"
    payload = {
        "spec_name": name,
        "version": version,
        "previous_version": previous_version,
        "owner": agent,
        "spec_type": "agent_state",
        "content": "state body",
        "tags": [],
        "json_schema": None,
        "project": project,
        "updated_at": "2026-05-15T20:00:00+00:00",
        "embedding": EMBEDDING_384,
        "origin_server_id": origin_server_id,
    }
    op = _op(
        op_type,
        seq,
        actor={"agent": agent, "project": project, "session_id": "s"},
        ref={"collection": f"proj_{project}", "doc_id": spec_doc_id},
        payload=payload,
    )
    op.update(overrides)
    return op


def _message_op(seq: int, *, message_id: Optional[str] = None, **overrides):
    mid = message_id or f"msg_{seq:012x}"
    op = _op(
        "message.sent",
        seq,
        ref={"collection": "messages", "doc_id": mid},
        payload={
            "_id": mid,
            "to_instance": "memory",
            "to_project": "junto",
            "from_instance": "peer-agent",
            "from_project": "junto",
            "from_session": "s",
            "message": "hi",
            "priority": "normal",
            "category": "info",
            "reply_to": None,
            "in_response_to": None,
            "chain_depth": 0,
            "require_human": False,
            "sent_by_human": False,
            "human_interacted": False,
            "user_originated": False,
            "status": "pending",
            "push_suppressed": False,
            "recency_bypass": False,
            "created_at": "2026-05-15T20:00:00+00:00",
            "delivered_at": None,
            "received_at": None,
            "completed_at": None,
        },
    )
    op.update(overrides)
    return op


# ───────────────────────────────────────────────────────────────────
# Helpers to invoke the materializer
# ───────────────────────────────────────────────────────────────────


def _push(db, chroma, ops, *, origin_server_id: str = "central"):
    """Synchronously drive the async core for terse test bodies."""
    return asyncio.run(
        sync_tool._push_ops(
            db=db, chroma=chroma, ops=ops, origin_server_id=origin_server_id
        )
    )


def _dispositions(result):
    return [r["disposition"] for r in result["results"]]


# ───────────────────────────────────────────────────────────────────
# Shape gates
# ───────────────────────────────────────────────────────────────────


def test_push_rejects_op_missing_required_key():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    bad = _learning_op(1)
    del bad["actor"]
    result = _push(db, chroma, [bad])
    assert _dispositions(result) == ["rejected"]
    assert "missing" in result["results"][0]["reason"].lower()


def test_push_rejects_unknown_op_type():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _op("not.a.real.op", 1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["rejected"]
    assert "op_type" in result["results"][0]["reason"]


# ───────────────────────────────────────────────────────────────────
# Schema gate (§4.4)
# ───────────────────────────────────────────────────────────────────


def test_push_halts_on_future_schema_version():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    ops = [
        _learning_op(1),
        _learning_op(2, schema_version=2),
        _learning_op(3),
    ]
    result = _push(db, chroma, ops)

    # First applies, second halts, third is never processed.
    assert result["results"][0]["disposition"] == "applied"
    assert result["results"][1]["disposition"] == "rejected_schema"
    # Per §4.4: halt with clear error. Either the third op is not present
    # in results, or it's marked rejected with a halted-reason. Either way,
    # it MUST NOT have been applied to Chroma.
    coll = chroma._collections.get("shared_patterns")
    if coll is not None:
        assert f"learning_{3:016x}" not in coll._rows


# ───────────────────────────────────────────────────────────────────
# Self-origin reject
# ───────────────────────────────────────────────────────────────────


def test_push_rejects_self_origin_ops():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _learning_op(1, origin="central")
    result = _push(db, chroma, [op], origin_server_id="central")
    assert _dispositions(result) == ["rejected"]
    assert "self" in result["results"][0]["reason"].lower() or \
        "origin" in result["results"][0]["reason"].lower()


# ───────────────────────────────────────────────────────────────────
# Dedupe — (origin, seq)
# ───────────────────────────────────────────────────────────────────


def test_push_dedupes_by_origin_seq():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _learning_op(1)
    first = _push(db, chroma, [op])
    second = _push(db, chroma, [op])
    assert _dispositions(first) == ["applied"]
    assert _dispositions(second) == ["deduped_seq"]


# ───────────────────────────────────────────────────────────────────
# Dedupe — intent_id
# ───────────────────────────────────────────────────────────────────


def test_push_dedupes_by_intent_id():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    iid = "intent-abc"
    a = _learning_op(1, intent_id=iid)
    b = _learning_op(2, intent_id=iid, op_id="op_alt")  # same intent, different seq
    first = _push(db, chroma, [a])
    second = _push(db, chroma, [b])
    assert _dispositions(first) == ["applied"]
    assert _dispositions(second) == ["deduped_intent"]


def test_push_intent_id_null_does_not_dedupe():
    """Multiple ops with intent_id=None must not collapse into each other."""
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    ops = [_learning_op(1, intent_id=None), _learning_op(2, intent_id=None)]
    result = _push(db, chroma, ops)
    assert _dispositions(result) == ["applied", "applied"]


def test_push_batch_dedupe_in_same_call():
    """Duplicate ops within a single batch dedupe against the first."""
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op_a = _learning_op(1)
    op_b = _learning_op(1, op_id="op_dup")  # same (origin, seq), different _id
    op_c = _learning_op(2)
    result = _push(db, chroma, [op_a, op_b, op_c])
    # First applies, second is caught by in-batch dedupe state, third applies.
    assert _dispositions(result) == ["applied", "deduped_seq", "applied"]


def test_push_batch_consecutive_ops_share_monotonicity_state():
    """Two ops in the same batch from the same origin both pass monotonicity."""
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    ops = [_learning_op(1), _learning_op(2)]  # consecutive seqs, same batch
    result = _push(db, chroma, ops)
    assert _dispositions(result) == ["applied", "applied"]


# ───────────────────────────────────────────────────────────────────
# Per-origin monotonicity (§4.3.a sequence-skip tolerance)
# ───────────────────────────────────────────────────────────────────


def test_push_tolerates_sequence_skip():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    first = _push(db, chroma, [_learning_op(1)])
    # seq 2 skipped (the §4.3.a Mongo-down case)
    second = _push(db, chroma, [_learning_op(5)])
    assert _dispositions(first) == ["applied"]
    assert _dispositions(second) == ["applied"]
    assert result_get_flag(second, 0, "sequence_skip") is True


def test_push_rejects_backwards_seq():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    _push(db, chroma, [_learning_op(5)])
    # Re-push seq=3 (older than current max=5, and it's a NEW op_id so
    # neither (origin,seq) nor intent_id catches it).
    backwards = _push(
        db,
        chroma,
        [_learning_op(3, op_id="op_alt_3", intent_id=None)],
    )
    assert _dispositions(backwards) == ["rejected"]


def result_get_flag(result, i, key):
    r = result["results"][i]
    return r.get("flags", {}).get(key) or r.get(key)


# ───────────────────────────────────────────────────────────────────
# Chroma apply
# ───────────────────────────────────────────────────────────────────


def test_push_applies_learning_recorded_with_embedding():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _learning_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    coll = chroma._collections["shared_patterns"]
    doc_id = op["ref"]["doc_id"]
    row = coll._rows[doc_id]
    assert row["embedding"] == EMBEDDING_384
    assert row["metadata"]["title"] == op["payload"]["title"]
    assert row["metadata"]["type"] == "learning"
    assert row["metadata"]["claude_instance"] == op["actor"]["agent"]


def test_push_applies_store_created():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _store_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    coll = chroma._collections["shared_patterns"]
    row = coll._rows[op["ref"]["doc_id"]]
    assert row["embedding"] == EMBEDDING_384
    assert row["metadata"]["type"] == "store" or row["metadata"]["title"] == op["payload"]["title"]


def test_push_applies_function_registered():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _function_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    coll = chroma._collections["proj_junto"]
    row = coll._rows[op["ref"]["doc_id"]]
    assert row["embedding"] == EMBEDDING_384


def test_push_applies_spec_defined():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _spec_defined_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    coll = chroma._collections["shared_patterns"]
    row = coll._rows[op["ref"]["doc_id"]]
    assert row["metadata"]["spec_version"] == "1.0.0"
    assert row["metadata"]["spec_name"] == op["payload"]["spec_name"]


# ───────────────────────────────────────────────────────────────────
# §7.2 spec.updated conflict
# ───────────────────────────────────────────────────────────────────


def test_spec_updated_fast_forward_applies():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    _push(db, chroma, [_spec_defined_op(1, name="design:x")])
    result = _push(db, chroma, [_spec_updated_op(2, name="design:x",
                                                  version="1.1.0",
                                                  previous_version="1.0.0")])
    assert _dispositions(result) == ["applied"]
    coll = chroma._collections["shared_patterns"]
    row = coll._rows["spec_design_x"]
    assert row["metadata"]["spec_version"] == "1.1.0"


def test_spec_updated_version_mismatch_is_conflict_and_files_backlog():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    _push(db, chroma, [_spec_defined_op(1, name="design:x")])  # v1.0.0
    # Locally bump to v2.0.0 to simulate divergence (would normally happen
    # via memory_define_spec; we just inject the new state directly).
    coll = chroma._collections["shared_patterns"]
    coll._rows["spec_design_x"]["metadata"]["spec_version"] = "2.0.0"

    # Peer tries to fast-forward from v1.0.0 → v1.1.0.
    result = _push(db, chroma, [_spec_updated_op(2, name="design:x",
                                                  version="1.1.0",
                                                  previous_version="1.0.0")])
    assert _dispositions(result) == ["conflict"]
    # Receiver kept its version.
    assert coll._rows["spec_design_x"]["metadata"]["spec_version"] == "2.0.0"
    # Backlog item was filed.
    backlog_coll = chroma._collections.get("shared_work") or \
        chroma._collections.get("proj_junto")
    has_backlog = False
    if backlog_coll is not None:
        for row in backlog_coll._rows.values():
            if row["metadata"].get("type") == "backlog" and \
                "design:x" in (row["metadata"].get("title") or ""):
                has_backlog = True
                break
    assert has_backlog


def test_spec_updated_missing_parent_is_conflict():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    # No prior spec.defined — receiver has never seen design:y.
    result = _push(db, chroma, [_spec_updated_op(1, name="design:y",
                                                  version="1.1.0",
                                                  previous_version="1.0.0")])
    assert _dispositions(result) == ["conflict"]
    assert "parent" in result["results"][0]["reason"].lower() or \
        "missing" in result["results"][0]["reason"].lower()


# ───────────────────────────────────────────────────────────────────
# §7.4 state-spec multi-instance detection
# ───────────────────────────────────────────────────────────────────


def test_state_spec_first_push_auto_registers_origin():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _state_spec_op(1, agent="remote-agent", project="junto",
                         origin_server_id=PEER_ORIGIN)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    owner_doc = db["agent_state_owner"].find_one(
        {"project": "junto", "agent": "remote-agent"}
    )
    assert owner_doc is not None
    assert owner_doc["registered_origin"] == PEER_ORIGIN


def test_state_spec_matching_origin_applies():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    _push(db, chroma, [_state_spec_op(1, agent="remote-agent",
                                       origin_server_id=PEER_ORIGIN)])
    result = _push(db, chroma, [_state_spec_op(
        2, agent="remote-agent", op_type="spec.updated",
        version="1.0.1", previous_version="1.0.0",
        origin_server_id=PEER_ORIGIN
    )])
    assert _dispositions(result) == ["applied"]


def test_state_spec_mismatched_origin_is_rejected():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    _push(db, chroma, [_state_spec_op(1, agent="remote-agent",
                                       origin_server_id=PEER_ORIGIN)])
    # Different peer claims same (project, agent) — multi-instance bug.
    other = "lan-other-office"
    result = _push(db, chroma, [_state_spec_op(
        1, agent="remote-agent", op_type="spec.defined",
        version="1.0.0", origin_server_id=other,
        # Use the OTHER peer's origin on the op envelope too.
        origin=other,
    )])
    assert _dispositions(result) == ["rejected_origin_owner"]


# ───────────────────────────────────────────────────────────────────
# Mongo apply
# ───────────────────────────────────────────────────────────────────


def test_push_applies_message_sent():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _message_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    msg_id = op["payload"]["_id"]
    msg = db["messages"].find_one({"_id": msg_id})
    assert msg is not None
    assert msg["message"] == "hi"


def test_push_message_sent_fires_inbox_notify(monkeypatch):
    """Materialized message must wake live inbox subscribers on this peer
    so the junto-inbox plugin's `notifications/resources/updated` fires
    after cross-peer delivery. Without this, replicated messages are
    durable but invisible until the next poll.
    """
    from shared_memory.tools import messaging as messaging_tool

    calls = []

    async def _spy(to_project, to_instance, packet=None):
        calls.append((to_project, to_instance))

    monkeypatch.setattr(messaging_tool, "_notify_inbox_for_send", _spy)

    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _message_op(1)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    assert calls == [("junto", "memory")]


def test_push_message_sent_respects_push_suppressed(monkeypatch):
    """Chain-depth-suppressed messages must NOT fire inbox notify on
    replay either. Same gate as the write-side emit (messaging.py:577).
    Otherwise replication becomes a free auto-delivery channel for
    spirals the depth cap is trying to brake.
    """
    from shared_memory.tools import messaging as messaging_tool

    calls = []

    async def _spy(to_project, to_instance, packet=None):
        calls.append((to_project, to_instance))

    monkeypatch.setattr(messaging_tool, "_notify_inbox_for_send", _spy)

    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _message_op(1)
    op["payload"]["push_suppressed"] = True
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    assert calls == []


def test_push_message_sent_skips_notify_on_duplicate(monkeypatch):
    """A re-applied (duplicate) message must not re-notify subscribers.
    They already saw it on the first arrival; a re-push (replay-from-
    different-peer, race) shouldn't double-ring the doorbell.
    """
    from shared_memory.tools import messaging as messaging_tool
    from shared_memory.tools import sync as sync_tool

    calls = []

    async def _spy(to_project, to_instance, packet=None):
        calls.append((to_project, to_instance))

    monkeypatch.setattr(messaging_tool, "_notify_inbox_for_send", _spy)

    db = _FakeMongoDB()
    # Real Mongo has a unique index on messages._id; the fake doesn't by
    # default, so configure it here to exercise the DuplicateKeyError
    # branch of _apply_message_sent. The fake raises a stand-in
    # `_DupKeyError`; swap the exception class sync.py catches so the
    # branch fires.
    db._cols["messages"] = _FakeMongoCollection(unique_keys=[("_id",)])
    monkeypatch.setattr(sync_tool, "DuplicateKeyError", _DupKeyError)
    chroma = _FakeChroma()
    op = _message_op(1)

    import asyncio as _aio
    _aio.run(sync_tool._apply_message_sent(db, chroma, op))
    assert calls == [("junto", "memory")]

    # Second apply of the same op_id triggers the duplicate branch.
    calls.clear()
    _aio.run(sync_tool._apply_message_sent(db, chroma, op))
    assert calls == []


def test_push_message_sent_swallows_notify_errors(monkeypatch):
    """Notify is best-effort. A failing push must not break apply —
    durability is the contract, the doorbell is a courtesy.
    """
    from shared_memory.tools import messaging as messaging_tool

    async def _boom(to_project, to_instance, packet=None):
        raise RuntimeError("transport gone")

    monkeypatch.setattr(messaging_tool, "_notify_inbox_for_send", _boom)

    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _message_op(2)
    result = _push(db, chroma, [op])
    assert _dispositions(result) == ["applied"]
    msg_id = op["payload"]["_id"]
    assert db["messages"].find_one({"_id": msg_id}) is not None


# ───────────────────────────────────────────────────────────────────
# Local op_log append preserves (origin, seq, intent_id, _id)
# ───────────────────────────────────────────────────────────────────


def test_push_appends_to_local_op_log_preserving_origin_seq():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    op = _learning_op(7, intent_id="iid-7")
    _push(db, chroma, [op])
    stored = db[op_log.OPLOG_COLLECTION].find_one(
        {"origin": PEER_ORIGIN, "seq": 7}
    )
    assert stored is not None
    assert stored["_id"] == op["_id"]
    assert stored["intent_id"] == "iid-7"


def test_push_does_not_advance_local_seq_counter_on_foreign_ops():
    db = _FakeMongoDB()
    chroma = _FakeChroma()
    # Foreign-origin op shouldn't bump the local op_log_meta counter.
    _push(db, chroma, [_learning_op(1)])
    meta_doc = db[op_log.OPLOG_META_COLLECTION].find_one(
        {"_id": "central"}
    )
    assert meta_doc is None  # never touched our own counter


# ───────────────────────────────────────────────────────────────────
# Tool wrapper: auth + JSON
# ───────────────────────────────────────────────────────────────────


def test_push_tool_requires_admin_or_owner():
    state.active_sessions["push_agent"] = {
        "claude_instance": "peer",
        "project": "junto",
        "role": "agent",
    }
    original_auth_enabled = auth.AUTH_ENABLED
    auth.AUTH_ENABLED = True
    try:
        raw = asyncio.run(
            sync_tool.memory_sync_push(
                session_id="push_agent", ops=[_learning_op(1)]
            )
        )
        parsed = json.loads(raw)
        assert "error" in parsed
        assert "permission" in parsed["error"].lower() or \
            "sync" in parsed["error"].lower()
    finally:
        auth.AUTH_ENABLED = original_auth_enabled
        del state.active_sessions["push_agent"]


def test_push_tool_returns_json_envelope():
    state.active_sessions["push_owner"] = {
        "claude_instance": "memory",
        "project": "junto",
        "role": "admin",
    }
    fake_db = _FakeMongoDB()
    fake_chroma = _FakeChroma()
    original_get_mongo = sync_tool.get_mongo
    sync_tool.get_mongo = lambda: fake_db
    # get_chroma is async; patch with an async lambda.
    original_get_chroma = getattr(sync_tool, "get_chroma", None)
    sync_tool.get_chroma = lambda: _async_return(fake_chroma)
    try:
        raw = asyncio.run(
            sync_tool.memory_sync_push(
                session_id="push_owner",
                ops=[_learning_op(1)],
            )
        )
        parsed = json.loads(raw)
        assert "results" in parsed
        assert parsed["results"][0]["disposition"] == "applied"
        assert parsed["applied_count"] == 1
    finally:
        sync_tool.get_mongo = original_get_mongo
        if original_get_chroma is None:
            if hasattr(sync_tool, "get_chroma"):
                delattr(sync_tool, "get_chroma")
        else:
            sync_tool.get_chroma = original_get_chroma
        del state.active_sessions["push_owner"]


async def _async_return(value):
    return value
