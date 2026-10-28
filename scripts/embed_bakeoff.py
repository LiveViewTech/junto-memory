#!/usr/bin/env python3
"""Embedder bake-off (retrieval-upgrade plan step 2). Offline: never writes prod.

    .venv/bin/python scripts/embed_bakeoff.py --candidates minilm,minilm-chunk,nomic,qwen3-0.6b,gemma-300m

Re-embeds the docs of the eval-set collections (retrieval_eval.COLLECTIONS) into
a SCRATCH in-process Chroma (--scratch dir, cosine space) per candidate, then
scores eval set --set with the same metrics as retrieval_eval.py and stores one
row per candidate in Mongo retrieval_eval_runs (retriever="bakeoff:<name>").

Candidates:
  minilm        Chroma default (ONNX all-MiniLM-L6-v2, 256-token cap). Sanity
                check: should land near the prod baseline (learning_af8b4045a3bae9e2).
  <any>-chunk   that model on docs split into 256-token windows (stride 224);
                a doc's score = its best chunk. minilm-chunk = the cheap truncation fix.
  nomic         nomic-embed-text via Ollama (768d, "search_document:/search_query:" prefixes).
  qwen3-0.6b    qwen3-embedding:0.6b via Ollama (1024d, instruction prefix on queries only).
  gemma-300m    embeddinggemma:300m via Ollama (768d, 2K context, task prefixes).

Ollama models run with num_ctx 8192 (gemma: 2048); longer docs are cut there.
"""

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from retrieval_eval import COLLECTIONS, QUERIES, RUNS, _chroma, _load_docs, _mongo, _tok  # noqa: E402

OLLAMA = "http://192.168.15.240:11434"
QWEN_INSTRUCT = ("Instruct: Given a question from a software agent, retrieve the team knowledge-base "
                 "note that answers it\nQuery: ")

CANDIDATES = {
    "nomic": {"model": "nomic-embed-text", "ctx": 8192, "doc": "search_document: ", "query": "search_query: "},
    "qwen3-0.6b": {"model": "qwen3-embedding:0.6b", "ctx": 8192, "doc": "", "query": QWEN_INSTRUCT},
    "gemma-300m": {"model": "embeddinggemma:300m", "ctx": 2048,
                   "doc": "title: none | text: ", "query": "task: search result | query: "},
    "qwen3-4b": {"model": "qwen3-embedding:4b", "ctx": 8192, "doc": "", "query": QWEN_INSTRUCT},
    "qwen3-8b": {"model": "qwen3-embedding:8b", "ctx": 8192, "doc": "", "query": QWEN_INSTRUCT},
    "bge-m3": {"model": "bge-m3", "ctx": 8192, "doc": "", "query": ""},
    "arctic2": {"model": "snowflake-arctic-embed2", "ctx": 8192, "doc": "", "query": "query: "},
    "mxbai": {"model": "mxbai-embed-large", "ctx": 512, "doc": "",
              "query": "Represent this sentence for searching relevant passages: "},
    "gemma2-740m": {"model": "embeddinggemma-2:740m", "ctx": 8192,
                    "doc": "title: none | text: ", "query": "task: search result | query: "},
}


def _ollama_embed(model, texts, ctx):
    body = {"model": model, "input": texts, "truncate": True, "options": {"num_ctx": ctx}}
    req = urllib.request.Request(OLLAMA + "/api/embed", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read())["embeddings"]


def _ollama_version():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


class _OllamaEF:
    def __init__(self, spec):
        self.spec = spec

    def docs(self, texts):
        out = []
        for i in range(0, len(texts), 32):
            out += _ollama_embed(self.spec["model"], [self.spec["doc"] + t for t in texts[i:i + 32]],
                                 self.spec["ctx"])
        return out

    def query(self, text):
        return _ollama_embed(self.spec["model"], [self.spec["query"] + text], self.spec["ctx"])[0]


class _MiniLMEF:
    def __init__(self):
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        self.ef = DefaultEmbeddingFunction()

    def docs(self, texts):
        out = []
        for i in range(0, len(texts), 64):
            out += [[float(x) for x in v] for v in self.ef(texts[i:i + 64])]
        return out

    def query(self, text):
        return [float(x) for x in self.ef([text])[0]]


def _chunks(tok, doc, size=256, stride=224):
    enc = tok.encode(doc, add_special_tokens=False)
    offs = enc.offsets
    if len(offs) <= size:
        return [doc]
    out = []
    for start in range(0, len(offs), stride):
        end = min(start + size, len(offs))
        out.append(doc[offs[start][0]:offs[end - 1][1]])
        if end == len(offs):
            break
    return out


