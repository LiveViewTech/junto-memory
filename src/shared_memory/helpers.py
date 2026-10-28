# =============================================================================
# Helper Functions - All async for use with AsyncHttpClient
# =============================================================================

import asyncio
import fnmatch
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shared_memory.config import (
    DEFAULT_EXPIRY_DAYS,
    OVERLAP_WINDOW_HOURS,
    PROJECT_PREFIX,
    SESSION_IDLE_HOURS,
    SESSION_TTL_DAYS,
    SHARED_PREFIX,
    SIGNAL_RETENTION_HOURS,
    STALE_LOCK_MINUTES,
)
from shared_memory.state import active_sessions, active_signals, file_locks

logger = logging.getLogger(__name__)

MIN_RELEVANCE_THRESHOLD = 0.3  # 30% minimum relevance


def utc_now() -> datetime:
    """Return the current time as a UTC-aware datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO8601 string with explicit offset."""
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value) -> Optional[datetime]:
    """Parse an ISO timestamp into a UTC-aware datetime.

    Tolerates both naive (legacy) and aware ISO strings. Naive timestamps
    are assumed to be UTC since the server container has always run UTC.
    Returns None on failure.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def calculate_relevance(distance: float) -> float:
    """Convert Chroma L2 distance to 0-1 relevance score.

    L2 distances typically range 0-2 for normalized embeddings.
    We clamp and convert to similarity percentage.
    """
    # Clamp distance to reasonable range
    dist = max(0, min(distance, 2.0))
    # Convert to similarity (0-1 range)
    return 1 - (dist / 2)


def normalize_project(name: str) -> str:
    """Canonical project name: lowercase, hyphens/spaces → underscores.

    Single source of truth for project-name comparison. All writes and filters
    on the `project` field must pass through this so case/separator variants
    (claudeControl vs claudecontrol vs claude-control) collapse to one bucket.
    """
    if not name:
        return name
    return name.lower().replace("-", "_").replace(" ", "_")


async def get_project_collection(client, project: str):
    """Get or create a project-specific collection (async)."""
    norm = normalize_project(project)
    name = f"{PROJECT_PREFIX}{norm}"
    return await client.get_or_create_collection(
        name=name,
        metadata={"project": norm, "created": utc_now_iso()}
    )


async def get_shared_collection(client, collection_type: str):
    """Get a shared collection (patterns, context, work) (async)."""
    return await client.get_or_create_collection(name=f"{SHARED_PREFIX}{collection_type}")


async def embed_query_once(collection, text: str) -> Optional[List[Any]]:
    """Embed `text` ONCE, OFF the event loop, so the caller can reuse the vector
    across every collection it scans.

    ⚠️ THE EMBEDDING IS CLIENT-SIDE, IN THIS PROCESS. `AsyncHttpClient`
    collections carry a `DefaultEmbeddingFunction` (ONNX MiniLM, 384-dim) and
    `query_texts=[...]` runs it HERE, not in the chromadb container. Two
    consequences that together were the whole performance story
    (learning_faca6ab430b48cbc, measured 2026-08-11):

    1. **It was run once PER COLLECTION.** memory_query scans project + shared
       patterns + shared context, and `/recall` does the same — so one request
       embedded the identical text THREE times at ~96-138 ms each, ~300-400 ms
       of pure duplicate work.
    2. **It ran ON the event loop.** ONNX inference is a blocking C call, so
       while one agent embedded, every other agent's request queued behind it —
       including 2 ms calls. Measured: `memory_query` 1-2 ms at rest vs 167 ms
       median (331 ms max) under 6 concurrent `/recall` clients, recovering
       instantly when load stopped. The server was never slow; it serialised.

    `asyncio.to_thread` fixes (2) because ONNX releases the GIL during
    inference; reusing the returned vector fixes (1).

    SAFE TO SHARE ONE VECTOR ACROSS COLLECTIONS because every collection in
    this server is created by `get_project_collection` / `get_shared_collection`,
    neither of which passes an `embedding_function` — so all of them use the
    same default. ⚠️ If a collection is ever created with a custom embedding
    function, it MUST embed its own query text or its distances become garbage.

    FAILS OPEN: returns None on any problem (private attribute missing, model
    error). Callers must fall back to `query_texts=[...]`, which is exactly the
    previous behaviour — a perf optimisation must never cost a result.
    """
    try:
        ef = getattr(collection, "_embedding_function", None)
        if ef is None:
            return None
        vectors = await asyncio.to_thread(ef, [text])
        if vectors is None or len(vectors) == 0:
            return None
        vector = vectors[0]
        return vector.tolist() if hasattr(vector, "tolist") else list(vector)
    except Exception as e:  # noqa: BLE001 - optimisation path, degrade to query_texts
        logger.warning("embed_query_once failed (%s) — falling back to query_texts", e)
        return None


def query_kwargs(text: str, vector: Optional[List[Any]]) -> Dict:
    """Chroma query kwargs: the precomputed vector when we have one, else the
    raw text (the pre-2026-08-11 path). Keeps the fallback in ONE place so a
    caller cannot half-adopt it."""
    return {"query_embeddings": [vector]} if vector else {"query_texts": [text]}


def project_from_collection(collection_name: str) -> str:
    """Derive the owning project from a Chroma collection name.

    THE COLLECTION IS THE AUTHORITY ON SCOPE, not the `project` metadata key.
    Routing has always been by collection; the metadata field is a copy — and
    for learnings it was a MISSING copy: `memory_record_learning` never wrote
    the key at all, so 4374 of 4397 learnings (99.5%, every project, since day
    one) reported `project: ""` on the get_by_id / query surfaces despite being
    correctly filed. Reported by coordinator@nimbus 2026-08-10 (msg_b91f7992752a).

    Read surfaces call this as a FALLBACK so every historical doc reads
    correctly without a backfill migration — a migration can half-apply and
    then nobody knows which half they are looking at. Shared collections have
    no project by construction and return "".
    """
    if collection_name and collection_name.startswith(PROJECT_PREFIX):
        return collection_name[len(PROJECT_PREFIX):]
    return ""


def generate_doc_id(content: str, doc_type: str) -> str:
    """Generate a stable document ID from content hash."""
    hash_input = f"{doc_type}:{content[:500]}:{utc_now_iso()}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def generate_content_hash(content: str) -> str:
    """Generate a hash of normalized content for duplicate detection."""
    # Normalize: lowercase, collapse whitespace, strip
    normalized = ' '.join(content.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


async def check_duplicate(collection, content: str, threshold: float = 0.95) -> Optional[Dict]:
    """Check if similar content already exists in the collection.

    Returns the existing doc info if a duplicate is found, None otherwise.
    Uses both hash matching (exact) and embedding similarity (near-duplicate).
    """
    content_hash = generate_content_hash(content)

    # First, check for exact hash match via metadata
    try:
        results = await collection.get(
            where={"content_hash": content_hash},
            include=["metadatas"]
        )
        if results["ids"]:
            return {
                "type": "exact",
                "doc_id": results["ids"][0],
                "title": results["metadatas"][0].get("title", "Unknown")
            }
    except Exception:
        pass  # content_hash field might not exist on older docs

    # Then check for near-duplicate via embedding similarity
    try:
        results = await collection.query(
            query_texts=[content[:1000]],  # Use first 1000 chars for query
            n_results=1,
            include=["metadatas", "distances"]
        )
        if results["distances"] and results["distances"][0]:
            # Chroma returns L2 distance; convert to similarity
            # Lower distance = more similar. Threshold ~0.1 for very similar
            distance = results["distances"][0][0]
            if distance < 0.15:  # Very similar content
                return {
                    "type": "similar",
                    "doc_id": results["ids"][0][0],
                    "title": results["metadatas"][0][0].get("title", "Unknown"),
                    "similarity": f"{(1 - distance):.0%}"
                }
    except Exception:
        pass

    return None


def calculate_expiry(memory_type: str, custom_days: int = None) -> Optional[str]:
    """Calculate expiry date based on memory type or custom value."""
    if custom_days is not None:
        if custom_days <= 0:
            return None  # Explicitly no expiry
        days = custom_days
    else:
        days = DEFAULT_EXPIRY_DAYS.get(memory_type)

    if days is None:
        return None

    return (utc_now() + timedelta(days=days)).isoformat()


def is_expired(meta: Dict) -> bool:
    """Check if a document has expired based on expires_at field."""
    expires_at = meta.get("expires_at")
    if not expires_at:
        return False
    parsed = parse_timestamp(expires_at)
    if parsed is None:
        return False
    return parsed < utc_now()


async def update_access_stats(collection, doc_id: str):
    """Update access count and last_accessed for a document."""
    try:
        result = await collection.get(ids=[doc_id], include=["metadatas"])
        if result["ids"]:
            meta = result["metadatas"][0]
            meta["access_count"] = meta.get("access_count", 0) + 1
            meta["last_accessed"] = utc_now_iso()
            await collection.update(ids=[doc_id], metadatas=[meta])
    except Exception:
        pass  # Non-critical, don't fail the query


def check_session(session_id: str) -> bool:
    """Check if a session is registered."""
    return session_id in active_sessions


def require_session(session_id: Optional[str]) -> str:
    """Validate session exists, return error message if not.

    On success, touches the session's last_activity so idle-expiry
    (cleanup_stale_sessions) measures real inactivity — any authenticated
    tool call counts as activity, not just update_work/heartbeat.
    """
    if not session_id:
        return "ERROR: No session_id provided. You must call memory_start_session first and include the returned session_id in all subsequent calls."
    if not check_session(session_id):
        return f"ERROR: Session '{session_id}' not found. Call memory_start_session first to register your session."
    active_sessions[session_id]["last_activity"] = utc_now_iso()
    return ""


STALENESS_THRESHOLD_DAYS = 30


def format_age(iso_timestamp: str) -> str:
    """Convert ISO timestamp to human-readable age string."""
    created = parse_timestamp(iso_timestamp)
    if created is None:
        return "unknown"
    delta = utc_now() - created
    if delta.days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return "just now"
        return f"{hours}h ago"
    elif delta.days == 1:
        return "yesterday"
    elif delta.days < 30:
        return f"{delta.days}d ago"
    elif delta.days < 365:
        months = delta.days // 30
        return f"{months}mo ago"
    else:
        years = delta.days // 365
        return f"{years}y ago"


def format_staleness_warning(meta: dict) -> str:
    """Generate staleness warning for old documents."""
    updated = meta.get("updated") or meta.get("created")
    if not updated:
        return ""
    parsed = parse_timestamp(updated)
    if parsed is None:
        return ""
    age_days = (utc_now() - parsed).days
    if age_days >= STALENESS_THRESHOLD_DAYS:
        return f"This document is {format_age(updated)} old. Search for newer versions before relying on it."
    return ""


def format_status_warning(status: str, superseded_by: str = None) -> str:
    """Generate warning for non-active documents."""
    if status == "active":
        return ""
    if status == "deprecated":
        return "\n⚠️ WARNING: This document is DEPRECATED. It may be outdated or no longer recommended.\n"
    if status == "superseded":
        msg = "\n⚠️ WARNING: This document has been SUPERSEDED."
        if superseded_by:
            msg += f" See newer version: {superseded_by}"
        return msg + "\n"
    if status == "archived":
        return "\n📁 NOTE: This document is ARCHIVED (historical reference only).\n"
    return ""


async def check_overlap(client, project: str, files_touched: List[str], current_session: str) -> List[Dict]:
    """Check if other Claudes recently touched these files (async)."""
    overlaps = []
    work_collection = await get_shared_collection(client, "work")

    cutoff = (utc_now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    for file_path in files_touched:
        try:
            results = await work_collection.query(
                query_texts=[file_path],
                n_results=10,
                where={"status": {"$in": ["in_progress", "completed"]}}
            )
        except Exception:
            continue

        if results["documents"] and results["documents"][0]:
            for meta in results["metadatas"][0]:
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                if meta.get("session_id") != current_session:
                    overlaps.append({
                        "file": file_path,
                        "other_session": meta.get("session_id"),
                        "other_claude": meta.get("claude_instance"),
                        "when": meta.get("updated"),
                        "what": meta.get("title")
                    })

    return overlaps


def format_overlap_warning(overlaps: List[Dict], current_claude: str = None) -> str:
    """Format overlap warnings for Claude. Keeps it brief."""
    if not overlaps:
        return ""

    # Filter out self-overlaps (same claude instance touching same files is normal)
    other_overlaps = [o for o in overlaps if o.get('other_claude') != current_claude]

    if not other_overlaps:
        return ""  # Only self-overlaps, no warning needed

    # Group by other claude to keep it concise
    by_claude = {}
    for o in other_overlaps:
        key = o.get('other_claude', 'unknown')
        if key not in by_claude:
            by_claude[key] = {'files': set(), 'task': o.get('what', '')}
        by_claude[key]['files'].add(o['file'].split('/')[-1])  # Just filename

    if not by_claude:
        return ""

    # One line per other claude
    warning = "\n⚠️ OVERLAP: "
    parts = []
    for claude, info in by_claude.items():
        files_str = ', '.join(list(info['files'])[:3])  # Max 3 files
        if len(info['files']) > 3:
            files_str += f" +{len(info['files'])-3} more"
        parts.append(f"{claude} touched {files_str}")
    warning += "; ".join(parts) + "\n"
    return warning


# =============================================================================
# File Locking Helper Functions
# =============================================================================

def is_lock_stale(lock_info: Dict) -> bool:
    """Check if a lock is stale (session inactive > STALE_LOCK_MINUTES)."""
    session_id = lock_info.get("session_id")
    if session_id not in active_sessions:
        return True  # Session ended, lock is stale

    last_activity = active_sessions[session_id].get("last_activity", "")
    if not last_activity:
        return False

    last_time = parse_timestamp(last_activity)
    if last_time is None:
        return False
    stale_threshold = utc_now() - timedelta(minutes=STALE_LOCK_MINUTES)
    return last_time < stale_threshold


def normalize_path(path: str) -> str:
    """Normalize a file path for consistent lock matching."""
    # Remove leading/trailing slashes, normalize separators
    return path.strip().strip('/').replace('\\', '/')


def path_matches_pattern(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a pattern (supports glob-like wildcards)."""
    file_path = normalize_path(file_path)
    pattern = normalize_path(pattern)

    # Directory pattern: "NimbusCommon/" matches all files within
    if pattern.endswith('/'):
        return file_path.startswith(pattern) or file_path.startswith(pattern[:-1] + '/')

    # Exact match or glob pattern
    return fnmatch.fnmatch(file_path, pattern) or file_path == pattern


