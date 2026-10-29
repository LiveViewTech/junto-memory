"""Lexical identifier lane for memory_query and /recall.

Vector search (MiniLM, 256-token window) is weak at exact identifiers: a
function name, msg_/backlog_/learning_ id, file path or class name deep in a
doc body. This lane pulls the query's identifier-shaped tokens, asks Chroma for
docs whose text CONTAINS one (`where_document={"$contains": tok}`, a trigram
index, ~20-270 ms on prod), and ranks those by the same query vector. Hits
come back with a real similarity score and are flagged `match="exact"`; they
are kept even below the similarity floor, capped at `lexical_max` per request
(query_config, default 2, 0 disables). Ordinary prose queries carry no
identifier tokens, so the lane is a no-op for them.

Not BM25: no term weighting, no prose matching. Full lexical ranking waits on
the retrieval eval harness so ranking doesn't change unmeasured.
"""

import re
from typing import Any, Dict, List, Optional

_TOKEN = re.compile(r"[A-Za-z0-9_./:#-]+")
_EDGE = ".,:;#-/"
MIN_TOKEN_LEN = 6
MAX_TOKENS = 3


def _is_identifier(tok: str) -> bool:
    if len(tok) < MIN_TOKEN_LEN or tok.startswith(("http:", "https:")):
        return False
    if "_" in tok:
        return True
    if re.search(r"\w[./]\w", tok):          # file.py, a/b/c, BuyCart.aspx
        return True
    if re.search(r"[a-z][A-Z]", tok):         # camelCase / PascalCase joins
        return True
    if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok) and re.fullmatch(r"[A-Za-z0-9]+", tok):
        return re.search(r"[A-Za-z]{2,}", tok) is not None and len(tok) >= 8
    return False


def identifier_tokens(text: str, limit: int = MAX_TOKENS) -> List[str]:
    """Identifier-shaped tokens in `text`, longest first, deduped."""
    seen = []
    for raw in _TOKEN.findall(text or ""):
        tok = raw.strip(_EDGE)
        if _is_identifier(tok) and tok not in seen:
            seen.append(tok)
    seen.sort(key=len, reverse=True)
    return seen[:limit]


async def lexical_scan(collection, qtext: str, qvec, where: Optional[dict],
                       n: int, exclude_ids=()) -> List[Dict[str, Any]]:
    """Docs containing one of qtext's identifier tokens, ranked by the query
    vector. Returns [{id, doc, meta, dist, token}] not in exclude_ids, at most
    n. Never raises: a lexical failure must not cost the vector results."""
    if n <= 0:
        return []
    tokens = identifier_tokens(qtext)
    if not tokens:
        return []
    from shared_memory.helpers import query_kwargs
    hits: List[Dict[str, Any]] = []
    seen = set(exclude_ids)
    for tok in tokens:
        try:
            got = await collection.query(
                **query_kwargs(qtext, qvec), n_results=n, where=where,
                where_document={"$contains": tok},
            )
        except Exception:
            continue
        if not got.get("ids") or not got["ids"][0]:
            continue
        for doc_id, doc, meta, dist in zip(got["ids"][0], got["documents"][0],
                                           got["metadatas"][0], got["distances"][0]):
            if doc_id in seen:
                continue
            seen.add(doc_id)
            hits.append({"id": doc_id, "doc": doc, "meta": meta or {}, "dist": dist, "token": tok})
    hits.sort(key=lambda h: h["dist"])
    return hits[:n]
