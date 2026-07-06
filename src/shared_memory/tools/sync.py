"""Sync endpoints — Phase 2 replication primitive.

Per `design:local-first-junto-v0-mvp` v0.5.0 §5.1.

`memory_sync_pull` — read side. Returns op_log rows newer than the
caller's cursor, per origin. Cursor map shape: `{origin_id: last_seen_seq}`.
Origins absent from the cursor are treated as `seq=0` (full history),
which is the cold-start case in §5.3.

`memory_sync_push` — write side. Validates per-origin seq monotonicity
(with §4.3.a "sequence skip" tolerance), op_type in the §4.1 catalog,
schema_version compat (§4.4), and `intent_id` dedupe (§4.6). Materializes
each op into the local source store (Mongo or Chroma) reusing the inline
A-path embedding (§4.3.a `payload.embedding`) so peers never re-embed
text. Append-only mirror of the source op to the local op_log preserves
`(origin, seq, intent_id, _id)`; the receiver's own seq counter is
NOT advanced by foreign-origin ops.

Per-op dispositions:
- `applied` — source write + local op_log append succeeded.
- `deduped_seq` — `(origin, seq)` already present in op_log.
- `deduped_intent` — `intent_id` already present in op_log (§4.6).
- `conflict` — §7.2 spec fast-forward failed; backlog item auto-filed.
- `rejected` — unknown op_type, missing required fields, self-origin op,
  malformed actor/ref/payload, or backwards seq.
- `rejected_schema` — `schema_version` ahead of receiver; per §4.4 the
  batch is halted at this op (remaining ops are flagged rejected with the
  halt reason).
- `rejected_origin_owner` — §7.4 state-spec multi-instance detection.

**Auth:** both endpoints require role `admin` or `owner` ("sync"
permission). These are server-to-server replication primitives — a
LAN-local junto-memory pulling from central — not for routine agents.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import Context
from pymongo.errors import DuplicateKeyError

from shared_memory.app import mcp
from shared_memory.auth import require_auth
from shared_memory.clients import get_chroma, get_mongo
from shared_memory.config import ORIGIN_SERVER_ID
from shared_memory.helpers import require_session, utc_now_iso
from shared_memory.op_log import (
    OPLOG_COLLECTION,
    OPLOG_SCHEMA_VERSION,
    claim_or_verify_state_owner,
    is_valid_op_type,
)
from shared_memory.state import active_sessions

log = logging.getLogger(__name__)


def _jsonable(obj):
    """Recursive JSON-default for datetime + anything Mongo-returned.

    Op_log payloads round-trip arbitrary fields including BSON dates;
    PyMongo decodes those to `datetime`. JSON has no date type — convert
    to ISO-8601 strings so peers can re-parse on the apply side.
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(
        f"Cannot JSON-serialize {type(obj).__name__} in op_log payload"
    )


# ───────────────────────────────────────────────────────────────────
# memory_sync_pull — read side (unchanged from Phase 2 first ship)
# ───────────────────────────────────────────────────────────────────


def _pull_op_log(
    db,
    cursors: Dict[str, int],
    limit: int,
    projects: Optional[List[str]],
    head_only: bool = False,
) -> Dict[str, Any]:
    """Core read logic for memory_sync_pull — extracted for unit testing.

    Pure function: takes a Mongo `db` handle + cursor map + page size +
    optional project filter; returns the response dict.

    Discovers all known origins (server-side `distinct` ∪ cursor keys),
    fetches up to `limit+1` rows per origin where `seq > cursor[origin]`,
    sets `has_more[origin]` from the +1 probe, and trims to `limit`.

    Internal events (no `actor.project`) are always included when a
    project filter is set; see module docstring open-question #2.

    `head_only=True`: skip transmitting ops; for each origin return only
    the max(seq) where `seq > cursor[origin]` as `next_cursor[origin]`,
    plus `has_more[origin]=True` whenever any rows exist past the cursor.
    Uses a descending-sort+limit(1) per origin so cost is constant per
    origin regardless of replication lag. `limit` is ignored in this mode.
    Returns the same shape as the normal path but with `ops=[]`.
    """
    op_log_coll = db[OPLOG_COLLECTION]
    known_origins = set(op_log_coll.distinct("origin")) | set(cursors.keys())

    ops: List[Dict[str, Any]] = []
    next_cursor: Dict[str, int] = {}
    has_more: Dict[str, bool] = {}

    for origin in sorted(known_origins):
        cur = int(cursors.get(origin, 0))
        query: Dict[str, Any] = {"origin": origin, "seq": {"$gt": cur}}

        if projects:
            query["$or"] = [
                {"actor.project": {"$in": projects}},
                {"actor.project": {"$exists": False}},
                {"actor.project": None},
            ]

        if head_only:
            head_row = op_log_coll.find(
                query, projection={"seq": 1}
            ).sort([("seq", -1)]).limit(1)
            head_list = list(head_row)
            if not head_list:
                continue
            next_cursor[origin] = head_list[0]["seq"]
            has_more[origin] = True  # caller hasn't fetched the body yet
            continue

        rows = list(
            op_log_coll.find(query).sort([("seq", 1)]).limit(limit + 1)
        )
        if not rows:
            continue

        page_has_more = len(rows) > limit
        rows = rows[:limit]

        has_more[origin] = page_has_more
        next_cursor[origin] = rows[-1]["seq"]
        ops.extend(rows)

    return {
        "ops": ops,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_origin": ORIGIN_SERVER_ID,
    }


