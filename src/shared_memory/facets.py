"""Write-time facets on learnings (design:memory-facets-v0 v0.4.0).

Four structured fields extracted once at write time so retrieval consumers
(sub's rater, the future task-declared preload, plain memory_query readers)
get indexing signal a topic-embedding cannot carry:

  claim      one-sentence assertion — interface:claim-extraction-v0 v1.0.2
             VERBATIM, shared claim cache (`learning_claims`) with the
             write-time gate; never re-extracted when the gate already did.
  operation  activity class, closed enum (RATIFIED at 8 per spec
             §operation-enum, msg_1a50fa44207e: diagnose | deploy | config |
             build | query | process | decision | reference); no-match
             → omitted.
  trigger    ≤3 applies-when conditions, free text, format convention
             "when <observable condition>" / "before <action> on <object>".
             Consumers may not HARD-rely on it until the spec's calibration
             gate passes.
  shelf_life durable | volatile. The classification question is deliberately
             NOT world-state-vs-code-knowledge but "could later evidence
             invalidate this?" (sub amendment msg_0af19e210279): diagnoses /
             root-cause / definitive-X claims are volatile BY DEFAULT even
             when they look durable by topic — that's where the labeled
             HARMFUL injections live.

Storage: mongo `learning_facets`, one row per learning doc id, tagged with
FACETS_RECIPE_VERSION for idempotent backfill. Chroma metadata mirrors ONLY
`facet_operation` (single scalar — multi-key Chroma where-filters silently
return nothing, the 28570d4 gotcha; filtering stays single-key + Python).

Failure posture: identical to the claim gate — ADVISORY, NEVER BLOCKS. The
learning write has already returned by the time extraction runs (fire-and-
forget task); every failure degrades to "no facets", which the contract makes
legal forever (the container is optional; backfill covers gaps).

Config (env):
  JUNTO_FACETS_ENABLED  "true" to enable (default false — dormant).
  Model/endpoint: shares JUNTO_CLAIM_GATE_URL / JUNTO_CLAIM_GATE_MODEL.
"""

import asyncio
import logging
import os
import re
from typing import Dict, List, Optional

from shared_memory import claim_gate

logger = logging.getLogger(__name__)

# Bump with spec §facets changes; a mismatch marks a row for backfill re-run.
FACETS_RECIPE_VERSION = "0.4.0"

FACETS_COLLECTION = "learning_facets"

OPERATION_ENUM = (
    "diagnose", "deploy", "config", "build", "query", "process",
    "decision", "reference",
)

OPERATION_SYSTEM = (
    "Classify the primary activity this engineering note is about, as exactly "
    "one word from this list:\n"
    "diagnose — debugging, root-causing, explaining a failure\n"
    "deploy — releasing, restarting, shipping, rollout\n"
    "config — settings, env vars, infrastructure setup\n"
    "build — implementing code, features, schemas\n"
    "query — looking up, reading, searching data or docs\n"
    "process — workflow, coordination, procedure, policy\n"
    "decision — a ruling, choice, or standing directive that was decided\n"
    "reference — an architecture map, survey, or decision-support overview "
    "with no verdict\n"
    "Reply with ONLY the one word. If none fits, reply NONE."
)

SHELF_LIFE_SYSTEM = (
    "Decide whether this engineering note is DURABLE or VOLATILE knowledge.\n"
    "The test is exactly this: could later evidence plausibly invalidate the "
    "note's claim?\n"
    "VOLATILE — diagnoses, root-cause conclusions, 'definitive' explanations "
    "(later evidence can overturn them even when the topic is code), and any "
    "snapshot of live state (locks, deploy state, queue contents, who is "
    "working on what, open invoices).\n"
    "DURABLE — how a mechanism works, API behavior, gotchas about code "
    "behavior, constraints and formats: true until the code itself changes.\n"
    "Reply with ONLY one word: DURABLE or VOLATILE."
)
# Probe record 2026-07-21 (scripts/facets_backfill.py --probe): this v1
# prompt scored volatile 3/3 (both pinned stale-diagnosis docs + same-day
# correction — the spec's HARD pass condition) and durable 2/3. Two
# alternative framings (evidence-test hardening; durable-default) both
# DEGRADED durable recall to 1/3 without helping — iteration stopped to
# avoid overfitting a 6-doc set. Documented residual bias: workaround-shaped
# durable gotchas can mislabel volatile (~1/3), which only costs an
# unnecessary verify-first framing — the SAFE direction. Durable-recall
# improvement is the backfill CC agent's job (stronger model re-judge), not
# prompt drift here.

