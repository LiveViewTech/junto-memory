"""Authentication, RBAC, and tenant isolation.

Optional API key authentication for the MCP server. When enabled
(MCP_AUTH_ENABLED=true), all sessions require a valid API key.

Roles:
    owner    - Full access, can manage API keys and guidelines, all projects
    admin    - Can manage backlog, specs, functions, messaging, all projects
    user     - Human-driven dashboard/CLI access; broader read across projects
               and ability to send messages, but cannot manage keys or guidelines.
               Use for the future ClaudeTerminal phone/web dashboard and similar
               human-originated tooling.
    agent    - Standard agent access, scoped to assigned projects
    readonly - Query and search only, no writes

Tenant isolation:
    Each API key can be scoped to specific projects. An empty project list
    means access to all projects (for owner/admin/user roles).
"""

import hashlib
import os
import secrets
from contextvars import ContextVar
from typing import Dict, List, Optional, Tuple

from shared_memory.helpers import utc_now

# Whether auth is required (opt-in)
AUTH_ENABLED = os.getenv("MCP_AUTH_ENABLED", "").lower() in ("true", "1", "yes")

# ── Header-based auth (design:header-auth-v0) ──
# An ASGI middleware parses the request's "Authorization: Bearer <key>" header
# into this contextvar; memory_start_session falls back to it when no per-tool
# api_key arg is supplied. The explicit arg always wins (backward-compat).
# Spike-proven (learning_348aa4ef8710f33f): a contextvar set in the per-request
# ASGI middleware survives into the FastMCP tools/call handler even with
# stateless_http=False.
_header_api_key: ContextVar[Optional[str]] = ContextVar(
    "junto_header_api_key", default=None
)


def parse_bearer_token(headers) -> Optional[str]:
    """Extract the token from an 'Authorization: Bearer <token>' header.

    `headers` is the ASGI scope's headers: an iterable of (name, value) byte
    tuples. Case-insensitive on both the header name and the 'Bearer' scheme.
    Returns None if the header is absent, malformed, a non-Bearer scheme, or
    has an empty token. Pure function — no contextvar side effects (testable).
    """
    for name, value in headers or ():
        if name.lower() == b"authorization":
            try:
                raw = value.decode("latin-1")
            except Exception:
                return None
            parts = raw.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
                return parts[1].strip()
            return None
    return None


def set_header_api_key(key: str):
    """Set the per-request header api_key contextvar. Returns the reset token —
    pass it to reset_header_api_key in a finally block (ASGI task-locality)."""
    return _header_api_key.set(key)


def reset_header_api_key(token) -> None:
    """Reset the header api_key contextvar using the token from set_."""
    _header_api_key.reset(token)


def get_header_api_key() -> Optional[str]:
    """Read the header api_key parsed by the ASGI middleware for this request,
    or None if no Bearer header was present."""
    return _header_api_key.get()


# ── Origin trust (design:auth-origin-trust-v0) ──
# The same ASGI middleware flags whether a request arrived via the public
# Cloudflare tunnel (Cloudflare sets the CF-Connecting-IP header on every
# proxied request). The keyless soft-fallback then grants agent tier only for
# LAN/local origins and rejects keyless tunnel traffic.
#
# Why trusting this header is safe HERE (it normally is not): the published
# ports are bound to 127.0.0.1 + the LAN IP only, so the tunnel is the ONLY
# off-LAN path that reaches the server. A client must already be on the trusted
# LAN to hit the keyless port, and supplying the header only SELF-RESTRICTS
# (forces the key requirement) — it can never elevate. Topology neutralises the
# usual XFF/CF-Connecting-IP spoofing footgun.
_request_via_tunnel: ContextVar[bool] = ContextVar(
    "junto_request_via_tunnel", default=False
)

# Require a valid API key for connections that arrived over the public tunnel.
TUNNEL_REQUIRES_KEY = os.getenv("JUNTO_TUNNEL_REQUIRES_KEY", "true").lower() in (
    "true", "1", "yes",
)

# Require a valid API key for ALL connections, regardless of origin. Unlike
# TUNNEL_REQUIRES_KEY (which only rejects keyless traffic detected as tunnel-origin
# via CF-Connecting-IP), this rejects every keyless session — the correct posture
# for a deployment where the transport (e.g. a Tailscale-only server) sets no
# tunnel header, so origin-trust can't distinguish trusted-LAN from remote, AND
# every legitimate client already holds a key. Default off preserves the
# keyless-LAN soft-fallback. See design:auth-origin-trust-v0.
REQUIRE_KEY = os.getenv("JUNTO_REQUIRE_KEY", "false").lower() in (
    "true", "1", "yes",
)


def detect_tunnel_origin(headers) -> bool:
    """True if the request carries Cloudflare's CF-Connecting-IP header, i.e.
    it arrived via the cloudflared tunnel. `headers` is the ASGI scope's
    headers: an iterable of (name, value) byte tuples. Pure function — no
    contextvar side effects (testable)."""
    for name, value in headers or ():
        if name.lower() == b"cf-connecting-ip":
            return True
    return False


def set_via_tunnel(flag: bool):
    """Set the per-request tunnel-origin contextvar. Returns the reset token —
    pass it to reset_via_tunnel in a finally block (ASGI task-locality)."""
    return _request_via_tunnel.set(flag)


def reset_via_tunnel(token) -> None:
    """Reset the tunnel-origin contextvar using the token from set_via_tunnel."""
    _request_via_tunnel.reset(token)