def get_files_in_directory_lock(dir_path: str) -> List[str]:
    """Get all currently locked files that fall under a directory lock."""
    dir_path = normalize_path(dir_path)
    if not dir_path.endswith('/'):
        dir_path += '/'

    matching = []
    for locked_file in file_locks.keys():
        if locked_file.startswith(dir_path):
            matching.append(locked_file)
    return matching


def release_session_locks(session_id: str) -> List[str]:
    """Release all locks held by a session. Returns list of released files."""
    released = []
    to_remove = [f for f, info in file_locks.items() if info.get("session_id") == session_id]
    for f in to_remove:
        del file_locks[f]
        released.append(f)
    return released


def _sids_with_live_subscription() -> set:
    """App session ids that hold at least one live inbox SSE subscription.

    Inverts mcp_session_to_app against the union of inbox_subscriptions
    buckets. Best-effort: any failure returns the empty set, which only makes
    idle-expiry MORE conservative callers-side when paired with `discard`
    semantics — callers must treat membership as \"do not idle-expire\".
    """
    subscribed = set()
    try:
        from shared_memory.state import mcp_session_to_app
        from shared_memory.tools.messaging import inbox_subscriptions
        live_transports = set()
        for bucket in inbox_subscriptions.values():
            live_transports.update(bucket)
        for transport, sid in mcp_session_to_app.items():
            if transport in live_transports:
                subscribed.add(sid)
    except Exception:
        pass
    return subscribed


