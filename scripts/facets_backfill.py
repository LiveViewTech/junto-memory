#!/usr/bin/env python3
"""Facets backfill runner (design:memory-facets-v0 §producers.2).

Host-side batch producer for the existing corpus — the write-time stage only
covers NEW learnings; this walks every project collection (plus shared) and
fills mongo `learning_facets` for learnings that have no row at the current
FACETS_RECIPE_VERSION. Idempotent and resumable by construction.

Modes:
  --probe             Run the spec's shelf_life micro-calibration probe set
                      and report PASS/FAIL. The backfill must not be trusted
                      until this passes (spec §shelf_life micro-calibration).
  --ids a,b,c         Backfill specific doc ids (searched across collections).
  --batch N           Backfill up to N unfaceted learnings across all
                      projects (use --project to restrict).
  --dry-run           List what would be processed, extract nothing.

Runs on the host (like librarian.py): pymongo direct to localhost:27019
(directConnection=True — the replica set advertises a compose-internal
hostname, learning_891d73c301f8ac2b), chromadb AsyncHttpClient to
localhost:8001, Ollama at localhost:11434 via the shared facets pipeline.
Reuses src/shared_memory/facets.py verbatim so backfilled rows are
byte-compatible with write-time rows.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Host-side endpoint overrides MUST land before the facets import chain reads
# them. In-container defaults point at host.docker.internal / mongodb.
os.environ.setdefault("JUNTO_CLAIM_GATE_URL", "http://localhost:11434/v1")
os.environ.setdefault("JUNTO_FACETS_ENABLED", "true")

from shared_memory import claim_gate, facets  # noqa: E402

# ── spec §shelf_life micro-calibration (pinned in design:memory-facets-v0) ──
PROBE_VOLATILE = [
    "learning_d39053bd36607932",  # JVM-state wrong conclusion (sub HARMFUL #1)
    "learning_e21c7f900136579d",  # stale 'DEFINITIVE root cause' (sub HARMFUL #2)
    "learning_a057cb7727f2e3fe",  # same-day-corrected power-off claim
]
PROBE_DURABLE = [
    "learning_6da8dfb1c5b54ca8",  # pymongo directConnection gotcha (mechanism)
    "learning_305e0f12b187eb32",  # SSE silence is expected behavior (mechanism)
    "learning_66ce35d381dd62f3",  # stdout banner corrupts $() capture (code gotcha)
    # NOTE: learning_17cc8115a8a1fa09 was tried as a durable control and
    # rejected — it's a workaround CONCLUSION ("must create user in target
    # db"), i.e. the revisable-belief class itself; phi4 calling it volatile
    # supports the boundary rather than failing it.
]


def _env():
    env = {}
    for line in open(REPO / ".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def _mongo():
    from pymongo import MongoClient
    env = _env()
    client = MongoClient(
        "localhost", int(env.get("MONGO_PORT", 27019)),
        username=env["MONGO_USER"], password=env["MONGO_PASSWORD"],
        authSource="admin", directConnection=True,
    )
    return client[env.get("MONGO_DB", "mcp_orchestrator")]


async def _chroma():
    import chromadb
    return await chromadb.AsyncHttpClient(host="localhost", port=8001)


async def _learning_collections(chroma, project=None):
    names = [c.name if hasattr(c, "name") else c for c in await chroma.list_collections()]
    if project:
        return [n for n in names if n == f"proj_{project}"]
    return sorted(n for n in names if n.startswith("proj_")) + \
        [n for n in names if n == "shared_patterns"]


def _needs_facets(db, doc_id):
    # Delegates to the shared preservation-aware check: reviewed rows are
    # NEVER re-extracted mechanically (facets.needs_extraction docstring).
    return facets.needs_extraction(db, doc_id)


async def _find_doc(chroma, doc_id, project=None):
    """Locate (collection, title, details, status) for a doc id across
    collections."""
    for name in await _learning_collections(chroma, project):
        col = await chroma.get_collection(name)
        got = await col.get(ids=[doc_id], include=["documents", "metadatas"])
        if got.get("ids") and got["ids"] and doc_id in got["ids"]:
            i = got["ids"].index(doc_id)
            doc = (got.get("documents") or [None])[i]
            meta = (got.get("metadatas") or [{}])[i] or {}
            if doc is None:
                return None
            title = meta.get("title", "")
            details = claim_gate._strip_title_header(doc, title)
            return col, title, details, meta.get("status") or "active"
    return None


async def _process_one(db, chroma, doc_id, project=None):
    found = await _find_doc(chroma, doc_id, project)
    if not found:
        print(f"  {doc_id}: NOT FOUND in any collection")
        return None
    col, title, details, status = found
    await facets._extract_and_store(db, col, doc_id, title, details)
    row = db[facets.FACETS_COLLECTION].find_one({"_id": doc_id})
    if not row:
        print(f"  {doc_id}: extraction produced no row (see server-side rules)")
        return None
    # Status-gap fix (coordinator msg_2a7b44249fff): the targeted path — unlike
    # batch mode — processes NON-ACTIVE docs deliberately (sub's A/B needs
    # facets on historically-injected docs even if since superseded), but the
    # row must SAY so, and downstream review/flagging must not re-litigate
    # already-dispositioned docs.
    if status != "active":
        db[facets.FACETS_COLLECTION].update_one(
            {"_id": doc_id}, {"$set": {"doc_status": status}}
        )
        row["doc_status"] = status
    print(f"  {doc_id}: operation={row.get('operation')} "
          f"shelf_life={row.get('shelf_life')} triggers={len(row.get('trigger', []))}"
          f"{'  status=' + status if status != 'active' else ''}"
          f"  [{title[:60]}]")
    return row


async def run_probe(db, chroma):
    print("== shelf_life micro-calibration probe (spec gate) ==")
    results = {}
    for doc_id in PROBE_VOLATILE + PROBE_DURABLE:
        row = await _process_one(db, chroma, doc_id)
        results[doc_id] = (row or {}).get("shelf_life")

    # Asymmetric gate (same precision-profile logic as the claim gate): the
    # HARD condition is the volatile side — a stale diagnosis marked durable
    # is the harm mode (trusted stale belief). Durable-side misses only cost
    # an unnecessary verify-first framing (safe direction), so they are
    # reported as recall, not gated. Probe record 2026-07-21: v1 prompt =
    # volatile 3/3, durable 2/3; two alternative framings both degraded
    # durable recall — iteration stopped (see SHELF_LIFE_SYSTEM note).
    hard_failures = [
        f"{doc_id}: expected volatile, got {results.get(doc_id)}"
        for doc_id in PROBE_VOLATILE if results.get(doc_id) != "volatile"
    ]
    durable_hits = sum(1 for d in PROBE_DURABLE if results.get(d) == "durable")

    if hard_failures:
        print("PROBE FAIL (HARD) — a revisable belief was marked durable; "
              "do not trust shelf_life until the prompt is fixed:")
        for f in hard_failures:
            print(f"  ✗ {f}")
        return False
    print(f"PROBE PASS — hard condition met (volatile {len(PROBE_VOLATILE)}/"
          f"{len(PROBE_VOLATILE)}); durable recall {durable_hits}/{len(PROBE_DURABLE)} "
          "(misses are safe-direction; stronger-model re-judge is the backfill "
          "agent's job).")
    return True


async def run_batch(db, chroma, batch, project=None, dry_run=False):
    print(f"== backfill batch (limit {batch}, project={project or 'ALL'}) ==")
    done = 0
    for name in await _learning_collections(chroma, project):
        if done >= batch:
            break
        col = await chroma.get_collection(name)
        # Single-key where filter ONLY (multi-key silently returns nothing —
        # the 28570d4 gotcha); status/type refinement happens in Python.
        got = await col.get(where={"type": "learning"},
                            include=["metadatas"])
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        for i, doc_id in enumerate(ids):
            if done >= batch:
                break
            meta = metas[i] or {}
            if meta.get("status") not in (None, "active"):
                continue
            if not _needs_facets(db, doc_id):
                continue
            if dry_run:
                print(f"  would process {name}/{doc_id} [{meta.get('title', '')[:60]}]")
                done += 1
                continue
            row = await _process_one(db, chroma, doc_id)
            if row:
                done += 1
    print(f"== {'listed' if dry_run else 'processed'} {done} learning(s) ==")
    return done


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--ids", type=str, default=None)
    ap.add_argument("--batch", type=int, default=0)
    ap.add_argument("--project", type=str, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = _mongo()
    chroma = await _chroma()

    if args.probe:
        ok = await run_probe(db, chroma)
        sys.exit(0 if ok else 1)
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
        if args.ids.startswith("@"):
            ids = [line.strip() for line in open(args.ids[1:])
                   if line.strip()]
        for doc_id in ids:
            if not _needs_facets(db, doc_id):
                print(f"  {doc_id}: already faceted at {facets.FACETS_RECIPE_VERSION} — skip")
                continue
            await _process_one(db, chroma, doc_id, args.project)
        return
    if args.batch:
        await run_batch(db, chroma, args.batch, args.project, args.dry_run)
        return
    ap.print_help()


if __name__ == "__main__":
    asyncio.run(main())
