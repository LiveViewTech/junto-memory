"""Unit tests for `emit_op_log` + the memory_record_learning canary.

Covers `design:local-first-junto-v0-mvp` v0.4.0 §4.3.a — best-effort
sequential append for Chroma-backed mutation tools.

The contract under test:
- On success: an op-log entry lands with the expected shape (op_type,
  origin, seq monotonic, intent_id stamped, schema_version=1).
- On invalid op_type: emit returns None, no insert attempted.
- On Mongo unavailable (db=None): emit returns None, logged.
- On Mongo insert raising: emit returns None, source write unaffected.
- intent_id auto-pulled from the contextvar via `emit_op_log_from_context`.
"""

import pytest

from shared_memory import op_log
from shared_memory.intent import _set_intent_id, _reset_intent_id


class _FakeOpLogCollection:
    """Records inserts so tests can assert on shape."""

    def __init__(self, raise_on_insert: Exception | None = None):
        self.inserts: list[dict] = []
        self._raise = raise_on_insert

    def insert_one(self, doc):
        if self._raise is not None:
            raise self._raise
        self.inserts.append(doc)

    def create_index(self, *args, **kwargs):
        pass  # not exercised here


class _FakeMetaCollection:
    """Implements just enough of find_one_and_update for next_seq."""

    def __init__(self):
        self._meta: dict[str, int] = {}

    def find_one_and_update(self, filt, update, upsert=False, return_document=None, session=None):
        origin = filt["_id"]
        delta = update.get("$inc", {}).get("seq", 0)
        if origin not in self._meta:
            if not upsert:
                return None
            self._meta[origin] = 0
        self._meta[origin] += delta
        return {"_id": origin, "seq": self._meta[origin]}

    def create_index(self, *args, **kwargs):
        pass


class _FakeDB:
    def __init__(self, raise_on_insert: Exception | None = None):
        self._cols = {
            op_log.OPLOG_COLLECTION: _FakeOpLogCollection(raise_on_insert),
            op_log.OPLOG_META_COLLECTION: _FakeMetaCollection(),
        }

    def __getitem__(self, name):
        if name not in self._cols:
            self._cols[name] = _FakeOpLogCollection()
        return self._cols[name]


def _actor():
    return {"agent": "memory", "project": "junto", "session_id": "test_session"}


def _ref():
    return {"collection": "shared_patterns", "doc_id": "learning_abc123"}


def test_emit_op_log_success_shape():
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="learning.recorded",
        actor=_actor(),
        ref=_ref(),
        payload={"title": "t", "details": "d", "tags": []},
        intent_id=None,
    )
    assert entry is not None
    assert entry["op_type"] == "learning.recorded"
    assert entry["seq"] == 1
    assert entry["intent_id"] is None
    assert entry["schema_version"] == 1
    assert entry["_id"].startswith("op_")
    assert entry["origin"]  # ORIGIN_SERVER_ID env default is "central"
    assert entry["actor"] == _actor()
    assert entry["ref"] == _ref()

    inserts = db[op_log.OPLOG_COLLECTION].inserts
    assert len(inserts) == 1
    assert inserts[0] is entry


def test_emit_op_log_monotonic_seq():
    db = _FakeDB()
    e1 = op_log.emit_op_log(db, "learning.recorded", _actor(), _ref(), {})
    e2 = op_log.emit_op_log(db, "learning.recorded", _actor(), _ref(), {})
    e3 = op_log.emit_op_log(db, "message.sent", _actor(), _ref(), {})
    assert e1["seq"] == 1
    assert e2["seq"] == 2
    assert e3["seq"] == 3


def test_emit_op_log_with_intent_id():
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="learning.recorded",
        actor=_actor(),
        ref=_ref(),
        payload={},
        intent_id="11111111-2222-3333-4444-555555555555",
    )
    assert entry["intent_id"] == "11111111-2222-3333-4444-555555555555"


def test_emit_op_log_rejects_invalid_op_type():
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="not_a_real_op_type",
        actor=_actor(),
        ref=_ref(),
        payload={},
    )
    assert entry is None
    # No insert should have been attempted; no seq increment either.
    assert db[op_log.OPLOG_COLLECTION].inserts == []
    assert db[op_log.OPLOG_META_COLLECTION]._meta == {}


