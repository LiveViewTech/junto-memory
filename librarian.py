#!/usr/bin/env python3
"""
Librarian Service for Function Reference Enrichment

Copyright (c) 2024-2026 Thomas Lemmons
Licensed under MIT License with Personal Ownership Clause - see LICENSE file.
Created on personal time. Not a work-for-hire. All IP rights retained by author.

Runs on the host (not in Docker) for direct file access.
Receives webhooks from MCP server when new functions are registered.
Uses Claude API (Haiku) to analyze code and enrich function references.

Usage:
    python3 librarian.py                    # Run with webhook server
    python3 librarian.py --process-queue    # Process existing queue once and exit
"""

import os
import sys
import json
import asyncio
import hashlib
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import httpx
from anthropic import Anthropic

# =============================================================================
# Configuration
# =============================================================================

# Anthropic API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-3-5-haiku-20241022"  # Fast, cheap, good at code

# MCP Server
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:8080")

# Librarian webhook server.
# Loopback by default (design:auth-origin-trust-v0). The enrichment webhook is
# triggered locally; binding 0.0.0.0 exposed it on the tailnet/OVH-wg. Override
# LIBRARIAN_HOST only if a remote trigger source is genuinely required.
LIBRARIAN_HOST = os.getenv("LIBRARIAN_HOST", "127.0.0.1")
LIBRARIAN_PORT = int(os.getenv("LIBRARIAN_PORT", "8085"))

# Project root paths for file resolution
# Configure via LIBRARIAN_PROJECT_ROOTS env var as JSON: {"project": "/path/to/root", ...}
PROJECT_ROOTS = json.loads(os.getenv("LIBRARIAN_PROJECT_ROOTS", "{}"))

# Memory-server session (the session_id argument passed to memory_* tools)
_librarian_session_id: Optional[str] = None

# MCP transport session (the Mcp-Session-Id HTTP header). The server runs a
# FastMCP Streamable-HTTP transport (mcp>=1.x) that REQUIRES an `initialize`
# handshake before any tools/call — without the header the server rejects with
# "400 Bad Request: Missing session ID". This is distinct from the memory
# session_id above. Captured once via _ensure_mcp_session(), reused on every
# call, and re-established if the server forgets it (e.g. after a restart).
_mcp_protocol_session_id: Optional[str] = None
MCP_PROTOCOL_VERSION = "2025-03-26"


# =============================================================================
# MCP Server Communication
# =============================================================================

def _parse_sse(text: str) -> Optional[Dict]:
    """Extract the first JSON object from an SSE 'data:' line."""
    for line in text.split("\n"):
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    return None


async def _ensure_mcp_session(force: bool = False) -> str:
    """Perform the MCP initialize handshake and cache the transport session id.

    Returns the Mcp-Session-Id. Set force=True to discard a stale id and
    re-handshake (used when the server reports the session is gone).
    """
    global _mcp_protocol_session_id

    if _mcp_protocol_session_id and not force:
        return _mcp_protocol_session_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        init_resp = await client.post(
            f"{MCP_SERVER_URL}/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "librarian", "version": "1.0"},
                },
                "id": 0,
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        if init_resp.status_code != 200:
            raise Exception(f"MCP initialize failed: {init_resp.status_code} {init_resp.text}")

        session_id = init_resp.headers.get("mcp-session-id")
        if not session_id:
            raise Exception("MCP initialize returned no Mcp-Session-Id header")

        # Complete the handshake: the spec requires a notifications/initialized
        # before the server will service requests on this session.
        await client.post(
            f"{MCP_SERVER_URL}/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session_id,
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
        )

    _mcp_protocol_session_id = session_id
    print(f"[Librarian] MCP transport session: {session_id}")
    return session_id


