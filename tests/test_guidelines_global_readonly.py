"""memory_guidelines refuses to set/delete GLOBAL-scope rules.

Global guidelines are code-managed (GLOBAL_GUIDELINES in global_guidelines.py,
re-seeded every boot). A live API edit used to succeed and then vanish silently
at the next restart. Tom, 2026-08-11: "the global rules are only set or changed
with my approval to you" (learning_66a2a5d8bb8f13c1). These tests pin four
guarantees:
  1. set(scope="global") is refused and writes nothing;
  2. delete of a global-scope row is refused and deletes nothing;
  3. project-scoped set/delete still work (the block is scoped to global);
  4. delete guards on the TARGET's scope, not the default-global scope param —
     omitting scope on a project-rule delete must still succeed.
"""

import asyncio
import json

import shared_memory.tools.guidelines as gl


class FakeGuidelines:
    def __init__(self, docs=None):
        self.docs = []
        self._next_id = 0
        for d in docs or []:
            self._add(d)
        self.writes = 0
        self.deletes = 0

    def _add(self, d):
        d = dict(d)
        d.setdefault("_id", self._next_id)
        self._next_id += 1
        self.docs.append(d)

    def find(self, q):
        return [d for d in self.docs if all(d.get(k) == v for k, v in q.items())]

    def find_one(self, q):
        rows = self.find(q)
        return rows[0] if rows else None

    def update_one(self, q, update, upsert=False):
        self.writes += 1
        vals = update.get("$set", {})
        existing = self.find_one(q)
        if existing:
            existing.update(vals)
        elif upsert:
            self._add({**q, **vals})

    def delete_one(self, q):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in q.items())]
        deleted = before - len(self.docs)
        self.deletes += deleted

        class _R:
            deleted_count = deleted

        return _R()


class FakeDB:
    def __init__(self, guidelines):
        self.guidelines = guidelines


def _run(db, monkeypatch, **kwargs):
    monkeypatch.setattr(gl, "get_mongo", lambda: db)
    return json.loads(asyncio.run(gl.memory_guidelines(**kwargs)))


def test_set_global_is_refused(monkeypatch):
    col = FakeGuidelines()
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="set", name="new_global", rule="x", scope="global")
    assert res.get("rejected") is True
    assert res.get("scope") == "global"
    assert col.writes == 0, "a refused global set must write nothing"
    assert col.find_one({"name": "new_global"}) is None


def test_set_global_via_hyphen_variant_is_refused(monkeypatch):
    # scope normalization ("Global" / "GLOBAL") must not slip past the guard.
    col = FakeGuidelines()
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="set", name="x", rule="y", scope="GLOBAL")
    assert res.get("rejected") is True
    assert col.writes == 0


def test_delete_global_is_refused(monkeypatch):
    col = FakeGuidelines([{"name": "mandatory_memory_query", "scope": "global", "rule": "r"}])
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="delete", name="mandatory_memory_query")
    assert res.get("rejected") is True
    assert col.deletes == 0, "a global row must not be deleted"
    assert col.find_one({"name": "mandatory_memory_query"}) is not None


def test_set_project_scope_still_works(monkeypatch):
    col = FakeGuidelines()
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="set", name="nimbus_rule", rule="r", scope="nimbus")
    assert res.get("status") in ("created", "updated")
    assert col.writes == 1
    assert col.find_one({"name": "nimbus_rule"})["scope"] == "nimbus"


def test_delete_project_scope_without_scope_param_still_works(monkeypatch):
    # The delete guard must read the target's scope, NOT the default-global param.
    col = FakeGuidelines([{"name": "nimbus_rule", "scope": "nimbus", "rule": "r"}])
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="delete", name="nimbus_rule")
    assert res.get("status") == "deleted"
    assert col.deletes == 1
    assert col.find_one({"name": "nimbus_rule"}) is None


# --- Project rules are keyed on (name, scope) ---
# memory_guidelines keys project rules on (name, scope).
#
# 2026-10-26: setting park_on_idle_nudge for nimbus, junto, cerebro and ha in a
# row left ONE row (scope=ha): `set` upserted on name only, so each call moved
# the row to the next project and nimbus silently lost its idle policy. The
# same name must be settable once per project, and get/delete must not pick an
# arbitrary project's row.


def test_same_name_in_two_projects_keeps_both(monkeypatch):
    col = FakeGuidelines()
    db = FakeDB(col)
    r1 = _run(db, monkeypatch, action="set", name="park_on_idle_nudge", rule="A", scope="nimbus")
    r2 = _run(db, monkeypatch, action="set", name="park_on_idle_nudge", rule="B", scope="junto")
    assert r1["status"] == "created" and r2["status"] == "created"
    assert col.find_one({"name": "park_on_idle_nudge", "scope": "nimbus"})["rule"] == "A"
    assert col.find_one({"name": "park_on_idle_nudge", "scope": "junto"})["rule"] == "B"
    assert len(col.docs) == 2


def test_set_again_updates_only_that_project(monkeypatch):
    col = FakeGuidelines([
        {"name": "r", "scope": "nimbus", "rule": "A"},
        {"name": "r", "scope": "junto", "rule": "B"},
    ])
    db = FakeDB(col)
    res = _run(db, monkeypatch, action="set", name="r", rule="B2", scope="junto")
    assert res["status"] == "updated"
    assert col.find_one({"name": "r", "scope": "nimbus"})["rule"] == "A"
    assert col.find_one({"name": "r", "scope": "junto"})["rule"] == "B2"


def test_get_with_scope_returns_that_project(monkeypatch):
    col = FakeGuidelines([
        {"name": "r", "scope": "nimbus", "rule": "A"},
        {"name": "r", "scope": "junto", "rule": "B"},
    ])
    res = _run(FakeDB(col), monkeypatch, action="get", name="r", scope="junto")
    assert res["scope"] == "junto" and res["rule"] == "B"


def test_get_without_scope_is_ambiguous_across_projects(monkeypatch):
    col = FakeGuidelines([
        {"name": "r", "scope": "nimbus", "rule": "A"},
        {"name": "r", "scope": "junto", "rule": "B"},
    ])
    res = _run(FakeDB(col), monkeypatch, action="get", name="r")
    assert "error" in res and res["scopes"] == ["junto", "nimbus"]


def test_delete_with_scope_removes_only_that_project(monkeypatch):
    col = FakeGuidelines([
        {"name": "r", "scope": "nimbus", "rule": "A"},
        {"name": "r", "scope": "junto", "rule": "B"},
    ])
    res = _run(FakeDB(col), monkeypatch, action="delete", name="r", scope="junto")
    assert res["status"] == "deleted" and res["scope"] == "junto"
    assert col.find_one({"name": "r", "scope": "nimbus"}) is not None
    assert col.find_one({"name": "r", "scope": "junto"}) is None


def test_delete_without_scope_refuses_when_ambiguous(monkeypatch):
    col = FakeGuidelines([
        {"name": "r", "scope": "nimbus", "rule": "A"},
        {"name": "r", "scope": "junto", "rule": "B"},
    ])
    res = _run(FakeDB(col), monkeypatch, action="delete", name="r")
    assert "error" in res
    assert col.deletes == 0 and len(col.docs) == 2