def get_via_tunnel() -> bool:
    """Read whether this request arrived via the public Cloudflare tunnel, as
    flagged by the ASGI middleware. False for LAN/local origins."""
    return _request_via_tunnel.get()

# Roles ordered by privilege level (user sits between agent and admin —
# broader cross-project read, can send messages, but no key/guideline management)
ROLES = ["readonly", "agent", "user", "admin", "owner"]

# Permission matrix: which roles can perform which operation categories
PERMISSIONS: Dict[str, List[str]] = {
    "session.start":    ["readonly", "agent", "user", "admin", "owner"],
    "session.end":      ["readonly", "agent", "user", "admin", "owner"],
    "query":            ["readonly", "agent", "user", "admin", "owner"],
    "store":            ["agent", "user", "admin", "owner"],
    "backlog":          ["agent", "user", "admin", "owner"],
    "messaging":        ["agent", "user", "admin", "owner"],
    "locking":          ["agent", "admin", "owner"],
    "functions":        ["agent", "admin", "owner"],
    "specs":            ["agent", "admin", "owner"],
    "skills":           ["agent", "user", "admin", "owner"],
    "lifecycle":        ["agent", "admin", "owner"],
    "checklists":       ["agent", "user", "admin", "owner"],
    "database":         ["agent", "user", "admin", "owner"],
    "autopilot":        ["user", "admin", "owner"],
    "guidelines":       ["admin", "owner"],
    "admin":            ["admin", "owner"],
    "admin.write":      ["owner"],
    # Sync endpoints (memory_sync_pull / memory_sync_push) are server-to-
    # server replication primitives — LAN-local junto-memory instances pull
    # from central. Operator-tier credentials only.
    "sync":             ["admin", "owner"],
}


def _hash_key(api_key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a new random API key."""
    return f"smk_{secrets.token_urlsafe(32)}"


def validate_api_key(api_key: str) -> Optional[Dict]:
    """Validate an API key and return its record, or None if invalid.

    Returns dict with: name, role, projects, created, last_used
    """
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return None

    key_hash = _hash_key(api_key)
    record = db.api_keys.find_one({"key_hash": key_hash, "active": True})
    if not record:
        return None

    # Update last_used
    db.api_keys.update_one(
        {"key_hash": key_hash},
        {"$set": {"last_used": utc_now()}}
    )

    return {
        "name": record["name"],
        "role": record["role"],
        "projects": record.get("projects", []),
        "created": record.get("created"),
    }


def create_api_key(
    name: str,
    role: str = "agent",
    projects: Optional[List[str]] = None,
    created_by: str = "system",
) -> Tuple[str, Dict]:
    """Create a new API key. Returns (raw_key, record).

    The raw key is only returned once — store it securely.
    """
    from shared_memory.clients import get_mongo

    if role not in ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {ROLES}")

    db = get_mongo()
    if db is None:
        raise RuntimeError("MongoDB not available")

    raw_key = generate_api_key()
    key_hash = _hash_key(raw_key)
    now = utc_now()

    record = {
        "key_hash": key_hash,
        "key_prefix": raw_key[:12],
        "name": name,
        "role": role,
        "projects": projects or [],
        "active": True,
        "created": now,
        "created_by": created_by,
        "last_used": None,
    }

    db.api_keys.insert_one(record)

    return raw_key, {
        "name": name,
        "role": role,
        "projects": projects or [],
        "key_prefix": raw_key[:12],
        "created": now.isoformat(),
    }


def revoke_api_key(name: str) -> bool:
    """Revoke an API key by name. Returns True if found and revoked."""
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return False

    result = db.api_keys.update_one(
        {"name": name, "active": True},
        {"$set": {"active": False, "revoked_at": utc_now()}}
    )
    return result.modified_count > 0


def list_api_keys() -> List[Dict]:
    """List all API keys (without hashes)."""
    from shared_memory.clients import get_mongo

    db = get_mongo()
    if db is None:
        return []

    keys = []
    for doc in db.api_keys.find({"active": True}).sort("created", 1):
        keys.append({
            "name": doc["name"],
            "role": doc["role"],
            "projects": doc.get("projects", []),
            "key_prefix": doc.get("key_prefix", ""),
            "created": doc.get("created", ""),
            "last_used": doc.get("last_used", ""),
        })
    return keys


def check_permission(role: str, permission: str) -> bool:
    """Check if a role has a specific permission."""
    allowed_roles = PERMISSIONS.get(permission, [])
    return role in allowed_roles


def check_project_access(allowed_projects: List[str], target_project: str) -> bool:
    """Check if a key has access to a specific project.

    Empty allowed_projects means access to all projects.
    """
    if not allowed_projects:
        return True  # No project restriction
    normalized = target_project.lower().replace("-", "_").replace(" ", "_")
    return normalized in [p.lower().replace("-", "_").replace(" ", "_") for p in allowed_projects]


def require_auth(session_info: Dict, permission: str, project: str = None) -> Optional[str]:
    """Check auth for a session. Returns error string or None if OK.

    When auth is disabled, always returns None (allowed).
    """
    if not AUTH_ENABLED:
        return None

    role = session_info.get("role", "agent")

    if not check_permission(role, permission):
        return (
            f"Permission denied: role '{role}' cannot perform '{permission}'. "
            f"Required roles: {PERMISSIONS.get(permission, [])}"
        )

    if project:
        allowed_projects = session_info.get("allowed_projects", [])
        if not check_project_access(allowed_projects, project):
            return (
                f"Tenant isolation: your API key does not have access to project '{project}'. "
                f"Allowed projects: {allowed_projects or 'none configured'}"
            )

    return None