def cleanup_stale_sessions():
    """Expire stale sessions on two tiers (backlog_940b9f9c66e1):

    1. Hard TTL: no activity for SESSION_TTL_DAYS (14d) — unconditional.
    2. Idle expiry: no tool call for SESSION_IDLE_HOURS (default 6h, env
       JUNTO_SESSION_IDLE_HOURS, 0 disables) AND no live inbox SSE
       subscription. A subscribed-but-quiet plugin session is healthy and is
       never idle-expired; a session whose process died (or whose socket
       half-opened and got keepalive-pruned) has no subscription and goes.
       require_session touches last_activity on every tool call, so tier 2
       measures real inactivity.
    """
    cutoff = utc_now() - timedelta(days=SESSION_TTL_DAYS)
    idle_cutoff = (
        utc_now() - timedelta(hours=SESSION_IDLE_HOURS)
        if SESSION_IDLE_HOURS > 0 else None
    )
    subscribed_sids = _sids_with_live_subscription() if idle_cutoff else set()
    to_remove = []
    for sid, info in active_sessions.items():
        last_activity = parse_timestamp(info.get("last_activity", ""))
        if not last_activity:
            continue
        if last_activity < cutoff:
            to_remove.append(sid)
        elif (idle_cutoff is not None
                and last_activity < idle_cutoff
                and sid not in subscribed_sids):
            to_remove.append(sid)
    for sid in to_remove:
        # Release any locks held by this session
        release_session_locks(sid)
        # Drop MCP-session binding + inbox subscriptions (Phase C2)
        try:
            from shared_memory.state import mcp_session_to_app
            from shared_memory.tools.messaging import inbox_subscriptions
            dropped = [k for k, v in mcp_session_to_app.items() if v == sid]
            for k in dropped:
                mcp_session_to_app.pop(k, None)
            if dropped:
                for uri in list(inbox_subscriptions.keys()):
                    bucket = inbox_subscriptions.get(uri)
                    if bucket is None:
                        continue
                    for s in dropped:
                        bucket.discard(s)
                    if not bucket:
                        inbox_subscriptions.pop(uri, None)
        except Exception:
            pass
        del active_sessions[sid]
    if to_remove:
        print(f"[MCP] Auto-expired {len(to_remove)} stale sessions "
              f"(>{SESSION_TTL_DAYS}d TTL or >{SESSION_IDLE_HOURS}h idle with no live subscription)")
    return to_remove