TRIGGER_SYSTEM = (
    "List the conditions under which a future engineer should look this note "
    "up. At most 3 conditions, one per line, each phrased exactly as "
    "\"when <observable condition>\" or \"before <action> on <object>\". "
    "Be concrete and specific to this note — name the actual symptom, "
    "command, file, or subsystem. No preamble, no numbering, no bullets."
)

TRIGGER_MAX_LINES = 3
TRIGGER_MAX_CHARS = 200
TRIGGER_MAX_TOKENS = 150
ONE_WORD_MAX_TOKENS = 8

# ── modality shape flag (coordinator@nimbus msg_b91f7992752a, Tom-approved) ──
#
# WHAT THIS IS AND IS NOT. It is a SHAPE flag: "this doc's body declares a
# proposal or a deployment state — open it." It is NOT a defect predictor and
# must never be worded as one. That distinction is the whole design:
#   - "this claim may be wrong" is unpredictable — the best discriminator
#     measured was 22% precision (phi4 judging its own output), so a warning
#     worded that way is wrong ~4 times in 5 and burns its own credibility.
#   - "the body declares a modality" is TRUE for every doc it fires on,
#     regardless of whether extraction actually inverted. It cannot mis-fire,
#     so it spends no false-clear budget.
#
# WHY IT EXISTS: `facets.claim` is what memory_query and /recall return, and
# the extractor renders proposals as accomplished fact. Measured instances:
# a PROPOSED, explicitly-unratified apex 301 extracted as "has been
# permanently redirected"; a conditional charging path extracted as "enables
# unauthorized charging". ⛔ The author cannot proofread the claim — it is
# generated after they leave — so the read side is the only place to catch it.
#
# TWO MARKER CLASSES, and the second was a blind spot worth remembering:
#   PROPOSAL   — proposed / unratified / withdrawn / rejected / not applied
#   DEPLOYMENT — committed-but-not-live: "not yet on production", "behind the
#                freeze", "built but not wired in"
# The deployment class is DISTINCT from proposal and is the one BOTH confirmed
# nimbus instances actually failed on. The original screen had no vocabulary
# for it at all and silently missed them — a pattern list is a vocabulary, and
# a vocabulary gap returns a CLEAN result (learning_da2f757e3f3dfa97).
#
# ⚠️ DEPLOY markers require the negation to attach to a DEPLOYMENT/LIVENESS
# object. A loose `NOT (YET )?(ON|IN)` matched 104 docs and was visibly junk
# ("not in upstream auth service"); the tightened form matches 29.
#
# Fires on 75 of 4,286 facet-bearing learnings = 1.75% per doc, 1.80% of the
# rows a reader is actually served. Rendering is PER DOC-ROW deliberately: as
# a response-level banner the rate would be 4.74% and it would become
# furniture within a week (coordinator's objection, which holds).
_SHAPE_PROPOSAL = re.compile(
    r"(PROPOSAL|PROPOSED|UNRATIFIED|NOT APPLIED|NOT YET APPLIED|HYPOTHETICAL|"
    r"REJECTED OPTION|WITHDRAWN|NEVER EXECUTED|"
    r"NOT (?:BEEN )?(?:EXECUTED|INSTALLED|IMPLEMENTED|RATIFIED)|"
    r"\bDRAFT\b|DO NOT EXECUTE|NOTHING HAS BEEN)",
    re.I,
)
_SHAPE_DEPLOYMENT = re.compile(
    r"(NOT (?:YET )?(?:ON|IN) (?:PRODUCTION|PROD|STARGATE|ABYDOS|LIVE)|"
    r"NOT (?:YET )?(?:LIVE|DEPLOYED|SHIPPED|MERGED|RELEASED|ROLLED OUT|PUSHED)|"
    r"AWAITING (?:DEPLOY|RELEASE|MERGE|PRODUCTION)|PENDING (?:DEPLOY|RELEASE|MERGE)|"
    r"BEHIND THE (?:PRODUCTION )?FREEZE|UNDEPLOYED|"
    r"HAS NOT (?:YET )?(?:SHIPPED|DEPLOYED|LANDED|GONE LIVE)|"
    r"(?:STAGED|BUILT|WRITTEN|MERGED|COMMITTED|IMPLEMENTED) BUT NOT (?:YET )?"
    r"(?:DEPLOYED|LIVE|SHIPPED|WIRED|RELEASED|ON))",
    re.I,
)
# Title + body head. A DISJUNCTION over the concatenation, NOT title-weighted —
# two reviewers independently mis-read it as title-weighted and concluded a
# confident title could mask a hedged body. It cannot: including the title only
# ever ADDS matches. 300 chars because that is where authors put the modality
# when they put it anywhere; a doc that buries it deeper is not covered, and
# that limitation is real and unfixed.
SHAPE_WINDOW_CHARS = 300

