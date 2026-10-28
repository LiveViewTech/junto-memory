#!/usr/bin/env python3
"""Retire active GLOBAL guidelines that are not in the deployed code.

Why: the boot seed (global_guidelines.seed_global_guidelines) inserts/updates
the code-defined globals but only LOGS "orphans" — active global rows whose name
is no longer in GLOBAL_GUIDELINES. A server upgraded from an older build (e.g.
the June 0e2a7ce work box: 15 pre-trim globals) would otherwise serve the old
and the new rule sets side by side. memory_guidelines refuses global edits
(321808d), so this is the supported cleanup. It supersedes
guideline_trim_flip.py for upgrades: it inserts nothing (the seed does that,
with the CURRENT text) and works before or after the first boot.

What it does (dry-run unless --execute):
  1. Lists active scope="global" rows whose name is not in GLOBAL_GUIDELINES.
  2. Copies each to `guidelines_archive` (archived_by=retire_orphan_globals) and
     sets active=False on the original. Project-scoped rows are never touched.
  3. Bumps guidelines_meta so agents pick up the change at next session start.

Usage:
  retire_orphan_globals.py                     # dry-run
  retire_orphan_globals.py --execute
  retire_orphan_globals.py --keep a,b --execute   # leave these orphans active
  retire_orphan_globals.py --rollback [--execute] # reactivate what this script retired
Connects with MONGO_* from the repo .env (same as facets_backfill.py), or —
on a box without the repo venv — run it INSIDE the server container, where it
uses the server's own Mongo client:
  docker exec -i mcp-rag-arch python - --in-container [--execute] < scripts/retire_orphan_globals.py
"""

import sys
from pathlib import Path

IN_CONTAINER = "--in-container" in sys.argv
if not IN_CONTAINER:
    REPO = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(REPO / "src"))
    sys.path.insert(0, str(REPO / "scripts"))

ACTOR = "retire_orphan_globals"


def _arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def orphan_globals(db, keep=()):
    """Active global rows not defined in code and not in `keep`."""
    from shared_memory.global_guidelines import GLOBAL_GUIDELINES
    code = {g["name"] for g in GLOBAL_GUIDELINES}
    keep = set(keep)
    return [g for g in db.guidelines.find({"scope": "global", "active": True})
            if g["name"] not in code and g["name"] not in keep]


def main():
    execute = "--execute" in sys.argv
    if IN_CONTAINER:
        from shared_memory.clients import get_mongo
        db = get_mongo()
        if db is None:
            print("ABORT: server Mongo client unavailable")
            return 1
    else:
        from facets_backfill import _env
        from pymongo import MongoClient
        env = _env()
        c = MongoClient("localhost", int(env.get("MONGO_PORT", 27019)),
                        username=env["MONGO_USER"], password=env["MONGO_PASSWORD"],
                        authSource="admin", directConnection=True)
        db = c[env.get("MONGO_DB", "mcp_orchestrator")]
    from shared_memory.helpers import utc_now
    from shared_memory.tools.guidelines import bump_guidelines_version
    mode = "EXECUTE" if execute else "DRY-RUN"

    if "--rollback" in sys.argv:
        rows = list(db.guidelines.find({"scope": "global", "active": False,
                                        "deactivated_by": ACTOR}))
        print(f"{mode} ROLLBACK: reactivate {len(rows)} rule(s): {[g['name'] for g in rows]}")
        if not rows or not execute:
            return 0
        for g in rows:
            db.guidelines.update_one({"_id": g["_id"]}, {"$set": {"active": True},
                                     "$unset": {"deactivated_by": "", "deactivated_at": ""}})
        print(f"guidelines_version -> {bump_guidelines_version(db, ACTOR + '_rollback')}")
        return 0

    keep = [k.strip() for k in (_arg("--keep") or "").split(",") if k.strip()]
    rows = orphan_globals(db, keep)
    print(f"{mode}: retire {len(rows)} orphan global rule(s)" + (f", keeping {keep}" if keep else ""))
    for g in rows:
        print(f"  retire: {g['name']}  (updated_by={g.get('updated_by')}, priority={g.get('priority')})")
    if not rows or not execute:
        if rows:
            print("dry-run only — rerun with --execute")
        return 0
    now = utc_now()
    for g in rows:
        db.guidelines_archive.insert_one({**{k: v for k, v in g.items() if k != "_id"},
                                          "original_id": g["_id"], "archived_at": now,
                                          "archived_by": ACTOR})
        db.guidelines.update_one({"_id": g["_id"]}, {"$set": {
            "active": False, "deactivated_by": ACTOR, "deactivated_at": now}})
    print(f"RETIRED. guidelines_version -> {bump_guidelines_version(db, ACTOR)}. "
          "Agents get the new set at next session start.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
