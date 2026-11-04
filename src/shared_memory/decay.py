"""Shelf-life decay at rank time (Memento #2, interface:ranking-decay-v0).

The facets extractor already labels every learning durable or volatile
(learning_facets.shelf_life). Ranking ignored it, so a 6-month-old snapshot
of "what's deployed right now" ranked like a mechanism that will hold
forever. This turns age into an exponential multiplier on relevance:

    factor = 0.5 ** (age_days / half_life)

| shelf_life | verification | half-life |
|------------|--------------|-----------|
| volatile   | any          | 21 days   |
| durable    | not verified | 90 days   |
| durable    | verified     | no decay  |
| (no facet) | any          | no decay  |

Docs without a facet row (specs, store docs, unextracted learnings) never
decay, so they rank as they did before. Gated per project by the
`shelf_life_decay` query_config flag, default OFF until the replay eval runs.

/recall multiplies its continuous score by the factor. memory_query does NOT
(v2, 10-31): it sorts by relevance BAND, and multiplying before banding
dropped on-topic rows two bands (the 10-30 replay, learning_9fb54b43c3819d1d).
There a row past its half-life (factor < STALE_BELOW) only sinks to the bottom
of its own band, after verified-first.
"""

from typing import Optional

HALF_LIFE_DAYS = {
    "volatile": 21.0,
    "durable": 90.0,
}
# Never drop a doc below this fraction of its raw relevance: decay reorders,
# it does not erase. A 1-year-old volatile row keeps 20%, not ~0.
DECAY_FLOOR = 0.2
# memory_query tiebreak: a row at or past one half-life sinks within its band.
STALE_BELOW = 0.5


def decay_factor(shelf_life: Optional[str], verification: Optional[str],
                 age_days: Optional[float]) -> float:
    if age_days is None or age_days <= 0 or shelf_life not in HALF_LIFE_DAYS:
        return 1.0
    if shelf_life == "durable" and verification == "verified":
        return 1.0
    return max(DECAY_FLOOR, 0.5 ** (age_days / HALF_LIFE_DAYS[shelf_life]))


def age_days_of(updated: Optional[str]) -> Optional[float]:
    from shared_memory.helpers import parse_timestamp, utc_now
    ts = parse_timestamp(updated) if updated else None
    if ts is None:
        return None
    return (utc_now() - ts).total_seconds() / 86400.0
