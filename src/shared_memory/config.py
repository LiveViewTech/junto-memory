"""
Configuration constants for the Shared Memory MCP Server.

All configuration values, environment-driven settings, and constant
definitions extracted from server.py for modular organization.
"""

import os
import re
from typing import Any, Dict

# =============================================================================
# Configuration
# =============================================================================

CHROMA_HOST = os.getenv("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.getenv("CHROMA_PORT", "8001"))

# MongoDB configuration
MONGO_HOST = os.getenv("MONGO_HOST", "localhost")
MONGO_PORT = int(os.getenv("MONGO_PORT", "27018"))
MONGO_DB = os.getenv("MONGO_DB", "mcp_orchestrator")
MONGO_USER = os.getenv("MONGO_USER", "mcp_orch")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD", "")

# Op-log origin identifier (Phase 1). Stable per-deployment string that
# tags every op-log entry written by this server. Used by sync_pull/push
# to maintain per-origin monotonic seq cursors. Per design v0.3.0 §4.5
# this MUST be operator-configured to a stable, unique-per-deployment
# value before Phase 1 instrumentation lands. Default `central` is a
# placeholder for the singleton-deployment case; a future LAN-local
# deployment MUST override (e.g. `lan-spg-office`).
ORIGIN_SERVER_ID = os.getenv("ORIGIN_SERVER_ID", "central")

# Database registry - external databases available for querying
# Built dynamically from environment variables with prefix DB_<NAME>_*
# Example: DB_MYDB_TYPE=mssql, DB_MYDB_HOST=server.com, DB_MYDB_PORT=1433, etc.
DB_REGISTRY: Dict[str, Dict[str, Any]] = {}

_DEFAULT_PORTS = {"mssql": 1433, "mysql": 3306}

def _build_db_registry():
    """Build DB_REGISTRY from DB_<NAME>_TYPE env vars."""
    seen = set()
    for key in os.environ:
        if key.startswith("DB_") and key.endswith("_TYPE"):
            name = key[3:-5].lower()  # DB_NIMBUS_TYPE -> nimbus
            if name not in seen:
                seen.add(name)
                prefix = f"DB_{name.upper()}_"
                db_type = os.getenv(f"{prefix}TYPE", "mssql").lower()
                default_port = _DEFAULT_PORTS.get(db_type, 1433)
                DB_REGISTRY[name] = {
                    "type": db_type,
                    "host": os.getenv(f"{prefix}HOST", ""),
                    "port": int(os.getenv(f"{prefix}PORT", str(default_port))),
                    "database": os.getenv(f"{prefix}NAME", ""),
                    "user": os.getenv(f"{prefix}USER", ""),
                    "password": os.getenv(f"{prefix}PASS", ""),
                    "read_only": os.getenv(f"{prefix}READONLY", "true").lower() == "true",
                    "query_timeout": int(os.getenv(f"{prefix}TIMEOUT", "30")),
                    "max_rows": int(os.getenv(f"{prefix}MAX_ROWS", "500")),
                }

_build_db_registry()

# SQL keywords that are NEVER allowed in read-only mode — applies to all dialects
SQL_BLOCKED_KEYWORDS = re.compile(
    r'\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|EXEC|EXECUTE|MERGE|'
    r'GRANT|REVOKE|DENY|BACKUP|RESTORE|SHUTDOWN|KILL|RECONFIGURE)\b',
    re.IGNORECASE
)

# MSSQL-specific bad keywords — xp_/sp_ procedures, OPENROWSET, etc.
SQL_BLOCKED_KEYWORDS_MSSQL = re.compile(
    r'\b(DBCC|BULK|OPENROWSET|OPENQUERY|xp_|sp_)\b',
    re.IGNORECASE
)

# MySQL-specific bad keywords — file I/O, LOAD DATA, user-defined functions
SQL_BLOCKED_KEYWORDS_MYSQL = re.compile(
    r'\b(LOAD_FILE|LOAD\s+DATA|INTO\s+OUTFILE|INTO\s+DUMPFILE|'
    r'BENCHMARK|SLEEP|RENAME\s+TABLE|HANDLER|LOCK\s+TABLES)\b',
    re.IGNORECASE
)

# Collection naming
PROJECT_PREFIX = "proj_"      # proj_emailtriage, proj_nimbus
SHARED_PREFIX = "shared_"     # shared_patterns, shared_context

# Overlap detection window
OVERLAP_WINDOW_HOURS = 24

# Stale lock timeout (30 minutes of no activity = stale)
STALE_LOCK_MINUTES = 30

# Session TTL - auto-expire sessions with no activity for this many days
SESSION_TTL_DAYS = 14

# Signal retention (24 hours)
SIGNAL_RETENTION_HOURS = 24

# Valid memory types
MEMORY_TYPES = [
    "api_spec", "architecture", "component", "config",  # Architecture
    "adr",  # Decisions
    "learning", "pattern", "gotcha",  # Learnings
    "code_snippet", "solution",  # Code
    "task_context", "handoff", "work_item",  # Task/Work
    "interface",  # Structured interface contracts
    "function_ref",  # AI-optimized function references
    "spec"  # Versioned specs with owner-only updates
]

# Valid doc statuses
DOC_STATUSES = ["active", "deprecated", "superseded", "archived", "conflicted", "review_pending", "expired"]

# Content size limit (50KB)
MAX_CONTENT_SIZE = 50 * 1024

# Default expiry days by memory type (None = never expires)
# Doubled from original values to accommodate part-time usage
DEFAULT_EXPIRY_DAYS = {
    "learning": 180,      # 6 months (was 90)
    "gotcha": 180,        # 6 months (was 90)
    "task_context": 60,   # 2 months (was 30)
    "handoff": 28,        # 4 weeks (was 14)
    "work_item": 14,      # 2 weeks (was 7)
    # These don't expire by default
    "api_spec": None,
    "architecture": None,
    "component": None,
    "config": None,
    "adr": None,
    "pattern": None,
    "code_snippet": None,
    "solution": None,
    "interface": None,
    "spec": None,  # Specs never expire
}

# Valid work statuses
WORK_STATUSES = ["in_progress", "blocked", "completed", "abandoned"]

# Backlog item statuses
BACKLOG_STATUSES = ["open", "in_progress", "deferred", "done", "wont_do", "retest", "blocked", "duplicate", "needs_info"]

# Backlog priorities
BACKLOG_PRIORITIES = ["critical", "high", "medium", "low"]

# Message priorities (queue now in MongoDB)
MESSAGE_PRIORITIES = ["urgent", "normal", "low"]

# Message categories
MESSAGE_CATEGORIES = ["contract", "task", "question", "info", "review", "blocker"]

# Message statuses for full lifecycle tracking (DELIVERY track — orthogonal to
# the obligation track below).
MESSAGE_STATUSES = ["pending", "delivered", "received", "completed", "failed"]

# ── Obligation track (design:unified-messaging-v0 Stage 3 / lanes-B) ──
# A SECOND axis on a message, separate from the delivery status above. Only
# ACTION-category messages carry an obligation; info carries none (unset). A
# reply from the message's OWNER auto-advances it (see
# _advance_parent_obligation_on_reply). Kept as its own field (NOT folded into
# MESSAGE_STATUSES) because the 3 worklist filters gate on status=="pending" and
# folding would make a responded task/blocker vanish from them — see
# contract:message-lanes-v0:B-wireshape R1.
#   ACTION_CATEGORIES         — get obligation="open" at send.
#   OBLIGATION_RESOLVE_ON_REPLY — an owner's reply RESOLVES (an answer satisfies).
#   OBLIGATION_RETAIN_ON_REPLY  — an owner's reply marks RESPONDED but the item
#                                 stays in the action lane until an explicit done.
OBLIGATION_STATES = ["open", "responded", "resolved"]
ACTION_CATEGORIES = ["task", "question", "blocker", "contract", "review"]
OBLIGATION_RESOLVE_ON_REPLY = ["question", "contract", "review"]
OBLIGATION_RETAIN_ON_REPLY = ["task", "blocker"]


def classify_lane(category, obligation):
    """Derive (lane, tier) for a message from its category + obligation.

    SINGLE source of truth for the category→lane map (design:unified-messaging-v0
    Stage 3 / lanes-A; interface:lanes-a-server-wire-v0, coordinator-converged).
    Computed fresh at serialize time and NEVER stored — so it cannot diverge from
    the category/obligation in the same payload. Both consumers (the junto-inbox
    badge and the control UI) read `lane`/`tier` instead of re-implementing this
    map, which is why it's server-emitted rather than client-derived.

      lane "action"  — an ACTION-category message not yet resolved (still owes work)
      lane "cleared" — an ACTION-category message whose obligation is resolved
                       (drops OUT of the action lane)
      lane "fyi"     — info / non-action

      tier 0 — obligation "open" (or a legacy action message with no obligation):
               an un-engaged ask, top of the action lane
      tier 1 — obligation "responded": engaged, deprioritized but still in-lane
      tier None — fyi / cleared (no action-lane position)
    """
    if category in ACTION_CATEGORIES:
        if obligation == "resolved":
            return "cleared", None
        # "open" or a legacy pre-lanes-B action message (obligation unset) → tier 0;
        # only an explicit "responded" demotes to tier 1.
        return "action", (1 if obligation == "responded" else 0)
    return "fyi", None

# ── Differential TTL (design:unified-messaging-v0 Stage 5 / lanes-C) ──
# Replaces the old flat "all messages expire at created+7d" rule. TTL now rides
# on a per-doc `expire_at` field (Mongo TTL index, expireAfterSeconds=0):
#   info / non-action  → expire_at = created + MESSAGE_INFO_TTL_HOURS (FYI ages fast)
#   ACTION, unacked     → expire_at = null  → NEVER ages (an open task/question must
#                         not silently vanish — the load-bearing safety property)
#   ACTION, acked/resolved → expire_at = created + MESSAGE_ACTION_TTL_DAYS
# Existing pre-migration messages are backfilled to created + ACTION_TTL_DAYS
# (preserves the old 7d behavior — no early deletion, no immortal docs).
MESSAGE_INFO_TTL_HOURS = 48
MESSAGE_ACTION_TTL_DAYS = 7

# ── SSE notification-stream keepalive ──
# The long-lived server→client SSE GET stream (Streamable HTTP) carries push
# notifications. During quiet periods it has no traffic, so an idle-connection
# reaper on the path (NAT/conntrack, Tailscale relay, proxy) silently drops it —
# and nothing notices (transport.onclose doesn't fire on a half-open socket, and
# the client heartbeat rides a SEPARATE POST channel). The server then "delivers"
# into a dead socket. A periodic keepalive notification on the stream keeps it
# warm so the path never idle-reaps it, and eventually surfaces a truly-dead
# socket (the write blocks → times out → the session is pruned). Interval must be
# shorter than the shortest idle-timeout on the path. See the half-open-SSE
# incident (2026-06-15).
SSE_KEEPALIVE_SECONDS = int(os.getenv("JUNTO_SSE_KEEPALIVE_SECONDS", "20"))
SSE_KEEPALIVE_SEND_TIMEOUT = float(os.getenv("JUNTO_SSE_KEEPALIVE_TIMEOUT", "5"))

# Auth roles (ordered by privilege)
AUTH_ROLES = ["readonly", "agent", "admin", "owner"]
