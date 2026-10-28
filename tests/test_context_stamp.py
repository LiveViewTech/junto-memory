"""Author context-fill stamps (coordinator msg_5b394f48b4d2)."""

from datetime import datetime, timedelta, timezone

import shared_memory.context_stamp as cs
from shared_memory.state import active_sessions

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


class _Coll:
    def __init__(self, doc=None):
        self.doc, self.rows = doc, []

    def find_one(self, q, proj=None):
        return self.doc if self.doc and self.doc.get("instance") == q.get("instance") else None

    def insert_one(self, row):
        self.rows.append(row)


class _DB:
    def __init__(self, status=None):
        self.agent_status = _Coll(status)
        self.context_stamps = _Coll()


def test_fresh_report_is_known():
    db = _DB({"instance": "pipeline", "context_tokens": 412000, "context_window": 1000000,
              "context_reported_at": (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    c = cs.current_context(db, "pipeline", NOW)
    assert c["known"] and c["context_tokens"] == 412000 and c["context_window"] == 1000000


def test_stale_or_missing_is_unknown_never_zero():
    stale = _DB({"instance": "pipeline", "context_tokens": 412000,
                 "context_reported_at": (NOW - timedelta(minutes=30)).isoformat()})
    c = cs.current_context(stale, "pipeline", NOW)
    assert c["known"] is False and c["context_tokens"] is None
    assert cs.current_context(_DB(), "pipeline", NOW)["context_tokens"] is None


def test_stamp_writes_row(monkeypatch):
    db = _DB()
    active_sessions["sx"] = {"claude_instance": "pipeline", "project": "nimbus"}
    try:
        cs.stamp(db, "sx", "status_change", "learning_abc", new_status="superseded")
    finally:
        active_sessions.pop("sx", None)
    row = db.context_stamps.rows[0]
    assert row["agent"] == "pipeline" and row["new_status"] == "superseded" and row["known"] is False


def test_history_buckets_by_ten_minutes():
    class _Hist:
        def __init__(self):
            self.rows = {}

        def create_index(self, *a, **k):
            pass

        def update_one(self, q, change, upsert=False):
            self.rows[(q["instance"], q["bucket"])] = change["$set"]

    class _HDB:
        context_history = _Hist()

    db = _HDB()
    cs._history_indexed = False
    for minute in (1, 4, 9, 12):
        cs.record_history(db, "pipeline", 400000 + minute, 1000000, NOW.replace(minute=minute))
    buckets = sorted(b.minute for (_, b) in db.context_history.rows)
    assert buckets == [0, 10]
    assert db.context_history.rows[("pipeline", NOW.replace(minute=0))]["context_tokens"] == 400009
