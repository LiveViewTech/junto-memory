"""Server-side pull-through instrumentation for /recall (A2(ii), done right).

The hook-side log records injections only and structurally cannot measure
pull-through (coordinator@nimbus msg_53e467692779 — the first-day read that
caught the half-instrument). Both halves of the metric are server-visible,
so the join lives here:

  recall_events  one row per /recall response with count>0:
                 {ts, project, agent, ids, floor, scores, matches, query}.
                 TTL 90d (was 7d; raised 2026-10-24 so the retrieval eval
                 harness can seed real queries — the maintainer retrieval-eval harness, not shipped).
  recall_pulls   one row per memory_get_by_id fetch of a doc that was
                 injected to the SAME project within the last 24h:
                 {ts, doc_id, agent, project, event_ts}. TTL 90d.
  query_events   one row per memory_query call with results:
                 {ts, project, agent, query, ids, scores, matches}. TTL 90d.
                 Separate from recall_events so memory_query results never
                 count as injections in the pull-through join.

`query` is truncated to QUERY_LOG_CHARS. It is the caller's text (for /recall,
the hook's conversation window), so these rows hold conversation excerpts.
                 The row's existence IS the join — pull-through rate is
                 count(recall_pulls) / sum(len(recall_events.ids)).

Everything here is best-effort: metrics must never fail a serve or a fetch.

KNOWN OVERCOUNT DIRECTION (sub's metering hit the same trap,
learning_9e76ae6e9598e532): a fetch inside the join window scores as a pull
even when the agent found the doc independently (query, message pointer) —
the join proves correlation, not causation. The 24h window is deliberately
generous for the first data; if pull counts matter for a go/no-go, tighten
the window or compare against a no-injection baseline before trusting them.
"""

import logging
from datetime import timedelta

from shared_memory.helpers import utc_now

logger = logging.getLogger(__name__)

EVENTS_COLLECTION = "recall_events"
PULLS_COLLECTION = "recall_pulls"
QUERY_EVENTS_COLLECTION = "query_events"
TTL_SECONDS = 90 * 24 * 3600
QUERY_LOG_CHARS = 2000
# JUNTO_LOG_QUERY_TEXT=false keeps the ids/scores rows (pull-through metrics,
# floor simulation) but stores query=None — for deployments where the text of
# what people search for must not sit in Mongo for 90 days (work box, Tom
# 2026-10-27). Read per call so tests and a restart-free env flip both work.
_TEXT_OFF = ("0", "false", "no", "off")


def _query_text(query):
    import os
    if os.environ.get("JUNTO_LOG_QUERY_TEXT", "true").strip().lower() in _TEXT_OFF:
        return None
    return (query or "")[:QUERY_LOG_CHARS] or None
PULL_JOIN_WINDOW_HOURS = 24

_indexed = False


def _ensure_indexes(db):
    global _indexed
    if _indexed or db is None:
        return
    try:
        for coll in (EVENTS_COLLECTION, PULLS_COLLECTION, QUERY_EVENTS_COLLECTION):
            _ensure_ttl(db, coll)
        db[EVENTS_COLLECTION].create_index("ids")
        db[EVENTS_COLLECTION].create_index("project")
        db[QUERY_EVENTS_COLLECTION].create_index("project")
        _indexed = True
    except Exception as e:  # noqa: BLE001 — metrics are never load-bearing
        logger.warning("recall_metrics: index setup failed (%s)", e)


def _ensure_ttl(db, coll):
    """TTL index on ts at TTL_SECONDS. An existing ts TTL index with another
    value can't be re-created (IndexOptionsConflict), so it is changed in
    place with collMod."""
    from pymongo.errors import OperationFailure
    try:
        db[coll].create_index("ts", expireAfterSeconds=TTL_SECONDS)
    except OperationFailure:
        db.command("collMod", coll, index={"keyPattern": {"ts": 1}, "expireAfterSeconds": TTL_SECONDS})


def log_recall_event(db, project, agent, ids, floor, scores=None, query=None, matches=None,
                     retriever=None):
    """Record a served injection set. Call only when count>0. `scores`
    (parallel to ids) lets a floor change be simulated from history
    instead of guessed; `matches` (parallel, "exact" or None) marks lexical
    rows, whose scores bypass the floor."""
    if db is None or not ids:
        return
    try:
        _ensure_indexes(db)
        db[EVENTS_COLLECTION].insert_one({
            "ts": utc_now(),
            "project": project,
            "agent": agent,
            "ids": list(ids),
            "floor": floor,
            "scores": list(scores) if scores is not None else None,
            "matches": list(matches) if matches is not None else None,
            "query": _query_text(query),
            "retriever": retriever,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("recall_metrics: event log failed (%s)", e)


def log_query_event(db, project, agent, query, ids, scores=None, matches=None):
    """Record one memory_query call (eval-set seeding). Never raises."""
    if db is None or not ids:
        return
    try:
        _ensure_indexes(db)
        db[QUERY_EVENTS_COLLECTION].insert_one({
            "ts": utc_now(),
            "project": project,
            "agent": agent,
            "query": _query_text(query),
            "ids": list(ids),
            "scores": list(scores) if scores is not None else None,
            "matches": list(matches) if matches is not None else None,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("recall_metrics: query event log failed (%s)", e)


def log_pull_if_injected(db, doc_id, agent, project):
    """If doc_id was recall-injected to this project in the last 24h, record
    the fetch as a pull-through. One indexed read per get_by_id; silent on
    any failure."""
    if db is None or not doc_id:
        return
    try:
        _ensure_indexes(db)
        cutoff = utc_now() - timedelta(hours=PULL_JOIN_WINDOW_HOURS)
        event = db[EVENTS_COLLECTION].find_one(
            {"ids": doc_id, "ts": {"$gte": cutoff}, "project": project},
            sort=[("ts", -1)],
        )
        if event is None:
            return
        db[PULLS_COLLECTION].insert_one({
            "ts": utc_now(),
            "doc_id": doc_id,
            "agent": agent,
            "project": project,
            "event_ts": event["ts"],
            "event_agent": event.get("agent"),
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("recall_metrics: pull log failed (%s)", e)
