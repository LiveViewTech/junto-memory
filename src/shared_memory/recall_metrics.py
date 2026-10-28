"""Server-side pull-through instrumentation for /recall (A2(ii), done right).

The hook-side log records injections only and structurally cannot measure
pull-through (coordinator@nimbus msg_53e467692779 — the first-day read that
caught the half-instrument). Both halves of the metric are server-visible,
so the join lives here:

  recall_events  one row per /recall response with count>0:
                 {ts, project, agent, ids, floor}. TTL 7d.
  recall_pulls   one row per memory_get_by_id fetch of a doc that was
                 injected to the SAME project within the last 24h:
                 {ts, doc_id, agent, project, event_ts}. TTL 7d.
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
TTL_SECONDS = 7 * 24 * 3600
PULL_JOIN_WINDOW_HOURS = 24

_indexed = False


def _ensure_indexes(db):
    global _indexed
    if _indexed or db is None:
        return
    try:
        db[EVENTS_COLLECTION].create_index("ts", expireAfterSeconds=TTL_SECONDS)
        db[EVENTS_COLLECTION].create_index("ids")
        db[EVENTS_COLLECTION].create_index("project")
        db[PULLS_COLLECTION].create_index("ts", expireAfterSeconds=TTL_SECONDS)
        _indexed = True
    except Exception as e:  # noqa: BLE001 — metrics are never load-bearing
        logger.warning("recall_metrics: index setup failed (%s)", e)


def log_recall_event(db, project, agent, ids, floor):
    """Record a served injection set. Call only when count>0."""
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
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("recall_metrics: event log failed (%s)", e)


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
