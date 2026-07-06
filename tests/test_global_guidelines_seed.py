"""Tests for the global-guidance code-seed (Option B).

seed_global_guidelines upserts code-defined scope="global" guidelines into
db.guidelines on startup, so a guidance change travels with the deploy to every
server (incl. the isolated work box) without federating data. Invariants:
  - idempotent: a no-change boot writes nothing (no timestamp churn)
  - content-aware: only rows whose rule/priority/active/scope differ are written
  - scope-safe: NEVER touches project-scoped rows
  - non-destructive: an active global in the DB but absent from code is reported
    as an orphan, NOT deleted
"""

import shared_memory.global_guidelines as gg


class _FakeGuidelines:
    def __init__(self, docs=None):
        self.docs = {d["name"]: dict(d) for d in (docs or [])}
        self.writes = 0

    def find_one(self, query, projection=None):
        d = self.docs.get(query.get("name"))
        return dict(d) if d else None

    def update_one(self, query, update, upsert=False):
        name = query["name"]
        setvals = update["$set"]
        if name in self.docs:
            self.docs[name].update(setvals)
        elif upsert:
            self.docs[name] = dict(setvals)
        self.writes += 1

    def find(self, query, projection=None):
        return [
            dict(d) for d in self.docs.values()
            if all(d.get(k) == v for k, v in query.items())
        ]


class _FakeDB:
    def __init__(self, docs=None):
        self.guidelines = _FakeGuidelines(docs)


def _row(name, rule, priority, scope="global", active=True):
    return {"name": name, "rule": rule, "priority": priority,
            "scope": scope, "active": active}


CODE = [
    {"name": "g1", "priority": 2, "rule": "rule one"},
    {"name": "g2", "priority": 5, "rule": "rule two"},
]


def test_fresh_db_inserts_all(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB()
    r = gg.seed_global_guidelines(db)
    assert r == {"inserted": 2, "updated": 0, "unchanged": 0, "orphans": 0}
    assert db.guidelines.writes == 2
    assert db.guidelines.docs["g1"]["updated_by"] == "code-seed"
    assert db.guidelines.docs["g1"]["scope"] == "global"


def test_matching_db_is_noop(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB([_row("g1", "rule one", 2), _row("g2", "rule two", 5)])
    r = gg.seed_global_guidelines(db)
    assert r == {"inserted": 0, "updated": 0, "unchanged": 2, "orphans": 0}
    assert db.guidelines.writes == 0  # no timestamp churn on a clean boot


def test_differing_rule_updates_only_that_row(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB([_row("g1", "STALE rule", 2), _row("g2", "rule two", 5)])
    r = gg.seed_global_guidelines(db)
    assert r["updated"] == 1 and r["unchanged"] == 1 and r["inserted"] == 0
    assert db.guidelines.writes == 1
    assert db.guidelines.docs["g1"]["rule"] == "rule one"
    assert db.guidelines.docs["g1"]["updated_by"] == "code-seed"


def test_priority_change_triggers_update(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB([_row("g1", "rule one", 99), _row("g2", "rule two", 5)])
    r = gg.seed_global_guidelines(db)
    assert r["updated"] == 1 and r["unchanged"] == 1
    assert db.guidelines.docs["g1"]["priority"] == 2


def test_orphan_global_is_reported_not_deleted(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB([
        _row("g1", "rule one", 2), _row("g2", "rule two", 5),
        _row("dropped_global", "old", 10),
    ])
    r = gg.seed_global_guidelines(db)
    assert r["orphans"] == 1
    assert "dropped_global" in db.guidelines.docs  # NOT deleted


def test_project_scoped_rows_untouched(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    proj = _row("nimbus_rule", "do nimbus things", 5, scope="nimbus")
    db = _FakeDB([proj])
    r = gg.seed_global_guidelines(db)
    # both globals inserted; the nimbus row neither updated nor counted as orphan
    assert r["inserted"] == 2 and r["orphans"] == 0
    assert db.guidelines.docs["nimbus_rule"] == proj  # byte-identical, untouched


def test_idempotent_second_run(monkeypatch):
    monkeypatch.setattr(gg, "GLOBAL_GUIDELINES", CODE)
    db = _FakeDB()
    gg.seed_global_guidelines(db)
    writes_after_first = db.guidelines.writes
    r2 = gg.seed_global_guidelines(db)
    assert r2["unchanged"] == 2 and r2["inserted"] == 0 and r2["updated"] == 0
    assert db.guidelines.writes == writes_after_first  # second run wrote nothing


def test_real_seed_data_is_wellformed():
    # The shipped global set: non-empty, unique names, sane priorities, real rules.
    assert len(gg.GLOBAL_GUIDELINES) >= 1
    names = [g["name"] for g in gg.GLOBAL_GUIDELINES]
    assert len(names) == len(set(names)), "duplicate guideline names"
    for g in gg.GLOBAL_GUIDELINES:
        assert g["rule"].strip(), f"empty rule for {g['name']}"
        assert 1 <= g["priority"] <= 100, f"bad priority for {g['name']}"
