#!/usr/bin/env python3
"""Retrieval eval harness (retrieval-upgrade plan step 0).

    .venv/bin/python scripts/retrieval_eval.py build [--per-slice 40] [--set v0]
    .venv/bin/python scripts/retrieval_eval.py run   [--set v0] [--retriever vector|vector+lexical|chunk|chunk+lexical] [--k 10]

`chunk*` retrievers query the LIVE chunk index (interface:chunk-index-v0)
through chunk_index.scan — the same code path memory_query and /recall use.

The eval set lives in Mongo (`retrieval_eval_queries`), NOT in this public repo:
the queries quote private corpus content. `run` results go to
`retrieval_eval_runs` so embedder candidates can be compared run against run.

Slices (each query has one or more positive doc ids):
  head   question answerable from the first ~150 tokens: inside MiniLM's
         256-token window, the case the current embedder handles.
  deep   question answerable only from text past token ~400: outside the
         window. 78.5% of the corpus is longer than 256 tokens
         (learning_d0c283d133f76b66); this slice measures what that costs.
  ident  a question naming an identifier (function, path, id) that appears in
         at most 3 docs; positives = every doc containing it.
  stale  question written from a CURRENT doc whose predecessor is marked
         superseded (superseded_by -> current). Run WITHOUT the active-status
         filter: measures how often the stale predecessor outranks the current
         doc, i.e. the risk for contradicted-but-UNMARKED docs.

Questions come from a local model on sage's Ollama (qwen3:14b, think off), so
they are synthetic. Spot-check a sample before trusting a delta, and add real
queries from recall_events.query once that field has history.

Read-only against the prod stores apart from the two eval collections.
"""

import argparse
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "contrib" / "local-inference"))

from facets_backfill import _chroma, _mongo  # noqa: E402

QUERIES = "retrieval_eval_queries"
RUNS = "retrieval_eval_runs"
COLLECTIONS = ["proj_junto", "proj_nimbus", "proj_cerebro", "shared_patterns"]
DOC_TYPES = {"learning", "spec", "function_ref", "architecture"}
OLLAMA = "http://192.168.15.240:11434"
GEN_MODEL = "qwen3:14b"

QSCHEMA = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}
GEN_INSTRUCTIONS = (
    "You write ONE search question that a software agent would type into a team knowledge base "
    "when it needs the information in the PASSAGE. Rules: the PASSAGE must answer it; ask about the "
    "specific fact, cause or procedure, not the topic in general; 8-25 words; do not copy a whole "
    "sentence; never say \"the passage\" or \"the text\" — the asker has not seen it; {ident_rule} Return JSON {{\"question\": ...}}."
)
NO_IDENT = "do NOT include code identifiers, file names or ids — describe them in plain words."
WITH_IDENT = "the question MUST contain the exact token `{tok}`."


def _tok():
    import os

    from tokenizers import Tokenizer
    t = Tokenizer.from_file(os.path.expanduser("~/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx/tokenizer.json"))
    t.no_truncation()
    return t


def _ask(passage, ident_rule):
    from li import call
    out = call(OLLAMA, GEN_MODEL, GEN_INSTRUCTIONS.format(ident_rule=ident_rule), passage, QSCHEMA, 180)
    q = (out or {}).get("question", "").strip()
    return q if 6 <= len(q.split()) <= 40 else None


def _raw(d, start_tok, end_tok):
    """Original-case text between two token positions (decode() lowercases)."""
    offs = d["offs"]
    if not offs or start_tok >= len(offs):
        return ""
    a = offs[start_tok][0]
    b = offs[min(end_tok, len(offs)) - 1][1] if end_tok else len(d["doc"])
    return d["doc"][a:b]


async def _load_docs(chroma):
    docs = {}
    for name in COLLECTIONS:
        col = await chroma.get_collection(name)
        n, off = await col.count(), 0
        while off < n:
            r = await col.get(limit=1000, offset=off, include=["documents", "metadatas"])
            for i, d, m in zip(r["ids"], r["documents"], r["metadatas"]):
                docs[i] = {"id": i, "col": name, "doc": d or "", "meta": m or {}}
            off += 1000
    return docs


