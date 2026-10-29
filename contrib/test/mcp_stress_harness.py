"""Concurrent MCP client stress harness.

Exercises the streamable-http MCP transport with multiple concurrent
clients to validate session isolation, lifecycle, and resilience under
both stateless_http=True (current) and =False (proposed).

Tests:
  1. Concurrent session start — N clients start sessions simultaneously,
     each must get a unique session_id and not see each other's data.
  2. Session isolation — client A's messages don't leak into client B's
     get_messages output.
  3. Lifecycle churn — open/close N sessions in a loop, watch for
     server memory/handle growth.
  4. Long-idle session — connect, idle 60s, call a tool, verify it still
     works (catches keepalive/heartbeat issues).
  5. Concurrent tool calls within one session — pipeline 5 calls without
     awaiting between them, ensure all complete.
  6. Server-side state checks — count active_sessions in /health before
     and after each test, verify cleanup.

The harness uses the official `mcp` Python client library so it exercises
the same code path real clients will use.
"""

from __future__ import annotations
import asyncio
import json
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = "http://localhost:8080/mcp"
HEALTH_URL = "http://localhost:8080/health"


@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    elapsed_s: float = 0.0
    extra: dict = field(default_factory=dict)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def get_health() -> dict:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(HEALTH_URL)
        return r.json()


@asynccontextmanager
async def open_mcp():
    """Open an MCP client session, yield the ClientSession."""
    async with streamablehttp_client(MCP_URL) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, args: dict) -> dict:
    """Call a tool and parse the JSON-string response."""
    result = await session.call_tool(name, args)
    # FastMCP returns content as TextContent with .text field
    if not result.content:
        return {}
    text = result.content[0].text  # type: ignore[union-attr]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def start_test_session(session: ClientSession, agent_suffix: str) -> str:
    """Start a memory session, return session_id."""
    payload = await call_tool(
        session,
        "memory_start_session",
        {
            "project": "shared_memory",
            "claude_instance": f"stress-{agent_suffix}",
            "task_description": f"stress test {agent_suffix}",
        },
    )
    return payload.get("session_id", "")


# ── Test 1: concurrent start, each gets unique session_id ──
async def test_concurrent_start(n: int = 5) -> TestResult:
    t0 = time.monotonic()
    h0 = await get_health()
    sessions_before = h0["active_sessions"]

    async def one(i: int) -> str:
        async with open_mcp() as s:
            return await start_test_session(s, f"concurrent-{i}-{uuid.uuid4().hex[:4]}")

    sids = await asyncio.gather(*[one(i) for i in range(n)])
    unique = len(set(sids))
    h1 = await get_health()
    elapsed = time.monotonic() - t0

    # NOTE: each `async with open_mcp` closes immediately so by the time we
    # check health, sessions may already be torn down on the client side. The
    # server-side memory session persists in active_sessions until TTL or end.
    detail = (
        f"started {n} concurrent, got {unique} unique session_ids, "
        f"server active_sessions: {sessions_before} -> {h1['active_sessions']}"
    )
    passed = unique == n and all(sid.startswith("shared_memory_stress-") for sid in sids)
    return TestResult(
        name="concurrent_start",
        passed=passed,
        detail=detail,
        elapsed_s=elapsed,
        extra={"sids": sids, "before": sessions_before, "after": h1["active_sessions"]},
    )


