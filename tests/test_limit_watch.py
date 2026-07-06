"""Limit-watch regression (design:limit-watch-v0, 2026-06-10).

The in-process emission counters remember only the current hour and are wiped
on restart, so limit tuning ("are 30/100 the right caps?") had no data and
soft breaches were invisible to the operator. record_emission_history persists
one doc per (sender, hour) and fires once-per-hour proximity alerts:

  - budget_warn         at warn_fraction * push_budget (default 0.8)
  - push_budget_breach  the first send past push_budget (silent containment
                        starts here — exactly the signal a limit may be too low)
  - counts >= hard_ceiling are handle_hard_trip's job, NOT alerted here

Dedup is durable (flags on the history doc), so each alert fires at most once
per sender-hour even across restarts.
"""



class _FakeEmissionHistory:
    def __init__(self):
        self.rows = {}

    @staticmethod
    def _key(filt):
        return (filt.get("instance"), filt.get("project"), filt.get("hour"))

    def update_one(self, filt, upd, upsert=False):
        key = self._key(filt)
        row = self.rows.get(key)

        class _R:
            modified_count = 0

        # Guarded flag-flip dedup: {flag: {"$ne": True}} must not match a row
        # where the flag is already True.
        for f, cond in filt.items():
            if isinstance(cond, dict) and "$ne" in cond:
                if row is None or row.get(f) == cond["$ne"]:
                    if row is not None and row.get(f) == cond["$ne"]:
                        return _R()

        if row is None:
            if not upsert:
                return _R()
            row = {
                "instance": filt.get("instance"),
                "project": filt.get("project"),
                "hour": filt.get("hour"),
            }
            for k, v in (upd.get("$setOnInsert") or {}).items():
                row[k] = v
            self.rows[key] = row

        for k, v in (upd.get("$set") or {}).items():
            row[k] = v
        for k, v in (upd.get("$max") or {}).items():
            if row.get(k) is None or v > row[k]:
                row[k] = v
        for k, v in (upd.get("$inc") or {}).items():
            row[k] = row.get(k, 0) + v

        r = _R()
        r.modified_count = 1
        return r


class _FakeAlerts:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(dict(doc))


class _FakeDB:
    def __init__(self):
        self.emission_history = _FakeEmissionHistory()
        self.alerts = _FakeAlerts()


CFG = {"push_budget": 10, "hard_ceiling": 20, "warn_fraction": 0.8}


def _run_counts(db, counts, suppressed_from=None):
    from shared_memory import push_control as pc

    for c in counts:
        suppressed = suppressed_from is not None and c > suppressed_from
        pc.record_emission_history(db, "watcher", "sage", c, suppressed, CFG)


def test_peak_and_send_counts_persist():
    db = _FakeDB()
    _run_counts(db, [1, 2, 3])
    row = next(iter(db.emission_history.rows.values()))
    assert row["peak_count"] == 3
    assert row["sends"] == 3
    assert row["suppressed"] == 0


def test_warn_fires_once_at_threshold():
    db = _FakeDB()
    # budget=10, warn_fraction=0.8 → warn_at=8
    _run_counts(db, [6, 7, 8, 9, 10])
    warns = [a for a in db.alerts.docs if a["trigger"] == "budget_warn"]
    assert len(warns) == 1, f"warn must fire exactly once; got {len(warns)}"
    assert warns[0]["prior_hour_message_count"] == 8


def test_breach_fires_once_past_budget_and_counts_suppressed():
    db = _FakeDB()
    _run_counts(db, [9, 10, 11, 12, 13], suppressed_from=10)
    breaches = [a for a in db.alerts.docs if a["trigger"] == "push_budget_breach"]
    assert len(breaches) == 1, f"breach must fire exactly once; got {len(breaches)}"
    assert breaches[0]["prior_hour_message_count"] == 11
    row = next(iter(db.emission_history.rows.values()))
    assert row["suppressed"] == 3  # sends 11, 12, 13

def test_hard_ceiling_counts_not_alerted_here():
    """Counts at/above hard_ceiling are handle_hard_trip's territory —
    record_emission_history must not double-alert them."""
    db = _FakeDB()
    _run_counts(db, [20, 21], suppressed_from=10)
    assert db.alerts.docs == []
    row = next(iter(db.emission_history.rows.values()))
    assert row["peak_count"] == 21  # history still records the peak


def test_system_sender_and_zero_count_excluded():
    from shared_memory import push_control as pc

    db = _FakeDB()
    pc.record_emission_history(db, "system", "junto", 5, False, CFG)
    pc.record_emission_history(db, "watcher", "sage", 0, False, CFG)
    assert db.emission_history.rows == {}
    assert db.alerts.docs == []


def test_never_raises_on_db_failure():
    from shared_memory import push_control as pc

    class _Boom:
        def update_one(self, *a, **k):
            raise RuntimeError("mongo down")

    class _BoomDB:
        emission_history = _Boom()

    pc.record_emission_history(_BoomDB(), "watcher", "sage", 5, False, CFG)
    # reaching here without an exception IS the assertion
