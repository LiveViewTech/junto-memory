"""Tests for the idle→park nudge timer (shared_memory.idle_park)."""

import pytest
from mcp import types as _mcp_types

from shared_memory import idle_park
from shared_memory.idle_park import (
    REARM_GRACE_SECONDS,
    build_call_tool_handler_with_idle_park,
    due_agents,
    mark_nudged,
    note_tool_call,
)
from shared_memory.state import active_sessions

KEY = ("junto", "memory")


@pytest.fixture(autouse=True)
def _sessions():
    idle_park._timers.clear()
    idle_park._parked.clear()
    active_sessions["s_agent"] = {"project": "junto", "claude_instance": "memory"}
    active_sessions["s_plugin"] = {"project": "junto", "claude_instance": "memory",
                                   "task_description": "junto-inbox channel plugin"}
    yield
    idle_park._timers.clear()
    idle_park._parked.clear()
    active_sessions.pop("s_agent", None)
    active_sessions.pop("s_plugin", None)


def _fifty(_p, _a):
    return 50


def test_real_call_arms_one_timer_per_agent_name():
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_send_message", "s_plugin", now=10)
    assert list(idle_park._timers) == [KEY]
    assert idle_park._timers[KEY]["last_real"] == 10


def test_background_tools_never_arm_or_reset():
    for tool in idle_park.BACKGROUND_TOOLS:
        note_tool_call(tool, "s_plugin", now=0)
    assert idle_park._timers == {}
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_heartbeat", "s_plugin", now=2000)
    note_tool_call("memory_report_context", "s_agent", now=2500)
    assert idle_park._timers[KEY]["last_real"] == 0


def test_unknown_session_ignored():
    note_tool_call("memory_query", "nope", now=0)
    note_tool_call("memory_query", None, now=0)
    assert idle_park._timers == {}


def test_due_after_threshold_and_fires_once():
    note_tool_call("memory_query", "s_agent", now=0)
    assert due_agents(_fifty, now=49 * 60) == []
    assert due_agents(_fifty, now=50 * 60) == [("junto", "memory", 50)]
    mark_nudged("junto", "memory", now=50 * 60)
    assert due_agents(_fifty, now=500 * 60) == []


def test_reply_turn_within_grace_does_not_rearm_but_later_activity_does():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_get_messages", "s_agent", now=3000 + 30)
    assert idle_park._timers[KEY]["nudged_at"] == 3000
    note_tool_call("memory_query", "s_agent", now=3000 + REARM_GRACE_SECONDS + 1)
    assert idle_park._timers[KEY]["nudged_at"] is None
    assert due_agents(_fifty, now=3000 + REARM_GRACE_SECONDS + 1 + 50 * 60) != []


def test_end_session_disarms():
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_end_session", "s_agent", now=5)
    assert idle_park._timers == {}
    assert due_agents(_fifty, now=10_000) == []


def test_opt_out_and_disabled_threshold_skip():
    note_tool_call("memory_query", "s_agent", now=0)
    assert due_agents(lambda p, a: 0, now=10_000) == []


def test_threshold_from_config_optout(monkeypatch):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_minutes": 40, "idle_park_optout": "nimbusSupport, memory"})
    assert idle_park._threshold_from_config(None, "junto", "memory") == 0
    assert idle_park._threshold_from_config(None, "junto", "other") == 40


class _FakeMessages:
    def __init__(self):
        self.docs = []

    def insert_one(self, doc):
        self.docs.append(doc)


class _FakeDb:
    def __init__(self):
        self.messages = _FakeMessages()


async def test_sweep_sends_injecting_system_notice(monkeypatch):
    from shared_memory.tools import messaging

    pushed = []

    async def fake_notify(project, agent, packet=None):
        pushed.append((project, agent, packet))

    monkeypatch.setattr(messaging, "_notify_inbox_for_send", fake_notify)
    monkeypatch.setattr(idle_park, "_threshold_from_config", lambda db, p, a: 50)
    note_tool_call("memory_query", "s_agent", now=0)
    idle_park._timers[KEY]["last_real"] -= 10_000_000  # long idle in monotonic terms

    db = _FakeDb()
    assert await idle_park.sweep_once(db) == 1
    assert await idle_park.sweep_once(db) == 0  # once per idle period
    doc = db.messages.docs[0]
    assert doc["to_instance"] == "memory" and doc["system_notice_kind"] == "idle_park_nudge"
    assert pushed[0][2]["mode"] == "inject"