def test_emit_op_log_handles_none_db():
    # Mongo unavailable (clients.get_mongo() returned None). Must not raise.
    entry = op_log.emit_op_log(
        db=None,
        op_type="learning.recorded",
        actor=_actor(),
        ref=_ref(),
        payload={},
    )
    assert entry is None


def test_emit_op_log_swallows_insert_exception():
    # The realistic §4.3.a failure: Mongo's network blip while Chroma is fine.
    # The source write has already landed; emit must NOT raise — it logs and
    # returns None so the calling tool returns success to the user.
    db = _FakeDB(raise_on_insert=RuntimeError("simulated mongo unavailability"))
    entry = op_log.emit_op_log(
        db=db,
        op_type="learning.recorded",
        actor=_actor(),
        ref=_ref(),
        payload={},
    )
    assert entry is None


def test_emit_op_log_from_context_reads_contextvar():
    db = _FakeDB()
    token = _set_intent_id("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    try:
        entry = op_log.emit_op_log_from_context(
            db=db,
            op_type="learning.recorded",
            actor=_actor(),
            ref=_ref(),
            payload={},
        )
    finally:
        _reset_intent_id(token)
    assert entry["intent_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_emit_op_log_from_context_no_intent_default():
    db = _FakeDB()
    # No contextvar set → intent_id should be None on the entry.
    entry = op_log.emit_op_log_from_context(
        db=db,
        op_type="learning.recorded",
        actor=_actor(),
        ref=_ref(),
        payload={},
    )
    assert entry["intent_id"] is None


# --- Canary 2/13: memory_store → store.created --------------------------------


def test_store_created_accepted_and_shape():
    """memory_store's canary op_type must round-trip through the helper."""
    db = _FakeDB()
    payload = {
        "title": "API spec for /foo",
        "content": "## Foo\nbody...",
        "memory_type": "api_spec",
        "tags": ["api", "foo"],
        "files_related": ["src/foo.py"],
        "interface_name": None,
        "interface_version": None,
        "interface_owner": None,
        "interface_schema": None,
        "expires_at": None,
        "content_hash": "abcd" * 8,
        "created": "2026-05-14T12:00:00+00:00",
    }
    entry = op_log.emit_op_log(
        db=db,
        op_type="store.created",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "doc_abc"},
        payload=payload,
    )
    assert entry is not None
    assert entry["op_type"] == "store.created"
    assert entry["ref"] == {"collection": "proj_junto", "doc_id": "doc_abc"}
    assert entry["payload"] is payload  # full payload threaded for sync replay


def test_store_created_interface_path_collection_name():
    """The interface-upsert path lands rows in the shared_patterns bucket
    when no project is given. The op-log ref.collection must match."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="store.created",
        actor=_actor(),
        ref={"collection": "shared_patterns", "doc_id": "interface_mqtt_frame_status"},
        payload={
            "title": "MQTT frame-status contract",
            "memory_type": "interface",
            "interface_name": "mqtt:frame-status",
            "interface_version": "1.2",
            "interface_owner": "frames-team",
        },
    )
    assert entry["ref"]["collection"] == "shared_patterns"
    assert entry["payload"]["interface_name"] == "mqtt:frame-status"


def test_emit_op_log_monotonic_across_three_canary_types():
    """Cross-type monotonic seq spanning the two Chroma canaries + the
    Mongo canary still on deck. Locks in that the counter is global per
    origin, not partitioned by op_type."""
    db = _FakeDB()
    e1 = op_log.emit_op_log(db, "learning.recorded", _actor(), _ref(), {})
    e2 = op_log.emit_op_log(db, "store.created", _actor(), _ref(), {})
    e3 = op_log.emit_op_log(db, "store.created", _actor(), _ref(), {})
    e4 = op_log.emit_op_log(db, "message.sent", _actor(), _ref(), {})
    assert [e1["seq"], e2["seq"], e3["seq"], e4["seq"]] == [1, 2, 3, 4]


def test_op_types_catalog_size_is_v1_locked():
    """§4.1 was closed at 26 entries for MVP; the 3 autopilot.* entries were
    removed by documented amendment (design:autopilot-removal-v0 Phase 2),
    closing it at 23. Adding a 24th requires a documented amendment. This
    guards against silent catalog drift."""
    assert len(op_log.OP_TYPES) == 23
    assert "store.created" in op_log.OP_TYPES
    assert "learning.recorded" in op_log.OP_TYPES
    assert "message.sent" in op_log.OP_TYPES


# --- Canary 3/13: memory_register_function → function.registered --------------


def test_function_registered_accepted_and_shape():
    """memory_register_function's canary op_type must round-trip and carry
    the full registration args so a peer replay can reconstruct the doc."""
    db = _FakeDB()
    payload = {
        "name": "parse_email",
        "file": "src/parser.py:145",
        "purpose": "Parse raw email into structured fields",
        "gotchas": "Use over v1 — attachment-handling bug",
        "prefer_over": "parse_email_v1",
        "requires": ["init_parser"],
        "has_code": True,
        "code": "def parse_email(raw):\n    ...",
        "registered_at": "2026-05-14T13:00:00+00:00",
    }
    entry = op_log.emit_op_log(
        db=db,
        op_type="function.registered",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "func_abc123def456"},
        payload=payload,
    )
    assert entry is not None
    assert entry["op_type"] == "function.registered"
    assert entry["ref"]["collection"] == "proj_junto"
    assert entry["ref"]["doc_id"].startswith("func_")
    assert entry["payload"]["code"] == "def parse_email(raw):\n    ..."
    assert entry["payload"]["has_code"] is True


def test_function_registered_no_code_path():
    """Minimal registration (no `code` arg) — payload.code is None, has_code
    False. Sync replay won't rebuild a code block but the registration row
    itself replays correctly."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="function.registered",
        actor=_actor(),
        ref={"collection": "shared_patterns", "doc_id": "func_999"},
        payload={
            "name": "get_user",
            "file": "src/users.py:45",
            "purpose": "Fetch user by ID",
            "gotchas": None,
            "prefer_over": None,
            "requires": [],
            "has_code": False,
            "code": None,
            "registered_at": "2026-05-14T13:00:00+00:00",
        },
    )
    assert entry["payload"]["has_code"] is False
    assert entry["payload"]["code"] is None
    assert entry["ref"]["collection"] == "shared_patterns"