async def mcp_call(tool_name: str, params: Dict[str, Any], _retried: bool = False) -> Dict:
    """Call an MCP tool via HTTP (Streamable-HTTP transport with session handshake)."""
    mcp_session = await _ensure_mcp_session()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # MCP uses JSON-RPC style with SSE response
        response = await client.post(
            f"{MCP_SERVER_URL}/mcp",
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": params
                },
                "id": 1
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": mcp_session,
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            }
        )

        # 400 (missing session) / 404 (unknown session) mean the server forgot
        # our transport session — typically after a restart. Re-handshake once.
        if response.status_code in (400, 404) and not _retried:
            print(f"[Librarian] MCP session lost ({response.status_code}); re-initializing")
            await _ensure_mcp_session(force=True)
            return await mcp_call(tool_name, params, _retried=True)

        if response.status_code != 200:
            raise Exception(f"MCP call failed: {response.status_code} {response.text}")

        # Parse SSE response - format is "event: message\ndata: {...}\n\n"
        text = response.text
        result = _parse_sse(text)

        if not result:
            print(f"[DEBUG] Full response text: {text[:500]}")
            raise Exception(f"No valid JSON in SSE response: {text[:200]}")

        if "error" in result:
            raise Exception(f"MCP error: {result['error']}")

        # Extract content from MCP response - result is nested
        if "result" in result:
            inner = result["result"]
            # The tool result is in content[0].text as a JSON string
            if "content" in inner and inner["content"]:
                text_content = inner["content"][0].get("text", "")
                if text_content and text_content.strip():
                    return json.loads(text_content)
                else:
                    print(f"[DEBUG] Empty text_content, full inner: {json.dumps(inner)[:500]}")
                    return {}
            # Or directly as result string
            if "result" in inner:
                inner_result = inner["result"]
                if inner_result and inner_result.strip():
                    return json.loads(inner_result)
                else:
                    return {}
            print(f"[DEBUG] No content or result in inner: {json.dumps(inner)[:500]}")

        print(f"[DEBUG] No result in response: {json.dumps(result)[:500]}")
        return result


async def get_session() -> str:
    """Get or create a librarian session."""
    global _librarian_session_id

    if _librarian_session_id:
        return _librarian_session_id

    result = await mcp_call("memory_start_session", {
        "project": "mcp_RagArch",
        "claude_instance": "librarian",
        "task_description": "Function reference enrichment"
    })

    _librarian_session_id = result.get("session_id")
    print(f"[Librarian] Started session: {_librarian_session_id}")
    return _librarian_session_id


async def end_session():
    """End the librarian session."""
    global _librarian_session_id

    if _librarian_session_id:
        try:
            await mcp_call("memory_end_session", {
                "session_id": _librarian_session_id,
                "summary": "Librarian enrichment session",
                "files_modified": []
            })
            print(f"[Librarian] Ended session: {_librarian_session_id}")
        except Exception as e:
            print(f"[Librarian] Error ending session: {e}")
        finally:
            _librarian_session_id = None


# =============================================================================
# File Resolution
# =============================================================================

def resolve_file_path(file_ref: str, project: str = None) -> Optional[Path]:
    """
    Resolve a file reference to an absolute path.

    file_ref: "src/parser.py:145" or "/absolute/path.py:145"
    project: "emailtriage" etc.

    Returns Path object or None if not found.
    """
    # Remove line number
    file_path = file_ref.split(":")[0] if ":" in file_ref else file_ref

    # If absolute, use directly
    if file_path.startswith("/"):
        p = Path(file_path)
        return p if p.exists() else None

    # Try project-specific root
    if project:
        project_key = project.lower().replace("-", "_").replace(" ", "_")
        if project_key in PROJECT_ROOTS:
            p = Path(PROJECT_ROOTS[project_key]) / file_path
            if p.exists():
                return p

    # Try all project roots
    for root in PROJECT_ROOTS.values():
        p = Path(root) / file_path
        if p.exists():
            return p

    return None


def extract_function_from_file(file_path: Path, line_number: int, func_name: str) -> Optional[str]:
    """
    Extract a function from a file starting at the given line.

    Uses indentation to determine function boundaries.
    Returns the function source code or None.
    """
    try:
        lines = file_path.read_text().splitlines()

        if line_number < 1 or line_number > len(lines):
            return None

        # Find the function start (adjust for 0-indexed)
        start_idx = line_number - 1

        # Look for function definition around the line
        search_start = max(0, start_idx - 5)
        search_end = min(len(lines), start_idx + 5)

        func_start = None
        for i in range(search_start, search_end):
            line = lines[i]
            # Python function
            if f"def {func_name}" in line or f"async def {func_name}" in line:
                func_start = i
                break
            # JavaScript/TypeScript function
            if f"function {func_name}" in line or f"{func_name} = " in line or f"{func_name}(" in line:
                func_start = i
                break
            # C#/Java method
            if func_name in line and ("public" in line or "private" in line or "protected" in line):
                func_start = i
                break

        if func_start is None:
            # Just use the specified line as start
            func_start = start_idx

        # Determine base indentation
        base_line = lines[func_start]
        base_indent = len(base_line) - len(base_line.lstrip())

        # Collect function lines
        func_lines = [lines[func_start]]

        for i in range(func_start + 1, len(lines)):
            line = lines[i]

            # Empty lines are included
            if not line.strip():
                func_lines.append(line)
                continue

            current_indent = len(line) - len(line.lstrip())

            # If we hit same or lower indentation (non-empty), function ended
            if current_indent <= base_indent and line.strip():
                # Unless it's a decorator or continuation
                if not line.strip().startswith("@") and not line.strip().startswith("#"):
                    break

            func_lines.append(line)

            # Limit to reasonable size (100 lines max)
            if len(func_lines) > 100:
                func_lines.append("    # ... (truncated)")
                break

        return "\n".join(func_lines)

    except Exception as e:
        print(f"[Librarian] Error extracting function: {e}")
        return None


