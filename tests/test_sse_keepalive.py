"""Tests for the SSE notification-stream keepalive (half-open-stream fix, 2026-06-15).

The keepalive sends a periodic no-op notification down each subscribed session's
long-lived stream to keep it warm (so idle network reapers don't silently
half-open it) and to prune genuinely-dead sessions. These tests lock down the
prune-on-failure + prune-on-timeout behavior and the start/stop lifecycle without
needing a live server. Sync tests drive the coroutines via asyncio.run().
"""

import asyncio

import shared_memory.tools.messaging as m


class GoodSession:
    def __init__(self):
        self.sent = 0

    async def send_message(self, msg):
        self.sent += 1


class RaisingSession:
    async def send_message(self, msg):
        raise RuntimeError("dead transport")


class HangingSession:
    """Simulates a half-open socket whose write blocks (zero-buffer stream)."""
    async def send_message(self, msg):
        await asyncio.sleep(10)


def _reset():
    m.inbox_subscriptions.clear()


# ── prune behavior ──

def test_sweep_keeps_healthy_prunes_dead():
    _reset()
    good, bad = GoodSession(), RaisingSession()
    m.inbox_subscriptions["inbox://p/a"] = {good, bad}
    asyncio.run(m._keepalive_sweep())
    bucket = m.inbox_subscriptions.get("inbox://p/a", set())
    assert good in bucket          # healthy session retained + pinged
    assert good.sent == 1
    assert bad not in bucket       # raising session pruned
    _reset()


def test_one_prunes_hanging_session_via_timeout(monkeypatch):
    # Tiny timeout so the blocked write is pruned fast instead of waiting 5s.
    monkeypatch.setattr(m, "SSE_KEEPALIVE_SEND_TIMEOUT", 0.05)
    _reset()
    hang = HangingSession()
    m.inbox_subscriptions["inbox://p/a"] = {hang}
    asyncio.run(m._keepalive_one("inbox://p/a", hang))
    # bucket emptied → key popped entirely
    assert "inbox://p/a" not in m.inbox_subscriptions
    _reset()


def test_sweep_empty_is_noop():
    _reset()
    asyncio.run(m._keepalive_sweep())  # must not raise
    assert m.inbox_subscriptions == {}


def test_sweep_keeps_other_buckets_when_one_dies():
    _reset()
    good = GoodSession()
    bad = RaisingSession()
    m.inbox_subscriptions["inbox://p/live"] = {good}
    m.inbox_subscriptions["inbox://p/dead"] = {bad}
    asyncio.run(m._keepalive_sweep())
    assert good in m.inbox_subscriptions["inbox://p/live"]
    assert "inbox://p/dead" not in m.inbox_subscriptions  # emptied → popped
    _reset()


# ── lifecycle ──

def test_start_stop_idempotent():
    async def run():
        m.start_keepalive()
        t1 = m._keepalive_task
        assert t1 is not None and not t1.done()
        m.start_keepalive()                 # idempotent — no new task
        assert m._keepalive_task is t1
        m.stop_keepalive()
        assert m._keepalive_task is None
        await asyncio.gather(t1, return_exceptions=True)  # drain the cancel
        assert t1.cancelled()
    asyncio.run(run())
