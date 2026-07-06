"""Client setup for Chroma and MongoDB connections."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import chromadb
from chromadb.config import Settings
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from shared_memory.config import (
    CHROMA_HOST,
    CHROMA_PORT,
    MESSAGE_ACTION_TTL_DAYS,
    MONGO_DB,
    MONGO_HOST,
    MONGO_PASSWORD,
    MONGO_PORT,
    MONGO_USER,
)

# =============================================================================
# Chroma Client Setup - Uses AsyncHttpClient for proper connection management
# =============================================================================

# Global client reference (lazy initialized)
_chroma_client = None
_chroma_lock = None  # Will be created when needed

async def _get_or_create_lock():
    """Get or create the asyncio lock for client initialization."""
    global _chroma_lock
    if _chroma_lock is None:
        _chroma_lock = asyncio.Lock()
    return _chroma_lock


async def get_chroma():
    """Get the shared async Chroma client (lazy initialization).

    CRITICAL: We use AsyncHttpClient instead of HttpClient because:
    1. HttpClient creates new TCP connections per request that don't get released
    2. This causes port exhaustion and server hangs under load
    3. AsyncHttpClient properly manages connections and supports async/await

    See: https://github.com/chroma-core/chroma/issues/4296

    This uses lazy initialization to work with stateless_http mode where
    the lifespan context may not be available.
    """
    global _chroma_client

    if _chroma_client is not None:
        return _chroma_client

    lock = await _get_or_create_lock()
    async with lock:
        # Double-check after acquiring lock
        if _chroma_client is not None:
            return _chroma_client

        # Create a single AsyncHttpClient instance for the entire application lifetime
        client = await chromadb.AsyncHttpClient(
            host=CHROMA_HOST,
            port=CHROMA_PORT,
            settings=Settings(anonymized_telemetry=False)
        )
        print(f"Connected to Chroma (async) at {CHROMA_HOST}:{CHROMA_PORT}")

        # Ensure shared collections exist
        for name in ["shared_patterns", "shared_context", "shared_work"]:
            await client.get_or_create_collection(
                name=name,
                metadata={"type": "shared", "created": datetime.now(timezone.utc).isoformat()}
            )

        _chroma_client = client
        return _chroma_client


@asynccontextmanager
async def app_lifespan(app):
    """Lifespan manager for startup tasks + shutdown cleanup.

    Chroma is lazy-initialized via get_chroma() and not handled here. Tasks that
    need an event loop (scheduled-message scanner, future periodic jobs) start
    here. We run with stateless_http=False (app.py), so lifespan is reliable.
    """
    global _chroma_client

    # Start the scheduled-message scanner (timed self-messages). Local import
    # avoids load-time cycles between clients.py and tools/scheduler.py.
    try:
        from shared_memory.tools.scheduler import ensure_indexes, start_scheduler
        ensure_indexes()
        start_scheduler()
    except Exception as e:
        # Don't take down the server if the scheduler can't start — log loudly
        # and keep going. The tools still register; just no auto-delivery.
        import logging
        logging.getLogger(__name__).error("lifespan: scheduler startup failed: %s", e)

    # Start the SSE notification-stream keepalive (keeps long-lived push streams
    # warm so idle network reapers don't silently half-open them).
    try:
        from shared_memory.tools.messaging import start_keepalive
        start_keepalive()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("lifespan: sse-keepalive startup failed: %s", e)

    yield {}

    # Shutdown cleanup.
    try:
        from shared_memory.tools.scheduler import stop_scheduler
        stop_scheduler()
    except Exception:
        pass
    try:
        from shared_memory.tools.messaging import stop_keepalive
        stop_keepalive()
    except Exception:
        pass
    _chroma_client = None


# =============================================================================
# MongoDB Client Setup - For message queue and agent status persistence
# =============================================================================

_mongo_client = None
_mongo_db = None


def _migrate_messages_ttl(messages):
    """Differential-TTL migration for the messages collection (Stage 5 / lanes-C).

    Idempotent — runs on every boot, does real work only the first time:

    1. Drop the legacy flat TTL index (created_at + expireAfterSeconds=7d) if it
       still exists. That index expired EVERY message at created+7d.
    2. Backfill expire_at on any message that lacks it, to created + 7d — i.e.
       EXACTLY the old behavior. This is the loss-safety property: existing
       messages keep their current expiry (no early deletion), and none become
       immortal (a missing TTL field would otherwise mean "never expires").
       New sends set expire_at via the differential rule (info=+48h,
       unacked-action=null/never, acked-action=+7d) in messaging.py.
    3. Create the new per-doc TTL index on expire_at (expireAfterSeconds=0 =
       "delete when expire_at <= now"; docs with expire_at unset never expire).
    4. Re-create a PLAIN created_at index — the dropped TTL index used to be the
       only index serving the recency-primary get_messages sort.

    Best-effort: a failure here must not block server start (TTL is a cleanup
    nicety, not a correctness requirement).
    """
    try:
        existing = messages.index_information()
    except Exception:
        existing = {}

    # 1. Drop legacy flat TTL index (named created_at_1 with expireAfterSeconds).
    legacy = existing.get("created_at_1")
    if legacy is not None and "expireAfterSeconds" in legacy:
        try:
            messages.drop_index("created_at_1")
        except Exception:
            pass

    # 2. Backfill expire_at = created_at + 7d for docs missing it (old behavior).
    #    Aggregation-pipeline update ($add date+ms); requires MongoDB 4.2+.
    try:
        messages.update_many(
            {"expire_at": {"$exists": False}},
            [{"$set": {"expire_at": {
                "$add": ["$created_at", MESSAGE_ACTION_TTL_DAYS * 24 * 3600 * 1000]
            }}}],
        )
    except Exception:
        pass

    # 3. New per-doc TTL index. expireAfterSeconds=0 → Mongo deletes when the
    #    expire_at date is in the past; a null/absent expire_at never expires.
    messages.create_index("expire_at", expireAfterSeconds=0)
    # 4. Plain created_at index for the recency sort (the old TTL index did this).
    messages.create_index("created_at")


def get_mongo():
    """Get the MongoDB client and database (lazy initialization).

    MongoDB is used for:
    - Message queue (persistent, supports change streams)
    - Agent status (heartbeats, current task)
    - Task lifecycle tracking

    Chroma remains the source of truth for:
    - Memories, learnings, patterns
    - Function references
    - Backlog items
    """
    global _mongo_client, _mongo_db

    if _mongo_db is not None:
        return _mongo_db

    try:
        # Mongo runs as a single-node replica set (rs0) so we get multi-document
        # transactions and change streams. PyMongo with `replicaSet=rs0` does
        # member discovery via the seed; the replica set advertises members by
        # the docker-network hostname `mongodb`, so this URI works from inside
        # the docker network. Host-side debugging via localhost:27019 needs
        # `?directConnection=true` instead.
        #
        # authSource=admin is required because the root user created via
        # MONGO_INITDB_ROOT_USERNAME lives in the admin database regardless
        # of MONGO_INITDB_DATABASE. Without this, PyMongo defaults authSource
        # to MONGO_DB and authentication fails on fresh installs — server
        # comes up but every mongo-backed call (guidelines, agent_directory,
        # messages, audit_log, op_log, autopilot) silently returns empty.
        # See PR #1 / Issue tlemmons-lvt for the install-time repro.
        mongo_uri = (
            f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@{MONGO_HOST}:{MONGO_PORT}"
            f"/{MONGO_DB}?replicaSet=rs0&authSource=admin"
        )
        _mongo_client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=5000
        )
        # Verify connection
        _mongo_client.admin.command('ping')
        _mongo_db = _mongo_client[MONGO_DB]

        # Ensure indexes for messages collection
        messages = _mongo_db.messages
        messages.create_index("to_instance")
        messages.create_index("to_project")
        messages.create_index("status")
        messages.create_index("priority")
        messages.create_index([("to_instance", 1), ("to_project", 1), ("status", 1)])
        # Differential TTL migration (design:unified-messaging-v0 Stage 5 / lanes-C).
        # Was: a single TTL index on created_at expiring ALL messages at +7d. Now:
        # TTL rides on a per-doc expire_at field so info ages in 48h, unacked
        # actions never age, and acked actions keep the 7d window. Idempotent —
        # safe to re-run every boot. See _migrate_messages_ttl.
        _migrate_messages_ttl(messages)

        # Ensure indexes for agent_status collection
        agent_status = _mongo_db.agent_status
        agent_status.create_index("instance", unique=True)
        agent_status.create_index("last_heartbeat", expireAfterSeconds=3600)  # TTL: 1 hour stale

        # Ensure indexes for checklists collection
        checklists_col = _mongo_db.checklists
        checklists_col.create_index("project")

        # Ensure indexes for agent_directory collection (auto-populated activity tracking)
        agent_dir = _mongo_db.agent_directory
        agent_dir.create_index([("project", 1), ("instance", 1)], unique=True)
        agent_dir.create_index("project")
        agent_dir.create_index("last_seen")
        # Component-peer lookup at session start (design:unified-messaging-v0
        # Stage 1): find agents in project P subscribed to component C, recently
        # active. Compound (project, subscribed_components) — multikey on the
        # array field — serves the $in + last_seen-filtered query.
        agent_dir.create_index([("project", 1), ("subscribed_components", 1)])

        # Ensure indexes for project registry (admin-controlled)
        projects_col = _mongo_db.projects
        projects_col.create_index("name", unique=True)

        # Ensure indexes for registered agents (admin-controlled, per-project)
        reg_agents = _mongo_db.registered_agents
        reg_agents.create_index([("project", 1), ("name", 1)], unique=True)
        reg_agents.create_index("project")
        reg_agents.create_index("tier")

        # Ensure indexes for guidelines collection (server-managed agent instructions)
        guidelines_col = _mongo_db.guidelines
        guidelines_col.create_index("scope")
        guidelines_col.create_index("name", unique=True)

        # Seed code-defined GLOBAL guidelines (scope="global") into the DB so a
        # guidance change travels with the deploy to every server — incl. the
        # isolated work box — without federating data. Idempotent: writes only
        # rows whose content differs (a no-change boot does zero writes), and
        # NEVER touches project-scoped rows. Best-effort: a seed failure must not
        # block mongo init (agents would just keep the prior DB guidance).
        try:
            from shared_memory.global_guidelines import seed_global_guidelines
            seed_global_guidelines(_mongo_db)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                "guidelines: global-guidance code-seed failed: %s", e
            )

        # Ensure indexes for audit_log collection
        audit_col = _mongo_db.audit_log
        audit_col.create_index("event_type")
        audit_col.create_index("actor")
        audit_col.create_index("timestamp", expireAfterSeconds=86400 * 90)  # 90-day retention

        # Ensure indexes for api_keys collection (auth system)
        api_keys_col = _mongo_db.api_keys
        api_keys_col.create_index("key_hash", unique=True)
        api_keys_col.create_index("name", unique=True)

        # Compaction events collection (per ADR 822c260ccfda — v3 measurement)
        compact_col = _mongo_db.compaction_events
        compact_col.create_index([("agent", 1), ("logged_at", -1)])
        compact_col.create_index("logged_at")

        # Autopilot config — one doc per (project, agent), introduced in Phase C1
        # for ClaudeTerminal channel-plugin integration.
        autopilot_col = _mongo_db.agent_autopilot
        autopilot_col.create_index([("project", 1), ("agent", 1)], unique=True)

        # Autopilot events — Phase C2 budget enforcement. One doc per
        # auto-processed message. TTL of 1 hour means rolling-window count
        # is just a count_documents call. Mongo's TTL monitor reaps expired
        # docs every ~60s so the count is approximate at the second-by-second
        # level; that's fine for budget gating.
        autopilot_events_col = _mongo_db.autopilot_events
        autopilot_events_col.create_index(
            "logged_at", expireAfterSeconds=3600
        )
        autopilot_events_col.create_index(
            [("project", 1), ("agent", 1), ("logged_at", -1)]
        )

        # New message indexes for chain semantics — chain_depth lets us enforce
        # the depth-5 hard cap efficiently; in_response_to lets us walk threads.
        messages_col = _mongo_db.messages
        messages_col.create_index("chain_depth")
        messages_col.create_index("in_response_to")

        # Skills registry (design:skill-registry-v0). Dedicated collection,
        # Mongo-primary (lifecycle/owner/version/pin/confirm). Identity is
        # (project, name); status is draft|active (confirm gate). See
        # tools/skills.py.
        skills_col = _mongo_db.skills
        skills_col.create_index([("project", 1), ("name", 1)], unique=True)
        skills_col.create_index("project")
        skills_col.create_index([("project", 1), ("status", 1)])

        # Fleet directives (cross-server "what you need to do" banners). Code-
        # seeded like global_guidelines so the TEXT travels with the deploy to
        # every server incl. the air-gapped work box; acks are per-server. See
        # shared_memory.directives + tools/directives.py.
        directives_col = _mongo_db.directives
        directives_col.create_index("key", unique=True)
        directives_col.create_index("active")
        directive_acks_col = _mongo_db.directive_acks
        directive_acks_col.create_index("key")
        directive_acks_col.create_index([("project", 1), ("agent", 1)])
        try:
            from shared_memory.directives import seed_directives
            seed_directives(_mongo_db)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(
                "directives: code-seed failed: %s", e
            )

        # Op-log (Phase 1 foundation, design v0.3.0 §4.2). Collection +
        # indexes only — no writers yet. Phase 1 #2 instruments mutation
        # tools to append here inside a Mongo transaction alongside their
        # source-collection write.
        from shared_memory.op_log import ensure_op_log_indexes
        ensure_op_log_indexes(_mongo_db)

        # Push control (design:push-control-v0 v1.1.0): alerts collection +
        # push_control_config collection. In-process emission counters live
        # in push_control module memory, not Mongo — they reset on restart.
        from shared_memory.push_control import init_push_control_indexes
        init_push_control_indexes(_mongo_db)

        # Query-tool defaults (backlog_6d5aa1a2849f): per-project overrides
        # for memory_query's expand/snippet_length/expand_top defaults.
        from shared_memory.query_config import init_query_config_indexes
        init_query_config_indexes(_mongo_db)

        print(f"Connected to MongoDB at {MONGO_HOST}:{MONGO_PORT}/{MONGO_DB}")
        return _mongo_db

    except ConnectionFailure as e:
        print(f"[MCP] MongoDB connection failed (messaging will use in-memory fallback): {e}")
        return None
