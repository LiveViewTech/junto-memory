"""Tests for the skill registry (design:skill-registry-v0, authoring layer).

Covers the trust-critical behaviors: register always emits draft, confirm is
the owner/human gate, a content edit to an active skill reverts it to draft,
metadata-only edits preserve active status, scope filtering, and ranking.
"""

import asyncio

import pytest

import shared_memory.tools.skills as sk
from shared_memory.state import active_sessions


# --- in-memory Mongo fake (flat-equality queries are all skills.py uses) -----
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def limit(self, n):
        return iter(self._docs[:n])

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self):
        self.docs = {}

    def _match(self, d, query):
        return all(d.get(k) == v for k, v in query.items())

    def insert_one(self, doc):
        if doc["_id"] in self.docs:
            raise Exception("duplicate _id")
        self.docs[doc["_id"]] = dict(doc)

    def find_one(self, query):
        for d in self.docs.values():
            if self._match(d, query):
                return dict(d)
        return None

    def update_one(self, query, update):
        for d in self.docs.values():
            if self._match(d, query):
                d.update(update.get("$set", {}))
                return
        raise AssertionError(f"update_one matched nothing: {query}")

    def find(self, query):
        return _FakeCursor([dict(d) for d in self.docs.values() if self._match(d, query)])


class _FakeDB:
    def __init__(self):
        self.skills = _FakeCollection()


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr(sk, "get_mongo", lambda: fake)
    # sessions: owner agent, another agent, a human operator
    active_sessions["s_owner"] = {"claude_instance": "memory", "project": "junto", "role": "agent"}
    active_sessions["s_other"] = {"claude_instance": "other", "project": "junto", "role": "agent"}
    active_sessions["s_human"] = {"claude_instance": "tom", "project": "junto", "role": "user"}
    yield fake
    for s in ("s_owner", "s_other", "s_human"):
        active_sessions.pop(s, None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reg(session="s_owner", **kw):
    args = dict(name="run-eval-gate", trigger="when shipping an accuracy-affecting change",
                steps="1. activate venv\n2. python run_eval.py", project="junto")
    args.update(kw)
    import json
    return json.loads(_run(sk.memory_register_skill(session_id=session, **args)))


# --- tests -------------------------------------------------------------------
def test_register_emits_draft(db):
    r = _reg()
    assert r["status"] == "registered"
    assert r["skill_status"] == "draft"
    doc = db.skills.find_one({"_id": r["id"]})
    assert doc["owner"] == "memory"
    assert doc["status"] == "draft"
    assert doc["confirmed_by"] is None
    assert doc["content_hash"]
    assert doc["scope"]["project"] == "junto"


def test_confirm_by_owner_activates(db):
    import json
    r = _reg()
    c = json.loads(_run(sk.memory_confirm_skill(session_id="s_owner", name_or_id=r["id"])))
    assert c["status"] == "confirmed"
    assert c["skill_status"] == "active"
    assert db.skills.find_one({"_id": r["id"]})["status"] == "active"


def test_confirm_by_name_and_human(db):
    """A human operator can confirm another agent's skill; resolve by name."""
    import json
    _reg()
    c = json.loads(_run(sk.memory_confirm_skill(
        session_id="s_human", name_or_id="run-eval-gate", project="junto")))
    assert c["skill_status"] == "active"
    assert c["confirmed_by"] == "tom"


def test_non_owner_agent_cannot_confirm(db):
    import json
    r = _reg()
    c = json.loads(_run(sk.memory_confirm_skill(session_id="s_other", name_or_id=r["id"])))
    assert "error" in c
    assert db.skills.find_one({"_id": r["id"]})["status"] == "draft"


def test_content_edit_reverts_active_to_draft(db):
    import json
    r = _reg()
    _run(sk.memory_confirm_skill(session_id="s_owner", name_or_id=r["id"]))
    assert db.skills.find_one({"_id": r["id"]})["status"] == "active"
    # edit the steps body
    r2 = _reg(steps="1. activate venv\n2. python run_eval.py --strict")
    assert r2["status"] == "updated"
    assert r2["skill_status"] == "draft"
    assert r2["version"] == "1.0.1"  # response reports the BUMPED version
    doc = db.skills.find_one({"_id": r["id"]})
    assert doc["status"] == "draft"
    assert doc["confirmed_by"] is None
    assert doc["version"] == "1.0.1"  # patch bumped on body change


def test_metadata_edit_preserves_active(db):
    """Changing tags only (same body) must NOT un-confirm an active skill."""
    r = _reg()
    _run(sk.memory_confirm_skill(session_id="s_owner", name_or_id=r["id"]))
    r2 = _reg(tags=["eval", "qa"])  # body identical → content_hash unchanged
    assert r2["skill_status"] == "active"
    assert r2["version"] == "1.0.0"  # response reports unchanged version
    doc = db.skills.find_one({"_id": r["id"]})
    assert doc["status"] == "active"
    assert doc["version"] == "1.0.0"  # no bump
    assert doc["tags"] == ["eval", "qa"]


def test_trigger_length_guard(db):
    r = _reg(trigger="x" * 121)
    assert "error" in r
    assert "too long" in r["error"]


def test_list_scope_and_ranking(db):
    import json
    # role-scoped draft, dir-scoped active+pinned, unscoped draft
    _reg(name="a-unscoped")
    _reg(name="b-role", role="et-engine")
    _reg(name="c-dir", directory="eval/")
    _run(sk.memory_confirm_skill(session_id="s_owner", name_or_id="c-dir", project="junto"))
    _run(sk.memory_pin_skill(session_id="s_owner", name_or_id="c-dir", project="junto"))

    # unfiltered: pinned-active c-dir first
    out = json.loads(_run(sk.memory_list_skills(session_id="s_owner", project="junto")))
    assert out["count"] == 3
    assert out["skills"][0]["name"] == "c-dir"
    assert out["skills"][0]["pin"] is True

    # role filter: et-engine returns role-agnostic + et-engine, excludes none here
    out_role = json.loads(_run(sk.memory_list_skills(
        session_id="s_owner", project="junto", role="et-engine")))
    names = {s["name"] for s in out_role["skills"]}
    assert "b-role" in names and "a-unscoped" in names

    # role filter for a different role excludes the et-engine-scoped one
    out_other = json.loads(_run(sk.memory_list_skills(
        session_id="s_owner", project="junto", role="et-qa")))
    names_other = {s["name"] for s in out_other["skills"]}
    assert "b-role" not in names_other
    assert "a-unscoped" in names_other

    # status filter
    out_active = json.loads(_run(sk.memory_list_skills(
        session_id="s_owner", project="junto", status="active")))
    assert {s["name"] for s in out_active["skills"]} == {"c-dir"}


def test_get_skill_by_id_and_name(db):
    import json
    r = _reg()
    by_id = json.loads(_run(sk.memory_get_skill(session_id="s_owner", name_or_id=r["id"])))
    assert by_id["name"] == "run-eval-gate"
    assert "steps" in by_id and "_id" not in by_id
    by_name = json.loads(_run(sk.memory_get_skill(
        session_id="s_owner", name_or_id="run-eval-gate", project="junto")))
    assert by_name["skill_id"] == r["id"]


def test_pin_requires_owner(db):
    import json
    r = _reg()
    bad = json.loads(_run(sk.memory_pin_skill(session_id="s_other", name_or_id=r["id"])))
    assert "error" in bad
    assert db.skills.find_one({"_id": r["id"]})["pin"] is False


# --- Phase-1 surfacing helper -----------------------------------------------
def _activate(name):
    _run(sk.memory_confirm_skill(session_id="s_owner", name_or_id=name, project="junto"))


def test_surfacing_only_active_skills(db):
    _reg(name="active-one")
    _activate("active-one")
    _reg(name="still-draft")  # never confirmed
    out = sk.get_scope_matched_skills("junto")
    names = {s["name"] for s in out}
    assert names == {"active-one"}
    # headers only
    assert set(out[0].keys()) == {"id", "name", "trigger"}


def test_surfacing_project_scope(db):
    _reg(name="ours")
    _activate("ours")
    assert sk.get_scope_matched_skills("other_project") == []


def test_surfacing_directory_scope(db):
    _reg(name="eval-only", directory="eval")
    _activate("eval-only")
    _reg(name="anywhere")
    _activate("anywhere")
    # working dir inside eval/ → both surface
    in_eval = {s["name"] for s in sk.get_scope_matched_skills(
        "junto", working_directory="/home/et/engine/eval/run.py")}
    assert in_eval == {"eval-only", "anywhere"}
    # working dir elsewhere → directory-scoped one excluded
    elsewhere = {s["name"] for s in sk.get_scope_matched_skills(
        "junto", working_directory="/home/et/web/app.py")}
    assert elsewhere == {"anywhere"}
    # unknown working dir → permissive (directory-scoped still shown)
    unknown = {s["name"] for s in sk.get_scope_matched_skills("junto")}
    assert unknown == {"eval-only", "anywhere"}


def test_surfacing_role_scope(db):
    _reg(name="engine-only", role="et-engine")
    _activate("engine-only")
    _reg(name="all-roles")
    _activate("all-roles")
    # match by instance name
    by_instance = {s["name"] for s in sk.get_scope_matched_skills(
        "junto", claude_instance="et-engine")}
    assert by_instance == {"engine-only", "all-roles"}
    # match by role_description substring
    by_desc = {s["name"] for s in sk.get_scope_matched_skills(
        "junto", role_description="the et-engine eval runner")}
    assert by_desc == {"engine-only", "all-roles"}
    # no role context → role-scoped excluded
    none_ctx = {s["name"] for s in sk.get_scope_matched_skills("junto")}
    assert none_ctx == {"all-roles"}


def test_surfacing_pin_ranks_first(db):
    _reg(name="plain")
    _activate("plain")
    _reg(name="pinned")
    _activate("pinned")
    _run(sk.memory_pin_skill(session_id="s_owner", name_or_id="pinned", project="junto"))
    out = sk.get_scope_matched_skills("junto")
    assert out[0]["name"] == "pinned"


# --- Phase-2 SKILL.md export -------------------------------------------------
def test_render_skill_md_shape(db):
    r = _reg(name="run-eval-gate", preconditions="Mongo up; gold CSVs present",
             gotchas="run_eval.py hard-exits on a missing gold CSV")
    doc = db.skills.find_one({"_id": r["id"]})
    md = sk.render_skill_md(doc)
    # YAML frontmatter with name + description (CC matcher reads these)
    assert md.startswith("---\n")
    assert "name: " in md and "description: " in md
    assert md.count("---") >= 2  # frontmatter delimiters
    # body sections present
    assert "## Preconditions" in md
    assert "## Steps" in md
    assert "## Gotchas" in md
    # description is single-line (trigger flattened) — no newline inside the value
    desc_line = [l for l in md.splitlines() if l.startswith("description: ")][0]
    assert "\\n" not in desc_line


def test_export_only_active_and_payload_shape(db):
    import json
    _reg(name="ready")
    _activate("ready")
    _reg(name="draft-skill")  # not confirmed → must not export
    out = json.loads(_run(sk.memory_export_skills(session_id="s_owner", project="junto")))
    assert out["count"] == 1
    item = out["skills"][0]
    assert item["name"] == "ready"
    assert item["relpath"] == "ready/SKILL.md"
    assert item["content"].startswith("---\n")
    assert "junto skill" in item["content"]  # provenance footer


def test_build_skill_export_shape_and_normalizes_project(db):
    """The shared core (used by BOTH the MCP tool and the REST route) returns
    the exact wire dict and normalizes the project name."""
    _reg(name="ready")
    _activate("ready")
    _reg(name="draft-skill")  # not confirmed → excluded
    result = sk.build_skill_export("JUNTO", role="memory")  # denormalized in
    assert result["project"] == "junto"                     # normalized out
    assert result["count"] == 1
    assert set(result["skills"][0].keys()) == {"id", "name", "relpath", "content"}
    assert result["skills"][0]["relpath"] == "ready/SKILL.md"


def test_build_skill_export_fail_loud_on_db_outage(db, monkeypatch):
    """A DB outage must RAISE, not return an empty export — the launcher prunes
    by footer, so a silent count:0 would wipe every materialized skill."""
    monkeypatch.setattr(sk, "get_mongo", lambda: None)
    with pytest.raises(Exception):
        sk.build_skill_export("junto", role="memory")


def test_export_tool_fail_loud_on_db_outage(db, monkeypatch):
    """The MCP tool surfaces the outage as an explicit error, never count:0."""
    import json
    monkeypatch.setattr(sk, "get_mongo", lambda: None)
    out = json.loads(_run(sk.memory_export_skills(session_id="s_owner", project="junto")))
    assert "error" in out
    assert "count" not in out


def test_scope_matcher_strict_raises_but_besteffort_swallows(db, monkeypatch):
    """Phase-1 surfacing (strict=False) keeps returning [] on outage so it can
    never break session start; the export path (strict=True) raises."""
    monkeypatch.setattr(sk, "get_mongo", lambda: None)
    assert sk._active_scope_matched_docs("junto") == []          # best-effort
    with pytest.raises(Exception):
        sk._active_scope_matched_docs("junto", strict=True)      # fail-loud
