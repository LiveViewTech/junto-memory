"""Unit tests for the POST /recall REST route decision logic
(_recall_payload in shared_memory.__main__), interface:recall-v0 v1.1.2.

Same testing shape as test_export_skills_route.py: the Starlette route is a
thin adapter; all branching — validation, soft-auth gating, floor resolution,
chroma fail-loud vs mongo best-effort, headers-only shaping — lives in the
helper so it is testable without a live server.
"""

import asyncio

import pytest

import shared_memory.__main__ as m
import shared_memory.auth as auth
import shared_memory.helpers as helpers
import shared_memory.query_config as qc


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeCollection:
    """Chroma-shaped .query() stub. rows = list of (id, meta, distance, doc)."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.seen_where = None
        self.seen_n = None

    async def query(self, query_texts, n_results, where=None):
        self.seen_where = where
        self.seen_n = n_results
        rows = self.rows[:n_results]
        return {
            "ids": [[r[0] for r in rows]],
            "metadatas": [[r[1] for r in rows]],
            "distances": [[r[2] for r in rows]],
            "documents": [[r[3] for r in rows]],
        }


def _meta(**kw):
    base = {"type": "learning", "title": "T", "status": "active",
            "updated": "2026-07-27T00:00:00+00:00", "claude_instance": "memory"}
    base.update(kw)
    return base


@pytest.fixture
def stub(monkeypatch):
    """Keyless-auth default, chroma up with one strong project hit, empty
    shared collections, mongo up but claim-less."""
    proj = FakeCollection([
        # score = 1 - d/2 → 0.8
        ("learning_aaaaaaaaaaaaaaaa", _meta(title="strong hit"), 0.4,
         "Body of the strong hit. " * 20),
        # score 0.5 — below the 0.6 floor, must be filtered
        ("learning_bbbbbbbbbbbbbbbb", _meta(title="weak hit"), 1.0, "weak body"),
    ])
    shared = FakeCollection()
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    monkeypatch.setattr(m, "get_chroma", _async_return(object()))
    monkeypatch.setattr(m, "get_mongo", lambda: object())
    monkeypatch.setattr(helpers, "get_project_collection", _async_return(proj))
    monkeypatch.setattr(helpers, "get_shared_collection", _async_return(shared))
    monkeypatch.setattr(qc, "get_effective_config",
                        lambda db, project=None: {"recall_floor": 0.6})
    import shared_memory.facets as facets
    monkeypatch.setattr(facets, "get_facets_for_ids", lambda db, ids: {})
    return {"proj": proj, "shared": shared}


def _async_return(value):
    async def _f(*a, **kw):
        return value
    return _f


# --- validation ---------------------------------------------------------------
def test_non_dict_body_is_400(stub):
    status, payload = _run(m._recall_payload("nope", None))
    assert status == 400


def test_missing_project_and_query_are_400(stub):
    assert _run(m._recall_payload({"query": "x"}, None))[0] == 400
    assert _run(m._recall_payload({"project": "junto"}, None))[0] == 400
    assert _run(m._recall_payload({"project": "junto", "query": "  "}, None))[0] == 400


def test_bad_scope_threshold_k_are_400(stub):
    base = {"project": "junto", "query": "x"}
    assert _run(m._recall_payload({**base, "scope": "everything"}, None))[0] == 400
    assert _run(m._recall_payload({**base, "threshold": "hot"}, None))[0] == 400
    assert _run(m._recall_payload({**base, "threshold": 1.5}, None))[0] == 400
    assert _run(m._recall_payload({**base, "k": "many"}, None))[0] == 400


# --- happy path ---------------------------------------------------------------
def test_headers_only_floor_filtering_and_shape(stub):
    status, payload = _run(m._recall_payload(
        {"project": "junto", "query": "strong"}, None))
    assert status == 200
    assert payload["count"] == 1  # weak hit (0.5) filtered by 0.6 floor
    assert payload["floor"] == 0.6
    snip = payload["snippets"][0]
    assert snip["id"] == "learning_aaaaaaaaaaaaaaaa"
    assert snip["score"] == 0.8
    assert snip["authored_by"] == "memory"
    assert "content" not in snip and "_content" not in snip  # HEADERS ONLY
    assert len(snip["one_line"]) <= 120
    assert "took_ms" in payload and "embed_ms" in payload


def test_claim_facet_preferred_for_one_line(stub, monkeypatch):
    import shared_memory.facets as facets
    monkeypatch.setattr(
        facets, "get_facets_for_ids",
        lambda db, ids: {"learning_aaaaaaaaaaaaaaaa": {"claim": "The claim."}})
    _, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert payload["snippets"][0]["one_line"] == "The claim."


def test_explicit_threshold_overrides_config(stub):
    _, payload = _run(m._recall_payload(
        {"project": "junto", "query": "x", "threshold": 0.4}, None))
    assert payload["floor"] == 0.4
    assert payload["count"] == 2  # weak hit (0.5) now clears the floor


def test_k_clamped_and_passed(stub):
    _run(m._recall_payload({"project": "junto", "query": "x", "k": 99}, None))
    assert stub["proj"].seen_n == 15
    _run(m._recall_payload({"project": "junto", "query": "x", "k": 0}, None))
    assert stub["proj"].seen_n == 1


def test_scope_filter_uses_explicit_and(stub):
    _run(m._recall_payload(
        {"project": "junto", "query": "x", "scope": "learnings"}, None))
    assert stub["proj"].seen_where == {
        "$and": [{"status": "active"}, {"type": {"$in": ["learning"]}}]}
    _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert stub["proj"].seen_where == {"status": "active"}  # single condition: no $and


# --- outage semantics ---------------------------------------------------------
def test_chroma_outage_is_503_never_count0(stub, monkeypatch):
    async def _boom():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(m, "get_chroma", _boom)
    status, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert status == 503
    assert "count" not in payload


def test_query_failure_is_500(stub, monkeypatch):
    class Exploding:
        async def query(self, **kw):
            raise RuntimeError("mid-query loss")
    monkeypatch.setattr(helpers, "get_project_collection",
                        _async_return(Exploding()))
    status, _ = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert status == 500


def test_mongo_down_degrades_but_still_200(stub, monkeypatch):
    monkeypatch.setattr(m, "get_mongo", lambda: None)
    import shared_memory.facets as facets
    monkeypatch.setattr(facets, "get_facets_for_ids",
                        lambda db, ids: (_ for _ in ()).throw(RuntimeError()))
    status, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert status == 200
    assert payload["count"] == 1
    assert payload["snippets"][0]["one_line"].startswith("Body of the strong hit.")


# --- auth gating --------------------------------------------------------------
def test_invalid_key_is_401(stub, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "validate_api_key", lambda k: None)
    status, _ = _run(m._recall_payload({"project": "junto", "query": "x"}, "smk_bad"))
    assert status == 401


def test_valid_key_wrong_project_is_403(stub, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "validate_api_key",
                        lambda k: {"projects": ["nimbus"]})
    monkeypatch.setattr(auth, "check_project_access", lambda allowed, p: False)
    status, _ = _run(m._recall_payload({"project": "junto", "query": "x"}, "smk_ok"))
    assert status == 403


def test_keyless_rejected_when_require_key(stub, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    status, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert status == 401 and payload.get("auth_required")


# --- expiry -------------------------------------------------------------------
def test_expired_docs_skipped(stub, monkeypatch):
    monkeypatch.setattr(helpers, "is_expired", lambda meta: True)
    status, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert status == 200 and payload["count"] == 0


# --- stale-handoff age-gate ---------------------------------------------------
def test_stale_handoffs_filtered_fresh_kept(stub, monkeypatch):
    from datetime import timedelta
    import shared_memory.helpers as h
    now = h.utc_now()
    fresh = (now - timedelta(days=3)).isoformat()
    stale = (now - timedelta(days=45)).isoformat()
    stub["proj"].rows = [
        ("handoff_fresh", _meta(type="handoff", title="fresh handoff", updated=fresh), 0.4, "f"),
        ("handoff_stale", _meta(type="handoff", title="stale handoff", updated=stale), 0.4, "s"),
        ("handoff_undated", _meta(type="handoff", title="undated", updated="", created=""), 0.4, "u"),
        ("learning_old_fine", _meta(title="old learning ok", updated=stale), 0.4, "l"),
    ]
    _, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    got = [s["id"] for s in payload["snippets"]]
    assert "handoff_fresh" in got
    assert "handoff_stale" not in got
    assert "handoff_undated" not in got
    assert "learning_old_fine" in got  # age-gate is handoff-only


# --- type-aware exclusions (nimbus first-day read, msg_53e467692779) ----------
def test_state_specs_always_excluded(stub):
    stub["proj"].rows = [
        ("spec_state_coordinator", _meta(type="spec", title="state spec"), 0.2, "s"),
        ("spec_interface_ble", _meta(type="spec", title="live contract"), 0.4, "c"),
    ]
    _, payload = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    got = [s["id"] for s in payload["snippets"]]
    assert "spec_state_coordinator" not in got  # even at score 0.9
    assert "spec_interface_ble" in got          # real specs stay


def test_self_echo_suppressed_only_with_agent(stub):
    from datetime import timedelta
    import shared_memory.helpers as h
    recent = (h.utc_now() - timedelta(hours=2)).isoformat()
    old = (h.utc_now() - timedelta(days=10)).isoformat()
    stub["proj"].rows = [
        ("learning_own_fresh", _meta(claude_instance="coord", updated=recent), 0.4, "a"),
        ("learning_own_old", _meta(claude_instance="coord", updated=old), 0.4, "b"),
        ("learning_other_fresh", _meta(claude_instance="peer", updated=recent), 0.4, "c"),
    ]
    _, payload = _run(m._recall_payload(
        {"project": "junto", "query": "x", "agent": "coord"}, None))
    got = [s["id"] for s in payload["snippets"]]
    assert "learning_own_fresh" not in got   # self-echo within 48h
    assert "learning_own_old" in got         # old own-docs still surface
    assert "learning_other_fresh" in got     # peers' fresh docs unaffected
    # No agent supplied -> no self-echo gate
    _, payload2 = _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert "learning_own_fresh" in [s["id"] for s in payload2["snippets"]]


def test_recall_event_logged_on_hits_only(stub, monkeypatch):
    import shared_memory.recall_metrics as rm
    logged = []
    monkeypatch.setattr(rm, "log_recall_event",
                        lambda db, project, agent, ids, floor: logged.append((project, agent, ids)))
    _run(m._recall_payload({"project": "junto", "query": "x", "agent": "coord"}, None))
    assert logged and logged[0][1] == "coord" and logged[0][2] == ["learning_aaaaaaaaaaaaaaaa"]
    logged.clear()
    stub["proj"].rows = []
    _run(m._recall_payload({"project": "junto", "query": "x"}, None))
    assert not logged  # count=0 -> no event row