def test_emit_op_log_monotonic_across_three_canary_ops_so_far():
    """Cross-type monotonic seq across the three Chroma canaries shipped to
    date: learning.recorded, store.created, function.registered."""
    db = _FakeDB()
    e1 = op_log.emit_op_log(db, "learning.recorded", _actor(), _ref(), {})
    e2 = op_log.emit_op_log(db, "store.created", _actor(), _ref(), {})
    e3 = op_log.emit_op_log(db, "function.registered", _actor(), _ref(), {})
    assert [e1["seq"], e2["seq"], e3["seq"]] == [1, 2, 3]


# --- Canary 4/13: memory_enrich_function → function.enriched ------------------


def test_function_enriched_accepted_and_shape():
    """function.enriched carries the librarian-analysis fields. Sync replay
    re-applies them on top of the function.registered row that landed
    earlier in the same op-log sequence."""
    db = _FakeDB()
    payload = {
        "signature": "def parse_email(raw: bytes) -> Email:",
        "parameters": [
            {"name": "raw", "type": "bytes", "description": "Raw RFC822 bytes"},
        ],
        "returns": "Email — structured fields, attachments parsed",
        "calls": ["decode_mime", "extract_attachments"],
        "called_by": ["triage_inbox"],
        "side_effects": [],
        "complexity": "O(n) over message body",
        "additional_gotchas": "Returns Email with empty body when MIME parse fails",
        "search_summary": "Parses raw email into structured Email object",
        "enriched_at": "2026-05-14T13:30:00+00:00",
    }
    entry = op_log.emit_op_log(
        db=db,
        op_type="function.enriched",
        actor=_actor(),
        ref={"collection": "proj_emailtriage", "doc_id": "func_abc123def456"},
        payload=payload,
    )
    assert entry is not None
    assert entry["op_type"] == "function.enriched"
    assert entry["payload"]["search_summary"].startswith("Parses raw email")
    assert entry["payload"]["calls"] == ["decode_mime", "extract_attachments"]


# --- Canary 5/13: memory_define_spec → spec.defined / spec.updated ------------


