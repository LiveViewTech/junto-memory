"""Per-run reporter sessions replace their predecessors (helpers.replace_ephemeral_sessions)."""
from shared_memory import helpers
from shared_memory.state import active_sessions


def _add(sid, project, instance):
    active_sessions[sid] = {"project": project, "claude_instance": instance, "last_activity": helpers.utc_now_iso()}


def test_reporter_sessions_replace_older_ones_only():
    for sid in list(active_sessions):
        if sid.startswith("t_eph_"):
            active_sessions.pop(sid)
    _add("t_eph_1", "cerebro", "ctx-reporter-xavier")
    _add("t_eph_2", "cerebro", "ctx-reporter-xavier")
    _add("t_eph_3", "cerebro", "ctx-reporter-sage")
    _add("t_eph_4", "cerebro", "madrox")
    assert helpers.replace_ephemeral_sessions("cerebro", "ctx-reporter-xavier") == 2
    assert "t_eph_1" not in active_sessions and "t_eph_2" not in active_sessions
    assert "t_eph_3" in active_sessions and "t_eph_4" in active_sessions
    assert helpers.replace_ephemeral_sessions("cerebro", "madrox") == 0
    for sid in ("t_eph_3", "t_eph_4"):
        active_sessions.pop(sid, None)