async def build(args):
    global GEN_MODEL
    GEN_MODEL = args.gen_model
    random.seed(args.seed)
    db = _mongo()
    if db[QUERIES].count_documents({"set": args.set}) and not args.replace:
        sys.exit(f"set {args.set!r} exists; pass --replace to rebuild")
    db[QUERIES].delete_many({"set": args.set})
    tok = _tok()
    chroma = await _chroma()
    docs = await _load_docs(chroma)
    active = [d for d in docs.values()
              if d["meta"].get("status") == "active" and d["meta"].get("type") in DOC_TYPES]
    for d in active:
        enc = tok.encode(d["doc"])
        d["ids_tok"], d["offs"] = enc.ids, enc.offsets
    print(f"{len(docs)} docs loaded, {len(active)} active candidates", flush=True)
    rows = []

    def add(slice_, q, positives, src, stale=None):
        rows.append({"set": args.set, "slice": slice_, "query": q, "positives": positives,
                     "source": src["id"], "collection": src["col"], "stale": stale,
                     "model": GEN_MODEL, "created": time.time()})

    # head + deep
    for slice_, pool in (
        ("head", [d for d in active if len(d["ids_tok"]) >= 80]),
        ("deep", [d for d in active if len(d["ids_tok"]) >= 700]),
    ):
        random.shuffle(pool)
        got = 0
        for d in pool:
            if got >= args.per_slice:
                break
            passage = (_raw(d, 0, 150) if slice_ == "head" else _raw(d, 400, 620))
            q = _ask(passage, NO_IDENT)
            if q:
                add(slice_, q, [d["id"]], d)
                got += 1
        print(f"{slice_}: {got}", flush=True)

    # ident: tokens present in <=3 docs of the same collection, taken from past token 256
    ident_re = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]{3,}\b|\b[a-z]+[A-Z][A-Za-z0-9]{3,}\b")
    pool = [d for d in active if len(d["ids_tok"]) >= 400]
    random.shuffle(pool)
    got = 0
    by_col = {}
    for d in docs.values():
        by_col.setdefault(d["col"], []).append(d)
    for d in pool:
        if got >= args.per_slice:
            break
        tail = _raw(d, 256, None)
        cands = sorted(set(ident_re.findall(tail)), key=len, reverse=True)[:5]
        for t in cands:
            holders = [x["id"] for x in by_col[d["col"]] if t in x["doc"]]
            if not 1 <= len(holders) <= 3:
                continue
            idx = tail.find(t)
            passage = tail[max(0, idx - 600): idx + 600]
            q = _ask(passage, WITH_IDENT.format(tok=t))
            if q and t in q:
                add("ident", q, holders, d)
                got += 1
            break
    print(f"ident: {got}", flush=True)

    # stale: current doc's distinguishing passage; stale = its superseded predecessor
    pairs = [(d, docs.get(d["meta"].get("superseded_by")))
             for d in docs.values() if d["meta"].get("status") == "superseded"]
    pairs = [(old, new) for old, new in pairs if new and new["meta"].get("status") == "active"]
    random.shuffle(pairs)
    got = 0
    for old, new in pairs:
        if got >= args.per_slice:
            break
        passage = new["doc"][:tok.encode(new["doc"]).offsets[min(300, len(tok.encode(new["doc"]).offsets)) - 1][1]]
        q = _ask(passage, NO_IDENT)
        if q:
            add("stale", q, [new["id"]], new, stale=old["id"])
            got += 1
    print(f"stale: {got} (from {len(pairs)} pairs)", flush=True)

    if rows:
        db[QUERIES].insert_many(rows)
    print(f"built set {args.set!r}: {len(rows)} queries -> mongo {QUERIES}")


async def _rank(col, qtext, qvec, k, where, lexical, cvec=None):
    """Ranked doc ids + {id: score}. cvec set → the PROD chunk-lane code path
    (chunk_index.scan against the live junto_chunks index)."""
    from shared_memory.helpers import query_kwargs
    from shared_memory.tools.query import calculate_relevance
    r = None
    if cvec is not None:
        from shared_memory import chunk_index
        r = await chunk_index.scan(col, cvec, k, where)
        if r is None:
            sys.exit(f"chunk index not usable for {col.name} (missing/stale) — build it first")
    if r is None:
        r = await col.query(**query_kwargs(qtext, qvec), n_results=k, where=where)
    ranked = list(r["ids"][0]) if r["ids"] else []
    scores = {i: round(calculate_relevance(d), 4) for i, d in zip(ranked, r["distances"][0])} if ranked else {}
    if lexical:
        from shared_memory.lexical import lexical_scan
        hits = await lexical_scan(col, qtext, qvec, where, 2, exclude_ids=set(ranked))
        if hits:
            ranked = ranked[:max(0, k - len(hits))] + [h["id"] for h in hits]
    return ranked, scores


