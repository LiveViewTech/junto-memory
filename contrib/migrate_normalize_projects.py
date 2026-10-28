"""One-shot migration: normalize the `project` field across all MongoDB
collections so case/separator variants of the same project name collapse
to a single bucket.

Run AFTER deploying the case-sensitivity fix (helpers.normalize_project +
boundary normalization in tools). Idempotent — safe to re-run; rows already
in normalized form are skipped.

Usage:
    docker exec mcp-rag-arch python /app/contrib/migrate_normalize_projects.py [--dry-run]
"""

import argparse
import os
import sys
from typing import Dict, List

from pymongo import MongoClient
from pymongo.collection import Collection

# Match the rule in src/shared_memory/helpers.py:normalize_project
def normalize(name: str) -> str:
    if not name:
        return name
    return name.lower().replace("-", "_").replace(" ", "_")


def migrate_field(coll: Collection, field: str, dry_run: bool) -> Dict[str, int]:
    """Normalize a single field across all documents in a collection."""
    counts = {"scanned": 0, "updated": 0, "already_clean": 0, "empty": 0}
    for doc in coll.find({field: {"$exists": True, "$type": "string"}}):
        counts["scanned"] += 1
        old = doc[field]
        if not old:
            counts["empty"] += 1
            continue
        new = normalize(old)
        if new == old:
            counts["already_clean"] += 1
            continue
        counts["updated"] += 1
        if not dry_run:
            coll.update_one({"_id": doc["_id"]}, {"$set": {field: new}})
    return counts


def migrate_checklists_id(coll: Collection, dry_run: bool) -> Dict[str, int]:
    """Checklists use _id = f"{project}:{name}". Need to rebuild _id when
    the project component normalizes to a different value. Collisions
    (two checklists sharing a normalized _id) are reported but not merged
    automatically — operator decides.
    """
    counts = {"scanned": 0, "rebuilt": 0, "collision": 0, "already_clean": 0}
    seen_targets = set()
    for doc in list(coll.find({})):
        counts["scanned"] += 1
        old_id = doc.get("_id", "")
        if ":" not in old_id:
            counts["already_clean"] += 1
            continue
        proj, _, name = old_id.partition(":")
        new_proj = normalize(proj)
        new_id = f"{new_proj}:{name}"
        if new_id == old_id:
            counts["already_clean"] += 1
            continue
        if coll.find_one({"_id": new_id}) is not None or new_id in seen_targets:
            counts["collision"] += 1
            print(f"  COLLISION: {old_id!r} → {new_id!r} already exists; skipped")
            continue
        seen_targets.add(new_id)
        counts["rebuilt"] += 1
        if not dry_run:
            new_doc = dict(doc)
            new_doc["_id"] = new_id
            new_doc["project"] = new_proj
            coll.insert_one(new_doc)
            coll.delete_one({"_id": old_id})
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, do not write")
    ap.add_argument(
        "--mongo-uri",
        default=os.environ.get(
            "MONGO_URI",
            # Matches the MONGO_PASSWORD:-changeme convention in docker-compose.yml /
            # .env.example — not a real credential. See README.md's MONGO_PASSWORD note.
            f"mongodb://mcp_orch:{os.environ.get('MONGO_PASSWORD', 'changeme')}@mongodb:27017/mcp_orchestrator?authSource=admin",
        ),
    )
    ap.add_argument("--db", default="mcp_orchestrator")
    args = ap.parse_args()

    client = MongoClient(args.mongo_uri)
    db = client[args.db]

    plan: List[tuple] = [
        ("messages", "to_project"),
        ("messages", "from_project"),
        ("registered_agents", "project"),
        ("agent_autopilot", "project"),
        ("autopilot_events", "project"),
        ("audit_log", "project"),
        ("checklists", "project"),  # project field separate from _id
    ]

    print(f"{'DRY RUN — no writes' if args.dry_run else 'LIVE RUN — writing changes'}")
    print(f"Database: {args.db}")
    print()

    grand_total = 0
    for coll_name, field in plan:
        coll = db[coll_name]
        counts = migrate_field(coll, field, args.dry_run)
        if counts["scanned"] == 0:
            continue
        print(f"{coll_name}.{field}: scanned={counts['scanned']} "
              f"updated={counts['updated']} clean={counts['already_clean']} "
              f"empty={counts['empty']}")
        grand_total += counts["updated"]

    print()
    print("checklists._id (rebuild composite key):")
    cl_counts = migrate_checklists_id(db["checklists"], args.dry_run)
    print(f"  scanned={cl_counts['scanned']} rebuilt={cl_counts['rebuilt']} "
          f"collision={cl_counts['collision']} clean={cl_counts['already_clean']}")
    grand_total += cl_counts["rebuilt"]

    print()
    print(f"TOTAL field updates: {grand_total}")
    if args.dry_run:
        print("Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
