"""Phase C2 inbox resource smoke test.

Exercises:
  1. Resource template is listed by the server.
  2. Reading inbox://<project>/<agent> returns the same payload shape as
     memory_get_messages (count + messages list + uri).
  3. Subscribing to an inbox URI followed by memory_send_message to that
     agent triggers a notifications/resources/updated for the subscriber.
  4. Unsubscribe stops further notifications.
  5. A non-subscriber does not receive the notification.

Run against a live server with stateful HTTP enabled (the default after
commit 7566597). Cleans up its own test messages on exit.
"""

from __future__ import annotations
import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8080/mcp")
HEALTH_URL = os.environ.get("MCP_HEALTH_URL", "http://localhost:8080/health")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@asynccontextmanager
async def open_mcp(notification_sink: list | None = None):
    """Open an MCP client, optionally collecting notifications into a list."""
    async with streamablehttp_client(MCP_URL) as (read, write, _gid):
        session = ClientSession(
            read,
            write,
            message_handler=_make_handler(notification_sink) if notification_sink is not None else None,
        )
        async with session:
            await session.initialize()
            yield session


def _make_handler(sink: list):
    async def handler(message):
        # message is an mcp.shared.session.RequestResponder | ServerNotification | Exception
        sink.append(message)
    return handler


async def call_tool(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    if not result.content:
        return {}
    text = result.content[0].text  # type: ignore[union-attr]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}


async def start_session(session: ClientSession, suffix: str) -> str:
    payload = await call_tool(session, "memory_start_session", {
        "project": "shared_memory",
        "claude_instance": f"inbox-smoke-{suffix}",
        "task_description": "inbox resource smoke",
    })
    return payload.get("session_id", "")


async def end_session(session: ClientSession, sid: str) -> None:
    await call_tool(session, "memory_end_session", {
        "session_id": sid,
        "summary": "inbox smoke done",
    })


async def test_resource_listed() -> bool:
    """The inbox resource template should appear in list_resource_templates."""
    async with open_mcp() as s:
        templates = await s.list_resource_templates()
        uris = [t.uriTemplate for t in templates.resourceTemplates]
    found = any("inbox://" in u for u in uris)
    log(f"  resource_listed       {'PASS' if found else 'FAIL':4}  templates={uris}")
    return found


async def test_resource_read_shape() -> bool:
    """Reading an empty inbox should return the documented payload shape.
    Caller must connect AS the inbox's agent (auth gate added in C2 follow-up)."""
    nonce = uuid.uuid4().hex[:6]
    me = f"inbox-smoke-empty-{nonce}"
    async with open_mcp() as s:
        payload = await call_tool(s, "memory_start_session", {
            "project": "shared_memory",
            "claude_instance": me,
            "task_description": "inbox smoke read shape",
        })
        sid = payload.get("session_id", "")
        try:
            result = await s.read_resource(f"inbox://shared_memory/{me}")
        finally:
            await end_session(s, sid)
    if not result.contents:
        log("  resource_read_shape   FAIL  no contents")
        return False
    text = result.contents[0].text  # type: ignore[union-attr]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        log(f"  resource_read_shape   FAIL  not JSON: {text[:80]}")
        return False
    needed = {"uri", "project", "agent", "count", "messages", "next_cursor", "has_more"}
    ok = needed.issubset(payload.keys()) and payload["count"] == 0 and payload["messages"] == []
    log(f"  resource_read_shape   {'PASS' if ok else 'FAIL':4}  keys={sorted(payload.keys())}")
    return ok


async def test_authz_blocks_other_agent() -> bool:
    """Agent X subscribing/reading inbox://Y should be denied."""
    nonce = uuid.uuid4().hex[:6]
    me = f"inbox-smoke-spy-{nonce}"
    other = f"inbox-smoke-victim-{nonce}"
    async with open_mcp() as spy, open_mcp() as victim:
        # Register both agents (so the project knows them)
        v_payload = await call_tool(victim, "memory_start_session", {
            "project": "shared_memory",
            "claude_instance": other,
            "task_description": "victim",
        })
        v_sid = v_payload.get("session_id", "")
        s_payload = await call_tool(spy, "memory_start_session", {
            "project": "shared_memory",
            "claude_instance": me,
            "task_description": "spy",
        })
        s_sid = s_payload.get("session_id", "")

        # Read should return permission denied JSON, not victim's messages.
        read = await spy.read_resource(f"inbox://shared_memory/{other}")
        body = json.loads(read.contents[0].text)  # type: ignore[union-attr]
        read_blocked = body.get("error") == "permission denied"

        # Subscribe should raise.
        try:
            await spy.subscribe_resource(f"inbox://shared_memory/{other}")
            sub_blocked = False
        except Exception:
            sub_blocked = True

        await end_session(spy, s_sid)
        await end_session(victim, v_sid)

    ok = read_blocked and sub_blocked
    log(f"  authz_blocks_other     {'PASS' if ok else 'FAIL':4}  read_blocked={read_blocked} sub_blocked={sub_blocked}")
    return ok


async def test_broadcast_fanout() -> bool:
    """A broadcast (to_instance='*') should notify every subscriber in the project."""
    nonce = uuid.uuid4().hex[:6]
    a = f"inbox-smoke-bcA-{nonce}"
    b = f"inbox-smoke-bcB-{nonce}"
    notes_a: list = []
    notes_b: list = []
    async with open_mcp(notes_a) as sa, open_mcp(notes_b) as sb, open_mcp() as sender:
        a_sid = (await call_tool(sa, "memory_start_session", {
            "project": "shared_memory", "claude_instance": a,
            "task_description": "bc A",
        })).get("session_id", "")
        b_sid = (await call_tool(sb, "memory_start_session", {
            "project": "shared_memory", "claude_instance": b,
            "task_description": "bc B",
        })).get("session_id", "")
        send_sid = await start_session(sender, f"bc-send-{nonce}")

        await sa.subscribe_resource(f"inbox://shared_memory/{a}")
        await sb.subscribe_resource(f"inbox://shared_memory/{b}")

        await call_tool(sender, "memory_send_message", {
            "session_id": send_sid,
            "to_instance": "*",
            "to_project": "shared_memory",
            "message": f"broadcast test {nonce}",
            "category": "info",
        })
        await asyncio.sleep(0.5)

        await end_session(sa, a_sid)
        await end_session(sb, b_sid)
        await end_session(sender, send_sid)

    from mcp.types import ServerNotification, ResourceUpdatedNotification

    def saw(notes, uri):
        return any(
            isinstance(n, ServerNotification) and isinstance(n.root, ResourceUpdatedNotification)
            and str(n.root.params.uri).rstrip("/") == uri.rstrip("/")
            for n in notes
        )
    a_got = saw(notes_a, f"inbox://shared_memory/{a}")
    b_got = saw(notes_b, f"inbox://shared_memory/{b}")
    ok = a_got and b_got
    log(f"  broadcast_fanout       {'PASS' if ok else 'FAIL':4}  a_got={a_got} b_got={b_got}")
    return ok


async def test_get_messages_cursor() -> bool:
    """memory_get_messages with cursor should return next_cursor + paginate."""
    nonce = uuid.uuid4().hex[:6]
    me = f"inbox-smoke-page-{nonce}"
    async with open_mcp() as recv, open_mcp() as sender:
        r_sid = (await call_tool(recv, "memory_start_session", {
            "project": "shared_memory", "claude_instance": me,
            "task_description": "cursor recv",
        })).get("session_id", "")
        s_sid = await start_session(sender, f"page-send-{nonce}")

        # Send 5 distinct messages
        for i in range(5):
            await call_tool(sender, "memory_send_message", {
                "session_id": s_sid,
                "to_instance": me,
                "to_project": "shared_memory",
                "message": f"page test {nonce} #{i}",
                "category": "info",
            })
            await asyncio.sleep(0.02)  # ensure distinct created_at ordering

        # First page: limit=2 → expect 2 + has_more + next_cursor
        page1 = await call_tool(recv, "memory_get_messages", {
            "session_id": r_sid, "limit": 2,
        })
        # Second page using cursor
        page2 = await call_tool(recv, "memory_get_messages", {
            "session_id": r_sid, "limit": 2, "cursor": page1.get("next_cursor"),
        })
        # Third page (final)
        page3 = await call_tool(recv, "memory_get_messages", {
            "session_id": r_sid, "limit": 2, "cursor": page2.get("next_cursor"),
        })

        await end_session(recv, r_sid)
        await end_session(sender, s_sid)

    ids1 = {m["id"] for m in page1.get("messages", [])}
    ids2 = {m["id"] for m in page2.get("messages", [])}
    ids3 = {m["id"] for m in page3.get("messages", [])}
    distinct = ids1.isdisjoint(ids2) and ids1.isdisjoint(ids3) and ids2.isdisjoint(ids3)
    counts_ok = (
        len(ids1) == 2 and len(ids2) == 2 and len(ids3) >= 1 and (len(ids1) + len(ids2) + len(ids3)) >= 5
    )
    cursors_ok = bool(page1.get("next_cursor")) and bool(page2.get("next_cursor"))
    final_ok = page3.get("has_more") is False
    ok = distinct and counts_ok and cursors_ok and final_ok
    log(
        f"  get_messages_cursor    {'PASS' if ok else 'FAIL':4}  "
        f"counts={len(ids1)}/{len(ids2)}/{len(ids3)} distinct={distinct} "
        f"cursors_ok={cursors_ok} final_ok={final_ok}"
    )
    return ok


async def test_subscribe_notification() -> bool:
    """Subscribe + send → expect at least one resource_updated notification on the URI."""
    nonce = uuid.uuid4().hex[:6]
    # Subscribe to our own inbox so the target is auto-registered when we
    # start_session as that name. (memory_send_message rejects unregistered
    # recipients.)
    target_agent = f"inbox-smoke-target-{nonce}"
    target_uri = f"inbox://shared_memory/{target_agent}"

    sub_notes: list = []
    other_notes: list = []

    async with open_mcp(sub_notes) as sub_session, open_mcp(other_notes) as other_session:
        # sub_session connects AS the target agent so that name auto-registers
        sub_sid = await call_tool(sub_session, "memory_start_session", {
            "project": "shared_memory",
            "claude_instance": target_agent,
            "task_description": "inbox smoke target",
        })
        sub_sid = sub_sid.get("session_id", "")
        send_sid = await start_session(other_session, f"send-{nonce}")

        # Subscribe from sub_session
        await sub_session.subscribe_resource(target_uri)

        # Send a message from the OTHER session
        send_result = await call_tool(other_session, "memory_send_message", {
            "session_id": send_sid,
            "to_instance": target_agent,
            "to_project": "shared_memory",
            "message": f"inbox smoke test {nonce}",
            "category": "info",
        })
        if "error" in send_result and send_result.get("error", "").startswith("Agent"):
            # Agent not registered in projects table — that's fine for this
            # test, the message still inserts (server falls through). Check:
            log(f"  send result: {send_result}")
            return False

        # Give the server a moment to dispatch
        await asyncio.sleep(0.5)

        # Verify the resource read sees the new message
        read = await sub_session.read_resource(target_uri)
        body = json.loads(read.contents[0].text)  # type: ignore[union-attr]

        await sub_session.unsubscribe_resource(target_uri)
        await end_session(sub_session, sub_sid)
        await end_session(other_session, send_sid)

    # Inspect notifications collected by the sub_session message handler
    from mcp.types import ServerNotification, ResourceUpdatedNotification
    got_update = any(
        isinstance(n, ServerNotification) and isinstance(n.root, ResourceUpdatedNotification)
        and str(n.root.params.uri).rstrip("/") == target_uri.rstrip("/")
        for n in sub_notes
    )
    other_got_update = any(
        isinstance(n, ServerNotification) and isinstance(n.root, ResourceUpdatedNotification)
        for n in other_notes
    )
    msg_present = any(f"inbox smoke test {nonce}" in m.get("message", "") for m in body.get("messages", []))

    ok = got_update and msg_present and not other_got_update
    log(
        f"  subscribe_notification {'PASS' if ok else 'FAIL':4}  "
        f"sub_got={got_update} msg_in_inbox={msg_present} other_got={other_got_update}"
    )
    return ok


async def test_unsubscribe_silences() -> bool:
    """After unsubscribe, no further notifications should arrive."""
    nonce = uuid.uuid4().hex[:6]
    target_agent = f"inbox-smoke-unsub-{nonce}"
    target_uri = f"inbox://shared_memory/{target_agent}"

    sub_notes: list = []
    async with open_mcp(sub_notes) as sub_session, open_mcp() as other_session:
        # connect as the target so the name auto-registers for memory_send_message
        sub_payload = await call_tool(sub_session, "memory_start_session", {
            "project": "shared_memory",
            "claude_instance": target_agent,
            "task_description": "inbox smoke unsub target",
        })
        sub_sid = sub_payload.get("session_id", "")
        send_sid = await start_session(other_session, f"unsub-send-{nonce}")

        await sub_session.subscribe_resource(target_uri)
        await sub_session.unsubscribe_resource(target_uri)

        await call_tool(other_session, "memory_send_message", {
            "session_id": send_sid,
            "to_instance": target_agent,
            "to_project": "shared_memory",
            "message": f"unsub test {nonce}",
            "category": "info",
        })
        await asyncio.sleep(0.5)

        await end_session(sub_session, sub_sid)
        await end_session(other_session, send_sid)

    from mcp.types import ServerNotification, ResourceUpdatedNotification
    got_update = any(
        isinstance(n, ServerNotification) and isinstance(n.root, ResourceUpdatedNotification)
        and str(n.root.params.uri).rstrip("/") == target_uri.rstrip("/")
        for n in sub_notes
    )
    ok = not got_update
    log(f"  unsubscribe_silences  {'PASS' if ok else 'FAIL':4}  got_update_after_unsub={got_update}")
    return ok


async def cleanup() -> None:
    """Drop test messages + agent_directory entries we created."""
    from pymongo import MongoClient
    pw = os.environ.get("MONGO_PASSWORD")
    if not pw:
        raise SystemExit("MONGO_PASSWORD is not set (export it from your .env)")
    pw = quote_plus(pw)
    db = MongoClient(
        f"mongodb://mcp_orch:{pw}@localhost:27019/mcp_orchestrator?authSource=admin"
    )["mcp_orchestrator"]
    msg_deleted = db.messages.delete_many({"$or": [
        {"message": {"$regex": "^inbox smoke test "}},
        {"message": {"$regex": "^unsub test "}},
        {"message": {"$regex": "^broadcast test "}},
        {"message": {"$regex": "^page test "}},
    ]}).deleted_count
    dir_deleted = db.agent_directory.delete_many(
        {"instance": {"$regex": "^inbox-smoke-"}}
    ).deleted_count
    log(f"cleanup: -{msg_deleted} messages, -{dir_deleted} directory entries")


async def main() -> int:
    log("=== Phase C2 inbox resource smoke ===")
    async with httpx.AsyncClient(timeout=5) as c:
        h = (await c.get(HEALTH_URL)).json()
    log(f"server health: {h}")

    results = []
    for name, coro in [
        ("resource_listed",        test_resource_listed()),
        ("resource_read_shape",    test_resource_read_shape()),
        ("subscribe_notification", test_subscribe_notification()),
        ("unsubscribe_silences",   test_unsubscribe_silences()),
        ("authz_blocks_other",     test_authz_blocks_other_agent()),
        ("broadcast_fanout",       test_broadcast_fanout()),
        ("get_messages_cursor",    test_get_messages_cursor()),
    ]:
        try:
            ok = await coro
        except Exception as e:
            log(f"  {name:25} FAIL  exception: {type(e).__name__}: {e}")
            ok = False
        results.append((name, ok))

    await cleanup()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    log(f"=== {passed}/{total} passed ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