# =============================================================================
# Claude Analysis
# =============================================================================

def analyze_function_with_claude(
    func_name: str,
    file_path: str,
    purpose: str,
    code: str,
    existing_gotchas: str = None
) -> Dict[str, Any]:
    """
    Use Claude to analyze a function and extract structured information.

    Returns dict with: signature, parameters, returns, calls, side_effects, complexity, gotchas
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Analyze this function and provide structured information for an AI coding assistant.

Function: {func_name}
File: {file_path}
Stated Purpose: {purpose}
{f"Known Gotchas: {existing_gotchas}" if existing_gotchas else ""}

Code:
```
{code}
```

Provide a JSON response with these fields (omit any that don't apply):

{{
    "signature": "full function signature with types if available",
    "parameters": [
        {{"name": "param1", "type": "string", "description": "what this param does"}}
    ],
    "returns": "return type and description",
    "calls": ["list", "of", "functions", "this", "calls"],
    "side_effects": ["file I/O", "network calls", "state mutations", "etc"],
    "complexity": "O(n) or performance notes",
    "gotchas": "any non-obvious behaviors, edge cases, or warnings - be specific",
    "search_summary": "A rich 1-2 sentence description for search. Include key concepts, synonyms, and what problem this solves. Example: 'ML classification pipeline for email triage. Classifies incoming emails by priority, assigns labels, determines routing.'"
}}

Focus on information that would help another AI assistant use this function correctly.
Be concise but specific about gotchas and edge cases.
The search_summary is critical - it's how other AIs will find this function. Include action verbs and domain terms."""

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )

        # Extract text from response
        text = response.content[0].text

        # Try to parse JSON from response
        # Handle markdown code blocks
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        result = json.loads(text.strip())
        return result

    except json.JSONDecodeError as e:
        print(f"[Librarian] Failed to parse Claude response as JSON: {e}")
        print(f"[Librarian] Response was: {text[:500]}")
        return {}
    except Exception as e:
        print(f"[Librarian] Claude API error: {e}")
        return {}


# =============================================================================
# Enrichment Processing
# =============================================================================

async def enrich_function(func_info: Dict[str, Any]) -> bool:
    """
    Enrich a single function reference.

    Returns True if successfully enriched, False otherwise.
    """
    func_id = func_info.get("id")
    func_name = func_info.get("name")
    file_ref = func_info.get("file")
    project = func_info.get("project")
    has_code = func_info.get("has_code", False)

    print(f"[Librarian] Processing: {func_name} in {file_ref}")

    # Get the code
    code = None

    if has_code:
        # Code was provided in the registration - need to fetch from MCP
        # For now, we'll need to query the function ref to get the code
        # This is a limitation - we should store code separately
        print(f"[Librarian] Code was provided at registration (will use stored)")
        # TODO: Fetch stored code from function ref document
        pass

    # Try to read from file
    if not code:
        # Parse line number from file ref
        line_number = 1
        if ":" in file_ref:
            parts = file_ref.rsplit(":", 1)
            try:
                line_number = int(parts[1])
            except ValueError:
                pass

        resolved_path = resolve_file_path(file_ref, project)
        if resolved_path:
            code = extract_function_from_file(resolved_path, line_number, func_name)
            if code:
                print(f"[Librarian] Extracted code from {resolved_path}")
        else:
            print(f"[Librarian] Could not resolve file path: {file_ref}")

    if not code:
        print(f"[Librarian] No code available for {func_name}, skipping")
        return False

    # Analyze with Claude
    print(f"[Librarian] Analyzing with Claude...")
    analysis = analyze_function_with_claude(
        func_name=func_name,
        file_path=file_ref,
        purpose=func_info.get("purpose", ""),
        code=code,
        existing_gotchas=func_info.get("gotchas")
    )

    if not analysis:
        print(f"[Librarian] Analysis failed for {func_name}")
        return False

    # Call MCP to enrich
    session_id = await get_session()

    enrich_params = {
        "session_id": session_id,
        "func_id": func_id
    }

    if analysis.get("signature"):
        enrich_params["signature"] = analysis["signature"]
    if analysis.get("parameters"):
        enrich_params["parameters"] = analysis["parameters"]
    if analysis.get("returns"):
        # Claude sometimes returns dict, ensure it's a string
        returns = analysis["returns"]
        if isinstance(returns, dict):
            returns = f"{returns.get('type', 'unknown')}: {returns.get('description', '')}"
        enrich_params["returns"] = str(returns)
    if analysis.get("calls"):
        enrich_params["calls"] = analysis["calls"]
    if analysis.get("side_effects"):
        enrich_params["side_effects"] = analysis["side_effects"]
    if analysis.get("complexity"):
        enrich_params["complexity"] = analysis["complexity"]
    if analysis.get("gotchas"):
        # Claude sometimes returns list, ensure it's a string
        gotchas = analysis["gotchas"]
        if isinstance(gotchas, list):
            gotchas = "; ".join(gotchas)
        enrich_params["additional_gotchas"] = str(gotchas)
    if analysis.get("search_summary"):
        enrich_params["search_summary"] = str(analysis["search_summary"])

    try:
        print(f"[Librarian] Calling memory_enrich_function with: {enrich_params}")
        result = await mcp_call("memory_enrich_function", enrich_params)
        print(f"[Librarian] Enriched {func_name}: {result.get('status', 'unknown')}")
        return result.get("status") == "enriched"
    except Exception as e:
        import traceback
        print(f"[Librarian] Failed to enrich {func_name}: {e}")
        traceback.print_exc()
        return False


