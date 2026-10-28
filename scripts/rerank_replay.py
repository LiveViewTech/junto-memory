#!/usr/bin/env python3
"""Query-rewrite + cross-encoder rerank experiment on REAL logged queries.

    JUNTO_CHUNK_DB=junto_chunks_v3 JUNTO_CHUNKER=3 .venv/bin/python scripts/rerank_replay.py run
    .venv/bin/python scripts/rerank_replay.py volume [--run RUN]

Needs the rerank service (contrib/reranker/rerank_server.py, 127.0.0.1:8091)
and sage Ollama for the rewrite model.

Per distinct nimbus query (recall_events + query_events, as lane_replay.py):
  rewrite   /recall contexts only: a local model writes a 5-20 word search
            query for the agent's CURRENT step (empty = nothing to look up).
            Cached in Mongo lane_replay_rewrites. memory_query text is already
            a question and is used as-is.
  pool      candidates = MiniLM top-10 + chunk3 top-10 from the project
            collection, + top-3 of each from shared patterns/context, with the
            prod filters (status=active, recall exclusions, expiry).
Variants (each stores its top-10 ranking with scores; `volume` picks a
per-variant threshold that matches MiniLM's served volume, then lists the
served pairs not yet graded):
  minilm      prod MiniLM on the raw text (baseline)
  rw          MiniLM + chunk3 on the rewritten query, RRF-fused
  rr          the pool for the raw text, reranked by the cross-encoder on the raw text
  rw_rr       the pool for the rewritten query, reranked on the rewritten query
Rows go to Mongo rerank_replay_runs.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "scripts"), str(REPO / "src"), str(REPO / "contrib" / "local-inference")]
logging.disable(logging.INFO)

from facets_backfill import _chroma, _mongo  # noqa: E402

OLLAMA = "http://192.168.15.240:11434"
RERANK = "http://127.0.0.1:8091/rerank"
REWRITE_MODEL = "gpt-oss:20b"
RUNS = "rerank_replay_runs"
REWRITES = "lane_replay_rewrites"
REWRITE_INSTRUCTIONS = (
    "You turn an AI software agent's recent working context into a search query for its team knowledge base. "
    "Read the CONTEXT (the tail of the agent's conversation: user requests, tool output, its own notes). "
    "Write ONE short search query (5-20 words) for notes that would help with the agent's CURRENT step: name the "
    "specific system, component, error or decision involved. If the current step needs no lookup (chit-chat, "
    "acknowledgement, or nothing specific), return an empty query. Return JSON {\"query\": \"...\"}.")
REWRITE_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
POOL_PROJ = 10
POOL_SHARED = 3
KEEP = 10
MINILM_FLOORS = {"recall": 0.6, "query": 0.3}


def _qhash(q):
    return hashlib.sha1(q.encode("utf-8", "replace")).hexdigest()[:16]


def _rewrite(db, ctx):
    qh = _qhash(ctx)
    hit = db[REWRITES].find_one({"qhash": qh, "model": REWRITE_MODEL})
    if hit:
        return hit["rewrite"]
    from li import call
    try:
        r = call(OLLAMA, REWRITE_MODEL, REWRITE_INSTRUCTIONS, "CONTEXT:\n" + ctx[-2500:], REWRITE_SCHEMA, 180)
        rw = (r.get("query") or "").strip()
    except Exception as e:  # noqa: BLE001
        rw = None
        print("rewrite failed:", e, flush=True)
    if rw is not None:
        db[REWRITES].update_one({"qhash": qh, "model": REWRITE_MODEL},
                                {"$set": {"rewrite": rw, "ts": time.time()}}, upsert=True)
    return rw


def _rerank(query, docs):
    if not docs:
        return []
    body = json.dumps({"query": query, "documents": docs}).encode()
    req = urllib.request.Request(RERANK, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["scores"]


async def _cands(q, text, cols, ci, lanes=("minilm", "chunk")):
    """{doc_id: {id, title, doc, source, minilm, chunk, mrank, crank}} over project + shared."""
    from shared_memory.__main__ import _recall_excluded, _recall_where
    from shared_memory.helpers import embed_query_once, is_expired, query_kwargs
    from shared_memory.tools.query import calculate_relevance
    recall = q["kind"] == "recall"
    where = _recall_where(None) if recall else {"status": "active"}
    proj, shared = cols
    mvec = await embed_query_once(proj, text[:8000])
    cvec = await ci.embed_query(text[:8000]) if "chunk" in lanes else None
    out = {}
    for col, n in [(proj, POOL_PROJ)] + [(s, POOL_SHARED) for s in shared]:
        for lane in lanes:
            if lane == "minilm":
                got = await col.query(**query_kwargs(text[:8000], mvec), n_results=n, where=where)
            else:
                got = await ci.scan(col, cvec, n, where)
                if got is None:
                    raise RuntimeError(f"chunk lane unusable for {col.name}")
            rank = 0
            for i, doc, meta, dist in zip(got["ids"][0], got["documents"][0], got["metadatas"][0], got["distances"][0]):
                meta = meta or {}
                if is_expired(meta) or (recall and _recall_excluded(i, meta, q["agent"])):
                    continue
                rank += 1
                c = out.setdefault(i, {"id": i, "title": meta.get("title", ""), "doc": doc or "",
                                       "source": col.name, "shared": col.name.startswith("shared")})
                c[lane] = round(calculate_relevance(dist), 4)
                c[lane[0] + "rank"] = rank
    return out


def _top(cands, key, n=KEEP):
    return [{"id": c["id"], "score": c[key], "source": c["source"], "title": c["title"]}
            for c in sorted(cands, key=lambda c: -c[key])[:n]]


async def run(a):
    from lane_replay import _queries

    from shared_memory import chunk_index as ci
    from shared_memory.helpers import get_project_collection, get_shared_collection
    assert ci.CHUNKER == 3, "run with JUNTO_CHUNK_DB=junto_chunks_v3 JUNTO_CHUNKER=3"
    ci.EMBED_URL = OLLAMA
    ci.EMBED_TIMEOUT_S = 30.0
    db, chroma = _mongo(), await _chroma()
    await ci.get_chunk_client("localhost", 8001)
    cols = (await get_project_collection(chroma, "nimbus"),
            [await get_shared_collection(chroma, n) for n in ("patterns", "context")])
    qs = [q for q in _queries(db) if q["project"] == "nimbus"]
    print(f"{len(qs)} nimbus queries", flush=True)
    rows, t_rr = [], []
    for n, q in enumerate(qs):
        raw = q["query"]
        rw = _rewrite(db, raw) if q["kind"] == "recall" else raw
        row = {"kind": q["kind"], "agent": q["agent"], "qhash": _qhash(raw), "query": raw, "rewrite": rw, "v": {}}
        base = await _cands(q, raw, cols, ci, lanes=("minilm",))
        fl = MINILM_FLOORS[q["kind"]]
        row["v"]["minilm"] = [d for d in _top(list(base.values()), "minilm")
                              if d["score"] >= fl + (0.1 if q["kind"] == "query" and d["source"].startswith("shared") else 0)]
        pool_raw = await _cands(q, raw, cols, ci)
        t0 = time.time()
        docs = list(pool_raw.values())
        for c, s in zip(docs, _rerank(raw, [f"{c['title']}\n{c['doc']}" for c in docs])):
            c["rr"] = round(s, 4)
        t_rr.append(time.time() - t0)
        row["v"]["rr"] = _top(docs, "rr")
        if rw:
            pool_rw = list((await _cands(q, rw, cols, ci)).values())
            for c in pool_rw:
                c["rrf"] = sum(1.0 / (60 + c[k]) for k in ("mrank", "crank") if k in c)
            row["v"]["rw"] = _top(pool_rw, "rrf")
            for c, s in zip(pool_rw, _rerank(rw, [f"{c['title']}\n{c['doc']}" for c in pool_rw])):
                c["rr"] = round(s, 4)
            row["v"]["rw_rr"] = _top(pool_rw, "rr")
        else:
            row["v"]["rw"] = row["v"]["rw_rr"] = []
        row["pool"] = len(pool_raw)
        rows.append(row)
        if n % 20 == 0:
            print(f"  {n}/{len(qs)}", flush=True)
    run_id = f"rr-{int(time.time())}"
    db[RUNS].insert_one({"_id": run_id, "ts": time.time(), "rewrite_model": REWRITE_MODEL, "rows": rows,
                         "rerank_ms_median": round(sorted(t_rr)[len(t_rr) // 2] * 1000)})
    print("run", run_id, "rerank median ms", round(sorted(t_rr)[len(t_rr) // 2] * 1000), flush=True)


def volume(a):
    """Per-variant threshold that matches MiniLM's served volume; print served sets' size."""
    db = _mongo()
    doc = db[RUNS].find_one({"_id": a.run}) if a.run else next(db[RUNS].find().sort("ts", -1).limit(1))
    rows = doc["rows"]
    out = {}
    for kind, cap in (("recall", 5), ("query", 10)):
        R = [r for r in rows if r["kind"] == kind]
        target = sum(len(r["v"]["minilm"]) for r in R) / len(R)
        out[kind] = {"target": round(target, 2), "thresholds": {}}
        for var in ("rw", "rr", "rw_rr"):
            scores = sorted((d["score"] for r in R for d in r["v"][var][:cap]), reverse=True)
            k = int(round(target * len(R)))
            thr = scores[k - 1] if 0 < k <= len(scores) else 0.0
            out[kind]["thresholds"][var] = thr
        print(kind, json.dumps(out[kind]))
    return doc, out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    v = sub.add_parser("volume")
    v.add_argument("--run", default=None)
    a = ap.parse_args()
    if a.cmd == "run":
        asyncio.run(run(a))
    else:
        volume(a)


if __name__ == "__main__":
    main()
