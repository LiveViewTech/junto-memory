"""(A) synthetic zero-row for memory_get_emission_stats (design:autopilot-removal-v0 §5).

When an explicit agent= + project= query matches no current-hour emission row
(the idle statusline case), the tool synthesizes a zero-row carrying the
project-resolved caps + a REAL suspended flag, instead of returning []. The
suspended flag must be resolved from the suspension store, never hardcoded
false — a suspended agent stops sending and falls to 0 emissions, so the idle
case INCLUDES suspended agents, and blanking suspended would hide a live
suspension on the operator's chip (the bug inbox and I caught in review).
"""

import json


def _setup(monkeypatch, snapshot, suspended=False, config=None):
    from shared_memory import push_control
    from shared_memory.state import active_sessions
    from shared_memory.tools import push_control_api as pca

    sid = "test-emission-zero"
    active_sessions[sid] = {
        "role": "agent", "claude_instance": "memory", "project": "junto",
    }
    monkeypatch.setattr(pca, "get_mongo", lambda: object())  # non-None sentinel
    monkeypatch.setattr(
        push_control, "snapshot_emission_counters", lambda: list(snapshot)
    )
    monkeypatch.setattr(
        push_control, "get_effective_config",
        lambda db, project=None: dict(
            config or {"depth_cap": 12, "push_budget": 30, "hard_ceiling": 100}
        ),
    )
    monkeypatch.setattr(
        push_control, "is_agent_suspended", lambda db, project, agent: suspended
    )
    monkeypatch.setattr(push_control, "_current_hour_bucket", lambda: "2026-06-11T11")
    return sid, pca


async def test_idle_agent_gets_synthetic_zero_row_with_caps(monkeypatch):
    sid, pca = _setup(monkeypatch, snapshot=[])  # no current-hour rows
    res = json.loads(
        await pca.memory_get_emission_stats(session_id=sid, agent="inbox", project="junto")
    )
    assert res["count"] == 1
    row = res["stats"][0]
    assert row["instance"] == "inbox"
    assert row["project"] == "junto"
    assert row["count"] == 0
    assert (row["depth_cap"], row["push_budget"], row["hard_ceiling"]) == (12, 30, 100)
    assert row["suspended"] is False
    assert row["over_push_budget"] is False
    assert row["over_hard_ceiling"] is False


async def test_idle_suspended_agent_shows_suspended_true(monkeypatch):
    # The bug-catch case: a suspended agent sits at 0 current-hour emissions;
    # the synthetic row MUST surface suspended=True, not blank it.
    sid, pca = _setup(monkeypatch, snapshot=[], suspended=True)
    res = json.loads(
        await pca.memory_get_emission_stats(session_id=sid, agent="inbox", project="junto")
    )
    assert res["count"] == 1
    assert res["stats"][0]["suspended"] is True
    assert res["stats"][0]["count"] == 0


async def test_no_synthesis_without_explicit_project(monkeypatch):
    # config is project-scoped; an agent-only idle query can't resolve which
    # project's caps to synthesize, so it must NOT fabricate a row.
    sid, pca = _setup(monkeypatch, snapshot=[])
    res = json.loads(
        await pca.memory_get_emission_stats(session_id=sid, agent="inbox")
    )
    assert res["count"] == 0
    assert res["stats"] == []


async def test_real_current_hour_row_suppresses_synthesis(monkeypatch):
    # When the agent HAS a current-hour emission row, use it (real count), no
    # synthetic zero-row.
    snapshot = [{"instance": "inbox", "project": "junto", "hour": "2026-06-11T11", "count": 7}]
    sid, pca = _setup(monkeypatch, snapshot=snapshot)
    res = json.loads(
        await pca.memory_get_emission_stats(session_id=sid, agent="inbox", project="junto")
    )
    assert res["count"] == 1
    assert res["stats"][0]["count"] == 7
    assert res["stats"][0]["push_budget"] == 30