async def test_wrapper_notes_then_delegates():
    seen = []

    async def inner(req):
        seen.append(req.params.name)
        return "ok"

    handler = build_call_tool_handler_with_idle_park(inner)
    req = _mcp_types.CallToolRequest(
        method="tools/call",
        params=_mcp_types.CallToolRequestParams(name="memory_query", arguments={"session_id": "s_agent"}),
    )
    assert await handler(req) == "ok"
    assert seen == ["memory_query"] and KEY in idle_park._timers


class _FakeStatus:
    def __init__(self, tokens):
        self.tokens = tokens

    def find_one(self, query, projection=None):
        return None if self.tokens is None else {"instance": query["instance"], "context_tokens": self.tokens}


class _StatusDb:
    def __init__(self, tokens):
        self.agent_status = _FakeStatus(tokens)


@pytest.mark.parametrize("tokens,expected", [(150_000, 0), (200_000, 50), (600_000, 50), (None, 50)])
def test_min_context_gate(monkeypatch, tokens, expected):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_minutes": 50, "idle_park_min_context": 200_000})
    assert idle_park._threshold_from_config(_StatusDb(tokens), "junto", "memory") == expected


# --- Phase 2: auto-reset (design:idle-park-autoreset-v0) -------------------------

class _FakeResult:
    def __init__(self, matched, upserted_id=None):
        self.matched_count = matched
        self.upserted_id = upserted_id


class _FakeResets:
    """Just enough of a pymongo collection for idle_park's reset queries."""

    def __init__(self):
        self.docs = []

    @staticmethod
    def _match(doc, flt):
        for k, cond in flt.items():
            v = doc.get(k)
            if isinstance(cond, dict):
                if "$in" in cond and v not in cond["$in"]:
                    return False
                if "$lt" in cond and not (v is not None and v < cond["$lt"]):
                    return False
            elif v != cond:
                return False
        return True

    @staticmethod
    def _apply(doc, update):
        doc.update(update.get("$set", {}))
        for k, n in update.get("$inc", {}).items():
            doc[k] = doc.get(k, 0) + n

    def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, flt):
                self._apply(doc, update)
                return _FakeResult(1)
        if upsert:
            doc = dict(update.get("$setOnInsert", {}))
            self._apply(doc, update)
            self.docs.append(doc)
            return _FakeResult(0, doc["_id"])
        return _FakeResult(0)

    def update_many(self, flt, update):
        n = 0
        for doc in self.docs:
            if self._match(doc, flt):
                self._apply(doc, update)
                n += 1
        return _FakeResult(n)

    def find(self, flt):
        return [d for d in self.docs if self._match(d, flt)]

    def find_one(self, flt):
        found = self.find(flt)
        return found[0] if found else None

    def count_documents(self, flt):
        return len(self.find(flt))


class _ResetsDb:
    def __init__(self):
        self.idle_park_resets = _FakeResets()


@pytest.fixture
def scratch_db():
    idle_park._parked_after_nudge.clear()
    yield _ResetsDb()
    idle_park._parked_after_nudge.clear()


def _autoreset(monkeypatch, agents):
    from shared_memory import push_control

    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_autoreset": agents})


def _nudged_park():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_end_session", "s_agent", now=3100)


def test_park_without_nudge_queues_nothing():
    idle_park._parked_after_nudge.clear()
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_end_session", "s_agent", now=5)
    assert list(idle_park._parked_after_nudge) == []


def test_park_after_rearm_queues_nothing():
    idle_park._parked_after_nudge.clear()
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    note_tool_call("memory_query", "s_agent", now=3000 + REARM_GRACE_SECONDS + 1)
    note_tool_call("memory_end_session", "s_agent", now=3000 + REARM_GRACE_SECONDS + 2)
    assert list(idle_park._parked_after_nudge) == []


def test_nudged_park_opted_in_goes_pending_once(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "nimbusSupport, memory")
    _nudged_park()
    _nudged_park()  # a second nudged park while one is pending is a no-op
    resets = idle_park.pending_resets_for(scratch_db, ["memory", "other"])
    assert [(r["project"], r["agent"]) for r in resets] == [("junto", "memory")]
    assert scratch_db.idle_park_resets.count_documents({}) == 1


def test_nudged_park_not_opted_in_is_dropped(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "nimbusSupport")
    _nudged_park()
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    assert scratch_db.idle_park_resets.count_documents({}) == 0