async def get_all_function_refs(session_id: str) -> List[Dict]:
    """Fetch all function_ref documents from all project collections."""
    all_funcs = []

    try:
        # Get list of projects
        projects_result = await mcp_call("memory_list_projects", {})
        projects = projects_result.get("projects", [])

        # For each project, query for function_refs
        for proj in projects:
            proj_name = proj.get("name", "").replace("proj_", "")
            if not proj_name or proj_name in ["shared", "test"]:
                continue

            try:
                # Use memory_query to find function_refs in this project
                query_result = await mcp_call("memory_query", {
                    "session_id": session_id,
                    "query": "function",
                    "project": proj_name,
                    "memory_types": ["function_ref"],
                    "limit": 100
                })

                for r in query_result.get("results", []):
                    all_funcs.append({
                        "id": r.get("id"),
                        "name": r.get("title", "").split(" - ")[0] if " - " in r.get("title", "") else r.get("title"),
                        "file": r.get("content", "").split("**Location:** ")[1].split("\n")[0] if "**Location:** " in r.get("content", "") else "",
                        "project": proj_name,
                        "purpose": r.get("content", "").split("**Purpose:** ")[1].split("\n")[0] if "**Purpose:** " in r.get("content", "") else "",
                        "has_code": False
                    })
            except Exception as e:
                print(f"[Librarian] Error querying project {proj_name}: {e}")

    except Exception as e:
        print(f"[Librarian] Error listing projects: {e}")

    return all_funcs


async def re_enrich_all():
    """Re-enrich all existing function references."""
    print("[Librarian] Re-enriching all function references...")

    session_id = await get_session()

    # Get all function refs from Chroma directly
    import chromadb
    client = await chromadb.AsyncHttpClient(host="localhost", port=8001)
    collections = await client.list_collections()

    all_funcs = []
    for col in collections:
        if not col.name.startswith("proj_"):
            continue

        try:
            # Get all function_refs from this collection
            results = await col.get(
                where={"type": "function_ref"},
                include=["metadatas", "documents"]
            )

            for i, doc_id in enumerate(results.get("ids", [])):
                meta = results["metadatas"][i] if results.get("metadatas") else {}
                all_funcs.append({
                    "id": doc_id,
                    "name": meta.get("func_name", "unknown"),
                    "file": meta.get("func_file", ""),
                    "project": col.name.replace("proj_", ""),
                    "purpose": meta.get("func_purpose", ""),
                    "gotchas": meta.get("gotchas"),
                    "has_code": meta.get("has_code") == "true"
                })
        except Exception as e:
            print(f"[Librarian] Error getting functions from {col.name}: {e}")

    print(f"[Librarian] Found {len(all_funcs)} function references to re-enrich")

    enriched = 0
    failed = 0

    for func in all_funcs:
        try:
            print(f"[Librarian] Re-enriching: {func['name']}")
            if await enrich_function(func):
                enriched += 1
            else:
                failed += 1
        except Exception as e:
            print(f"[Librarian] Error re-enriching {func['name']}: {e}")
            failed += 1

        # Small delay between items
        await asyncio.sleep(0.5)

    print(f"[Librarian] Re-enrichment complete: {enriched} succeeded, {failed} failed")
    return enriched, failed


