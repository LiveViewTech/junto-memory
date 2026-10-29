"""JUNTO_LOG_QUERY_TEXT switch: keep metric rows, drop the searched text."""

import shared_memory.recall_metrics as rm


class _Col:
    def __init__(self):
        self.rows = []

    def insert_one(self, doc):
        self.rows.append(doc)


class _Db(dict):
    def __getitem__(self, k):
        return self.setdefault(k, _Col())


def _log_both(monkeypatch):
    monkeypatch.setattr(rm, "_ensure_indexes", lambda db: None)
    db = _Db()
    rm.log_query_event(db, "p", "a", "secret project plan", ["d1"], [0.9])
    rm.log_recall_event(db, "p", "a", ["d1"], 0.6, [0.9], query="secret prompt")
    return db[rm.QUERY_EVENTS_COLLECTION].rows[0], db[rm.EVENTS_COLLECTION].rows[0]


def test_text_logged_by_default(monkeypatch):
    monkeypatch.delenv("JUNTO_LOG_QUERY_TEXT", raising=False)
    q, r = _log_both(monkeypatch)
    assert q["query"] == "secret project plan" and r["query"] == "secret prompt"


def test_text_dropped_when_switched_off_but_rows_kept(monkeypatch):
    monkeypatch.setenv("JUNTO_LOG_QUERY_TEXT", "false")
    q, r = _log_both(monkeypatch)
    assert q["query"] is None and r["query"] is None
    assert q["ids"] == ["d1"] and r["scores"] == [0.9]
