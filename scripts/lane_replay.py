#!/usr/bin/env python3
"""Replay REAL logged queries through both retrieval lanes and judge the results.

    .venv/bin/python scripts/lane_replay.py run    [--model gpt-oss:20b] [--workers 4] [--chunk-db junto_chunks_exp --chunker 3]
    .venv/bin/python scripts/lane_replay.py report [--run RUN_ID]

Why: interface:chunk-index-v0 rollout step 4 asks for evidence on real traffic
before flipping a project. The synthetic eval sets (retrieval_eval.py) say the
chunk lane wins; this checks it on what agents actually asked.

Sources: recall_events.query (POST /recall, the per-turn hook; agent prompt
windows) and query_events.query (explicit memory_query). Read-only against prod.
BIAS: recall_events only logs turns where the LIVE lane served something, so
turns where MiniLM found nothing (where the chunk lane could add hits) are
missing.

Each distinct (kind, project, query) runs through both lanes, plus `fused`
(per-collection RRF of the two, eligible if either clears its floor), with the prod
filters (status=active, recall exclusions, shared collections capped at 2) and
prod floors:
  recall: minilm 0.6 / chunk 0.63, k=5;   memory_query: 0.3 / 0.37 (+0.1 shared), limit 3.
A local model grades every (query, doc) pair once, blind to the lane:
2 = directly useful for the agent's current work, 1 = related background, 0 = noise.
Judgments are cached in Mongo lane_replay_judgments keyed by (query hash, doc id).
"""

import argparse
import asyncio
import hashlib
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "src"), str(REPO / "contrib" / "local-inference")]
logging.disable(logging.INFO)

from facets_backfill import _chroma, _mongo  # noqa: E402

OLLAMA = "http://192.168.15.240:11434"
RUNS = "lane_replay_runs"
JUDGE = "lane_replay_judgments"
QUERY_CAP = 2500
DOC_CAP = 1500
FLOORS = {"recall": {"minilm": 0.6, "chunk": 0.63}, "query": {"minilm": 0.3, "chunk": 0.37}}
JUDGE_SCHEMA = {"type": "object", "properties": {
    "grade": {"type": "integer", "enum": [0, 1, 2]}, "reason": {"type": "string"}},
    "required": ["grade", "reason"]}
JUDGE_INSTRUCTIONS = (
    "You grade a team memory system. An AI software agent was working; CONTEXT is what it was "
    "doing or asking. The memory system offered it the NOTE. Grade how useful the NOTE is for the "
    "agent's CURRENT work: 2 = directly useful (answers the question or materially informs the "
    "current step), 1 = related background the agent might glance at, 0 = irrelevant or noise. "
    "Be strict: topical overlap alone is 1 at most. Return JSON {\"grade\": 0|1|2, \"reason\": "
    "\"<10 words>\"}.")


def _qhash(q):
    return hashlib.sha1(q.encode("utf-8", "replace")).hexdigest()[:16]


def _queries(db):
    seen, out = set(), []
    for e in db.recall_events.find({"query": {"$ne": None}}):
        if e.get("agent") == "deploy-verify" or not e.get("project"):
            continue
        key = ("recall", e["project"], e["query"])
        if key not in seen:
            seen.add(key)
            out.append({"kind": "recall", "project": e["project"], "agent": e.get("agent"), "query": e["query"]})
    for e in db.query_events.find({}):
        if not e.get("project") or not e.get("query"):
            continue
        key = ("query", e["project"], e["query"])
        if key not in seen:
            seen.add(key)
            out.append({"kind": "query", "project": e["project"], "agent": e.get("agent"), "query": e["query"]})
    return out


