"""Chunked arctic2 side index for memory_query and /recall (interface:chunk-index-v0).

Why: MiniLM sees only a doc's first 256 tokens, so a question about something
deep in a body misses (deep recall@5 0.30, learning_af8b4045a3bae9e2). Chunking
every doc into 256-token windows and embedding them with snowflake-arctic-embed2
measured ~0.80 on the same slice (learning_84ee8de42b344ff6).

Shape:
  - Chroma DATABASE `junto_chunks` (isolated from every list_collections() caller
    in the default database), one collection `c__<source>` per source collection,
    cosine space, vectors supplied (embedding_function=None).
  - Collection metadata is the STAMP (model, ollama_version, chunk_size, stride,
    complete). A collection is only read when its stamp matches the live embedder
    — Ollama vectors change across versions (learning_03d81c3a69bf6667).
  - A background sweep diffs sources against their chunk collections by
    (doc_id, body hash, status, type). No write-path edits.

Reads are gated per project by query_config `chunk_index` (default False) and
FAIL OPEN: any problem returns None and the caller scans the MiniLM way.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Overridable so an experimental chunker can be built side by side without
# touching the live index (the server and the host script must agree).
DATABASE = os.environ.get("JUNTO_CHUNK_DB", "junto_chunks")
PREFIX = "c__"
NEXT_SUFFIX = "__next"
MODEL = "snowflake-arctic-embed2"
QUERY_PREFIX = "query: "
NUM_CTX = 8192
CHUNK_SIZE = 256
STRIDE = 224
# Bump on ANY change to chunk_text output: it is part of the stamp, so a bump
# makes every chunk collection stale and the sweep rebuilds it.
#   2 = 256-token body windows
#   3 = same windows, each prefixed with the doc title ("contextual" chunks)
CHUNKER = int(os.environ.get("JUNTO_CHUNKER", "2"))
EMBED_BATCH = 32
KEEP_ALIVE = "24h"
# n_results multiplier: several chunks of one doc can crowd the top, so ask for
# more and collapse to doc ids (the bake-off used k*6).
OVERFETCH = 6
# Extra depth for the unfiltered first query (scan's PERF note).
UNFILTERED_FACTOR = 4
VERSION_TTL_S = 300

SWEEP_SECONDS = int(os.environ.get("JUNTO_CHUNK_SWEEP_SECONDS", "900"))
EMBED_URL = os.environ.get("JUNTO_EMBED_URL", "http://host.docker.internal:11434").rstrip("/")
EMBED_TIMEOUT_S = float(os.environ.get("JUNTO_CHUNK_EMBED_TIMEOUT", "1.5"))

_client = None
_client_lock: Optional[asyncio.Lock] = None
_tokenizer = None
_version_cache = {"v": None, "at": 0.0}
_sweep_task: Optional[asyncio.Task] = None
last_sweep: Dict[str, Any] = {}


def is_chunk_source(name: str) -> bool:
    """The collections memory_query and /recall scan (shared_work excluded)."""
    from shared_memory.config import PROJECT_PREFIX
    return name.startswith(PROJECT_PREFIX) or name in ("shared_patterns", "shared_context")


# ── chunking ───────────────────────────────────────────────────────────────

def _get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        from tokenizers import Tokenizer
        # Chroma downloads the MiniLM bundle lazily on the first embed, so in
        # a fresh container the tokenizer file may not exist yet.
        ONNXMiniLM_L6_V2()._download_model_if_not_exists()
        path = os.path.join(ONNXMiniLM_L6_V2.DOWNLOAD_PATH, ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME,
                            "tokenizer.json")
        tok = Tokenizer.from_file(str(path))
        tok.no_truncation()
        _tokenizer = tok
    return _tokenizer


def chunk_text(text: str, size: int = CHUNK_SIZE, stride: int = STRIDE) -> List[str]:
    """256-MiniLM-token windows, stride 224 — byte-identical to the measured
    bake-off (scripts/embed_bakeoff.py _chunks). Short docs are one chunk."""
    if not text:
        return []
    # add_special_tokens=False: [CLS]/[SEP] carry offset (0,0), so a window
    # ending on [SEP] sliced to '' and the doc's tail was never indexed.
    offs = _get_tokenizer().encode(text, add_special_tokens=False).offsets
    if len(offs) <= size:
        return [text]
    out = []
    for start in range(0, len(offs), stride):
        end = min(start + size, len(offs))
        out.append(text[offs[start][0]:offs[end - 1][1]])
        if end == len(offs):
            break
    return out


def doc_texts(body: str, meta: Optional[dict]) -> List[str]:
    """The texts to embed for one doc under the active CHUNKER."""
    parts = chunk_text(body)
    title = ((meta or {}).get("title") or "").strip()
    if CHUNKER >= 3 and title:
        parts = [f"{title}\n\n{p}" for p in parts]
    return parts


def doc_hash(body: str, meta: Optional[dict]) -> str:
    """Change detector for the sweep: the body, plus the title when chunks embed it."""
    title = ((meta or {}).get("title") or "") if CHUNKER >= 3 else ""
    return body_hash(title + "\0" + (body or "") if title else body)


def body_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()


# ── embedder ───────────────────────────────────────────────────────────────

async def _post(path: str, body: dict, timeout: float) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0))) as c:
        r = await c.post(EMBED_URL + path, json=body)
        r.raise_for_status()
        return r.json()


async def embed(texts: List[str], timeout: float = 600.0) -> List[List[float]]:
    body = {"model": MODEL, "input": texts, "truncate": True,
            "options": {"num_ctx": NUM_CTX}, "keep_alive": KEEP_ALIVE}
    return (await _post("/api/embed", body, timeout))["embeddings"]


async def embed_query(text: str) -> Optional[List[float]]:
    """Query vector under the per-request latency budget, or None (caller falls back)."""
    try:
        return (await asyncio.wait_for(embed([QUERY_PREFIX + text], EMBED_TIMEOUT_S),
                                       EMBED_TIMEOUT_S))[0]
    except Exception as e:
        log.warning("chunk-index: query embed failed (%s) — MiniLM fallback", e)
        return None


async def ollama_version(force: bool = False) -> Optional[str]:
    now = time.monotonic()
    if not force and _version_cache["v"] and now - _version_cache["at"] < VERSION_TTL_S:
        return _version_cache["v"]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(EMBED_URL + "/api/version")
            v = r.json().get("version")
    except Exception:
        v = None
    _version_cache.update(v=v, at=now)
    return v


def stamp(version: str) -> Dict[str, Any]:
    return {"model": MODEL, "ollama_version": version, "chunk_size": CHUNK_SIZE, "stride": STRIDE,
            "chunker": CHUNKER}


def is_current(meta: Optional[dict], version: Optional[str]) -> bool:
    if not meta or not version or not meta.get("complete"):
        return False
    return all(meta.get(k) == v for k, v in stamp(version).items())


# ── chroma client (database junto_chunks) ─────────────────────────────────

def _ensure_database(host: str, port: int) -> None:
    import chromadb
    from chromadb.config import Settings
    admin = chromadb.AdminClient(Settings(chroma_api_impl="chromadb.api.fastapi.FastAPI",
                                          chroma_server_host=host, chroma_server_http_port=port,
                                          anonymized_telemetry=False))
    try:
        admin.get_database(DATABASE)
    except Exception:
        admin.create_database(DATABASE)


async def get_chunk_client(host: str = None, port: int = None):
    global _client, _client_lock
    if _client is not None:
        return _client
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    async with _client_lock:
        if _client is None:
            import chromadb
            from chromadb.config import Settings

            from shared_memory.config import CHROMA_HOST, CHROMA_PORT
            host, port = host or CHROMA_HOST, int(port or CHROMA_PORT)
            await asyncio.to_thread(_ensure_database, host, port)
            _client = await chromadb.AsyncHttpClient(
                host=host, port=port, database=DATABASE,
                settings=Settings(anonymized_telemetry=False))
    return _client


async def _get_current(cclient, source: str):
    """The chunk collection for `source` if it exists and is current, else None."""
    try:
        col = await cclient.get_collection(PREFIX + source, embedding_function=None)
    except Exception:
        return None
    return col if is_current(col.metadata, await ollama_version()) else None


# ── query path ─────────────────────────────────────────────────────────────

class _Unsupported(Exception):
    pass


def _matches(meta: dict, where: Optional[dict]) -> bool:
    """Evaluate the Chroma `where` shapes memory_query / /recall build
    ({k: v}, {k: {"$in"|"$eq"|"$ne": ...}}, {"$and"|"$or": [...]}) against a
    metadata dict. Raises _Unsupported for anything else."""
    if not where:
        return True
    ok = True
    for k, v in where.items():
        if k == "$and":
            ok = ok and all(_matches(meta, w) for w in v)
        elif k == "$or":
            ok = ok and any(_matches(meta, w) for w in v)
        elif k.startswith("$"):
            raise _Unsupported(k)
        elif isinstance(v, dict):
            if len(v) != 1:
                raise _Unsupported(str(v))
            op, arg = next(iter(v.items()))
            have = meta.get(k)
            if op == "$in":
                ok = ok and have in arg
            elif op == "$eq":
                ok = ok and have == arg
            elif op == "$ne":
                ok = ok and have != arg
            else:
                raise _Unsupported(op)
        else:
            ok = ok and meta.get(k) == v
    return ok


def _best_per_doc(got, where, python_filter: bool) -> Dict[str, float]:
    best: Dict[str, float] = {}
    for m, d in zip(got["metadatas"][0], got["distances"][0]):
        m = m or {}
        did = m.get("doc_id")
        if not did or did in best:
            continue
        if python_filter and not _matches(m, where):
            continue
        best[did] = d
    return best


async def scan(source_col, qvec: List[float], n: int, where: Optional[dict]) -> Optional[dict]:
    """Chunk-lane replacement for `source_col.query(...)`.

    Returns a Chroma-query-shaped dict (ids/documents/metadatas/distances, one
    row) of SOURCE docs ranked by best chunk, or None when the lane is not
    usable (caller scans MiniLM). Documents and metadatas are re-read from the
    source collection, and `where` is re-checked against the SOURCE metadata —
    a chunk's copied status can lag the source by up to one sweep.

    PERF: Chroma's `where` pre-filter costs 20-60 ms per query on the big
    collections vs ~3 ms without it (measured 2026-10-25). So the chunk query
    runs unfiltered with a deeper over-fetch, filters on the copied chunk
    metadata here, and only re-queries WITH `where` when too few docs survive.

    ⚠️ DISTANCES ARE SYNTHETIC: 2*(1-cos), so the existing
    calculate_relevance(d) = 1 - d/2 returns the chunk's cosine similarity.
    That is NOT the MiniLM scale — callers must apply the chunk thresholds
    (query_config chunk_min_relevance / chunk_recall_floor)."""
    try:
        cclient = await get_chunk_client()
        ccol = await _get_current(cclient, source_col.name)
        if ccol is None:
            return None
        include = ["metadatas", "distances"]
        best = None
        try:
            _matches({}, where)
            fetch = max(1, n * OVERFETCH * UNFILTERED_FACTOR)
            got = await ccol.query(query_embeddings=[qvec], n_results=fetch, include=include)
            best = _best_per_doc(got, where, True)
            if len(best) < n and len(got["ids"][0]) >= fetch:
                best = None             # filter too selective here — let Chroma do it
        except _Unsupported:
            best = None
        if best is None:
            got = await ccol.query(query_embeddings=[qvec], n_results=max(1, n * OVERFETCH),
                                   where=where, include=include)
            best = _best_per_doc(got, where, False)
        ids = list(best)[:n]
        if not ids:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        src = await source_col.get(ids=ids, include=["documents", "metadatas"])
        by_id = {i: (doc, meta) for i, doc, meta in zip(src["ids"], src["documents"], src["metadatas"])}
        out_ids, docs, metas, dists = [], [], [], []
        for did in ids:
            if did not in by_id:        # deleted since the last sweep
                continue
            doc, meta = by_id[did]
            meta = meta or {}
            try:
                if not _matches(meta, where):   # status/type changed since the sweep
                    continue
            except _Unsupported:
                pass
            out_ids.append(did)
            docs.append(doc)
            metas.append(meta)
            dists.append(2.0 * best[did])   # cosine distance (1-cos) → 2*(1-cos)
        return {"ids": [out_ids], "documents": [docs], "metadatas": [metas], "distances": [dists]}
    except Exception as e:
        log.warning("chunk-index: scan of %s failed (%s) — MiniLM fallback", getattr(source_col, "name", "?"), e)
        return None


def enabled_for(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("chunk_index"))


# ── build / sweep ──────────────────────────────────────────────────────────

async def _read_all(col, include) -> dict:
    out = {"ids": [], "documents": [], "metadatas": []}
    n, off = await col.count(), 0
    while off < n:
        r = await col.get(limit=1000, offset=off, include=include)
        out["ids"] += r["ids"]
        for k in ("documents", "metadatas"):
            if k in include:
                out[k] += r[k]
        off += 1000
    return out


def _chunk_meta(doc_id: str, n: int, h: str, meta: dict) -> dict:
    return {"doc_id": doc_id, "n": n, "src_hash": h,
            "status": (meta or {}).get("status") or "active",
            "type": (meta or {}).get("type") or ""}


async def _add_docs(ccol, rows: List[tuple]) -> int:
    """rows = [(doc_id, body, meta)]. Chunk, embed, add. Returns chunk count."""
    ids, texts, metas = [], [], []
    for doc_id, body, meta in rows:
        h = doc_hash(body, meta)
        parts = await asyncio.to_thread(doc_texts, body, meta)
        for j, p in enumerate(parts):
            ids.append(f"{doc_id}#{j}")
            texts.append(p)
            metas.append(_chunk_meta(doc_id, j, h, meta))
    for i in range(0, len(ids), 256):
        vecs = []
        for b in range(i, min(i + 256, len(ids)), EMBED_BATCH):
            vecs += await embed(texts[b:min(b + EMBED_BATCH, i + 256, len(ids))])
        await ccol.upsert(ids=ids[i:i + 256], embeddings=vecs, metadatas=metas[i:i + 256])
    return len(ids)


async def sync_source(chroma, source: str, version: str, log_fn=None) -> Dict[str, Any]:
    """Bring c__<source> up to date with `source`. Full rebuild (into __next, then
    swap) when the collection is missing or its stamp is stale; else an
    incremental diff. Returns stats."""
    t0 = time.monotonic()
    cclient = await get_chunk_client()
    src = await chroma.get_collection(source)
    data = await _read_all(src, ["documents", "metadatas"])
    want = {i: (d or "", m or {}) for i, d, m in zip(data["ids"], data["documents"], data["metadatas"])}
    stats = {"source": source, "docs": len(want), "added": 0, "changed": 0, "meta": 0, "deleted": 0,
             "chunks": 0, "rebuilt": False}
    try:
        live = await cclient.get_collection(PREFIX + source, embedding_function=None)
    except Exception:
        live = None

    if live is None or not is_current(live.metadata, version):
        name_next = PREFIX + source + NEXT_SUFFIX
        try:
            await cclient.delete_collection(name_next)
        except Exception:
            pass
        nxt = await cclient.create_collection(
            name_next, embedding_function=None,
            metadata={"hnsw:space": "cosine", **stamp(version), "complete": False, "source": source})
        rows = [(i, d, m) for i, (d, m) in want.items() if d]
        for b in range(0, len(rows), 64):
            stats["chunks"] += await _add_docs(nxt, rows[b:b + 64])
            if log_fn and b % 1024 == 0:
                log_fn(f"  {source}: {b + len(rows[b:b + 64])}/{len(rows)} docs")
        stats["added"] = len(rows)
        if live is not None:
            await cclient.delete_collection(PREFIX + source)
        meta = {k: v for k, v in nxt.metadata.items() if k != "hnsw:space"}
        meta.update(complete=True, built_at=time.time())
        await nxt.modify(name=PREFIX + source, metadata=meta)
        stats["rebuilt"] = True
    else:
        have: Dict[str, dict] = {}
        cdata = await _read_all(live, ["metadatas"])
        for cid, m in zip(cdata["ids"], cdata["metadatas"]):
            m = m or {}
            h = have.setdefault(m.get("doc_id"), {"ids": [], "hash": m.get("src_hash"),
                                                  "status": m.get("status"), "type": m.get("type")})
            h["ids"].append(cid)
        gone = [cid for did, h in have.items() if did not in want for cid in h["ids"]]
        stats["deleted"] = sum(1 for did in have if did not in want)
        redo, meta_only = [], []
        for did, (body, meta) in want.items():
            h = have.get(did)
            if not body:
                if h:
                    gone += h["ids"]
                continue
            if h is None or h["hash"] != doc_hash(body, meta):
                if h:
                    gone += h["ids"]
                    stats["changed"] += 1
                else:
                    stats["added"] += 1
                redo.append((did, body, meta))
            elif (h["status"], h["type"]) != ((meta.get("status") or "active"), (meta.get("type") or "")):
                meta_only.append((did, body, meta, h["ids"]))
        for i in range(0, len(gone), 500):
            await live.delete(ids=gone[i:i + 500])
        for b in range(0, len(redo), 64):
            stats["chunks"] += await _add_docs(live, redo[b:b + 64])
        for did, body, meta, cids in meta_only:
            h = doc_hash(body, meta)
            await live.update(ids=cids, metadatas=[
                _chunk_meta(did, int(c.rsplit("#", 1)[1]), h, meta) for c in cids])
        stats["meta"] = len(meta_only)
    stats["seconds"] = round(time.monotonic() - t0, 1)
    return stats


async def sweep_once(chroma, log_fn=None) -> Dict[str, Any]:
    version = await ollama_version(force=True)
    if not version:
        return {"error": "embedder unreachable", "url": EMBED_URL}
    out = {"version": version, "sources": []}
    for col in await chroma.list_collections():
        if not is_chunk_source(col.name):
            continue
        try:
            out["sources"].append(await sync_source(chroma, col.name, version, log_fn))
        except Exception as e:
            log.error("chunk-index: sync %s failed: %s", col.name, e)
            out["sources"].append({"source": col.name, "error": str(e)})
    return out


def any_scope_enabled(db) -> bool:
    """True when chunk_index is on in code or in any query_config scope.

    The sweep is pure cost until something reads the index: without this gate
    every server polled the embedder (default host.docker.internal:11434) every
    15 min and, if anything answered, embedded every collection, even with
    chunk_index off everywhere (work-box upgrade review, 2026-10-27)."""
    from shared_memory.query_config import DEFAULT_CHUNK_INDEX
    if DEFAULT_CHUNK_INDEX:
        return True
    if db is None:
        return False
    try:
        return db.query_config.find_one({"chunk_index": True}, {"_id": 1}) is not None
    except Exception:
        return False


async def _loop() -> None:
    from shared_memory.clients import get_chroma, get_mongo
    while True:
        try:
            await asyncio.sleep(SWEEP_SECONDS)
            if not any_scope_enabled(get_mongo()):
                continue
            res = await sweep_once(await get_chroma())
            busy = [s for s in res.get("sources", []) if s.get("added") or s.get("changed")
                    or s.get("deleted") or s.get("meta") or s.get("error")]
            last_sweep.clear()
            last_sweep.update(at=time.time(), version=res.get("version"), error=res.get("error"),
                              touched=busy)
            if busy or res.get("error"):
                log.info("chunk-index sweep: %s", json.dumps(last_sweep)[:2000])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("chunk-index: sweep failed: %s", e)


def start_chunk_sweep() -> None:
    """Start the freshness sweep. Idempotent (lifespan runs per client session).
    JUNTO_CHUNK_SWEEP_SECONDS=0 disables it; each pass is skipped while no
    scope has chunk_index on (any_scope_enabled)."""
    global _sweep_task
    if SWEEP_SECONDS <= 0:
        return
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(_loop(), name="chunk_index_sweep")
