"""Cross-encoder rerank for memory_query (interface:rerank-v0).

MiniLM ranks by vector similarity; a cross-encoder reads the query and each
candidate TOGETHER and scores relevance. On real nimbus queries it lifted
memory_query precision .84 → .96 (learning_ee9e873f9f08a779). It did not help
/recall, so this module is memory_query-only.

The model runs outside this process: contrib/reranker/rerank_server.py
(systemd user unit junto-reranker, GPU, docker0 172.17.0.1:8091). Per-project
flag query_config `rerank` (default False). FAIL OPEN: any service problem
returns None and the caller keeps today's ranking.
"""

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

RERANK_URL = os.environ.get("JUNTO_RERANK_URL", "http://host.docker.internal:8091").rstrip("/")
RERANK_TIMEOUT_S = float(os.environ.get("JUNTO_RERANK_TIMEOUT", "2.0"))
DOC_CHARS = 2000
POOL_MIN = 10
POOL_SHARED = 3

# Row keys memory_query attaches for rerank mode; never leave the server.
PRIVATE_KEYS = ("_sim", "_min", "_cap_key", "_text", "_col")


def enabled_for(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("rerank"))


def pool_size(limit: int) -> int:
    return max(POOL_MIN, 3 * limit)


def doc_text(title: str, body: str) -> str:
    return f"{title or ''}\n{(body or '')[:DOC_CHARS]}"


async def score(query: str, texts: List[str]) -> Optional[List[float]]:
    """Cross-encoder scores in [0,1], one per text, or None on any failure."""
    if not texts:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(RERANK_TIMEOUT_S, connect=1.0)) as c:
            r = await c.post(RERANK_URL + "/rerank", json={"query": query, "documents": texts})
            r.raise_for_status()
            scores = r.json().get("scores")
        if not isinstance(scores, list) or len(scores) != len(texts):
            raise ValueError("bad scores payload")
        return [float(s) for s in scores]
    except Exception as e:  # noqa: BLE001
        log.warning("rerank: service unavailable (%s) — keeping vector ranking", e)
        return None


def apply(rows: List[dict], scores: Optional[List[float]], rerank_min: float,
          caps: Dict[str, int]) -> List[dict]:
    """Select from the rerank pool.

    rows: vector-lane rows (no exact-match rows), each carrying `_sim` (vector
    relevance), `_min` (that row's normal similarity floor) and `_cap_key`
    (its collection). With scores: keep score >= rerank_min, relevance = score,
    best first. Without (service down): exactly today's selection — rows at or
    above their floor, at most caps[_cap_key] per collection, in vector order.
    """
    if scores is None:
        taken: Dict[str, int] = {}
        out = []
        for r in rows:
            k = r["_cap_key"]
            if r["_sim"] >= r["_min"] and taken.get(k, 0) < caps.get(k, 0):
                taken[k] = taken.get(k, 0) + 1
                out.append(r)
        return out
    kept = []
    for r, s in zip(rows, scores):
        if s >= rerank_min:
            r["relevance"] = f"{s:.0%}"
            r["_score"] = s
            kept.append(r)
    kept.sort(key=lambda r: -r["_score"])
    for r in kept:
        r.pop("_score", None)
    return kept


def strip_private(rows: List[dict]) -> None:
    for r in rows:
        for k in PRIVATE_KEYS:
            r.pop(k, None)