def test_spec_defined_on_first_version():
    """First call to memory_define_spec(name=X) emits spec.defined with
    previous_version=None."""
    db = _FakeDB()
    payload = {
        "spec_name": "mqtt:frame-status",
        "version": "1.0.0",
        "previous_version": None,
        "owner": "frames-team",
        "spec_type": "interface",
        "content": "# Frame status contract\n...",
        "tags": ["mqtt", "frames"],
        "json_schema": None,
        "project": "nimbus",
        "updated_at": "2026-05-14T14:00:00+00:00",
    }
    entry = op_log.emit_op_log(
        db=db,
        op_type="spec.defined",
        actor=_actor(),
        ref={"collection": "proj_nimbus", "doc_id": "spec_mqtt_frame-status"},
        payload=payload,
    )
    assert entry["op_type"] == "spec.defined"
    assert entry["payload"]["previous_version"] is None
    assert entry["payload"]["version"] == "1.0.0"


def test_spec_updated_carries_previous_version():
    """Subsequent calls emit spec.updated with previous_version set so peers
    can run §7.2 fast-forward conflict detection."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="spec.updated",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "spec_state_memory"},
        payload={
            "spec_name": "state:memory",
            "version": "1.0.22",
            "previous_version": "1.0.21",
            "owner": "memory",
            "spec_type": "agent_state",
            "content": "## Current Task\n...",
            "tags": [],
            "json_schema": None,
            "project": "junto",
            "updated_at": "2026-05-14T14:00:00+00:00",
        },
    )
    assert entry["op_type"] == "spec.updated"
    assert entry["payload"]["previous_version"] == "1.0.21"
    assert entry["payload"]["version"] == "1.0.22"


def test_spec_defined_and_spec_updated_both_in_catalog():
    """Both op_types must be valid catalog entries — guards against
    accidentally renaming one and breaking the other."""
    assert op_log.is_valid_op_type("spec.defined")
    assert op_log.is_valid_op_type("spec.updated")


def test_function_enriched_partial_payload_allowed():
    """Librarian may call enrich_function with only a subset of fields
    populated. Payload fields are None when unprovided; the op-log doesn't
    care — it just records what was passed."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="function.enriched",
        actor=_actor(),
        ref={"collection": "shared_patterns", "doc_id": "func_999"},
        payload={
            "signature": None,
            "parameters": None,
            "returns": None,
            "calls": None,
            "called_by": None,
            "side_effects": None,
            "complexity": None,
            "additional_gotchas": None,
            "search_summary": "ML pipeline for email classification",
            "enriched_at": "2026-05-14T13:30:00+00:00",
        },
    )
    assert entry["payload"]["search_summary"]
    assert entry["payload"]["signature"] is None


# --- Canary 7-10: backlog tools → backlog.added / backlog.updated -------------


def test_backlog_added_accepted_and_shape():
    """memory_add_backlog_item emits backlog.added with the full registration
    payload so peers can replay the doc."""
    db = _FakeDB()
    payload = {
        "title": "Verify Chroma embedding determinism",
        "description": "Mechanical pre-Phase-2 work.",
        "priority": "high",
        "project": "junto",
        "assigned_to": "memory",
        "tags": ["phase-2", "chroma"],
        "target_version": "local-first-v0-phase-2",
        "deferred_reason": "",
        "created": "2026-05-14T14:00:00+00:00",
    }
    entry = op_log.emit_op_log(
        db=db,
        op_type="backlog.added",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "backlog_abc123def456"},
        payload=payload,
    )
    assert entry is not None
    assert entry["op_type"] == "backlog.added"
    assert entry["ref"]["doc_id"].startswith("backlog_")
    assert entry["payload"]["priority"] == "high"
    assert entry["payload"]["assigned_to"] == "memory"


def test_backlog_updated_carries_status_transition():
    """memory_update_backlog_item / complete_backlog_item / batch update all
    share backlog.updated. Payload.backlog_status disambiguates the new state
    for replay."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="backlog.updated",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "backlog_abc"},
        payload={
            "title": "Verify Chroma embedding determinism",
            "backlog_status": "done",
            "completed_at": "2026-05-14T15:00:00+00:00",
            "completed_by": "memory",
            "resolution": "Verified across both deployments.",
            "updated": "2026-05-14T15:00:00+00:00",
        },
    )
    assert entry["op_type"] == "backlog.updated"
    assert entry["payload"]["backlog_status"] == "done"


def test_backlog_move_records_origin_collection_in_payload():
    """memory_update_backlog_item project-move case: ref.collection is the
    NEW home (where the doc lives now); payload.moved_from_collection
    preserves the source for replay."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="backlog.updated",
        actor=_actor(),
        ref={"collection": "proj_nimbus", "doc_id": "backlog_xyz"},
        payload={
            "title": "Cross-team alerts",
            "backlog_status": "open",
            "priority": "medium",
            "moved_from_collection": "proj_junto",
            "edit_count": 2,
            "updated": "2026-05-14T15:00:00+00:00",
        },
    )
    assert entry["ref"]["collection"] == "proj_nimbus"
    assert entry["payload"]["moved_from_collection"] == "proj_junto"