async def run(args):
    from shared_memory import chunk_index
    from shared_memory.helpers import embed_query_once
    chunk_index.EMBED_URL = OLLAMA
    chunk_index.EMBED_TIMEOUT_S = 30.0
    chunked = args.retriever.startswith("chunk")
    if chunked:
        await chunk_index.get_chunk_client("localhost", 8001)
    db = _mongo()
    qs = list(db[QUERIES].find({"set": args.set}))
    if not qs:
        sys.exit(f"no queries for set {args.set!r}; run build first")
    chroma = await _chroma()
    cols = {}
    per = {}
    detail = []
    for q in qs:
        col = cols.get(q["collection"]) or await chroma.get_collection(q["collection"])
        cols[q["collection"]] = col
        qvec = await embed_query_once(col, q["query"])
        where = None if q["slice"] == "stale" else {"status": "active"}
        cvec = await chunk_index.embed_query(q["query"]) if chunked else None
        if chunked and cvec is None:
            sys.exit("chunk embed failed")
        ranked, scores = await _rank(col, q["query"], qvec, args.k, where,
                                     args.retriever.endswith("+lexical"), cvec)
        pos = set(q["positives"])
        first = next((i + 1 for i, d in enumerate(ranked) if d in pos), None)
        s = per.setdefault(q["slice"], {"n": 0, "r1": 0, "r5": 0, "r10": 0, "mrr": 0.0, "stale_above": 0})
        s["n"] += 1
        s["r1"] += bool(first and first <= 1)
        s["r5"] += bool(first and first <= 5)
        s["r10"] += bool(first and first <= 10)
        s["mrr"] += 1.0 / first if first else 0.0
        if q.get("stale"):
            sr = ranked.index(q["stale"]) + 1 if q["stale"] in ranked else None
            s["stale_above"] += bool(sr and (first is None or sr < first))
        detail.append({"qid": str(q["_id"]), "slice": q["slice"], "rank": first,
                       "pos_score": scores.get(ranked[first - 1]) if first else None,
                       "top_score": scores.get(ranked[0]) if ranked else None})
    report = {}
    for name, s in per.items():
        n = s["n"]
        report[name] = {"n": n, "recall@1": round(s["r1"] / n, 3), "recall@5": round(s["r5"] / n, 3),
                        "recall@10": round(s["r10"] / n, 3), "mrr": round(s["mrr"] / n, 3)}
        if name == "stale":
            report[name]["stale_outranks_current"] = round(s["stale_above"] / n, 3)
    embedder = (f"{chunk_index.MODEL} chunked (prod index, chunker {chunk_index.CHUNKER})" if chunked
                else "minilm-l6-v2 (chroma default)")
    db[RUNS].insert_one({"set": args.set, "retriever": args.retriever, "embedder": embedder,
                         "k": args.k, "ts": time.time(), "report": report, "detail": detail})
    print(json.dumps({"set": args.set, "retriever": args.retriever, "report": report}, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--set", default="v0")
    b.add_argument("--per-slice", type=int, default=40)
    b.add_argument("--seed", type=int, default=1024)
    b.add_argument("--replace", action="store_true")
    b.add_argument("--gen-model", default=GEN_MODEL, help="question writer (use a non-Qwen model to check family bias)")
    r = sub.add_parser("run")
    r.add_argument("--set", default="v0")
    r.add_argument("--retriever", default="vector", choices=["vector", "vector+lexical", "chunk", "chunk+lexical"])
    r.add_argument("--k", type=int, default=10)
    a = ap.parse_args()
    asyncio.run(build(a) if a.cmd == "build" else run(a))


if __name__ == "__main__":
    main()