SHAPE_LABELS = {
    "proposal": "PROPOSAL/NOT-APPLIED",
    "deployment": "NOT-YET-DEPLOYED",
}


def modality_shape(title: str, body: str) -> Optional[str]:
    """Return 'proposal' | 'deployment' | None for a learning's subject window.

    Derived at READ time from the body rather than stored at write time, so it
    covers the entire existing corpus with no backfill — the same reasoning
    that made project_from_collection the right shape: a backfill can
    half-apply, and then nobody knows which half they are reading.

    Proposal wins ties: "unratified" is a stronger statement about the world
    than "not yet deployed", and if a doc says both, the reader needs the
    stronger one first.
    """
    window = f"{title or ''}\n{(body or '')[:SHAPE_WINDOW_CHARS]}"
    if _SHAPE_PROPOSAL.search(window):
        return "proposal"
    if _SHAPE_DEPLOYMENT.search(window):
        return "deployment"
    return None

# Strong references to in-flight extraction tasks — asyncio only keeps weak
# refs, so without this a fire-and-forget task can be GC'd mid-run.
_inflight: set = set()


def facets_enabled() -> bool:
    return os.environ.get("JUNTO_FACETS_ENABLED", "false").lower() in (
        "1", "true", "yes",
    )


# ── per-facet extraction (each returns None on any soft failure) ────────────

def _parse_one_word(raw: str, allowed) -> Optional[str]:
    """Match a one-word completion against an allow-list, case-insensitive.
    Substring parse, earliest hit wins (same posture as the gate's classify
    parse); no hit → None (fail-quiet omit)."""
    upper = (raw or "").upper()
    hits = [(upper.find(w.upper()), w) for w in allowed if w.upper() in upper]
    if not hits:
        return None
    return min(hits)[1]


