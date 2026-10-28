"""transport_loss_repro.py — pin what HTTPMCPClient.call_tool raises when the
transport dies mid-session.

Background: §13 30-min drop test showed the sync engine exiting cleanly with
stats=errors=0 transport_reconnects=0 within 2s of tailscale-down. Theory:
MCP SDK's streamablehttp_client cancels its internal task group on transport
loss; the resulting CancelledError propagates through sync_engine.run uncaught
(because asyncio.CancelledError is BaseException, not Exception).

This script confirms or refutes that hypothesis by deliberately breaking the
transport mid-session and printing the exception type + traceback.

Usage (from the primary host):
    JUNTO_PEER_URL=http://<peer-host>:8080/mcp \
    JUNTO_PEER_KEY=<peer-admin-key> \
    PEER_SSH_HOST=<peer-host> \
    python contrib/test/transport_loss_repro.py
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


def describe_exception(e: BaseException, label: str) -> None:
    log(f"--- {label} ---")
    log(f"  type: {type(e).__module__}.{type(e).__name__}")
    log(f"  is BaseException: {isinstance(e, BaseException)}")
    log(f"  is Exception: {isinstance(e, Exception)}")
    log(f"  is CancelledError: {isinstance(e, asyncio.CancelledError)}")
    log(f"  args: {e.args!r}")
    log(f"  __cause__: {type(e.__cause__).__name__ if e.__cause__ else None}")
    log(f"  __context__: {type(e.__context__).__name__ if e.__context__ else None}")
    log("  traceback:")
    for line in traceback.format_exception(type(e), e, e.__traceback__):
        for sub in line.rstrip().split("\n"):
            log(f"    {sub}")


async def main() -> int:
    client = HTTPMCPClient(
        url=PEER_URL,
        api_key=PEER_KEY,
        agent_name="repro-transport-loss",
        project="junto",
        role_description="Diagnose what call_tool raises on transport loss.",
    )

    log(f"Connecting to {PEER_URL}…")
    await client.connect()
    log(f"  session_id={client.session_id}")

    log("Pre-failure sanity call: memory_query(query='ping', limit=1)")
    resp = await client.call_tool("memory_query", {"query": "ping", "limit": 1})
    log(f"  OK, result_count={resp.get('result_count', 'unknown')}")

    log("Stopping peer's junto-peer-mcp-server container via SSH…")
    ssh("docker stop junto-peer-mcp-server")
    log("  stopped. Sleeping 2s to ensure connection state has settled.")
    await asyncio.sleep(2)

    log("Post-failure call: memory_query(query='ping', limit=1) — expect exception")
    try:
        resp = await client.call_tool("memory_query", {"query": "ping", "limit": 1})
        log(f"  UNEXPECTED: call succeeded, resp={resp}")
    except BaseException as e:
        describe_exception(e, "exception from call_tool after transport loss")

    log("Restarting peer's mcp-server container…")
    ssh("docker start junto-peer-mcp-server")
    log("  restart issued. Waiting up to 30s for health.")
    for _ in range(15):
        await asyncio.sleep(2)
        status = ssh("docker inspect --format '{{.State.Health.Status}}' junto-peer-mcp-server").stdout.strip()
        if status == "healthy":
            log(f"  healthy after wait.")
            break
        log(f"  status={status}, retrying…")
    else:
        log("  WARN: never reached healthy in 30s; manual check needed.")

    log("Best-effort aclose() on dead client…")
    try:
        await client.aclose()
        log("  aclose returned cleanly")
    except BaseException as e:
        describe_exception(e, "exception from aclose() on dead client")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
