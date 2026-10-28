#!/usr/bin/env python3
"""Restore backlog items destroyed by the same-project-move defect.

One-shot forensic restoration (backlog_dd9654145345, 2026-08-07). The
update_backlog_item project=<current> path silently deleted items from
≤2026-03-11 until fix 316b351; the op-log (shipped 2026-05-14) records the
14 destruction events within instrument coverage, and every destroyed
item's `backlog.added` entry carries the full description.

COUNT CLAUSE (do not smooth away): 14 is the count WITHIN INSTRUMENT
COVERAGE — the honest total is 14 plus an unknown for Mar→May.

Dry-run by default (read-only). --execute performs the chroma re-adds with
ORIGINAL ids (so existing references resolve), tags each doc
restored-from-oplog-2026-08-07, and stamps restored_at/restored_by.
Tom-gated: run --execute only with recorded approval.

Run INSIDE the container:
  docker exec mcp-rag-arch python /app/scripts/restore_oplog_backlog_20260807.py [--execute]
"""

import os
import sys
from datetime import datetime, timezone

import chromadb
from pymongo import MongoClient

# The 13 to restore (pipeline's backlog_1c5237bc6566 self-recreated — excluded).
RESTORE_IDS = [
    "backlog_bd4b26e4667f",  # server-team
    "backlog_660a63f0c449",  # coordinator
    "backlog_1fb71ae2ea0b",  # coordinator
    "backlog_bfba870fdf29",  # billing-team
    "backlog_5b1a4e01fcbf",  # jobs-team
    "backlog_a5eb7625f2cc",  # jobs-team
    "backlog_80f9bbb265cc",  # inbox
    "backlog_30fb3a1e3db8",  # inbox
    "backlog_804acc00ef7a",  # frames-team
    "backlog_0972ea163846",  # frames-team
    "backlog_bb3c9ea71280",  # frames-team
    "backlog_eb6538f345cd",  # tom-assistant-124220 (escheat → coordinator)
    "backlog_44180aa6055c",  # tom-assistant-124220 (escheat → coordinator)
]

RESTORE_TAG = "restored-from-oplog-2026-08-07"


def mongo():
    host = os.environ.get("MONGO_HOST", "mongodb")
    port = int(os.environ.get("MONGO_PORT", "27017"))
    user = os.environ.get("MONGO_USER") or os.environ.get("MONGO_INITDB_ROOT_USERNAME")
    pw = os.environ.get("MONGO_PASSWORD") or os.environ.get("MONGO_INITDB_ROOT_PASSWORD")
    dbname = os.environ.get("MONGO_DB", "mcp_orchestrator")
    uri = f"mongodb://{user}:{pw}@{host}:{port}/?authSource=admin" if user else f"mongodb://{host}:{port}"
    return MongoClient(uri)[dbname]


def main():
    execute = "--execute" in sys.argv
    db = mongo()
    client = chromadb.HttpClient(
        host=os.environ.get("CHROMA_HOST", "chromadb"),
        port=int(os.environ.get("CHROMA_PORT", "8000")),
    )
    now = datetime.now(timezone.utc).isoformat()

    restored, skipped = 0, 0
    for item_id in RESTORE_IDS:
        added = db.op_log.find_one({"op_type": "backlog.added", "ref.doc_id": item_id})
        if not added:
            print(f"SKIP {item_id}: no backlog.added op-log entry")
            skipped += 1
            continue
        pay = added.get("payload", {})
        coll_name = added["ref"]["collection"]

        # Replay metadata evolution from the update trail (latest wins).
        latest = pay
        for upd in db.op_log.find(
            {"op_type": "backlog.updated", "ref.doc_id": item_id}
        ).sort("ts", 1):
            latest = {**latest, **{k: v for k, v in upd.get("payload", {}).items()
                                   if v not in (None, "") and k != "embedding"}}

        coll = client.get_or_create_collection(coll_name)
        existing = coll.get(ids=[item_id], include=[])
        if existing.get("ids"):
            print(f"SKIP {item_id}: already exists in {coll_name} (collision guard)")
            skipped += 1
            continue

        title = latest.get("title") or pay.get("title") or "(untitled)"
        desc = pay.get("description", "")
        import json as _json
        meta = {
            "title": title,
            "type": "backlog",
            "backlog_status": latest.get("backlog_status", "open"),
            "priority": latest.get("priority", pay.get("priority", "medium")),
            "project": pay.get("project", ""),
            "assigned_to": latest.get("assigned_to", pay.get("assigned_to", "")),
            "tags": _json.dumps((pay.get("tags") or []) + [RESTORE_TAG]),
            "target_version": latest.get("target_version", ""),
            "deferred_reason": latest.get("deferred_reason", ""),
            "created": pay.get("created", ""),
            "updated": now,
            "restored_at": now,
            "restored_by": "memory (op-log restoration, backlog_dd9654145345)",
            "edit_count": int(latest.get("edit_count") or 0),
        }
        verb = "RESTORE" if execute else "DRY-RUN"
        print(f"{verb} {item_id} -> {coll_name} :: {title[:60]} :: desc={len(desc)}ch "
              f"status={meta['backlog_status']} prio={meta['priority']} owner={meta['assigned_to']}")
        if execute:
            coll.add(ids=[item_id], documents=[f"# {title}\n\n{desc}"], metadatas=[meta])
            restored += 1

    print(f"\n{'restored' if execute else 'would restore'}: "
          f"{restored if execute else len(RESTORE_IDS) - skipped}, skipped: {skipped}")


if __name__ == "__main__":
    main()