def is_park_summary(tags) -> bool:
    """True for a park-summary doc: end_session's auto-written session digest,
    whose body is a list of pointers to the learnings recorded that session.

    Such a doc has no operation OF ITS OWN — it indexes other docs — so
    `reference` is correct by construction. phi4 gets it wrong reliably by
    latching onto whichever pointer it read hardest: the librarian measured
    8/8 overrides in one night's batch (diagnose x7, process x1), which is a
    batch-level miss on one recognisable shape, not eight independent ones.

    ⚠️ THE DISCRIMINATOR IS THE TAG, NOT THE BODY. I first wrote this as a
    content heuristic (>=3 doc-id pointers + pointer-line density) and
    measured it against the corpus: 5/8 recall with 2 FALSE POSITIVES on 6
    ordinary analytical learnings — my own notes cite many ids and tripped it.
    The tag is written by end_session (08-07 metadata defaulting) and is
    exact: 8/8, no ambiguity, nothing to calibrate. Precision matters more
    than recall here — a wrong `reference` on a real analytical doc is worse
    than missing an untagged digest.
    """
    if not tags:
        return False
    if isinstance(tags, str):
        try:
            import json as _json
            tags = _json.loads(tags)
        except Exception:
            return "park-summary" in tags
    try:
        return "park-summary" in tags
    except TypeError:
        return False


async def extract_operation(client, title: str, content: str,
                            tags=None) -> Optional[str]:
    # SHAPE RULE, PRE-MODEL (librarian-proposed 07-31, adopted; sub released
    # the hold 2026-08-10 — their bakeoff bar governs the SHARED Stage-1 claim
    # recipe, not this facet). Deciding in CODE rather than by prompt edit is
    # deliberate: it cannot move phi4's behaviour because phi4 is not asked,
    # so it sidesteps the bakeoff constraint entirely — the same move that
    # made the max_tokens fix safe.
    if is_park_summary(tags):
        return "reference"
    raw = await claim_gate._chat(
        client, OPERATION_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        ONE_WORD_MAX_TOKENS,
    )
    return _parse_one_word(raw, OPERATION_ENUM)


async def extract_shelf_life(client, title: str, content: str) -> Optional[str]:
    raw = await claim_gate._chat(
        client, SHELF_LIFE_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        ONE_WORD_MAX_TOKENS,
    )
    word = _parse_one_word(raw, ("DURABLE", "VOLATILE"))
    return word.lower() if word else None


async def extract_trigger(client, title: str, content: str) -> List[str]:
    raw = await claim_gate._chat(
        client, TRIGGER_SYSTEM,
        f"{title}\n\n{content[:claim_gate.CONTENT_HEAD_CHARS]}",
        TRIGGER_MAX_TOKENS,
    )
    lines = []
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*•0123456789. ").strip()
        if not line:
            continue
        low = line.lower()
        # Enforce the format convention — a line that doesn't open with the
        # convention is a model digression, not a trigger; drop it.
        if not (low.startswith("when ") or low.startswith("before ")):
            continue
        lines.append(line[:TRIGGER_MAX_CHARS])
        if len(lines) >= TRIGGER_MAX_LINES:
            break
    return lines


# ── storage ─────────────────────────────────────────────────────────────────

def _store_facets(db, doc_id: str, facets: Dict) -> None:
    if db is None:
        return
    from shared_memory.helpers import utc_now_iso

    row = {k: v for k, v in facets.items() if v}
    row.update({
        "recipe_version": FACETS_RECIPE_VERSION,
        "model": claim_gate._gate_model(),
        "extracted_at": utc_now_iso(),
    })
    db[FACETS_COLLECTION].update_one({"_id": doc_id}, {"$set": row}, upsert=True)


async def _mirror_operation_to_chroma(collection, doc_id: str, operation: str) -> None:
    """Best-effort single-scalar metadata mirror. Merges into the existing
    metadata dict (Chroma update replaces the metadata payload wholesale, so a
    naive one-key update would erase title/type/status)."""
    got = await collection.get(ids=[doc_id], include=["metadatas"])
    metas = got.get("metadatas") or []
    if not metas or metas[0] is None:
        return
    meta = dict(metas[0])
    meta["facet_operation"] = operation
    await collection.update(ids=[doc_id], metadatas=[meta])


