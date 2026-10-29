"""Unit tests for shared_memory.op_log.

Phase 1 #1 lands the catalog + collection + indexes + seq counter. The
actual writers ship in Phase 1 #2; these tests cover the foundation:

- Closed op_type enumeration is intact and validates as expected.
- Index creation is idempotent and produces the indexes the design calls
  for (seq, (origin, seq) unique, ts, op_type, intent_id sparse).
- next_seq is atomic, monotonic, per-origin, and starts at 1.
"""

import pytest

from shared_memory import op_log


def test_op_types_is_closed_catalog():
    """The catalog matches design v0.3.0 §4.1 minus the 3 autopilot.* entries
    removed in design:autopilot-removal-v0 Phase 2 (23 entries)."""
    assert len(op_log.OP_TYPES) == 23
    # Sanity-check a few representative entries from each category.
    for required in (
        "session.started",
        "message.sent",
        "learning.recorded",
        "function.registered",
        "spec.defined",
        "spec.updated",
        "store.created",
        "backlog.added",
        "lock.acquired",
        "lock.expired",
        "agent.heartbeat",
        "audit.event",
        "rename.applied",
    ):
        assert required in op_log.OP_TYPES, f"missing op_type: {required}"


def test_op_types_unique():
    """Catalog has no duplicates."""
    assert len(set(op_log.OP_TYPES)) == len(op_log.OP_TYPES)


def test_is_valid_op_type():
    assert op_log.is_valid_op_type("message.sent") is True
    assert op_log.is_valid_op_type("not_a_real_op_type") is False
    assert op_log.is_valid_op_type("") is False


def test_schema_version_pinned_at_one():
    assert op_log.OPLOG_SCHEMA_VERSION == 1


def test_collection_names():
    """Collection names are stable contract values."""
    assert op_log.OPLOG_COLLECTION == "op_log"
    assert op_log.OPLOG_META_COLLECTION == "op_log_meta"


class _FakeCollection:
    """Minimal stand-in for a pymongo Collection — records index calls
    and supports the find_one_and_update pattern next_seq uses."""

    def __init__(self):
        self.indexes_created = []  # list of (keys, kwargs)
        self._meta = {}  # {origin: seq}

    def create_index(self, keys, **kwargs):
        self.indexes_created.append((keys, kwargs))

    def find_one_and_update(self, filt, update, upsert=False, return_document=None, session=None):
        origin = filt["_id"]
        delta = update.get("$inc", {}).get("seq", 0)
        if origin not in self._meta:
            if not upsert:
                return None
            self._meta[origin] = 0
        self._meta[origin] += delta
        return {"_id": origin, "seq": self._meta[origin]}


class _FakeDB:
    def __init__(self):
        self._cols = {}

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = _FakeCollection()
        return self._cols[name]


def test_ensure_op_log_indexes_creates_required_indexes():
    db = _FakeDB()
    op_log.ensure_op_log_indexes(db)

    op_log_col = db[op_log.OPLOG_COLLECTION]
    # We expect 5 indexes (seq, (origin, seq), ts, op_type, intent_id-sparse).
    assert len(op_log_col.indexes_created) == 5

    # Pull out for inspection by name/keys.
    keys_seen = [keys for keys, _ in op_log_col.indexes_created]
    assert [("seq", 1)] in keys_seen
    assert [("origin", 1), ("seq", 1)] in keys_seen
    assert [("ts", 1)] in keys_seen
    assert [("op_type", 1)] in keys_seen
    assert [("intent_id", 1)] in keys_seen

    # (origin, seq) must be unique; intent_id must be sparse.
    by_keys = {tuple(k): kw for k, kw in op_log_col.indexes_created}
    assert by_keys[(("origin", 1), ("seq", 1))].get("unique") is True
    assert by_keys[(("intent_id", 1),)].get("sparse") is True


def test_ensure_op_log_indexes_is_idempotent():
    """Calling twice doesn't raise; pymongo's create_index is naturally
    idempotent for matching definitions, and our fake records both calls
    without error."""
    db = _FakeDB()
    op_log.ensure_op_log_indexes(db)
    op_log.ensure_op_log_indexes(db)
    op_log_col = db[op_log.OPLOG_COLLECTION]
    assert len(op_log_col.indexes_created) == 10  # 5 + 5


def test_next_seq_starts_at_one_and_is_monotonic():
    db = _FakeDB()
    assert op_log.next_seq(db, "central") == 1
    assert op_log.next_seq(db, "central") == 2
    assert op_log.next_seq(db, "central") == 3


def test_next_seq_is_per_origin():
    db = _FakeDB()
    assert op_log.next_seq(db, "central") == 1
    assert op_log.next_seq(db, "lan-office") == 1
    assert op_log.next_seq(db, "central") == 2
    assert op_log.next_seq(db, "lan-office") == 2
    assert op_log.next_seq(db, "central") == 3


def test_next_seq_rejects_empty_origin():
    db = _FakeDB()
    with pytest.raises(op_log.OpLogError):
        op_log.next_seq(db, "")