def test_backlog_batch_index_threaded_through_payload():
    """memory_batch_backlog emits one op-log entry per item, with batch_index
    in the payload so replay can correlate to the original batch call."""
    db = _FakeDB()
    e1 = op_log.emit_op_log(
        db=db,
        op_type="backlog.added",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "backlog_b1"},
        payload={"title": "Item 1", "batch_index": 0, "created": "2026-05-14T15:00:00+00:00"},
    )
    e2 = op_log.emit_op_log(
        db=db,
        op_type="backlog.added",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "backlog_b2"},
        payload={"title": "Item 2", "batch_index": 1, "created": "2026-05-14T15:00:00+00:00"},
    )
    assert e1["payload"]["batch_index"] == 0
    assert e2["payload"]["batch_index"] == 1
    # Cross-item monotonic seq is the structural guarantee that the batch
    # produced N entries in order.
    assert e2["seq"] == e1["seq"] + 1


def test_backlog_added_and_updated_both_in_catalog():
    """Both op_types must be valid catalog entries — guards against drift."""
    assert op_log.is_valid_op_type("backlog.added")
    assert op_log.is_valid_op_type("backlog.updated")


# --- Canary 11-12: archive_by_tag + restore_by_tag → store.tagged -------------


def test_store_tagged_archive_payload():
    """memory_archive_by_tag emits store.tagged with operation=archive and
    full state-transition fields so replay can re-apply the archive."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="store.tagged",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "learning_abc123"},
        payload={
            "title": "Learning about v5",
            "tag": "v5",
            "operation": "archive",
            "status": "archived",
            "previous_status": "active",
            "archive_reason": "Bulk archive by tag: v5",
            "archived_at": "2026-05-14T15:00:00+00:00",
            "archived_by": "junto_memory_abc",
        },
    )
    assert entry["op_type"] == "store.tagged"
    assert entry["payload"]["operation"] == "archive"
    assert entry["payload"]["status"] == "archived"


def test_store_tagged_restore_payload():
    """memory_restore_by_tag emits store.tagged with operation=restore and
    the resolved previous_status — replay restores to that exact state."""
    db = _FakeDB()
    entry = op_log.emit_op_log(
        db=db,
        op_type="store.tagged",
        actor=_actor(),
        ref={"collection": "shared_patterns", "doc_id": "pattern_xyz"},
        payload={
            "title": "Pattern for v5",
            "tag": "v5",
            "operation": "restore",
            "status": "active",
            "restored_at": "2026-05-14T16:00:00+00:00",
            "restored_by": "junto_memory_abc",
        },
    )
    assert entry["op_type"] == "store.tagged"
    assert entry["payload"]["operation"] == "restore"
    assert entry["payload"]["status"] == "active"


def test_store_tagged_bulk_emits_per_touched_doc():
    """The bulk tools fan out per-doc rather than collapsing the N-row mutation
    into one op-log entry. seq monotonic across touched docs."""
    db = _FakeDB()
    e1 = op_log.emit_op_log(
        db=db,
        op_type="store.tagged",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "learning_1"},
        payload={"tag": "v5", "operation": "archive"},
    )
    e2 = op_log.emit_op_log(
        db=db,
        op_type="store.tagged",
        actor=_actor(),
        ref={"collection": "proj_junto", "doc_id": "learning_2"},
        payload={"tag": "v5", "operation": "archive"},
    )
    e3 = op_log.emit_op_log(
        db=db,
        op_type="store.tagged",
        actor=_actor(),
        ref={"collection": "shared_patterns", "doc_id": "pattern_3"},
        payload={"tag": "v5", "operation": "archive"},
    )
    assert [e1["seq"], e2["seq"], e3["seq"]] == [1, 2, 3]
    assert {e1["ref"]["doc_id"], e2["ref"]["doc_id"], e3["ref"]["doc_id"]} == {
        "learning_1",
        "learning_2",
        "pattern_3",
    }


def test_store_tagged_in_catalog():
    """Guards against renaming store.tagged out from under the bulk tools."""
    assert op_log.is_valid_op_type("store.tagged")


# --- Phase 2 A-path: fetch_embedding_for_op_log -------------------------------


class _FakeChromaCollection:
    """Just enough of AsyncCollection.get to drive fetch_embedding tests."""

    def __init__(self, embeddings_by_id=None, raise_exc=None):
        self._embeddings = embeddings_by_id or {}
        self._raise = raise_exc

    async def get(self, ids, include=None):
        if self._raise is not None:
            raise self._raise
        out_ids = []
        out_embs = []
        for doc_id in ids:
            if doc_id in self._embeddings:
                out_ids.append(doc_id)
                out_embs.append(self._embeddings[doc_id])
        result = {"ids": out_ids}
        if include and "embeddings" in include:
            result["embeddings"] = out_embs
        return result


@pytest.mark.asyncio
async def test_fetch_embedding_returns_list_of_floats():
    col = _FakeChromaCollection(embeddings_by_id={"doc_abc": [0.1, 0.2, 0.3]})
    emb = await op_log.fetch_embedding_for_op_log(col, "doc_abc")
    assert emb == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_fetch_embedding_handles_numpy_array():
    """Chroma returns embeddings as numpy arrays under the typed client.
    Helper must convert to plain list-of-floats for JSON/Mongo storage."""
    class _ArrayLike:
        def __init__(self, values):
            self._values = values
        def tolist(self):
            return list(self._values)
    col = _FakeChromaCollection(embeddings_by_id={"doc_abc": _ArrayLike([0.5, 0.6])})
    emb = await op_log.fetch_embedding_for_op_log(col, "doc_abc")
    assert emb == [0.5, 0.6]


@pytest.mark.asyncio
async def test_fetch_embedding_returns_none_on_missing_doc():
    """If the doc isn't found in Chroma (race condition: deleted before fetch),
    helper returns None instead of raising."""
    col = _FakeChromaCollection(embeddings_by_id={})
    emb = await op_log.fetch_embedding_for_op_log(col, "doc_missing")
    assert emb is None


@pytest.mark.asyncio
async def test_fetch_embedding_swallows_exception():
    """Source write already landed. Embedding fetch failing must NOT raise —
    return None so the op-log emission can proceed with embedding=None and
    §4.7 reconciliation handles it."""
    col = _FakeChromaCollection(raise_exc=RuntimeError("simulated chroma down"))
    emb = await op_log.fetch_embedding_for_op_log(col, "doc_abc")
    assert emb is None


@pytest.mark.asyncio
async def test_fetch_embedding_handles_none_embedding_entry():
    """Chroma can return an embeddings list with None entries (defensive)."""
    col = _FakeChromaCollection(embeddings_by_id={"doc_abc": None})
    emb = await op_log.fetch_embedding_for_op_log(col, "doc_abc")
    assert emb is None


@pytest.mark.asyncio
async def test_fetch_embedding_handles_numpy_outer_container():
    """Real Chroma's typed async client returns the OUTER `embeddings` value as
    a numpy 2D array (shape (n, dim)), not a Python list-of-lists. `if not arr`
    raises ambiguity on a multi-element numpy array. Regression guard for the
    2026-05-15 A-path deploy outage."""

    class _NumpyLikeOuter:
        """Mimics numpy 2D array: indexable, has len(), raises on bool()."""
        def __init__(self, rows):
            self._rows = rows

        def __len__(self):
            return len(self._rows)

        def __getitem__(self, idx):
            return self._rows[idx]

        def __bool__(self):
            raise ValueError(
                "The truth value of an array with more than one element is ambiguous."
            )

    class _NumpyLikeRow:
        def __init__(self, values):
            self._values = values

        def tolist(self):
            return list(self._values)

    class _Collection:
        async def get(self, ids, include=None):
            return {
                "ids": list(ids),
                "embeddings": _NumpyLikeOuter([_NumpyLikeRow([0.7, 0.8, 0.9])]),
            }

    emb = await op_log.fetch_embedding_for_op_log(_Collection(), "doc_abc")
    assert emb == [0.7, 0.8, 0.9]