def test_resets_only_returned_for_reported_agents(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "memory")
    _nudged_park()
    assert idle_park.pending_resets_for(scratch_db, ["someone-else"]) == []
    assert len(idle_park.pending_resets_for(scratch_db, ["memory"])) == 1


def test_results_deferred_keeps_pending_done_closes(monkeypatch, scratch_db):
    _autoreset(monkeypatch, "memory")
    _nudged_park()
    rid = idle_park.pending_resets_for(scratch_db, ["memory"])[0]["reset_id"]

    out = idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "deferred", "detail": "busy"}])
    assert out == {"applied": [rid], "rejected": []}
    doc = scratch_db.idle_park_resets.find_one({"_id": rid})
    assert doc["status"] == "pending" and doc["attempts"] == 1 and doc["last_detail"] == "busy"

    idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "done"}])
    assert scratch_db.idle_park_resets.find_one({"_id": rid})["status"] == "done"
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    # closed resets can't be reopened; bad outcomes are rejected
    out = idle_park.apply_reset_results(scratch_db, [{"reset_id": rid, "outcome": "failed"},
                                                     {"reset_id": rid, "outcome": "bogus"}])
    assert out["applied"] == [] and len(out["rejected"]) == 2


def test_old_pending_reset_expires(monkeypatch, scratch_db):
    from datetime import timedelta

    from shared_memory.helpers import utc_now

    _autoreset(monkeypatch, "memory")
    _nudged_park()
    rid = idle_park.pending_resets_for(scratch_db, ["memory"])[0]["reset_id"]
    scratch_db.idle_park_resets.update_one(
        {"_id": rid}, {"$set": {"requested_at": utc_now() - timedelta(hours=idle_park.RESET_EXPIRY_HOURS + 1)}})
    assert idle_park.pending_resets_for(scratch_db, ["memory"]) == []
    assert scratch_db.idle_park_resets.find_one({"_id": rid})["status"] == "expired"


# ── turns reported by the statusline (note_turn) ──

from datetime import datetime, timedelta, timezone  # noqa: E402

from shared_memory.idle_park import note_turn  # noqa: E402

WALL = datetime(2026, 10, 15, 14, 0, tzinfo=timezone.utc)


def _stamp(seconds_before_now):
    return (WALL - timedelta(seconds=seconds_before_now)).isoformat().replace("+00:00", "Z")


def test_turn_without_junto_calls_keeps_agent_from_going_idle():
    # The coordinator case: last junto call at 0, then 40 min of conversation turns.
    note_tool_call("memory_define_spec", "s_agent", now=0)
    assert note_turn("memory", _stamp(60), now=3000, now_wall=WALL) is True
    assert idle_park._timers[KEY]["last_real"] == 2940
    assert due_agents(_fifty, now=3000 + 10) == []
    assert due_agents(_fifty, now=2940 + 50 * 60) == [("junto", "memory", 50)]


def test_turn_never_arms_a_timer():
    assert note_turn("memory", _stamp(0), now=100, now_wall=WALL) is False
    assert idle_park._timers == {}


def test_repeated_or_older_stamp_is_a_no_op():
    note_tool_call("memory_query", "s_agent", now=1000)
    assert note_turn("memory", _stamp(500), now=1000, now_wall=WALL) is False
    assert idle_park._timers[KEY]["last_real"] == 1000


def test_turn_answering_the_nudge_does_not_rearm():
    note_tool_call("memory_query", "s_agent", now=0)
    mark_nudged("junto", "memory", now=3000)
    assert note_turn("memory", _stamp(0), now=3060, now_wall=WALL) is False
    assert idle_park._timers[KEY]["nudged_at"] == 3000
    later = 3000 + REARM_GRACE_SECONDS + 60
    assert note_turn("memory", _stamp(0), now=later, now_wall=WALL) is True
    assert idle_park._timers[KEY]["nudged_at"] is None


def test_future_stamp_counts_as_now_and_bad_stamps_are_ignored():
    note_tool_call("memory_query", "s_agent", now=0)
    assert note_turn("memory", _stamp(-300), now=500, now_wall=WALL) is True
    assert idle_park._timers[KEY]["last_real"] == 500
    assert note_turn("memory", "not a date", now=900, now_wall=WALL) is False
    assert note_turn("memory", None, now=900, now_wall=WALL) is False
    assert note_turn("other-agent", _stamp(0), now=900, now_wall=WALL) is False


# --- nudge text: project idle-policy guideline (msg_9f1cf850e7d6) -------------