# ── Test 2: session isolation — A doesn't see B's messages ──
async def test_session_isolation() -> TestResult:
    t0 = time.monotonic()
    nonce = uuid.uuid4().hex[:8]

    async def session_a():
        async with open_mcp() as s:
            sid = await start_test_session(s, f"iso-A-{nonce}")
            # send a message from A to itself with nonce in body
            await call_tool(s, "memory_send_message", {
                "session_id": sid,
                "to_instance": f"stress-iso-A-{nonce}",
                "to_project": "shared_memory",
                "message": f"isolation-A-{nonce}",
                "category": "info",
            })
            # read messages
            msgs = await call_tool(s, "memory_get_messages", {
                "session_id": sid, "include_delivered": True, "limit": 50,
            })
            await call_tool(s, "memory_end_session", {
                "session_id": sid, "summary": "isolation A done",
            })
            return msgs

    async def session_b():
        async with open_mcp() as s:
            sid = await start_test_session(s, f"iso-B-{nonce}")
            await call_tool(s, "memory_send_message", {
                "session_id": sid,
                "to_instance": f"stress-iso-B-{nonce}",
                "to_project": "shared_memory",
                "message": f"isolation-B-{nonce}",
                "category": "info",
            })
            msgs = await call_tool(s, "memory_get_messages", {
                "session_id": sid, "include_delivered": True, "limit": 50,
            })
            await call_tool(s, "memory_end_session", {
                "session_id": sid, "summary": "isolation B done",
            })
            return msgs

    a_msgs, b_msgs = await asyncio.gather(session_a(), session_b())

    # A should see its own message but not B's
    a_bodies = [m.get("message", "") for m in a_msgs.get("messages", [])]
    b_bodies = [m.get("message", "") for m in b_msgs.get("messages", [])]
    a_has_a = any(f"isolation-A-{nonce}" in m for m in a_bodies)
    a_has_b = any(f"isolation-B-{nonce}" in m for m in a_bodies)
    b_has_b = any(f"isolation-B-{nonce}" in m for m in b_bodies)
    b_has_a = any(f"isolation-A-{nonce}" in m for m in b_bodies)

    passed = a_has_a and b_has_b and not a_has_b and not b_has_a
    elapsed = time.monotonic() - t0
    return TestResult(
        name="session_isolation",
        passed=passed,
        detail=(
            f"A sees own={a_has_a} B={a_has_b}; "
            f"B sees own={b_has_b} A={b_has_a}"
        ),
        elapsed_s=elapsed,
    )


# ── Test 3: lifecycle churn ──
async def test_lifecycle_churn(rounds: int = 20) -> TestResult:
    t0 = time.monotonic()
    h0 = await get_health()

    for i in range(rounds):
        async with open_mcp() as s:
            sid = await start_test_session(s, f"churn-{i}-{uuid.uuid4().hex[:4]}")
            await call_tool(s, "memory_end_session", {
                "session_id": sid, "summary": f"churn iter {i}",
            })

    h1 = await get_health()
    elapsed = time.monotonic() - t0
    delta = h1["active_sessions"] - h0["active_sessions"]
    # Server should have at most +1 (this harness's own connection might
    # leave one straggler) net change after end_session was called on each.
    passed = delta <= 1
    return TestResult(
        name="lifecycle_churn",
        passed=passed,
        detail=f"{rounds} open+end cycles, active_sessions delta={delta}",
        elapsed_s=elapsed,
        extra={"before": h0["active_sessions"], "after": h1["active_sessions"]},
    )


# ── Test 4: idle session ──
async def test_idle_session(idle_s: int = 30) -> TestResult:
    t0 = time.monotonic()
    async with open_mcp() as s:
        sid = await start_test_session(s, f"idle-{uuid.uuid4().hex[:4]}")
        log(f"  idle test sleeping {idle_s}s...")
        await asyncio.sleep(idle_s)
        # tool call after idle
        result = await call_tool(s, "memory_get_messages", {
            "session_id": sid, "limit": 1,
        })
        await call_tool(s, "memory_end_session", {
            "session_id": sid, "summary": "idle done",
        })
    passed = "messages" in result or "count" in result
    return TestResult(
        name="idle_session",
        passed=passed,
        detail=f"after {idle_s}s idle, tool call returned: {list(result.keys())}",
        elapsed_s=time.monotonic() - t0,
    )


