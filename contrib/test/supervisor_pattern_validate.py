"""supervisor_pattern_validate.py — verify the subtask-supervisor pattern
recovers cleanly from a poisoned cancel scope.

Goal: if the inner task's HTTPMCPClient gets killed by transport-loss
CancelledError, the OUTER supervisor task should be able to (a) catch the
cancellation, (b) sleep normally without its own awaits getting cancelled,
and (c) build a fresh HTTPMCPClient + run another inner task.

If this passes, the supervisor pattern is the right structure for the
sync_engine fix.

Usage (from the primary host):
    JUNTO_PEER_URL=http://<peer-host>:8080/mcp \
    JUNTO_PEER_KEY=<peer-admin-key> \
    PEER_SSH_HOST=<peer-host> \
    python contrib/test/supervisor_pattern_validate.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import traceback

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "src"))

from shared_memory.sync_engine import HTTPMCPClient  # noqa: E402

PEER_URL = os.environ["JUNTO_PEER_URL"]
PEER_KEY = os.environ["JUNTO_PEER_KEY"]
PEER_SSH_HOST = os.environ["PEER_SSH_HOST"]
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519_junto_test")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ssh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "ssh", "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"ubuntu@{PEER_SSH_HOST}", cmd,
        ],
        capture_output=True, text=True, check=True,
    )


async def make_client() -> HTTPMCPClient:
    c = HTTPMCPClient(
        url=PEER_URL,
        api_key=PEER_KEY,
        agent_name="supervisor-validate",
        project="junto",
        role_description="Validate supervisor pattern recovers from cancel-scope poison.",
    )
    await c.connect()
    return c


async def inner_with_break() -> None:
    """One inner-task lifecycle: connect, call, transport break, call again."""
    log("  [inner-1] connecting…")
    c = await make_client()
    log(f"  [inner-1] connected, session_id={c.session_id}")

    log("  [inner-1] pre-break call")
    r = await c.call_tool("memory_query", {"query": "ping", "limit": 1})
    log(f"  [inner-1] OK, result_count={r.get('result_count')}")

    log("  [inner-1] stopping peer mcp-server via SSH (subprocess)")
    ssh("docker stop junto-peer-mcp-server")
    log("  [inner-1] stopped. Calling again (expect CancelledError to crash inner task)")
    # Don't catch — let it propagate to the supervisor.
    await c.call_tool("memory_query", {"query": "ping", "limit": 1})
    log("  [inner-1] UNEXPECTED: call succeeded post-break")


async def inner_simple() -> None:
    """Second inner-task lifecycle: just connect + one call."""
    log("  [inner-2] connecting…")
    c = await make_client()
    log(f"  [inner-2] connected, session_id={c.session_id}")
    log("  [inner-2] call")
    r = await c.call_tool("memory_query", {"query": "ping", "limit": 1})
    log(f"  [inner-2] OK, result_count={r.get('result_count')}")
    log("  [inner-2] aclose")
    await c.aclose()


async def main() -> int:
    log("=== Phase 1: inner task is expected to fail on transport break ===")
    inner_1 = asyncio.create_task(inner_with_break(), name="inner-1")
    cancelled_caught = False
    try:
        await inner_1
        log("supervisor: inner-1 returned normally (UNEXPECTED)")
    except asyncio.CancelledError as e:
        cancelled_caught = True
        log(f"supervisor: CAUGHT CancelledError from inner-1: {e!r}")
    except BaseException as e:
        log(f"supervisor: caught {type(e).__name__}: {e!r}")
        log(f"  traceback:")
        for line in traceback.format_exception(type(e), e, e.__traceback__):
            for sub in line.rstrip().split("\n"):
                log(f"    {sub}")

    log(f"=== Phase 2: outer supervisor MUST be able to sleep + rebuild ===")
    log(f"  cancelled_caught={cancelled_caught}")

    log("supervisor: restarting peer mcp-server via SSH")
    ssh("docker start junto-peer-mcp-server")
    log("supervisor: waiting for healthy (subprocess polls; doesn't touch event loop)")
    for _ in range(15):
        time.sleep(2)
        st = ssh("docker inspect --format '{{.State.Health.Status}}' junto-peer-mcp-server").stdout.strip()
        if st == "healthy":
            log("  healthy.")
            break
        log(f"  status={st}, retrying…")
    else:
        log("  WARN: never reached healthy; abort phase 3.")
        return 1

    log("supervisor: testing that outer task's asyncio.sleep is NOT cancelled")
    try:
        await asyncio.sleep(1)
        log("supervisor: ✓ outer asyncio.sleep(1) completed cleanly")
    except asyncio.CancelledError:
        log("supervisor: ✗ outer asyncio.sleep was CANCELLED — supervisor pattern fails")
        return 2

    log("=== Phase 3: build fresh client in new inner task ===")
    inner_2 = asyncio.create_task(inner_simple(), name="inner-2")
    try:
        await inner_2
        log("supervisor: ✓ inner-2 completed successfully — supervisor pattern WORKS")
    except BaseException as e:
        log(f"supervisor: ✗ inner-2 failed: {type(e).__name__}: {e!r}")
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