class _FakeGuidelines:
    def __init__(self, docs):
        self.docs = docs

    def find_one(self, query, projection=None):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None


class _PolicyDb:
    def __init__(self, docs):
        self.guidelines = _FakeGuidelines(docs)


def test_nudge_text_default_has_no_park_now_and_names_minutes():
    subject, body = idle_park.nudge_text(_PolicyDb([]), "junto", 50)
    assert "park now" not in subject.lower() and "park now" not in body.lower()
    assert "50 min" in subject and body.startswith("Idle 50 min: park")


def test_nudge_text_carries_live_project_policy():
    db = _PolicyDb([{"name": "park_on_idle_nudge", "scope": "nimbus", "active": True,
                     "rule": "  KEEP-ALIVE OR PARK: check context first.  "}])
    _, body = idle_park.nudge_text(db, "nimbus", 50)
    assert "park_on_idle_nudge" in body
    assert "KEEP-ALIVE OR PARK: check context first.\n\nYour local time:" in body
    assert "clean stopping point" not in body


@pytest.mark.parametrize("doc", [
    {"name": "park_on_idle_nudge", "scope": "nimbus", "active": False, "rule": "x"},
    {"name": "park_on_idle_nudge", "scope": "other", "active": True, "rule": "x"},
    {"name": "park_on_idle_nudge", "scope": "nimbus", "active": True, "rule": "   "},
])
def test_nudge_text_falls_back_when_policy_inactive_other_project_or_blank(doc):
    _, body = idle_park.nudge_text(_PolicyDb([doc]), "nimbus", 50)
    assert "clean stopping point" in body


def test_nudge_text_normalizes_project_scope():
    db = _PolicyDb([{"name": "park_on_idle_nudge", "scope": "my_proj", "active": True,
                     "rule": "policy"}])
    assert "\n\npolicy\n\nYour local time:" in idle_park.nudge_text(db, "My-Proj", 50)[1]


def test_nudge_text_survives_db_without_guidelines():
    _, body = idle_park.nudge_text(_FakeDb(), "nimbus", 50)
    assert "clean stopping point" in body


# --- self-wake skip: own reminder due soon suppresses the nudge (learning_222e630bf8b35d24)

@pytest.fixture
def wake_db():
    import os
    import uuid as _uuid

    url = os.environ.get("JUNTO_TEST_MONGO_URL")
    if not url:
        pytest.skip("JUNTO_TEST_MONGO_URL not set (use a throwaway mongo, not production)")
    from pymongo import MongoClient
    client = MongoClient(url, serverSelectionTimeoutMS=3000)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip("test mongo unreachable")
    name = f"test_idle_wake_{_uuid.uuid4().hex[:8]}"
    yield client[name]
    client.drop_database(name)
    client.close()


def _reminder(db, minutes_from_now, status="pending", agent="memory", project="junto"):
    from datetime import datetime, timedelta, timezone
    db.scheduled_messages.insert_one({
        "agent_instance": agent, "agent_project": project, "status": status,
        "deliver_at": datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now),
    })


@pytest.mark.parametrize("minutes,status,agent,expected", [
    (55, "pending", "memory", True),     # keep-alive armed: skip
    (59, "pending", "memory", True),
    (61, "pending", "memory", False),    # beyond the window
    (60 * 24, "pending", "memory", False),
    (30, "delivered", "memory", False),  # already fired
    (30, "pending", "other", False),     # someone else's reminder
])
def test_has_pending_self_wake(wake_db, minutes, status, agent, expected):
    _reminder(wake_db, minutes, status=status, agent=agent)
    assert idle_park._has_pending_self_wake(wake_db, "junto", "memory") is expected


def test_has_pending_self_wake_survives_db_without_collection():
    assert idle_park._has_pending_self_wake(_FakeDb(), "junto", "memory") is False


async def test_sweep_skips_agent_with_armed_keepalive(monkeypatch):
    from shared_memory.tools import messaging

    async def fake_notify(project, agent, packet=None):
        raise AssertionError("must not push")

    monkeypatch.setattr(messaging, "_notify_inbox_for_send", fake_notify)
    monkeypatch.setattr(idle_park, "_threshold_from_config", lambda db, p, a: 50)
    monkeypatch.setattr(idle_park, "_has_pending_self_wake", lambda db, p, a: True)
    note_tool_call("memory_query", "s_agent", now=0)
    idle_park._timers[KEY]["last_real"] -= 10_000_000

    db = _FakeDb()
    assert await idle_park.sweep_once(db) == 0
    assert db.messages.docs == []
    assert idle_park._timers[KEY]["nudged_at"] is not None  # marked: no retry every minute