def cleanup_stale_signals():
    """Remove signals older than SIGNAL_RETENTION_HOURS."""
    cutoff = utc_now() - timedelta(hours=SIGNAL_RETENTION_HOURS)
    to_remove = []
    for signal_name, info in active_signals.items():
        signal_time = parse_timestamp(info.get("timestamp", ""))
        if signal_time and signal_time < cutoff:
            to_remove.append(signal_name)
    for s in to_remove:
        del active_signals[s]


def get_relevant_locks_for_session(session_id: str, project: str) -> List[Dict]:
    """Get locks relevant to a session based on project and recent file patterns."""
    # For now, return all locks in the same project or shared paths
    relevant = []
    for file_path, lock_info in file_locks.items():
        if lock_info.get("session_id") != session_id:
            relevant.append({
                "file": file_path,
                "held_by": lock_info.get("claude_instance"),
                "session_id": lock_info.get("session_id"),
                "since": lock_info.get("locked_at"),
                "reason": lock_info.get("reason"),
                "stale": is_lock_stale(lock_info)
            })
    return relevant


async def get_recent_modifications(client, project: str, session_id: str) -> List[Dict]:
    """Get files modified recently by other sessions."""
    modifications = []
    cutoff = (utc_now() - timedelta(hours=OVERLAP_WINDOW_HOURS)).isoformat()

    try:
        work_collection = await get_shared_collection(client, "work")
        results = await work_collection.get(
            where={"project": project} if project else None,
            include=["metadatas"]
        )

        if results["metadatas"]:
            for meta in results["metadatas"]:
                if meta.get("session_id") == session_id:
                    continue
                updated = meta.get("updated", "")
                if updated < cutoff:
                    continue
                files = json.loads(meta.get("files_touched", "[]"))
                for f in files[:3]:  # Limit to 3 files per work item
                    modifications.append({
                        "file": f,
                        "modified_by": meta.get("claude_instance"),
                        "when": updated,
                        "summary": meta.get("title", "")[:50]
                    })
    except Exception:
        pass

    return modifications[:10]  # Limit total


