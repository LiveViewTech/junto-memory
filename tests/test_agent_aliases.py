"""Tests for recipient-alias resolution + uniqueness-checked persistence.

Two new primitives in tools/projects.py back the ET-routing fix:

- resolve_agent_name(db, project, name): maps a recipient name to a canonical
  live agent — a direct name passes through, a nickname/alias (e.g.
  "coordinator" -> "emailTriage") is redirected, an unknown name returns None.
  memory_send_message uses it to redirect aliases and to fail loud on unknown
  recipients even for unregistered-but-rostered projects (emailtriage).

- persist_agent_aliases(db, project, agent, aliases): writes the alias list to
  the agent's registered_agents doc, rejecting any alias that would shadow a
  live agent name or another agent's alias (uniqueness per project).
"""

from types import SimpleNamespace

from shared_memory.tools.projects import resolve_agent_name, persist_agent_aliases


def _matches(doc, query):
    """Minimal Mongo-query matcher for the shapes these helpers issue."""
    for k, v in query.items():
        if k in ("_id",):  # projection-ish keys never appear in our filters
            continue
        if k == "aliases":
            # membership: query value must be in the doc's alias list
            if v not in (doc.get("aliases") or []):
                return False
        elif isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        else:
            if doc.get(k) != v:
                return False
    return True


class _FakeRegisteredAgents:
    def __init__(self, docs):
        self.docs = docs  # list of {project, name, aliases?}

    def find_one(self, query, projection=None):
        for d in self.docs:
            if _matches(d, query):
                return d
        return None

    def update_one(self, filt, update):
        # Mirror pymongo's UpdateResult so callers can read .matched_count
        # (persist_agent_aliases fails loud when nothing matched).
        for d in self.docs:
            if _matches(d, filt):
                d.update(update["$set"])
                return SimpleNamespace(matched_count=1, modified_count=1,
                                       upserted_id=None)
        # upsert=False semantics: no matching doc -> no write
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)


class _FakeDB:
    def __init__(self, docs):
        self.registered_agents = _FakeRegisteredAgents(docs)


def _et_db():
    return _FakeDB([
        {"project": "emailtriage", "name": "emailTriage",
         "aliases": ["coordinator", "main"]},
        {"project": "emailtriage", "name": "et-engine"},
        {"project": "emailtriage", "name": "et-qa"},
    ])


# ── resolve_agent_name ──────────────────────────────────────────────────────

def test_resolve_direct_name_passes_through():
    assert resolve_agent_name(_et_db(), "emailtriage", "et-engine") == "et-engine"


def test_resolve_alias_redirects_to_canonical():
    db = _et_db()
    assert resolve_agent_name(db, "emailtriage", "coordinator") == "emailTriage"
    assert resolve_agent_name(db, "emailtriage", "main") == "emailTriage"


def test_resolve_unknown_returns_none():
    assert resolve_agent_name(_et_db(), "emailtriage", "ghost") is None


def test_resolve_alias_is_project_scoped():
    # "coordinator" is only an alias within emailtriage; another project that
    # has no such agent/alias must not resolve it.
    db = _FakeDB([{"project": "nimbus", "name": "coordinator"}])
    assert resolve_agent_name(db, "nimbus", "coordinator") == "coordinator"  # real agent
    assert resolve_agent_name(db, "nimbus", "main") is None


def test_resolve_guards_empty_and_none():
    assert resolve_agent_name(_et_db(), "emailtriage", "") is None
    assert resolve_agent_name(None, "emailtriage", "coordinator") is None


# ── persist_agent_aliases ───────────────────────────────────────────────────

def test_persist_accepts_clean_alias():
    db = _FakeDB([
        {"project": "emailtriage", "name": "emailTriage"},
        {"project": "emailtriage", "name": "et-qa"},
    ])
    res = persist_agent_aliases(db, "emailtriage", "emailTriage", ["coordinator", "main"])
    assert res["accepted"] == ["coordinator", "main"]
    assert res["rejected"] == {}
    # and it actually wrote them
    assert resolve_agent_name(db, "emailtriage", "coordinator") == "emailTriage"


def test_persist_rejects_collision_with_live_agent():
    db = _FakeDB([
        {"project": "emailtriage", "name": "emailTriage"},
        {"project": "emailtriage", "name": "et-qa"},
    ])
    res = persist_agent_aliases(db, "emailtriage", "emailTriage", ["et-qa", "coordinator"])
    assert "coordinator" in res["accepted"]
    assert "et-qa" in res["rejected"]
    assert "live agent" in res["rejected"]["et-qa"]


def test_persist_rejects_collision_with_other_alias():
    db = _FakeDB([
        {"project": "emailtriage", "name": "emailTriage", "aliases": ["coordinator"]},
        {"project": "emailtriage", "name": "et-engine"},
    ])
    res = persist_agent_aliases(db, "emailtriage", "et-engine", ["coordinator"])
    assert res["accepted"] == []
    assert "coordinator" in res["rejected"]
    assert "emailTriage" in res["rejected"]["coordinator"]


def test_persist_drops_self_alias_and_blanks():
    db = _FakeDB([{"project": "emailtriage", "name": "emailTriage"}])
    res = persist_agent_aliases(db, "emailtriage", "emailTriage", ["emailTriage", "  ", "coordinator"])
    assert res["accepted"] == ["coordinator"]


def test_persist_empty_list_clears_aliases():
    db = _FakeDB([{"project": "emailtriage", "name": "emailTriage", "aliases": ["coordinator"]}])
    res = persist_agent_aliases(db, "emailtriage", "emailTriage", [])
    assert res["accepted"] == []
    assert resolve_agent_name(db, "emailtriage", "coordinator") is None


def test_persist_dedupes_input():
    db = _FakeDB([{"project": "emailtriage", "name": "emailTriage"}])
    res = persist_agent_aliases(db, "emailtriage", "emailTriage", ["coordinator", "coordinator"])
    assert res["accepted"] == ["coordinator"]


def test_persist_fails_loud_when_agent_not_registered():
    """PR #4 fix: an update that matches no doc must NOT silently report success.
    If the agent has no registered_agents doc, every alias is rejected."""
    db = _FakeDB([{"project": "emailtriage", "name": "emailTriage"}])
    res = persist_agent_aliases(db, "emailtriage", "ghost-agent", ["coordinator"])
    assert res["accepted"] == []
    assert "coordinator" in res["rejected"]
    assert "not registered" in res["rejected"]["coordinator"]
    # nothing was written — the alias does not resolve
    assert resolve_agent_name(db, "emailtriage", "coordinator") is None
