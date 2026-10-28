"""Unit tests for the POST /export-skills REST route decision logic
(_export_skills_payload in shared_memory.__main__).

The route itself is a thin Starlette adapter (parse JSON + read the Bearer
header → call the helper → JSONResponse). All the branching — body validation,
auth gating, tenant isolation, limit clamp, DB-outage fail-loud — lives in the
helper so it is testable without a live server. The tenant-check (403) only
fires when AUTH_ENABLED, which the keyless home server can't exercise via curl;
these tests are the only coverage of that security-relevant path.
"""

import asyncio

import pytest

import shared_memory.__main__ as m
import shared_memory.auth as auth
import shared_memory.tools.skills as sk


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_CANNED = {"project": "junto", "count": 1,
           "skills": [{"id": "skill_x", "name": "ready",
                       "relpath": "ready/SKILL.md", "content": "---\n..."}]}


@pytest.fixture
def stub(monkeypatch):
    """DB reachable + build_skill_export stubbed to a canned payload, so these
    tests exercise ONLY the route's own logic, not the export core (covered in
    test_skills.py)."""
    monkeypatch.setattr(m, "get_mongo", lambda: object())  # truthy → not 503
    monkeypatch.setattr(sk, "build_skill_export",
                        lambda project, **kw: {**_CANNED, "_kw": kw})
    # default: auth disabled (keyless home)
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)


# --- body validation (auth-independent) --------------------------------------
def test_non_dict_body_is_400(stub):
    status, payload = _run(m._export_skills_payload(["not", "a", "dict"], None))
    assert status == 400 and "error" in payload


def test_missing_project_is_400(stub):
    status, payload = _run(m._export_skills_payload({}, None))
    assert status == 400
    status2, _ = _run(m._export_skills_payload({"project": "   "}, None))
    assert status2 == 400  # whitespace-only project also rejected


def test_happy_path_keyless(stub):
    status, payload = _run(m._export_skills_payload(
        {"project": "junto", "role": "memory"}, None))
    assert status == 200
    assert payload["count"] == 1
    assert payload["skills"][0]["relpath"] == "ready/SKILL.md"


def test_limit_clamped_and_coerced(stub):
    # huge → clamped to 100
    _, p1 = _run(m._export_skills_payload({"project": "junto", "limit": 9999}, None))
    assert p1["_kw"]["limit"] == 100
    # garbage string → default 25
    _, p2 = _run(m._export_skills_payload({"project": "junto", "limit": "oops"}, None))
    assert p2["_kw"]["limit"] == 25
    # zero/negative → floored to 1
    _, p3 = _run(m._export_skills_payload({"project": "junto", "limit": 0}, None))
    assert p3["_kw"]["limit"] == 1


def test_db_outage_is_503_not_empty_200(stub, monkeypatch):
    monkeypatch.setattr(m, "get_mongo", lambda: None)
    status, payload = _run(m._export_skills_payload({"project": "junto"}, None))
    assert status == 503
    assert "skills" not in payload  # never a prunable empty success


def test_export_core_error_is_500(stub, monkeypatch):
    def boom(project, **kw):
        raise RuntimeError("mongo died mid-query")
    monkeypatch.setattr(sk, "build_skill_export", boom)
    status, payload = _run(m._export_skills_payload({"project": "junto"}, None))
    assert status == 500 and "error" in payload


# --- auth + tenant isolation (AUTH_ENABLED, Path B soft-auth) ----------------
@pytest.fixture
def authed(stub, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    # default gates OFF (home posture): keyless LAN soft-falls to agent tier.
    monkeypatch.setattr(auth, "REQUIRE_KEY", False)
    monkeypatch.setattr(auth, "TUNNEL_REQUIRES_KEY", False)
    # key "good" → access to junto; key "other" → only nimbus
    keys = {"good": {"name": "launcher", "role": "agent", "projects": ["junto"]},
            "wild": {"name": "root", "role": "admin", "projects": []},  # all-access
            "other": {"name": "nimbus-box", "role": "agent", "projects": ["nimbus"]}}
    monkeypatch.setattr(auth, "validate_api_key", lambda k: keys.get(k))


def test_soft_auth_keyless_lan_is_200(authed):
    """AUTH on + no key + gates off (home) → soft-fall to agent tier, allowed.
    This is the keyless-home launcher path — the whole reason the endpoint
    exists — and the case my first cut wrongly hard-401'd."""
    status, payload = _run(m._export_skills_payload({"project": "junto"}, None))
    assert status == 200 and payload["count"] == 1


def test_keyless_rejected_when_require_key(authed, monkeypatch):
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    status, payload = _run(m._export_skills_payload({"project": "junto"}, None))
    assert status == 401 and payload.get("auth_required")


def test_keyless_rejected_via_tunnel_when_gated(authed, monkeypatch):
    monkeypatch.setattr(auth, "TUNNEL_REQUIRES_KEY", True)
    status, payload = _run(m._export_skills_payload({"project": "junto"}, None, via_tunnel=True))
    assert status == 401 and payload.get("auth_required")
    # same tunnel request WITH a valid key is fine
    status2, _ = _run(m._export_skills_payload({"project": "junto"}, "good", via_tunnel=True))
    assert status2 == 200


def test_auth_invalid_key_is_401(authed):
    status, payload = _run(m._export_skills_payload({"project": "junto"}, "nope"))
    assert status == 401


def test_auth_wrong_tenant_is_403(authed):
    status, payload = _run(m._export_skills_payload({"project": "junto"}, "other"))
    assert status == 403
    assert payload["allowed_projects"] == ["nimbus"]


def test_auth_valid_tenant_is_200(authed):
    status, payload = _run(m._export_skills_payload({"project": "junto"}, "good"))
    assert status == 200 and payload["count"] == 1


def test_auth_wildcard_key_all_projects_is_200(authed):
    status, payload = _run(m._export_skills_payload({"project": "junto"}, "wild"))
    assert status == 200  # empty projects list = all-access