def get_pending_signals(claude_instance: str) -> List[Dict]:
    """Get signals that might be relevant to this agent."""
    cleanup_stale_signals()
    signals = []
    for signal_name, info in active_signals.items():
        signals.append({
            "signal": signal_name,
            "from": info.get("from_claude"),
            "timestamp": info.get("timestamp"),
            "details": info.get("details")
        })
    return signals


def get_blocking_others(claude_instance: str) -> List[Dict]:
    """Find agents that are blocked waiting for this agent."""
    blocking = []
    for sid, info in active_sessions.items():
        if info.get("blocked_by") == claude_instance:
            blocking.append({
                "agent": info.get("claude_instance"),
                "session_id": sid,
                "waiting_for": info.get("waiting_for_signal"),
                "reason": info.get("blocked_reason")
            })
    return blocking


def _match_path_patterns(working_directory: str, path_patterns: List[str]) -> bool:
    """Check if a working directory matches any of the path patterns."""
    if not working_directory or not path_patterns:
        return False

    # Normalize separators
    wd_normalized = working_directory.replace("\\", "/").lower()

    for pattern in path_patterns:
        pattern_normalized = pattern.replace("\\", "/").lower()
        if fnmatch.fnmatch(wd_normalized, pattern_normalized):
            return True
        # Also try matching just the end of the path
        if fnmatch.fnmatch(wd_normalized, f"*/{pattern_normalized}"):
            return True
        if fnmatch.fnmatch(wd_normalized, f"*/{pattern_normalized}/*"):
            return True
        # Check if pattern appears as a substring
        if pattern_normalized.strip("*") in wd_normalized:
            return True

    return False


async def get_interface_updates(client, project: str, last_session_end: str = None) -> List[Dict]:
    """Get interface contracts that changed recently."""
    updates = []
    try:
        proj_collection = await get_project_collection(client, project)
        results = await proj_collection.get(
            where={"type": "interface"},
            include=["metadatas"]
        )

        cutoff = last_session_end or (utc_now() - timedelta(hours=24)).isoformat()

        if results["metadatas"]:
            for meta in results["metadatas"]:
                updated = meta.get("updated", "")
                if updated > cutoff:
                    updates.append({
                        "interface": meta.get("interface_name", meta.get("title")),
                        "version": meta.get("interface_version", "unknown"),
                        "changed_by": meta.get("claude_instance"),
                        "when": updated
                    })
    except Exception:
        pass

    return updates