def needs_extraction(db, doc_id: str) -> bool:
    """True when a doc should be (re-)extracted by the MECHANICAL tier.

    REVIEWED-ROW PRESERVATION (spec §operation-enum hard requirement): a row
    carrying reviewed_by was judged by the strong-model tier — mechanical
    re-extraction must NEVER touch it, regardless of recipe_version (a
    recipe-mismatch re-extract would clobber judged shelf_life/trigger while
    leaving the stale reviewed_by marker). Reviewed rows only change via a
    new review pass.
    """
    if db is None:
        return False
    row = db[FACETS_COLLECTION].find_one({"_id": doc_id})
    if row is None:
        return True
    if row.get("reviewed_by"):
        return False
    return row.get("recipe_version") != FACETS_RECIPE_VERSION


def get_facets_for_ids(db, doc_ids: List[str]) -> Dict[str, Dict]:
    """Batched read for the delivery-surface guarantee (spec §consumer):
    memory_query attaches facets INLINE to result rows — consumers must never
    need a per-candidate get_by_id round-trip. Returns {doc_id: facets_dict}
    with mongo plumbing stripped; empty dict on any failure."""
    if db is None or not doc_ids:
        return {}
    try:
        out = {}
        for row in db[FACETS_COLLECTION].find({"_id": {"$in": doc_ids}}):
            doc_id = row.pop("_id")
            out[doc_id] = row
        return out
    except Exception as e:  # noqa: BLE001 - delivery is best-effort
        logger.warning("facets: batched read failed (%s)", e)
        return {}


# ── orchestrator ────────────────────────────────────────────────────────────

async def _extract_and_store(db, collection, doc_id: str, title: str,
                             details: str) -> None:
    """The whole pipeline for one learning. Runs detached from the recording
    request — every failure logs and dies quietly here."""
    try:
        import httpx  # lazy, mirrors the gate

        timeout = httpx.Timeout(60.0, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            # claim: the gate may have already extracted+cached it this write.
            claim = claim_gate._cached_claim(db, doc_id)
            if not claim:
                claim = await claim_gate.extract_claim(client, title, details)
                if claim:
                    claim_gate._cache_claim(db, doc_id, claim)
            if not claim:
                # Contract: claim is GUARANTEED non-empty when the facets
                # container exists — no claim means no container this pass
                # (backfill retries under its own recipe stamp).
                logger.warning("facets: empty claim for %s — skipping", doc_id)
                return

            # Tags drive the park-summary shape rule; read them from the
            # doc's own metadata so no caller signature has to change.
            _tags = None
            try:
                _got = await collection.get(ids=[doc_id], include=["metadatas"])
                _tags = ((_got.get("metadatas") or [{}])[0] or {}).get("tags")
            except Exception:
                pass
            operation = await extract_operation(client, title, details, _tags)
            shelf_life = await extract_shelf_life(client, title, details)
            trigger = await extract_trigger(client, title, details)

        _store_facets(db, doc_id, {
            "claim": claim,
            "operation": operation,
            "trigger": trigger,
            "shelf_life": shelf_life,
        })
        if operation:
            await _mirror_operation_to_chroma(collection, doc_id, operation)
        logger.info(
            "facets: stored for %s (operation=%s shelf_life=%s triggers=%d)",
            doc_id, operation, shelf_life, len(trigger),
        )
    except Exception as e:  # noqa: BLE001 - advisory path
        logger.warning("facets: extraction failed for %s (%s) — skipped", doc_id, e)


def schedule_facet_extraction(db, collection, doc_id: str, title: str,
                              details: str) -> bool:
    """Fire-and-forget entry point called by memory_record_learning AFTER the
    write has landed. Returns True when a task was scheduled. Never raises —
    the write is the load-bearing operation."""
    if not facets_enabled():
        return False
    try:
        task = asyncio.create_task(
            _extract_and_store(db, collection, doc_id, title, details),
            name=f"facets_{doc_id}",
        )
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
        return True
    except Exception as e:  # noqa: BLE001 - e.g. no running loop in exotic contexts
        logger.warning("facets: could not schedule extraction for %s (%s)", doc_id, e)
        return False