# --- parked agents are not re-armed (msg_9684a5d90e59) ------------------------

def test_parked_agent_calls_do_not_arm_a_timer():
    note_tool_call("memory_query", "s_agent", now=0)
    note_tool_call("memory_end_session", "s_agent", now=100)
    note_tool_call("memory_get_messages", "s_plugin", now=200)  # plugin still attached
    note_tool_call("memory_query", "s_agent", now=300)
    assert KEY not in idle_park._timers
    assert list(due_agents(lambda p, a: 1, now=1_000_000)) == []


def test_plugin_rebind_does_not_unpark():
    note_tool_call("memory_end_session", "s_agent", now=100)
    idle_park.note_session_start("junto", "memory", "junto-inbox channel plugin")
    note_tool_call("memory_get_messages", "s_plugin", now=200)
    assert KEY not in idle_park._timers


@pytest.mark.parametrize("task", ["", None, "Resume session (go)"])
def test_agent_session_start_unparks(task):
    note_tool_call("memory_end_session", "s_agent", now=100)
    idle_park.note_session_start("junto", "memory", task)
    note_tool_call("memory_query", "s_agent", now=200)
    assert idle_park._timers[KEY]["last_real"] == 200


@pytest.mark.parametrize("tool", ["memory_update_work", "memory_lock_files", "memory_define_spec"])
def test_real_work_while_parked_unparks_and_arms(tool):
    # Woken while parked, working on the plugin's session, no start_session
    # (server-team/billing-team 10-26, msg_df405eec7915).
    note_tool_call("memory_end_session", "s_agent", now=100)
    idle_park.note_session_start("junto", "memory", "junto-inbox channel plugin")
    note_tool_call(tool, "s_plugin", now=200)
    assert idle_park._timers[KEY]["last_real"] == 200
    note_tool_call("memory_query", "s_plugin", now=300)
    assert idle_park._timers[KEY]["last_real"] == 300
    assert list(due_agents(_fifty, now=300 + 50 * 60)) == [("junto", "memory", 50)]


@pytest.mark.parametrize("tool", ["memory_send_message", "memory_acknowledge_message",
                                  "memory_get_messages", "memory_query"])
def test_answering_while_parked_stays_parked(tool):
    note_tool_call("memory_end_session", "s_agent", now=100)
    note_tool_call(tool, "s_plugin", now=200)
    assert KEY not in idle_park._timers


def test_park_only_affects_that_project_and_agent():
    active_sessions["s_other"] = {"project": "nimbus", "claude_instance": "memory"}
    try:
        note_tool_call("memory_end_session", "s_agent", now=100)
        note_tool_call("memory_query", "s_other", now=200)
        assert ("nimbus", "memory") in idle_park._timers
    finally:
        active_sessions.pop("s_other", None)


# --- auto-reset agents get a direct park-now nudge (Tom 2026-10-17) ----------

class _AutoresetDb(_PolicyDb):
    def __init__(self, docs=(), tokens=None):
        super().__init__(list(docs))
        self._tokens = tokens

        class _Status:
            def find_one(_s, q, proj=None):
                return {"context_tokens": tokens} if tokens else None
        self.agent_status = _Status()


def _autoreset_cfg(monkeypatch, agents, night=True):
    from shared_memory import push_control
    monkeypatch.setattr(push_control, "get_effective_config",
                        lambda db, p: {"idle_park_autoreset": agents})
    monkeypatch.setattr(idle_park, "_is_night", lambda now=None, tz=None: night)


def test_autoreset_agent_gets_park_now_with_context(monkeypatch):
    _autoreset_cfg(monkeypatch, "nimbusSupport, other")
    policy = {"name": "park_on_idle_nudge", "scope": "nimbus", "active": True, "rule": "KEEP-ALIVE"}
    _, body = idle_park.nudge_text(_AutoresetDb([policy], tokens=285_708), "nimbus", 50, "nimbusSupport")
    assert body.startswith("Idle 50 min (your context: ~285K tokens).")
    assert "PARK NOW" in body and "/clear" in body and "KEEP-ALIVE" not in body