@mcp.tool()
async def memory_sync_pull(
    session_id: str,
    since_cursor_by_origin: Optional[Dict[str, int]] = None,
    limit: int = 500,
    projects: Optional[List[str]] = None,
    head_only: bool = False,
    ctx: Context = None,
) -> str:
    """Pull op_log entries newer than the caller's cursor (per origin).

    Phase 2 replication read endpoint per §5.1 of
    `design:local-first-junto-v0-mvp`. A LAN-local junto-memory calls this
    against central on a cadence (60s online, 10s when MQTT silent) and
    materializes the returned ops into its local Mongo+Chroma stores.

    Args:
        session_id: Your session ID. Caller must hold `admin` or `owner`
            tier — these are server-to-server credentials.
        since_cursor_by_origin: Map `{origin_id: last_seen_seq}`. The
            server returns ops where `seq > cursor[origin]` for each
            origin. Origins absent from the map are treated as `seq=0`
            (full history). Empty/None map = cold start.
        limit: Max ops returned per origin per call. Default 500. Server
            caps responses at this many rows even if more are available;
            caller pages forward via the returned `next_cursor`. Ignored
            when `head_only=True`.
        projects: When set, restrict to ops whose `actor.project` is in
            this list. Op_log entries without a project (server-side
            internal events) are always included.
        head_only: When True, skip transmitting op bodies and return only
            `next_cursor[origin] = max(seq)` per origin where rows exist
            past the cursor. Constant-cost per origin regardless of lag.
            Used by lag observers and connection-health probes. The
            response shape is identical to the normal path except `ops=[]`.

    Returns JSON:
        {
          "ops": [<op_log entry>, ...],   # sorted by (origin, seq) ascending
          "next_cursor": {origin: last_returned_seq, ...},
          "has_more": {origin: bool, ...},
          "server_origin": "<this server's origin_id>",
        }

    `next_cursor` only contains origins that returned at least one row
    on this call. `has_more[origin]=True` means another page exists; the
    caller should re-call with the returned cursor. Empty result =
    caller is up to date. With `head_only=True`, `has_more[origin]=True`
    whenever rows exist past the cursor (since the caller hasn't fetched
    the body); fetch them with a follow-up call with `head_only=False`.
    """
    err = require_session(session_id)
    if err:
        return err

    session_info = active_sessions[session_id]

    auth_err = require_auth(session_info, "sync")
    if auth_err:
        return json.dumps({"error": auth_err})

    if not isinstance(limit, int) or limit < 1:
        return json.dumps({"error": "limit must be a positive integer"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    cursors: Dict[str, int] = dict(since_cursor_by_origin or {})
    result = _pull_op_log(db, cursors, limit, projects, head_only=bool(head_only))
    return json.dumps(result, default=_jsonable)


# ───────────────────────────────────────────────────────────────────
# memory_sync_push — write side (materializer)
# ───────────────────────────────────────────────────────────────────


REQUIRED_OP_KEYS = (
    "_id", "seq", "origin", "intent_id", "actor", "op_type",
    "ref", "payload", "schema_version",
)


def _validate_shape(op: Dict[str, Any]) -> Optional[str]:
    """Return None if shape is valid, else a string explaining the gap."""
    for k in REQUIRED_OP_KEYS:
        if k not in op:
            return f"missing required key: {k}"
    if not isinstance(op["actor"], dict):
        return "actor must be a dict"
    if not isinstance(op["ref"], dict):
        return "ref must be a dict"
    if not isinstance(op["payload"], dict):
        return "payload must be a dict"
    if "collection" not in op["ref"] or "doc_id" not in op["ref"]:
        return "ref must contain collection + doc_id"
    if not isinstance(op["seq"], int) or op["seq"] < 1:
        return "seq must be a positive integer"
    if not isinstance(op["schema_version"], int):
        return "schema_version must be an integer"
    if not op["origin"]:
        return "origin must be non-empty"
    return None


def _preload_dedupe_state(
    db, ops: List[Dict[str, Any]]
) -> tuple[set, set, Dict[str, int]]:
    """One-pass batch dedupe preload.

    For an incoming batch of N ops, the old per-op `count_documents` path
    cost 2N Mongo round-trips just on dedupe (one per op for (origin, seq),
    one per op for intent_id). Cold sync of 80k rows × ~5ms/query ≈ 13 min
    of pure dedupe overhead.

    This pre-loads:
    - `seq_hits`: set of `(origin, seq)` tuples already present in our
      op_log that match any incoming op. One $in query per distinct
      origin in the batch — uses the (origin, seq) unique compound index.
    - `intent_hits`: set of `intent_id` strings already present, from a
      single $in query over the sparse intent_id index.
    - `max_seq_by_origin`: largest local seq per incoming origin, used
      for monotonicity. Mutated in-place as ops apply so later ops in
      the same batch see the freshly-applied seq as the new ceiling.

    Race protection is unchanged: the op_log `(origin, seq)` unique index
    still catches concurrent pushes that committed the same row between
    pre-load and apply (the apply path's `insert_one` catches the
    DuplicateKeyError and routes the op to `deduped_seq`).
    """
    seq_hits: set = set()
    intent_hits: set = set()
    max_seq_by_origin: Dict[str, int] = {}

    if not ops:
        return seq_hits, intent_hits, max_seq_by_origin

    # Group incoming (origin, seq) by origin so the $in queries each
    # land within a single origin's index slice.
    by_origin: Dict[str, list] = {}
    intent_ids: set = set()
    origins: set = set()
    for op in ops:
        origin = op.get("origin")
        seq = op.get("seq")
        if origin and isinstance(seq, int):
            by_origin.setdefault(origin, []).append(seq)
            origins.add(origin)
        intent = op.get("intent_id")
        if intent:
            intent_ids.add(intent)

    op_log_coll = db[OPLOG_COLLECTION]

    for origin, seqs in by_origin.items():
        for row in op_log_coll.find(
            {"origin": origin, "seq": {"$in": seqs}},
            {"origin": 1, "seq": 1, "_id": 0},
        ):
            seq_hits.add((row["origin"], row["seq"]))

    if intent_ids:
        for row in op_log_coll.find(
            {"intent_id": {"$in": list(intent_ids)}},
            {"intent_id": 1, "_id": 0},
        ):
            intent_hits.add(row["intent_id"])

    for origin in origins:
        rows = list(
            op_log_coll.find({"origin": origin}).sort([("seq", -1)]).limit(1)
        )
        max_seq_by_origin[origin] = int(rows[0]["seq"]) if rows else 0

    return seq_hits, intent_hits, max_seq_by_origin


async def _push_ops(
    db,
    chroma,
    ops: List[Dict[str, Any]],
    *,
    origin_server_id: str,
) -> Dict[str, Any]:
    """Materialize a batch of op_log entries into local Mongo+Chroma.

    Pure-ish: takes Mongo `db` + Chroma client + ops list + this server's
    origin id. Returns the disposition envelope. Extracted from the tool
    wrapper for unit testing.

    See module docstring for the disposition catalog.
    """
    results: List[Dict[str, Any]] = []
    applied_count = 0
    rejected_count = 0
    conflict_count = 0
    deduped_count = 0
    halted = False
    halt_reason: Optional[str] = None

    # One-pass batch preload. Replaces 2N Mongo round-trips with a small
    # constant number of $in queries — see _preload_dedupe_state.
    seq_hits, intent_hits, max_seq_by_origin = _preload_dedupe_state(db, ops)

    for op in ops:
        if halted:
            results.append({
                "op_id": op.get("_id"),
                "disposition": "rejected_schema",
                "reason": f"batch halted: {halt_reason}",
            })
            rejected_count += 1
            continue

        # ── Shape ──
        shape_err = _validate_shape(op)
        if shape_err:
            results.append({
                "op_id": op.get("_id"),
                "disposition": "rejected",
                "reason": shape_err,
            })
            rejected_count += 1
            continue

        # ── Schema version (§4.4 — halt-on-future) ──
        if op["schema_version"] > OPLOG_SCHEMA_VERSION:
            halted = True
            halt_reason = (
                f"schema_version {op['schema_version']} ahead of receiver "
                f"{OPLOG_SCHEMA_VERSION}"
            )
            results.append({
                "op_id": op["_id"],
                "disposition": "rejected_schema",
                "reason": halt_reason,
            })
            rejected_count += 1
            continue

        # ── Catalog ──
        if not is_valid_op_type(op["op_type"]):
            results.append({
                "op_id": op["_id"],
                "disposition": "rejected",
                "reason": f"op_type not in §4.1 catalog: {op['op_type']}",
            })
            rejected_count += 1
            continue

        # ── Self-origin reject (loop prevention) ──
        if op["origin"] == origin_server_id:
            results.append({
                "op_id": op["_id"],
                "disposition": "rejected",
                "reason": "self-origin op refused (cannot push your own ops back)",
            })
            rejected_count += 1
            continue

        # ── Dedupe (origin, seq) — set lookup, O(1) ──
        if (op["origin"], op["seq"]) in seq_hits:
            results.append({
                "op_id": op["_id"],
                "disposition": "deduped_seq",
            })
            deduped_count += 1
            continue

        # ── Dedupe intent_id (§4.6) — set lookup, O(1) ──
        if op.get("intent_id") and op["intent_id"] in intent_hits:
            results.append({
                "op_id": op["_id"],
                "disposition": "deduped_intent",
            })
            deduped_count += 1
            continue

        # ── Monotonicity (§4.3.a sequence-skip tolerance) ──
        # max_seq_by_origin is updated in-batch after each successful
        # apply so consecutive ops from the same origin see the correct
        # ceiling without a round-trip.
        current_max = max_seq_by_origin.get(op["origin"], 0)
        if op["seq"] <= current_max:
            # (origin, seq) dedupe already covered the exact-replay case;
            # this is a smaller-seq op_id we've never seen, which means
            # the sender's seq went backwards. Reject — never apply.
            results.append({
                "op_id": op["_id"],
                "disposition": "rejected",
                "reason": (
                    f"seq {op['seq']} <= local max {current_max} for "
                    f"origin {op['origin']} (backwards seq, never apply)"
                ),
            })
            rejected_count += 1
            continue
        sequence_skip = op["seq"] > current_max + 1

        # ── §7.4 state-spec origin-owner check ──
        if op["op_type"] in ("spec.defined", "spec.updated") and \
                op["payload"].get("spec_type") == "agent_state":
            project = op["payload"].get("project")
            agent = op["payload"].get("owner")
            origin_in_payload = op["payload"].get("origin_server_id")
            if project and agent and origin_in_payload:
                outcome, registered = claim_or_verify_state_owner(
                    db, project, agent, origin_in_payload
                )
                if outcome == "mismatch":
                    results.append({
                        "op_id": op["_id"],
                        "disposition": "rejected_origin_owner",
                        "reason": (
                            f"state-spec (project={project}, agent={agent}) "
                            f"registered to {registered}, "
                            f"incoming origin {origin_in_payload}"
                        ),
                    })
                    rejected_count += 1
                    continue

        # ── Apply ──
        try:
            apply_result = await _apply_op(db, chroma, op)
        except Exception as exc:
            log.exception(
                "sync_push apply failed for %s seq=%s: %s",
                op["op_type"], op["seq"], exc,
            )
            results.append({
                "op_id": op["_id"],
                "disposition": "rejected",
                "reason": f"apply error: {type(exc).__name__}: {exc}",
            })
            rejected_count += 1
            continue

        if apply_result["disposition"] == "conflict":
            results.append({
                "op_id": op["_id"],
                **apply_result,
            })
            conflict_count += 1
            continue

        # ── Local op_log append (preserve origin/seq/intent_id/_id) ──
        try:
            db[OPLOG_COLLECTION].insert_one(dict(op))
        except DuplicateKeyError:
            # Race: another concurrent push committed this op while we
            # were applying. Apply is idempotent (we use upsert everywhere
            # in handlers); accept the race as deduped.
            results.append({
                "op_id": op["_id"],
                "disposition": "deduped_seq",
                "reason": "race: another push committed (origin, seq) concurrently",
            })
            deduped_count += 1
            continue

        record: Dict[str, Any] = {"op_id": op["_id"], "disposition": "applied"}
        if sequence_skip:
            record["flags"] = {"sequence_skip": True}
            record["sequence_skip"] = True  # also at top level for terse checks
        if apply_result.get("flags"):
            record.setdefault("flags", {}).update(apply_result["flags"])
        results.append(record)
        applied_count += 1

        # Update in-batch dedupe state so later ops in this same batch
        # see this row as already-applied (avoids the read-after-write
        # race that would otherwise need another round-trip to detect).
        seq_hits.add((op["origin"], op["seq"]))
        if op.get("intent_id"):
            intent_hits.add(op["intent_id"])
        max_seq_by_origin[op["origin"]] = op["seq"]

    return {
        "results": results,
        "applied_count": applied_count,
        "rejected_count": rejected_count,
        "conflict_count": conflict_count,
        "deduped_count": deduped_count,
        "server_origin": origin_server_id,
    }


async def _apply_op(db, chroma, op: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a single op to its apply handler. Returns disposition dict."""
    handler = _APPLY_HANDLERS.get(op["op_type"])
    if handler is None:
        # Op_type is in §4.1 catalog (passed the catalog gate above) but
        # we haven't implemented apply for it yet. This happens for the
        # not-yet-instrumented internal-event op_types (session.*, agent.*,
        # lock.*, autopilot.*, signal.*, audit.*, rename.*).
        return {
            "disposition": "rejected",
            "reason": f"apply_unimplemented: no handler for op_type {op['op_type']!r}",
        }
    return await handler(db, chroma, op)


# ───────────────────────────────────────────────────────────────────
# Chroma write helpers
# ───────────────────────────────────────────────────────────────────


async def _chroma_upsert(chroma, ref, document, metadata, embedding):
    """Idempotent Chroma upsert with optional inline embedding (A-path).

    When `embedding` is non-None, Chroma stores it directly via the
    embeddings parameter — peer-applied vectors bypass the local model
    entirely, which is the determinism guarantee §4.3.a + §10 OQ#2 pay
    off. When None, falls back to Chroma's bundled model (older ops
    without A-path; reconciliation §4.7 can backfill later).
    """
    coll = await chroma.get_or_create_collection(name=ref["collection"])
    kwargs: Dict[str, Any] = {
        "ids": [ref["doc_id"]],
        "documents": [document],
        "metadatas": [metadata],
    }
    if embedding is not None:
        kwargs["embeddings"] = [embedding]
    await coll.upsert(**kwargs)


# ───────────────────────────────────────────────────────────────────
# Apply handlers — one per supported op_type
# ───────────────────────────────────────────────────────────────────


async def _apply_learning_recorded(db, chroma, op):
    payload, actor, ref = op["payload"], op["actor"], op["ref"]
    metadata = {
        "title": payload.get("title", ""),
        "type": "learning",
        "status": "active",
        "tags": json.dumps(payload.get("tags") or []),
        "session_id": actor.get("session_id", ""),
        "claude_instance": actor.get("agent", ""),
        "created": payload.get("created"),
        "updated": payload.get("created"),
    }
    content = f"# {payload.get('title', '')}\n\n{payload.get('details', '')}"
    await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_store_created(db, chroma, op):
    payload, actor, ref = op["payload"], op["actor"], op["ref"]
    memory_type = payload.get("memory_type", "context")
    metadata = {
        "title": payload.get("title", ""),
        "type": memory_type,
        "status": "active",
        "tags": json.dumps(payload.get("tags") or []),
        "files_related": json.dumps(payload.get("files_related") or []),
        "session_id": actor.get("session_id", ""),
        "claude_instance": actor.get("agent", ""),
        "project": actor.get("project") or "",
        "created": payload.get("created"),
        "updated": payload.get("created"),
        "content_hash": payload.get("content_hash", ""),
        "access_count": 0,
        "last_accessed": payload.get("created"),
    }
    if payload.get("expires_at"):
        metadata["expires_at"] = payload["expires_at"]
    if memory_type == "interface":
        if payload.get("interface_name"):
            metadata["interface_name"] = payload["interface_name"]
        if payload.get("interface_version"):
            metadata["interface_version"] = payload["interface_version"]
        if payload.get("interface_owner"):
            metadata["interface_owner"] = payload["interface_owner"]
        if payload.get("interface_schema"):
            metadata["interface_schema"] = json.dumps(payload["interface_schema"])
    content = payload.get("content", "")
    await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_function_registered(db, chroma, op):
    payload, actor, ref = op["payload"], op["actor"], op["ref"]
    name = payload.get("name", "")
    purpose = payload.get("purpose", "")
    registered_at = payload.get("registered_at")
    metadata = {
        "title": f"{name} - {purpose[:50]}",
        "type": "function_ref",
        "status": "active",
        "func_name": name,
        "func_file": payload.get("file", ""),
        "func_purpose": purpose,
        "project": actor.get("project") or "",
        "session_id": actor.get("session_id", ""),
        "claude_instance": actor.get("agent", ""),
        "created": registered_at,
        "updated": registered_at,
        "enriched": "false",
        "has_code": "true" if payload.get("code") else "false",
        "access_count": 0,
        "last_accessed": registered_at,
    }
    if payload.get("gotchas"):
        metadata["gotchas"] = payload["gotchas"]
    if payload.get("prefer_over"):
        metadata["prefer_over"] = payload["prefer_over"]
    if payload.get("requires"):
        metadata["requires"] = json.dumps(payload["requires"])

    doc_parts = [
        f"# Function: {name}",
        f"**Location:** {payload.get('file', '')}",
        f"**Purpose:** {purpose}",
    ]
    if payload.get("gotchas"):
        doc_parts.append(f"**Gotchas:** {payload['gotchas']}")
    if payload.get("prefer_over"):
        doc_parts.append(f"**Prefer over:** {payload['prefer_over']}")
    if payload.get("requires"):
        doc_parts.append(f"**Requires:** {', '.join(payload['requires'])}")
    if payload.get("code"):
        doc_parts.append(f"\n**Code:**\n```\n{payload['code']}\n```")
    content = "\n\n".join(doc_parts)
    await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_function_enriched(db, chroma, op):
    """Apply a partial enrichment over an existing function row.

    If the row doesn't exist locally yet, soft-skip (the registered op
    should land first in seq order). Don't fail — we still want the
    op_log append so we don't re-apply forever; flag target_missing.
    """
    payload, ref = op["payload"], op["ref"]
    coll = await chroma.get_or_create_collection(name=ref["collection"])
    existing = await coll.get(
        ids=[ref["doc_id"]], include=["metadatas", "documents"]
    )
    if not existing["ids"]:
        log.warning(
            "function.enriched apply: target %s not found in %s; "
            "skipping enrichment merge (registered op may arrive later)",
            ref["doc_id"], ref["collection"],
        )
        return {"disposition": "applied", "flags": {"target_missing": True}}

    metadata = dict(existing["metadatas"][0] or {})
    document = existing["documents"][0]

    if payload.get("signature"):
        metadata["signature"] = payload["signature"]
    if payload.get("calls"):
        metadata["calls"] = json.dumps(payload["calls"])
    if payload.get("called_by"):
        metadata["called_by"] = json.dumps(payload["called_by"])
    if payload.get("side_effects"):
        metadata["side_effects"] = json.dumps(payload["side_effects"])
    if payload.get("complexity"):
        metadata["complexity"] = payload["complexity"]
    if payload.get("additional_gotchas"):
        metadata["gotchas"] = payload["additional_gotchas"]
    metadata["enriched"] = "true"
    metadata["enriched_at"] = payload.get("enriched_at")
    metadata["updated"] = payload.get("enriched_at")
    if payload.get("search_summary"):
        metadata["search_summary"] = payload["search_summary"]
        document = f"**Search Summary:** {payload['search_summary']}\n\n" + (document or "")

    await _chroma_upsert(chroma, ref, document, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_spec_defined(db, chroma, op):
    payload, ref = op["payload"], op["ref"]
    metadata = _spec_metadata(payload)
    content = payload.get("content", "")
    await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_spec_updated(db, chroma, op):
    """§7.2 fast-forward conflict check.

    Read receiver's current spec version; fast-forward apply iff
    incoming.previous_version exactly matches local current_version.
    Otherwise file a backlog item and return `conflict` (no Chroma
    write, no op_log append).
    """
    payload, ref = op["payload"], op["ref"]
    coll = await chroma.get_or_create_collection(name=ref["collection"])
    existing = await coll.get(
        ids=[ref["doc_id"]], include=["metadatas", "documents"]
    )

    incoming_parent = payload.get("previous_version")
    incoming_version = payload.get("version")

    if not existing["ids"]:
        # Receiver has never seen this spec_name — can't fast-forward
        # to a nonexistent ancestor. File a backlog item; don't silently
        # upsert (that would be a hidden spec.defined when the op says
        # spec.updated).
        await _file_spec_conflict_backlog(
            chroma, op,
            local_version=None,
            incoming_version=incoming_version,
            reason="missing parent: receiver has no prior version of this spec",
        )
        return {
            "disposition": "conflict",
            "reason": "missing parent: receiver has never seen this spec",
        }

    current_version = (existing["metadatas"][0] or {}).get("spec_version")

    if current_version == incoming_parent:
        # Fast-forward: archive prior to history collection, then upsert
        # current with the incoming content + embedding.
        await _archive_spec_to_history(
            chroma, payload,
            prior_content=existing["documents"][0] if existing["documents"] else "",
            prior_meta=existing["metadatas"][0] or {},
        )
        metadata = _spec_metadata(payload)
        content = payload.get("content", "")
        await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
        return {"disposition": "applied"}

    # Conflict.
    await _file_spec_conflict_backlog(
        chroma, op,
        local_version=current_version,
        incoming_version=incoming_version,
        reason=(
            f"version mismatch: local v{current_version}, "
            f"incoming previous_version=v{incoming_parent}"
        ),
    )
    return {
        "disposition": "conflict",
        "reason": (
            f"version mismatch: local v{current_version}, "
            f"incoming previous_version=v{incoming_parent}"
        ),
    }


def _spec_metadata(payload):
    """Mirror specs.py:198-212 metadata shape for replay parity."""
    return {
        "title": f"Spec: {payload.get('spec_name', '')}",
        "type": "spec",
        "spec_name": payload.get("spec_name", ""),
        "spec_version": payload.get("version", ""),
        "spec_type": payload.get("spec_type", ""),
        "spec_owner": payload.get("owner", ""),
        "status": "active",
        "tags": json.dumps(payload.get("tags") or []),
        "project": payload.get("project") or "",
        "created": payload.get("updated_at"),
        "updated": payload.get("updated_at"),
        "created_by": payload.get("owner", ""),
        "updated_by": payload.get("owner", ""),
    }


async def _archive_spec_to_history(chroma, payload, prior_content, prior_meta):
    """Mirror memory_define_spec's history archive (specs.py:168-187)."""
    history_collection = await chroma.get_or_create_collection(name="shared_context")
    spec_name = payload.get("spec_name", "")
    prior_version = prior_meta.get("spec_version", "")
    history_id = (
        f"spec_history_{spec_name.replace(':', '_')}_"
        f"{prior_version.replace('.', '_')}"
    )
    metadata = {
        "title": f"Spec History: {spec_name} v{prior_version}",
        "type": "spec",
        "spec_name": spec_name,
        "spec_version": prior_version,
        "spec_owner": prior_meta.get("spec_owner", ""),
        "archived_at": payload.get("updated_at"),
        "archived_by": payload.get("owner", ""),
        "status": "archived",
    }
    try:
        await history_collection.add(
            ids=[history_id],
            documents=[prior_content or ""],
            metadatas=[metadata],
        )
    except Exception:
        # Matches emit-side semantics — history is best-effort. The
        # current-spec write is what callers query.
        pass


async def _file_spec_conflict_backlog(
    chroma, op, *, local_version, incoming_version, reason
):
    """File a backlog item recording the §7.2 conflict for human resolution."""
    payload = op["payload"]
    spec_name = payload.get("spec_name", "")
    project = payload.get("project")
    backlog_collection_name = f"proj_{project}" if project else "shared_work"
    coll = await chroma.get_or_create_collection(name=backlog_collection_name)

    title = (
        f"Spec conflict: {spec_name} — local v{local_version} vs "
        f"incoming v{incoming_version} from {op['origin']}"
    )
    backlog_id = f"backlog_{uuid.uuid4().hex[:12]}"
    now = utc_now_iso()
    description = (
        "Auto-filed by memory_sync_push (§7.2 fast-forward conflict).\n\n"
        f"**Spec:** {spec_name}\n"
        f"**Local version:** v{local_version}\n"
        f"**Incoming version:** v{incoming_version}\n"
        f"**Incoming previous_version:** v{payload.get('previous_version')}\n"
        f"**Incoming origin:** {op['origin']}\n"
        f"**Reason:** {reason}\n\n"
        "## Incoming content\n\n"
        f"{payload.get('content', '')}\n\n"
        "## Resolution\n\n"
        "Read both versions, decide whose wins, then call memory_define_spec "
        "locally with the chosen content + a NEW version "
        f"(e.g., v{_bump_version(local_version or incoming_version)}). "
        "That emits a fresh spec.updated op that will fast-forward on the "
        "other peer."
    )
    metadata = {
        "title": title,
        "type": "backlog",
        "backlog_status": "open",
        "priority": "medium",
        "project": project or "",
        "assigned_to": payload.get("owner", ""),
        "tags": json.dumps(["conflict", "sync", "auto-filed"]),
        "target_version": "",
        "deferred_reason": "",
        "created_by": "memory_sync_push",
        "created": now,
        "updated": now,
        "edit_count": 0,
    }
    try:
        await coll.add(
            ids=[backlog_id],
            documents=[f"# {title}\n\n{description}"],
            metadatas=[metadata],
        )
    except Exception as exc:
        log.error("failed to auto-file spec-conflict backlog: %s", exc)


def _bump_version(version):
    """Best-effort patch-bump for the resolution-hint string."""
    if not version:
        return "1.0.1"
    parts = str(version).split(".")
    if len(parts) == 3:
        try:
            parts[2] = str(int(parts[2]) + 1)
            return ".".join(parts)
        except ValueError:
            pass
    return str(version) + ".1"


async def _apply_store_tagged(db, chroma, op):
    payload, ref = op["payload"], op["ref"]
    coll = await chroma.get_or_create_collection(name=ref["collection"])
    existing = await coll.get(ids=[ref["doc_id"]], include=["metadatas"])
    if not existing["ids"]:
        return {"disposition": "applied", "flags": {"target_missing": True}}
    metadata = dict(existing["metadatas"][0] or {})
    for field in ("status", "previous_status", "archived_at", "archived_by",
                  "archive_reason", "restored_at", "restored_by"):
        if payload.get(field) is not None:
            metadata[field] = payload[field]
    await coll.update(ids=[ref["doc_id"]], metadatas=[metadata])
    return {"disposition": "applied"}


async def _apply_backlog_added(db, chroma, op):
    payload, actor, ref = op["payload"], op["actor"], op["ref"]
    metadata = {
        "title": payload.get("title", ""),
        "type": "backlog",
        "backlog_status": "open",
        "priority": payload.get("priority", "medium"),
        "project": payload.get("project") or actor.get("project") or "",
        "assigned_to": payload.get("assigned_to") or "",
        "tags": json.dumps(payload.get("tags") or []),
        "target_version": payload.get("target_version") or "",
        "deferred_reason": payload.get("deferred_reason") or "",
        "created_by": actor.get("agent", ""),
        "created": payload.get("created"),
        "updated": payload.get("created"),
        "edit_count": 0,
    }
    content = f"# {payload.get('title', '')}\n\n{payload.get('description', '')}"
    await _chroma_upsert(chroma, ref, content, metadata, payload.get("embedding"))
    return {"disposition": "applied"}


async def _apply_backlog_updated(db, chroma, op):
    """Apply a backlog mutation; also handles cross-collection moves.

    When a backlog item changes project, the emit-side (backlog.py:350-360)
    adds the new project's collection AND deletes from the old. Replay
    mirrors that: upsert into ref.collection (the new home), then if
    payload.moved_from_collection differs, delete from the old collection.
    """
    payload, ref = op["payload"], op["ref"]
    new_collection_name = ref["collection"]
    moved_from = payload.get("moved_from_collection")
    is_move = bool(moved_from and moved_from != new_collection_name)

    # Read the existing row from whichever collection it lives in pre-move.
    source_collection_name = moved_from if is_move else new_collection_name
    source_coll = await chroma.get_or_create_collection(name=source_collection_name)
    existing = await source_coll.get(
        ids=[ref["doc_id"]], include=["metadatas", "documents"]
    )
    if not existing["ids"]:
        return {"disposition": "applied", "flags": {"target_missing": True}}

    metadata = dict(existing["metadatas"][0] or {})
    for field in (
        "title", "backlog_status", "priority", "assigned_to",
        "target_version", "deferred_reason", "completed_at",
        "completed_by", "resolution", "edit_count", "updated",
    ):
        if payload.get(field) is not None:
            metadata[field] = payload[field]
    # On move, the metadata.project follows the new collection so
    # downstream listing queries find it in the right bucket.
    if is_move:
        new_project = new_collection_name[len("proj_"):] if new_collection_name.startswith("proj_") else ""
        metadata["project"] = new_project

    document = existing["documents"][0] if existing["documents"] else None
    if payload.get("resolution"):
        document = (document or "") + f"\n\n## Resolution\n{payload['resolution']}"

    await _chroma_upsert(
        chroma, ref, document, metadata, payload.get("embedding")
    )
    if is_move:
        # Delete from old collection so the stale row doesn't linger.
        try:
            await source_coll.delete(ids=[ref["doc_id"]])
        except Exception as exc:
            log.warning(
                "backlog move: failed to delete %s from %s: %s",
                ref["doc_id"], moved_from, exc,
            )
    return {"disposition": "applied"}


async def _apply_learning_superseded(db, chroma, op):
    payload, ref = op["payload"], op["ref"]
    coll = await chroma.get_or_create_collection(name=ref["collection"])
    existing = await coll.get(ids=[ref["doc_id"]], include=["metadatas"])
    if not existing["ids"]:
        return {"disposition": "applied", "flags": {"target_missing": True}}
    metadata = dict(existing["metadatas"][0] or {})
    metadata["status"] = payload.get("status", "superseded")
    if payload.get("superseded_by"):
        metadata["superseded_by"] = payload["superseded_by"]
    if payload.get("updated"):
        metadata["updated"] = payload["updated"]
    await coll.update(ids=[ref["doc_id"]], metadatas=[metadata])
    return {"disposition": "applied"}


async def _apply_message_sent(db, chroma, op):
    """Mongo-backed: insert the message doc verbatim from payload.

    The emit site (messaging.py:550-562) stores the full message doc in
    `payload` (msg_doc passed through). Replay = insert it as-is. The
    receiver's local op_log append happens separately, so the §4.3.b
    transactional atomicity that emit-side gets isn't required here —
    the (origin, seq) unique index on op_log gives us race-safe dedupe.

    After insert, fire the same inbox push the write-side emit fires
    (messaging.py:577-578) so any in-process subscriber on THIS peer
    gets a `notifications/resources/updated` for the materialized
    message. Without this, cross-peer messages are durable but invisible
    to live subscribers (e.g., the junto-inbox plugin) until the next
    poll. Mirrors the write-side `suppress_push` semantic.
    """
    payload = op["payload"]
    duplicate = False
    try:
        db.messages.insert_one(dict(payload))
    except DuplicateKeyError:
        # Receiver already has this message_id (rare under normal flow,
        # but defense-in-depth for race / replay-from-different-peer).
        # Skip notify — subscribers already saw this msg's first arrival.
        duplicate = True

    if not duplicate and not payload.get("push_suppressed", False):
        to_project = payload.get("to_project")
        to_instance = payload.get("to_instance")
        if to_project and to_instance:
            from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send
            try:
                # Server-authoritative delivery §E: content-push the announce for
                # a federated-replicated message too (None for badge-only/info).
                await _notify_inbox_for_send(to_project, to_instance, _build_announce_packet(payload))
            except Exception as exc:  # pragma: no cover — defensive
                # Notify is best-effort; apply success is the durability
                # contract. Don't break replay on a push failure.
                log.warning(
                    "sync apply: inbox notify failed for %s/%s: %s",
                    to_project, to_instance, exc,
                )

    return {"disposition": "applied"}


async def _apply_message_status_changed(db, chroma, op):
    payload = op["payload"]
    message_id = op["ref"]["doc_id"]
    update_fields = {
        k: v for k, v in payload.items()
        if k != "_id" and v is not None
    }
    if not update_fields:
        return {"disposition": "applied", "flags": {"empty_update": True}}
    db.messages.update_one({"_id": message_id}, {"$set": update_fields})
    return {"disposition": "applied"}


_APPLY_HANDLERS = {
    # Chroma-backed (§4.3.a A-path embedding direct-apply)
    "learning.recorded": _apply_learning_recorded,
    "store.created": _apply_store_created,
    "function.registered": _apply_function_registered,
    "function.enriched": _apply_function_enriched,
    "spec.defined": _apply_spec_defined,
    "spec.updated": _apply_spec_updated,
    "store.tagged": _apply_store_tagged,
    "backlog.added": _apply_backlog_added,
    "backlog.updated": _apply_backlog_updated,
    "learning.superseded": _apply_learning_superseded,
    # Mongo-backed (§4.3.b transactional on emit; receiver uses unique
    # (origin, seq) for race protection instead of cross-store atomicity)
    "message.sent": _apply_message_sent,
    "message.status_changed": _apply_message_status_changed,
    # Not yet instrumented on emit side (catalog has them; emit canaries
    # haven't shipped — state:memory next-step #5 lists the gap):
    #   session.started, session.ended, agent.registered, agent.heartbeat,
    #   agent.work_updated, lock.acquired, lock.released, lock.expired,
    #   autopilot.config_changed, autopilot.event_recorded,
    #   autopilot.auto_disabled, signal.emitted, audit.event, rename.applied
    # When emit canaries land, add handlers here. Until then ops of those
    # types fall through to "rejected" with apply_unimplemented reason.
}


@mcp.tool()
async def memory_sync_push(
    session_id: str,
    ops: List[Dict[str, Any]],
    ctx: Context = None,
) -> str:
    """Apply a batch of op_log entries received from a peer.

    Phase 2 replication write endpoint per §5.1. Validates per-origin seq
    monotonicity (with §4.3.a sequence-skip tolerance), op_type membership
    in the §4.1 catalog, schema_version compatibility (§4.4),
    `intent_id` dedupe (§4.6), state-spec origin ownership (§7.4), and
    mutable-spec fast-forward (§7.2). Materializes each op into local
    Mongo+Chroma reusing the inline A-path embedding so peers never
    re-embed text.

    Args:
        session_id: Your session ID. Caller must hold `admin` or `owner`
            tier — these are server-to-server credentials.
        ops: Op_log entries to apply. Order matters when ops in the same
            batch depend on each other (e.g., spec.defined before
            spec.updated, function.registered before function.enriched);
            callers should preserve sender-side (origin, seq) ordering.

    Returns JSON envelope:
        {
          "results": [
            {"op_id": ..., "disposition": "applied" | "deduped_seq" |
             "deduped_intent" | "conflict" | "rejected" |
             "rejected_schema" | "rejected_origin_owner",
             "reason"?: str, "flags"?: dict},
            ...
          ],
          "applied_count": int,
          "rejected_count": int,
          "conflict_count": int,
          "deduped_count": int,
          "server_origin": "<this server's origin_id>",
        }

    See module docstring for the disposition catalog.
    """
    err = require_session(session_id)
    if err:
        return err

    session_info = active_sessions[session_id]
    auth_err = require_auth(session_info, "sync")
    if auth_err:
        return json.dumps({"error": auth_err})

    if not isinstance(ops, list):
        return json.dumps({"error": "ops must be a list"})

    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"})

    chroma = await get_chroma()
    if chroma is None:
        return json.dumps({"error": "Chroma unavailable"})

    result = await _push_ops(
        db=db, chroma=chroma, ops=ops, origin_server_id=ORIGIN_SERVER_ID
    )
    return json.dumps(result, default=_jsonable)