async def _lane(lane, q, chroma, ci, cols_for):
    """Prod-shaped retrieval for one query on one lane → [{id, score, source, title, doc}] (served set)."""
    from shared_memory.__main__ import _recall_excluded, _recall_where
    from shared_memory.helpers import embed_query_once, is_expired, query_kwargs
    from shared_memory.tools.query import calculate_relevance
    recall = q["kind"] == "recall"
    k = 5 if recall else 3
    where = _recall_where(None) if recall else {"status": "active"}
    floor = FLOORS[q["kind"]][lane]
    proj, shared = await cols_for(q["project"])
    qtext = q["query"][:8000]
    mvec = await embed_query_once(proj, qtext)
    cvec = await ci.embed_query(qtext) if lane == "chunk" else None
    out = []
    for col, n, bump in [(proj, k, 0.0)] + [(s, min(2, k), 0.0 if recall else 0.1) for s in shared]:
        got = await ci.scan(col, cvec, n, where) if lane == "chunk" else None
        if got is None:
            if lane == "chunk":
                raise RuntimeError(f"chunk lane unusable for {col.name}")
            got = await col.query(**query_kwargs(qtext, mvec), n_results=n, where=where)
        for i, doc, meta, dist in zip(got["ids"][0], got["documents"][0], got["metadatas"][0], got["distances"][0]):
            meta = meta or {}
            if is_expired(meta) or (recall and _recall_excluded(i, meta, q["agent"])):
                continue
            s = calculate_relevance(dist)
            if s < floor + bump:
                continue
            out.append({"id": i, "score": round(s, 3), "source": col.name,
                        "title": meta.get("title", ""), "doc": doc or ""})
    out.sort(key=lambda r: -r["score"])
    return out[:k] if recall else out


async def _fused(q, chroma, ci, cols_for, rrf_k=60):
    """Hybrid lane: per collection, RRF over the MiniLM and chunk rankings (each
    2x the served depth); a doc is eligible if EITHER lane's score clears that
    lane's prod floor. Same per-collection caps as prod."""
    from shared_memory.__main__ import _recall_excluded, _recall_where
    from shared_memory.helpers import embed_query_once, is_expired, query_kwargs
    from shared_memory.tools.query import calculate_relevance
    recall = q["kind"] == "recall"
    k = 5 if recall else 3
    where = _recall_where(None) if recall else {"status": "active"}
    fl = FLOORS[q["kind"]]
    proj, shared = await cols_for(q["project"])
    qtext = q["query"][:8000]
    mvec = await embed_query_once(proj, qtext)
    cvec = await ci.embed_query(qtext)
    out = []
    for col, n, bump in [(proj, k, 0.0)] + [(s, min(2, k), 0.0 if recall else 0.1) for s in shared]:
        lanes = {"minilm": await col.query(**query_kwargs(qtext, mvec), n_results=2 * n, where=where),
                 "chunk": await ci.scan(col, cvec, 2 * n, where)}
        cand = {}
        for lane, got in lanes.items():
            rank = 0
            for i, doc, meta, dist in zip(got["ids"][0], got["documents"][0], got["metadatas"][0], got["distances"][0]):
                meta = meta or {}
                if is_expired(meta) or (recall and _recall_excluded(i, meta, q["agent"])):
                    continue
                rank += 1
                c = cand.setdefault(i, {"id": i, "source": col.name, "title": meta.get("title", ""),
                                        "doc": doc or "", "rrf": 0.0, "ok": False})
                c["rrf"] += 1.0 / (rrf_k + rank)
                c["ok"] |= calculate_relevance(dist) >= fl[lane] + bump
        keep = sorted((c for c in cand.values() if c["ok"]), key=lambda c: -c["rrf"])[:n]
        out += [{**c, "score": round(c["rrf"], 4)} for c in keep]
    out.sort(key=lambda r: -r["score"])
    return out[:k] if recall else out


def _judge_one(model, qtext, row):
    from li import call
    passage = (f"CONTEXT:\n{qtext[-QUERY_CAP:]}\n\nNOTE (title: {row['title']}):\n{row['doc'][:DOC_CAP]}")
    err = "bad output"
    for _ in range(2):
        try:
            r = call(OLLAMA, model, JUDGE_INSTRUCTIONS, passage, JUDGE_SCHEMA, 180)
            g = int(r.get("grade"))
            if g in (0, 1, 2):
                return g, str(r.get("reason", ""))[:120]
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return None, f"judge failed: {err}"