async def process_queue():
    """Process all pending items in the enrichment queue."""
    print("[Librarian] Fetching enrichment queue...")

    session_id = await get_session()

    try:
        queue = await mcp_call("memory_get_enrichment_queue", {
            "session_id": session_id
        })
    except Exception as e:
        print(f"[Librarian] Failed to get queue: {e}")
        return

    items = queue.get("items", [])
    pending_count = queue.get("pending_count", 0)

    print(f"[Librarian] Found {pending_count} pending items")

    if not items:
        return

    enriched = 0
    failed = 0

    for item in items:
        try:
            if await enrich_function(item):
                enriched += 1
            else:
                failed += 1
        except Exception as e:
            print(f"[Librarian] Error processing {item.get('name')}: {e}")
            failed += 1

        # Small delay between items to be nice to APIs
        await asyncio.sleep(0.5)

    print(f"[Librarian] Completed: {enriched} enriched, {failed} failed")


# =============================================================================
# Webhook Server
# =============================================================================

async def handle_webhook(request_body: bytes) -> Dict:
    """Handle incoming webhook from MCP server."""
    try:
        data = json.loads(request_body)
        event_type = data.get("event")

        if event_type == "function_registered":
            func_info = data.get("function", {})
            print(f"[Librarian] Received webhook: new function {func_info.get('name')}")

            # Process immediately
            success = await enrich_function(func_info)
            return {"status": "processed", "enriched": success}

        elif event_type == "ping":
            return {"status": "pong"}

        else:
            return {"status": "unknown_event", "event": event_type}

    except Exception as e:
        print(f"[Librarian] Webhook error: {e}")
        return {"status": "error", "message": str(e)}


async def run_webhook_server(host: str, port: int):
    """Run the webhook server using aiohttp."""
    from aiohttp import web

    async def webhook_handler(request):
        body = await request.read()
        result = await handle_webhook(body)
        return web.json_response(result)

    async def health_handler(request):
        return web.json_response({
            "status": "healthy",
            "service": "librarian",
            "session": _librarian_session_id
        })

    async def re_enrich_handler(request):
        """Trigger re-enrichment of all function refs."""
        print("[Librarian] Re-enrich-all triggered via HTTP")
        enriched, failed = await re_enrich_all()
        return web.json_response({
            "status": "complete",
            "enriched": enriched,
            "failed": failed
        })

    app = web.Application()
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/re-enrich-all", re_enrich_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"[Librarian] Webhook server running on http://{host}:{port}")
    print(f"[Librarian] Endpoints:")
    print(f"  POST /webhook        - Receive function registration events")
    print(f"  POST /re-enrich-all  - Re-enrich all existing function refs")
    print(f"  GET  /health         - Health check")

    # Keep running
    while True:
        await asyncio.sleep(3600)


# =============================================================================
# Main
# =============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Librarian Service for Function Enrichment")
    parser.add_argument("--process-queue", action="store_true",
                        help="Process existing queue once and exit")
    parser.add_argument("--re-enrich-all", action="store_true",
                        help="Re-enrich all existing function refs and exit")
    parser.add_argument("--host", default=LIBRARIAN_HOST,
                        help=f"Webhook server host (default: {LIBRARIAN_HOST})")
    parser.add_argument("--port", type=int, default=LIBRARIAN_PORT,
                        help=f"Webhook server port (default: {LIBRARIAN_PORT})")
    args = parser.parse_args()

    webhook_host = args.host
    webhook_port = args.port

    print("""
╔══════════════════════════════════════════════════════════════╗
║              Librarian - Function Enrichment Service          ║
╠══════════════════════════════════════════════════════════════╣
║  Analyzes code and enriches function references with:         ║
║  - Full signatures and parameter types                        ║
║  - Function call graphs (calls/called_by)                     ║
║  - Side effects detection                                     ║
║  - Complexity analysis                                        ║
║  - Additional gotchas and edge cases                          ║
╚══════════════════════════════════════════════════════════════╝
""")

    try:
        if args.process_queue:
            # One-shot mode: process queue and exit
            await process_queue()
            await end_session()
        elif args.re_enrich_all:
            # Re-enrich all existing function refs
            await re_enrich_all()
            await end_session()
        else:
            # Server mode: run webhook server
            # Process any existing queue first
            await process_queue()
            # Then run server for new items
            await run_webhook_server(webhook_host, webhook_port)
    except KeyboardInterrupt:
        print("\n[Librarian] Shutting down...")
        await end_session()
    except Exception as e:
        print(f"[Librarian] Fatal error: {e}")
        await end_session()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
