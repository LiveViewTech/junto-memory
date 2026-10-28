"""Session-lifecycle fixes (backlog_940b9f9c66e1, workbox bug msg_af7a290ceebb).

Pins the three defenses against orphaned-session delivery breakage:
  1. require_session touches last_activity on every tool call, so idle-expiry
     measures real inactivity.
  2. cleanup_stale_sessions two-tier expiry: hard 14d TTL unconditional; idle
     expiry (SESSION_IDLE_HOURS) skips sessions holding a live inbox SSE
     subscription (a quiet-but-connected plugin is healthy).
  3. _notify_inbox per-send timeout: one stuck (half-open) subscriber cannot
     wedge the announce loop — live subscribers after it still get the push,
     and the stuck one is pruned.
"""

import asyncio
from datetime import timedelta

import pytest

from shared_memory import helpers as helpers_mod
from shared_memory.helpers import (
    cleanup_stale_sessions,
    require_session,
    utc_now,
)
from shared_memory.state import active_sessions, mcp_session_to_app
from shared_memory.tools import messaging as messaging_mod


def _iso(dt):
    return dt.isoformat()


@pytest.fixture(autouse=True)
def _clean_state():
    active_sessions.clear()
    mcp_session_to_app.clear()
    messaging_mod.inbox_subscriptions.clear()
    yield
    active_sessions.clear()
    mcp_session_to_app.clear()
    messaging_mod.inbox_subscriptions.clear()


def _mk_session(sid, idle_hours=0):
    active_sessions[sid] = {
        "claude_instance": "t",
        "project": "junto",
        "started": _iso(utc_now() - timedelta(hours=idle_hours)),
        "last_activity": _iso(utc_now() - timedelta(hours=idle_hours)),
    }


# ---------------------------------------------------------------------------
# 1. require_session touch
# ---------------------------------------------------------------------------

def test_require_session_bumps_last_activity():
    _mk_session("s1", idle_hours=5)
    before = active_sessions["s1"]["last_activity"]
    assert require_session("s1") == ""
    assert active_sessions["s1"]["last_activity"] > before


def test_require_session_missing_still_errors():
    assert "not found" in require_session("nope")
    assert require_session(None).startswith("ERROR")


# ---------------------------------------------------------------------------
# 2. two-tier expiry
# ---------------------------------------------------------------------------

def test_idle_session_without_subscription_expires():
    _mk_session("idle", idle_hours=8)
    _mk_session("fresh", idle_hours=1)
    removed = cleanup_stale_sessions()
    assert "idle" in removed
    assert "fresh" in active_sessions


def test_idle_session_with_live_subscription_survives():
    _mk_session("plugin", idle_hours=8)
    transport = object()
    mcp_session_to_app[transport] = "plugin"
    messaging_mod.inbox_subscriptions["inbox://junto/t"] = {transport}
    removed = cleanup_stale_sessions()
    assert "plugin" not in removed
    assert "plugin" in active_sessions


def test_hard_ttl_expires_even_with_subscription():
    _mk_session("ancient", idle_hours=15 * 24)
    transport = object()
    mcp_session_to_app[transport] = "ancient"
    messaging_mod.inbox_subscriptions["inbox://junto/t"] = {transport}
    removed = cleanup_stale_sessions()
    assert "ancient" in removed


def test_idle_expiry_disabled_by_zero(monkeypatch):
    monkeypatch.setattr(helpers_mod, "SESSION_IDLE_HOURS", 0)
    _mk_session("idle", idle_hours=8)
    removed = cleanup_stale_sessions()
    assert removed == []
    assert "idle" in active_sessions


def test_expiry_drops_transport_binding():
    # Transport bound to the session but present in NO subscription bucket —
    # a connection that never subscribed (or was keepalive-pruned). Not live,
    # so the idle session expires and its binding is cleaned up with it.
    _mk_session("dead", idle_hours=8)
    transport = object()
    mcp_session_to_app[transport] = "dead"
    removed = cleanup_stale_sessions()
    assert "dead" in removed
    assert transport not in mcp_session_to_app


# ---------------------------------------------------------------------------
# 3. notify-path per-send timeout
# ---------------------------------------------------------------------------

class _StuckSession:
    """send_resource_updated blocks forever — the half-open-socket shape."""

    def __init__(self):
        self.pushed = False

    async def send_resource_updated(self, url):
        await asyncio.sleep(3600)

    async def send_message(self, msg):  # pragma: no cover — never reached
        self.pushed = True


class _LiveSession:
    def __init__(self):
        self.updated = False
        self.pushed = False

    async def send_resource_updated(self, url):
        self.updated = True

    async def send_message(self, msg):
        self.pushed = True


def test_stuck_subscriber_cannot_wedge_live_ones(monkeypatch):
    monkeypatch.setattr(messaging_mod, "NOTIFY_SEND_TIMEOUT", 0.05)
    stuck, live = _StuckSession(), _LiveSession()
    uri = messaging_mod.inbox_uri("junto", "agentx")
    # dict/set iteration order: ensure the stuck one is hit first by inserting
    # it first (CPython set order is not guaranteed, so use both orderings).
    messaging_mod.inbox_subscriptions[uri] = {stuck, live}

    asyncio.run(
        messaging_mod._notify_inbox("junto", "agentx",
                                    {"mode": "header", "msg_id": "m1"})
    )
    # live subscriber got both the resource-updated and the content push
    assert live.updated and live.pushed
    # stuck subscriber was pruned from the bucket
    assert stuck not in messaging_mod.inbox_subscriptions.get(uri, set())
    assert live in messaging_mod.inbox_subscriptions.get(uri, set())


def test_all_dead_bucket_is_removed(monkeypatch):
    monkeypatch.setattr(messaging_mod, "NOTIFY_SEND_TIMEOUT", 0.05)
    stuck = _StuckSession()
    uri = messaging_mod.inbox_uri("junto", "agenty")
    messaging_mod.inbox_subscriptions[uri] = {stuck}
    asyncio.run(messaging_mod._notify_inbox("junto", "agenty", None))
    assert uri not in messaging_mod.inbox_subscriptions