def test_autoreset_body_keeps_reset_gate_anchor(monkeypatch):
    _autoreset_cfg(monkeypatch, "nimbusSupport")
    _, body = idle_park.nudge_text(_AutoresetDb(), "nimbus", 50, "nimbusSupport")
    assert ("[SYSTEM NOTICE] " + body).startswith("[SYSTEM NOTICE] Idle")
    assert body.startswith("Idle 50 min. You're on auto-reset")  # no context reported


def test_non_autoreset_agent_still_gets_project_policy(monkeypatch):
    _autoreset_cfg(monkeypatch, "nimbusSupport")
    policy = {"name": "park_on_idle_nudge", "scope": "nimbus", "active": True, "rule": "KEEP-ALIVE"}
    _, body = idle_park.nudge_text(_AutoresetDb([policy]), "nimbus", 50, "coordinator")
    assert "KEEP-ALIVE\n\nYour local time:" in body


# --- Auto-reset is night-only (Tom, 2026-10-26) --------------------------------

def test_autoreset_agent_gets_project_policy_by_day(monkeypatch):
    _autoreset_cfg(monkeypatch, "nimbusSupport", night=False)
    policy = {"name": "park_on_idle_nudge", "scope": "nimbus", "active": True, "rule": "KEEP-ALIVE"}
    _, body = idle_park.nudge_text(_AutoresetDb([policy], tokens=285_708), "nimbus", 50, "nimbusSupport")
    assert "KEEP-ALIVE\n\nYour local time:" in body and "PARK NOW" not in body


@pytest.mark.parametrize("utc_hour,night", [
    (1, False),   # 20:00 CDT
    (2, True),    # 21:00 CDT
    (10, True),   # 05:00 CDT
    (11, False),  # 06:00 CDT
    (17, False),  # 12:00 CDT
])
def test_is_night_window_central(utc_hour, night):
    from datetime import datetime, timezone
    # 2026-10-26 is CDT (UTC-5).
    assert idle_park._is_night(datetime(2026, 10, 26, utc_hour, 0, tzinfo=timezone.utc)) is night


# --- per-agent timezone + server default policy (Tom 2026-10-27, work box) ---

class _TzDb(_PolicyDb):
    def __init__(self, docs=(), agent_tz=None):
        super().__init__(list(docs))

        class _Dir:
            def find_one(_s, q, proj=None):
                return {"timezone": agent_tz} if agent_tz else None
        self.agent_directory = _Dir()


def _cfg(monkeypatch, **cfg):
    from shared_memory import push_control
    monkeypatch.setattr(push_control, "get_effective_config", lambda db, p: dict(cfg))


def test_agent_timezone_order(monkeypatch):
    _cfg(monkeypatch, idle_park_timezone="Europe/London")
    assert idle_park.agent_timezone(_TzDb(agent_tz="America/Denver"), "work", "eric") == "America/Denver"
    assert idle_park.agent_timezone(_TzDb(agent_tz="Not/AZone"), "work", "eric") == "Europe/London"
    _cfg(monkeypatch)
    assert idle_park.agent_timezone(_TzDb(), "work", "eric") == "America/Chicago"


def test_clock_line_uses_agent_zone_for_day_night(monkeypatch):
    from datetime import datetime, timezone
    _cfg(monkeypatch)
    now = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # 23:00 Chicago, 05:00 London, 13:00 Tokyo
    _, chi = idle_park.nudge_text(_TzDb(agent_tz="America/Chicago"), "work", 50, "a", now=now)
    _, tok = idle_park.nudge_text(_TzDb(agent_tz="Asia/Tokyo"), "work", 50, "a", now=now)
    assert chi.endswith("Your local time: 23:00 (America/Chicago) — NIGHT (night = 21:00–06:00).")
    assert "13:00 (Asia/Tokyo) — DAY" in tok


def test_server_policy_fills_in_when_project_has_none(monkeypatch):
    _cfg(monkeypatch, idle_park_policy="  KEEP WARM BY DAY  ")
    _, body = idle_park.nudge_text(_TzDb(), "awareness", 50, "roy")
    assert "push_control idle_park_policy" in body and "KEEP WARM BY DAY\n\nYour local time:" in body
    proj = {"name": "park_on_idle_nudge", "scope": "awareness", "active": True, "rule": "PROJECT RULE"}
    _, body = idle_park.nudge_text(_TzDb([proj]), "awareness", 50, "roy")
    assert "PROJECT RULE" in body and "KEEP WARM" not in body


def test_valid_timezone():
    assert idle_park.valid_timezone("America/Denver")
    assert not idle_park.valid_timezone("Mountain") and not idle_park.valid_timezone(None)