async def run(a):
    from shared_memory import chunk_index as ci
    from shared_memory.helpers import get_project_collection, get_shared_collection
    sfx = "" if ci.CHUNKER == 2 else str(ci.CHUNKER)
    ci.EMBED_URL = OLLAMA
    ci.EMBED_TIMEOUT_S = 30.0
    db, chroma = _mongo(), await _chroma()
    await ci.get_chunk_client("localhost", 8001)
    cache = {}

    async def cols_for(project):
        if project not in cache:
            cache[project] = (await get_project_collection(chroma, project),
                              [await get_shared_collection(chroma, n) for n in ("patterns", "context")])
        return cache[project]

    qs = _queries(db)
    print(f"{len(qs)} distinct real queries", flush=True)
    rows = []
    for n, q in enumerate(qs):
        res = {}
        for lane in ("minilm", "chunk"):
            res[lane if lane == "minilm" else lane + sfx] = await _lane(lane, q, chroma, ci, cols_for)
        res["fused" + sfx] = await _fused(q, chroma, ci, cols_for)
        rows.append({**q, "qhash": _qhash(q["query"]), "lanes": res})
        if n % 25 == 0:
            print(f"  retrieved {n}/{len(qs)}", flush=True)

    todo = {}
    for r in rows:
        for lane in r["lanes"]:
            for d in r["lanes"][lane]:
                key = (r["qhash"], d["id"])
                if key not in todo and not db[JUDGE].find_one({"qhash": r["qhash"], "doc_id": d["id"], "model": a.model}):
                    todo[key] = (r["query"], d)
    print(f"{len(todo)} new judgments to make with {a.model}", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(_judge_one, a.model, qt, d): key for key, (qt, d) in todo.items()}
        for i, f in enumerate(futs):
            g, why = f.result()
            qh, did = futs[f]
            db[JUDGE].update_one({"qhash": qh, "doc_id": did, "model": a.model},
                                 {"$set": {"grade": g, "reason": why, "ts": time.time()}}, upsert=True)
            if i % 50 == 0:
                print(f"  judged {i}/{len(todo)} ({time.time() - t0:.0f}s)", flush=True)
    run_id = f"replay-{int(time.time())}"
    db[RUNS].insert_one({"_id": run_id, "model": a.model, "ts": time.time(), "rows": [
        {k: v for k, v in r.items() if k != "lanes"} | {"lanes": {
            ln: [{x: d[x] for x in ("id", "score", "source", "title")} for d in ds]
            for ln, ds in r["lanes"].items()}} for r in rows]})
    print("run", run_id, flush=True)
    report_run(db, run_id)


def report_run(db, run_id=None):
    doc = db[RUNS].find_one({"_id": run_id}) if run_id else next(db[RUNS].find().sort("ts", -1).limit(1))
    model = doc["model"]
    grades = {(j["qhash"], j["doc_id"]): j.get("grade") for j in db[JUDGE].find({"model": model})}
    out = {}
    for r in doc["rows"]:
        for lane, ds in r["lanes"].items():
            for scope in (r["kind"], f"{r['kind']}:{r['project']}", "all"):
                s = out.setdefault(scope, {}).setdefault(lane, {"queries": 0, "served": 0, "g2": 0, "g1": 0, "g0": 0,
                                                              "ungraded": 0, "q_with_useful": 0, "q_empty": 0})
                s["queries"] += 1
                s["served"] += len(ds)
                s["q_empty"] += not ds
                gs = [grades.get((r["qhash"], d["id"])) for d in ds]
                s["q_with_useful"] += any(g == 2 for g in gs)
                for g in gs:
                    s[{2: "g2", 1: "g1", 0: "g0"}.get(g, "ungraded")] += 1
    print(f"run {doc['_id']} judge={model} queries={len(doc['rows'])}")
    for scope in sorted(out):
        for lane, s in sorted(out[scope].items()):
            graded = s["g2"] + s["g1"] + s["g0"]
            prec = (s["g2"] + 0.5 * s["g1"]) / graded if graded else 0
            print(f"{scope:22s} {lane:6s} q={s['queries']:3d} served={s['served']:4d} "
                  f"useful(2)={s['g2']:4d} related(1)={s['g1']:4d} noise(0)={s['g0']:4d} "
                  f"precision={prec:.2f} q_with_useful={s['q_with_useful'] / s['queries']:.2f} "
                  f"q_empty={s['q_empty'] / s['queries']:.2f}")
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--model", default="gpt-oss:20b")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--chunk-db", default=None, help="read an experimental chunk database (JUNTO_CHUNK_DB)")
    r.add_argument("--chunker", default=None, help="its CHUNKER version (JUNTO_CHUNKER); lanes get it as a suffix")
    p = sub.add_parser("report")
    p.add_argument("--run", default=None)
    a = ap.parse_args()
    if a.cmd == "run":
        import os
        if a.chunk_db:
            os.environ["JUNTO_CHUNK_DB"] = a.chunk_db
        if a.chunker:
            os.environ["JUNTO_CHUNKER"] = a.chunker
        asyncio.run(run(a))
    else:
        report_run(_mongo(), a.run)


if __name__ == "__main__":
    main()
