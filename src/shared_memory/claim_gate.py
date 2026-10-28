"""Write-time contradiction gate — classifier stage (backlog_6471d8348393).

Implements interface:claim-extraction-v0 v1.0.3 VERBATIM on top of the
threshold stage in tools/storage.py. Pipeline per the contract:

  EXTRACT (phi4:14b, temp 0, title + first ~3000 chars, max_tokens 400,
  non-empty validate) → title-anchor claims ("{title}\\n{claim}") →
  CLASSIFY (CONTRADICTS / CONSISTENT / UNRELATED, one word, substring
  parse, no-match → CONSISTENT fail-quiet).

Mapping (confirmed in the contract): prior = CLAIM A (earlier), the new
note = CLAIM B (later); CONTRADICTS means the new note invalidates the
surfaced prior.

Contract amendments honored here:
- Extracted claims are PERSISTED in mongo `learning_claims`, tagged with
  RECIPE_VERSION; a recipe bump invalidates the cache lazily (mismatched
  rows are re-extracted on next touch).
- The judge model for THIS consumer is phi4:14b (precision-decisive:
  a false CONTRADICTS flags a legitimate write). Do NOT normalize to
  gpt-oss — that variant belongs to junto-sub's trap-escalation check.

Known limit (do not overclaim): temporal invalidation (dataset P12,
"A was true, the world changed") is missed by every tested model.

Failure posture: ADVISORY, NEVER BLOCKS. Endpoint down, model evicted,
empty extraction, timeout — every failure degrades to the threshold-only
response (pointers without relationships). The write is the load-bearing
operation; the classification is not.

Config (env):
  JUNTO_CLAIM_GATE_ENABLED  "true" to enable (default false — dormant).
  JUNTO_CLAIM_GATE_URL      OpenAI-compatible base, default
                            http://host.docker.internal:11434/v1 (sage Ollama
                            via the compose host-gateway extra_host).
  JUNTO_CLAIM_GATE_MODEL    default phi4:14b (contract reference model).
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Bump ONLY with a contract version bump (requires a bakeoff2.py rerun
# posted to both consumers — see interface:claim-extraction-v0 Versioning).
RECIPE_VERSION = "1.0.3"

CONTENT_HEAD_CHARS = 3000
EXTRACT_MAX_TOKENS = 400
CLASSIFY_MAX_TOKENS = 8
# Classify only the strongest priors — bounds worst-case gate latency to
# 1 extract + MAX_CLASSIFY_PRIORS classify calls per record.
MAX_CLASSIFY_PRIORS = 3
CLAIMS_COLLECTION = "learning_claims"

# ── contract prompts (interface:claim-extraction-v0 v1.0.3, VERBATIM) ──────
# Prompts are UNCHANGED from v1.0.2 — the v1.0.3 bump is the output BUDGET
# only (max_tokens 120 → 400). Nothing the model is ASKED has changed.

EXTRACT_SYSTEM = (
    "Extract the CORE FACTUAL CLAIM of this engineering note as 1-2 short "
    "present-tense sentences. State only WHAT IS claimed about the "
    "system/world. Do NOT mention that the note corrects, supersedes, "
    "confirms, or relates to any other note. No preamble."
)

CLASSIFY_SYSTEM = (
    "Two engineering claims about the same system, recorded at different "
    "times: CLAIM A (earlier) and CLAIM B (later). Classify B's relationship "
    "to A as exactly one word:\n"
    "CONTRADICTS — B makes A wrong. A stated something as fact and B shows "
    "it is not true (error correction, reversed conclusion, or the state "
    "changed such that A's fact no longer holds).\n"
    "CONSISTENT — A remains true given B. B restates, confirms, proves, "
    "extends A, or reports added progress/work on top of what A described.\n"
    "UNRELATED — different subject matter.\n"
    "Reply with ONLY the one word."
)

CLASSIFY_USER_TEMPLATE = (
    "CLAIM A (earlier):\n{a}\n\nCLAIM B (later):\n{b}\n\n"
    "Relationship of B to A (one word):"
)

LABELS = ("CONTRADICTS", "CONSISTENT", "UNRELATED")


def gate_enabled() -> bool:
    return os.environ.get("JUNTO_CLAIM_GATE_ENABLED", "false").lower() in (
        "1", "true", "yes",
    )


def _gate_url() -> str:
    return os.environ.get(
        "JUNTO_CLAIM_GATE_URL", "http://host.docker.internal:11434/v1"
    ).rstrip("/")


def _gate_model() -> str:
    return os.environ.get("JUNTO_CLAIM_GATE_MODEL", "phi4:14b")


async def _chat(client, system: str, user: str, max_tokens: int) -> str:
    """One /chat/completions call, temperature 0. Returns content ('' on a
    shape surprise); transport errors propagate to the orchestrator's
    fail-quiet boundary."""
    resp = await client.post(
        f"{_gate_url()}/chat/completions",
        json={
            "model": _gate_model(),
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


async def extract_claim(client, title: str, content: str) -> Optional[str]:
    """Stage 1. Returns the whitespace-collapsed claim, or None when the
    model returned empty (contract: output MUST be non-empty — an empty
    result means this pair is skipped, e.g. a think-mode model ate the
    budget)."""
    user = f"{title}\n\n{content[:CONTENT_HEAD_CHARS]}"
    raw = await _chat(client, EXTRACT_SYSTEM, user, EXTRACT_MAX_TOKENS)
    claim = " ".join(raw.split())
    return claim or None


async def classify_pair(
    client, a_title: str, a_claim: str, b_title: str, b_claim: str
) -> str:
    """Stage 2 on TITLE-ANCHORED claims (load-bearing — naive claim-only
    causes facet mismatch, learning_b91a39851a56af95). Substring parse,
    first hit (earliest position) wins; no match → CONSISTENT."""
    a = f"{a_title}\n{a_claim}"
    b = f"{b_title}\n{b_claim}"
    raw = await _chat(
        client,
        CLASSIFY_SYSTEM,
        CLASSIFY_USER_TEMPLATE.format(a=a, b=b),
        CLASSIFY_MAX_TOKENS,
    )
    upper = raw.upper()
    hits = [(upper.find(label), label) for label in LABELS if label in upper]
    if not hits:
        return "CONSISTENT"
    return min(hits)[1]


# ── claim cache (mongo, recipe-version tagged) ─────────────────────────────

def _cached_claim(db, doc_id: str) -> Optional[str]:
    if db is None:
        return None
    row = db[CLAIMS_COLLECTION].find_one({"_id": doc_id})
    if not row:
        return None
    # HAND-REPAIRED CLAIMS ARE STICKY ACROSS RECIPE BUMPS (sub + memory,
    # ratified with the v1.0.3 amendment). The librarian repairs defective
    # claims by hand — meta-openers, truncations, factual inversions — and a
    # lazy recipe-version invalidation would silently overwrite four nights of
    # manual work with a fresh machine extraction. A human/agent correction
    # outranks a re-run: the repair was made BECAUSE the extractor was wrong.
    if row.get("claim_cleaned_by"):
        return row.get("claim")
    if row.get("recipe_version") == RECIPE_VERSION:
        return row.get("claim")
    return None  # miss, or stale recipe → lazy re-extract


def _cache_claim(db, doc_id: str, claim: str) -> None:
    if db is None:
        return
    from shared_memory.helpers import utc_now_iso

    db[CLAIMS_COLLECTION].update_one(
        {"_id": doc_id},
        {"$set": {
            "claim": claim,
            "recipe_version": RECIPE_VERSION,
            "model": _gate_model(),
            "extracted_at": utc_now_iso(),
        }},
        upsert=True,
    )


def _strip_title_header(document: str, title: str) -> str:
    """Learning docs are stored as '# {title}\\n\\n{details}'; the contract
    wants title + body head, so peel the header when present."""
    prefix = f"# {title}\n\n"
    if document.startswith(prefix):
        return document[len(prefix):]
    return document


async def _prior_claim(db, client, collection, doc_id: str, title: str) -> Optional[str]:
    cached = _cached_claim(db, doc_id)
    if cached:
        return cached
    got = await collection.get(ids=[doc_id], include=["documents"])
    docs = got.get("documents") or []
    if not docs or docs[0] is None:
        return None
    claim = await extract_claim(client, title, _strip_title_header(docs[0], title))
    if claim:
        _cache_claim(db, doc_id, claim)
    return claim


# ── orchestrator (the only entry point storage.py calls) ───────────────────

async def classify_against_priors(
    db,
    collection,
    new_doc_id: str,
    new_title: str,
    new_details: str,
    priors: List[Dict],
) -> Tuple[List[Dict], List[str]]:
    """Annotate the top threshold-stage priors with a `relationship` field
    (CONTRADICTS / CONSISTENT / UNRELATED, per the contract's B-vs-A
    mapping) and return (priors, contradicted_prior_ids).

    Advisory by contract: ANY failure — endpoint down, empty extraction,
    timeout — returns the priors exactly as given and an empty contradiction
    list. Never raises.
    """
    if not gate_enabled() or not priors:
        return priors, []
    try:
        import httpx  # lazy — only on the priors-found path

        timeout = httpx.Timeout(60.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            new_claim = await extract_claim(client, new_title, new_details)
            if not new_claim:
                logger.warning("claim gate: empty extraction for %s — skipping", new_doc_id)
                return priors, []
            _cache_claim(db, new_doc_id, new_claim)

            contradicted: List[str] = []
            for prior in priors[:MAX_CLASSIFY_PRIORS]:
                a_claim = await _prior_claim(
                    db, client, collection, prior["id"], prior.get("title", "")
                )
                if not a_claim:
                    continue
                relationship = await classify_pair(
                    client,
                    a_title=prior.get("title", ""),
                    a_claim=a_claim,
                    b_title=new_title,
                    b_claim=new_claim,
                )
                prior["relationship"] = relationship
                if relationship == "CONTRADICTS":
                    contradicted.append(prior["id"])
            return priors, contradicted
    except Exception as e:  # noqa: BLE001 - advisory path, degrade to threshold-only
        logger.warning("claim gate: classifier stage failed (%s) — threshold-only", e)
        return priors, []