def _index(client, name, docs, ef, chunk_tok=None):
    """Build one scratch collection per source collection for candidate `name`."""
    cols = {}
    t0 = time.time()
    for src in COLLECTIONS:
        col = client.get_or_create_collection(f"{name}__{src}", metadata={"hnsw:space": "cosine"},
                                              embedding_function=None)
        if col.count() == 0:
            mine = [d for d in docs.values() if d["col"] == src and d["doc"]]
            ids, texts, metas = [], [], []
            for d in mine:
                parts = _chunks(chunk_tok, d["doc"]) if chunk_tok else [d["doc"]]
                for j, p in enumerate(parts):
                    ids.append(f"{d['id']}#{j}" if chunk_tok else d["id"])
                    texts.append(p)
                    metas.append({"doc_id": d["id"], "status": d["meta"].get("status") or "none"})
            for i in range(0, len(ids), 512):
                col.add(ids=ids[i:i + 512], embeddings=ef.docs(texts[i:i + 512]), metadatas=metas[i:i + 512])
        cols[src] = col
    print(f"  {name}: indexed in {time.time() - t0:.0f}s", flush=True)
    return cols


def _score(qs, rank_fn, k):
    per, detail = {}, []
    for q in qs:
        ranked = rank_fn(q)
        pos = set(q["positives"])
        first = next((i + 1 for i, d in enumerate(ranked) if d in pos), None)
        s = per.setdefault(q["slice"], {"n": 0, "r1": 0, "r5": 0, "r10": 0, "mrr": 0.0, "stale_above": 0})
        s["n"] += 1
        s["r1"] += bool(first and first <= 1)
        s["r5"] += bool(first and first <= 5)
        s["r10"] += bool(first and first <= k)
        s["mrr"] += 1.0 / first if first else 0.0
        if q.get("stale"):
            sr = ranked.index(q["stale"]) + 1 if q["stale"] in ranked else None
            s["stale_above"] += bool(sr and (first is None or sr < first))
        detail.append({"qid": str(q["_id"]), "slice": q["slice"], "rank": first})
    report = {}
    for name, s in per.items():
        n = s["n"]
        report[name] = {"n": n, "recall@1": round(s["r1"] / n, 3), "recall@5": round(s["r5"] / n, 3),
                        "recall@10": round(s["r10"] / n, 3), "mrr": round(s["mrr"] / n, 3)}
        if name == "stale":
            report[name]["stale_outranks_current"] = round(s["stale_above"] / n, 3)
    return report, detail


def _ranker(cols, ef, k, chunked):
    def rank(q):
        col = cols[q["collection"]]
        where = None if q["slice"] == "stale" else {"status": "active"}
        n = k * 6 if chunked else k
        r = col.query(query_embeddings=[ef.query(q["query"])], n_results=n, where=where, include=["metadatas"])
        out = []
        for m in r["metadatas"][0]:
            if m["doc_id"] not in out:
                out.append(m["doc_id"])
        return out[:k]
    return rank


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="minilm,minilm-chunk,nomic,qwen3-0.6b,gemma-300m")
    ap.add_argument("--set", default="v0")
    ap.add_argument("--prefix", default="", help="scratch collection name prefix (re-index after an Ollama upgrade)")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--scratch", required=True, help="dir for the scratch Chroma (never prod)")
    a = ap.parse_args()
    import chromadb
    db = _mongo()
    qs = list(db[QUERIES].find({"set": a.set}))
    docs = await _load_docs(await _chroma())
    print(f"{len(docs)} docs, {len(qs)} queries", flush=True)
    client = chromadb.PersistentClient(path=a.scratch)
    tok = _tok()
    for name in a.candidates.split(","):
        chunked = name.endswith("-chunk")
        base = name[:-len("-chunk")] if chunked else name
        ef = _MiniLMEF() if base == "minilm" else _OllamaEF(CANDIDATES[base])
        cols = _index(client, a.prefix + name, docs, ef, chunk_tok=tok if chunked else None)
        t0 = time.time()
        report, detail = _score(qs, _ranker(cols, ef, a.k, chunked), a.k)
        db[RUNS].insert_one({"set": a.set, "retriever": f"bakeoff:{name}", "embedder": name, "ollama": _ollama_version(), "k": a.k,
                             "ts": time.time(), "report": report, "detail": detail,
                             "query_s": round(time.time() - t0, 1)})
        print(json.dumps({"candidate": name, "report": report}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
