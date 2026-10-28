#!/usr/bin/env python3
"""Bulk-librarian prep: cluster-aware slices for strong-model facet waves.

For a target set of ACTIVE learnings lacking facet rows, this:
  1. pulls embeddings from Chroma and finds similarity candidate pairs
     (top-5 neighbors >= SIM_THRESHOLD, active docs only, cross-checked both
     directions) — the systematic half of the dedup sweep
     (backlog_19644ab5e00d);
  2. union-finds pairs into clusters and PACKS WHOLE CLUSTERS into the same
     slice, so near-duplicate docs are always judged by the SAME agent
     (fixes the 2026-07-21 partition artifact: twins in different slices are
     invisible to opportunistic dupe-noticing);
  3. writes slice files {ids: [...], pairs: [[a, b, sim], ...]} as JSON into
     the given output dir, ~SLICE_SIZE docs each.

Pairs include ALREADY-FACETED partners when similar to a target doc (the
agent should judge the pair even if only one member needs facets).

Usage: facets_bulk_prep.py --projects nimbus,shared --out DIR [--slice 50]
       (project "shared" = shared_patterns; "all" = everything)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

SIM_THRESHOLD = 0.60
NEIGHBORS = 5

from facets_backfill import _env  # noqa: E402  (same scripts dir)


def _mongo():
    from pymongo import MongoClient
    env = _env()
    c = MongoClient("localhost", int(env.get("MONGO_PORT", 27019)),
                    username=env["MONGO_USER"], password=env["MONGO_PASSWORD"],
                    authSource="admin", directConnection=True)
    return c[env.get("MONGO_DB", "mcp_orchestrator")]


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--slice", type=int, default=50)
    args = ap.parse_args()

    import chromadb
    chroma = await chromadb.AsyncHttpClient(host="localhost", port=8001)
    db = _mongo()
    from shared_memory import facets as facets_mod

    wanted = args.projects.split(",")
    names = [x.name if hasattr(x, "name") else x
             for x in await chroma.list_collections()]
    targets = []
    for w in wanted:
        if w == "all":
            targets = [n for n in names
                       if n.startswith("proj_") or n == "shared_patterns"]
            break
        targets.append("shared_patterns" if w == "shared" else f"proj_{w}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    slice_no = 0

    for cname in targets:
        if cname not in names:
            print(f"{cname}: no such collection, skipping")
            continue
        col = await chroma.get_collection(cname)
        got = await col.get(where={"type": "learning"},
                            include=["metadatas", "embeddings"])
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        embs = got.get("embeddings")
        embs = list(embs) if embs is not None else []

        active = {}
        for i, did in enumerate(ids):
            m = metas[i] or {}
            if m.get("status") in (None, "active") and embs[i] is not None:
                active[did] = embs[i]
        need = [d for d in active
                if facets_mod.needs_extraction(db, d)]
        print(f"{cname}: {len(active)} active, {len(need)} need facets")
        if not need:
            continue

        # Similarity pairs: query each doc-needing-facets against the
        # collection; keep active neighbors above threshold.
        pairs = {}
        for did in need:
            res = await col.query(
                query_embeddings=[active[did]],
                n_results=NEIGHBORS + 1,
                where={"type": "learning"},
                include=["distances"],
            )
            rids = (res.get("ids") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for rid, dist in zip(rids, dists):
                if rid == did or rid not in active:
                    continue
                sim = 1.0 - dist
                if sim >= SIM_THRESHOLD:
                    key = tuple(sorted((did, rid)))
                    pairs[key] = max(pairs.get(key, 0.0), round(sim, 3))

        # Cluster: pack whole clusters into one slice.
        dsu = DSU()
        for a, b in pairs:
            dsu.union(a, b)
        clusters = {}
        for d in need:
            clusters.setdefault(dsu.find(d), []).append(d)
        # Largest clusters first so they never straddle slice boundaries.
        ordered = sorted(clusters.values(), key=len, reverse=True)

        cur, cur_ids = [], set()

        def flush():
            nonlocal cur, cur_ids, slice_no
            if not cur:
                return
            slice_pairs = [[a, b, s] for (a, b), s in pairs.items()
                           if a in cur_ids or b in cur_ids]
            path = outdir / f"bulk_{slice_no:03d}.json"
            path.write_text(json.dumps(
                {"collection": cname, "ids": cur, "pairs": slice_pairs}))
            print(f"  wrote {path.name}: {len(cur)} ids, {len(slice_pairs)} pairs")
            slice_no += 1
            cur, cur_ids = [], set()

        for cluster in ordered:
            if cur and len(cur) + len(cluster) > args.slice:
                flush()
            cur.extend(cluster)
            cur_ids.update(cluster)
        flush()

    print(f"done: {slice_no} slice files in {outdir}")


if __name__ == "__main__":
    asyncio.run(main())