# ── Test 5: concurrent calls within one session ──
async def test_concurrent_calls_one_session() -> TestResult:
    t0 = time.monotonic()
    async with open_mcp() as s:
        sid = await start_test_session(s, f"concurrent-calls-{uuid.uuid4().hex[:4]}")
        # 5 simultaneous get_messages calls
        results = await asyncio.gather(
            *[call_tool(s, "memory_get_messages", {"session_id": sid, "limit": 5})
              for _ in range(5)],
            return_exceptions=True,
        )
        await call_tool(s, "memory_end_session", {
            "session_id": sid, "summary": "concurrent calls done",
        })
    errors = [r for r in results if isinstance(r, Exception)]
    passed = len(errors) == 0 and all(isinstance(r, dict) for r in results)
    return TestResult(
        name="concurrent_calls_one_session",
        passed=passed,
        detail=f"5 concurrent calls, errors={len(errors)}",
        elapsed_s=time.monotonic() - t0,
        extra={"errors": [str(e) for e in errors]},
    )


# ── Test 6: tools/list before initialize race ──
async def test_initialize_race() -> TestResult:
    """Try to call a tool before completing initialize — this is the
    -32602 bug class. With stateless_http it's a non-issue because every
    call is its own request. With stateful, the client must complete the
    initialize handshake before tools work.

    We simulate by hammering the endpoint with N parallel initialize+
    immediate-tool-call sequences and check none of them error.
    """
    t0 = time.monotonic()

    async def one(i: int):
        try:
            async with open_mcp() as s:
                # Don't even start a memory session — just call list_tools
                tools = await s.list_tools()
                return len(tools.tools) if tools.tools else 0
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    results = await asyncio.gather(*[one(i) for i in range(8)])
    errors = [r for r in results if isinstance(r, str)]
    counts = [r for r in results if isinstance(r, int)]
    passed = len(errors) == 0 and len(set(counts)) == 1
    return TestResult(
        name="initialize_race",
        passed=passed,
        detail=f"8 parallel init+list_tools, errors={len(errors)}, tool_counts={set(counts)}",
        elapsed_s=time.monotonic() - t0,
        extra={"errors": errors, "counts": counts},
    )


# ── Cleanup ──
async def cleanup():
    """Remove any test artifacts in MongoDB."""
    import os
    from urllib.parse import quote_plus
    from pymongo import MongoClient
    pw = os.environ.get("MONGO_PASSWORD")
    if not pw:
        raise SystemExit("MONGO_PASSWORD is not set (export it from your .env)")
    pw = quote_plus(pw)
    db = MongoClient(
        f"mongodb://mcp_orch:{pw}@localhost:27019/mcp_orchestrator?authSource=admin"
    )["mcp_orchestrator"]
    deleted = db.messages.delete_many({"message": {"$regex": "isolation-[AB]-"}}).deleted_count
    db.agent_directory.delete_many({"instance": {"$regex": "^stress-"}})
    print(f"\nCleanup: deleted {deleted} test messages, removed stress-* directory entries\n")


async def main(label: str = "test"):
    log(f"=== Harness run: {label} ===")
    log(f"Health pre-test: {await get_health()}")
    print()

    tests = [
        test_initialize_race(),
        test_concurrent_start(5),
        test_session_isolation(),
        test_concurrent_calls_one_session(),
        test_lifecycle_churn(20),
    ]
    # Idle test is slow, run it last and in serial
    results = []
    for coro in tests:
        r = await coro
        log(f"  {r.name:35} {'PASS' if r.passed else 'FAIL':4}  {r.elapsed_s:5.1f}s  {r.detail}")
        results.append(r)

    # Skip long idle test unless explicitly requested
    if "--with-idle" in sys.argv:
        r = await test_idle_session(60)
        log(f"  {r.name:35} {'PASS' if r.passed else 'FAIL':4}  {r.elapsed_s:5.1f}s  {r.detail}")
        results.append(r)

    print()
    log(f"Health post-test: {await get_health()}")
    await cleanup()

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print()
    log(f"=== {label}: {passed}/{total} passed ===")

    return all(r.passed for r in results)


if __name__ == "__main__":
    label = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "test"
    ok = asyncio.run(main(label))
    sys.exit(0 if ok else 1)
