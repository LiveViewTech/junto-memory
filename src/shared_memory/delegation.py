"""Delegated inference: agent-owned tasks run by generic local-model runners.

Contract: interface:delegated-inference-v0 (1.3.x). Design: design:delegated-inference-v0.

Concepts
- SCOPE (Tom-owned, di_scopes): a data source a runner can read, which runner
  hosts may read it, which agents may build tasks on it, and who may see raw
  item content. The credential stays on the runner host (credential_ref is a
  runner-local path); the server never holds it.
- TASK (agent-owned, di_tasks): instructions + output_schema + source_params +
  routes + schedule + canaries. Everything an agent can change is data.
  Every create/edit writes a version row (di_task_versions).
- RUN (di_runs): one execution. The scheduler loop queues runs for due tasks;
  a runner claims one over HTTP (runner_api.py), fetches items via its source
  adapter, calls the local model per item, and completes the run. The SERVER
  then applies the task's routes (notify / digest / drop).
- RUNNER (di_runners): a service principal, not an agent. Authenticated by a
  runner key (di_runner_keys, sha256-hashed); never appears in any roster.

All functions here take an explicit `db` so they are testable against a
scratch database; the MCP tools (tools/delegation.py) and HTTP routes
(runner_api.py) are thin wrappers.
"""

import asyncio
import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_TZ = "America/Chicago"
DEFAULT_MODEL = "gpt-oss:20b"  # resisted every injection case; qwen3:14b did not (learning_12b17e9a94c49685)
LEASE_SECONDS = 600
LOOP_SECONDS = 30
RUN_TTL_DAYS = 30
MAX_INSTRUCTIONS_CHARS = 20000
MAX_TASKS_PER_AGENT = 25
MAX_ROUTES = 20
MAX_CANARIES = 20
MAX_ITEMS_PER_RUN = 200
MIN_EVERY_SECONDS = 300
DEFAULT_PAUSE_AFTER_FAILURES = 3
DEFAULT_DIGEST_AT = "07:00"

ADAPTERS = {"imap", "gmail", "gmail_api", "file", "inline", "mcp", "none"}
SECRET_KINDS = {"none", "password", "token", "oauth"}
ACTIVE = (None, "active")  # pre-1.5 scopes have no status and count as active
TOOL_PROVIDERS = {"mcp"}
MAX_TOOLS = 30
DEFAULT_MAX_TOOL_CALLS = 8
MAX_TOOL_CALLS = 20
ROUTE_ACTIONS = {"notify", "digest", "drop"}
ROUTE_OPS = {"eq", "ne", "in", "not_in", "lt", "lte", "gt", "gte", "exists", "true", "false"}
EDITABLE_FIELDS = {"instructions", "output_schema", "source_params", "model", "routes", "schedule",
                   "canaries", "description", "per_item", "expect_items", "tools", "tool_scope",
                   "max_tool_calls"}
# Edits to these re-require Tom's approval on an approval=first_run scope.
APPROVAL_FIELDS = {"instructions", "output_schema", "source_params"}
# Edits to these invalidate today's canary pass: the next run re-checks canaries.
CANARY_RESET_FIELDS = {"instructions", "output_schema", "model", "canaries", "tools", "source_params"}
RUN_STATUSES = {"queued", "claimed", "done", "failed", "cancelled"}

_EVERY_RE = re.compile(r"^(\d+)\s*([smhd])$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")

_loop_task: Optional[asyncio.Task] = None


# ── small helpers ──────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Mongo returns naive UTC datetimes; make them aware for arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _jsonable(doc):
    return json.loads(json.dumps(doc, default=_iso))


def ident(project: str, agent: str) -> str:
    return f"{project}/{agent}"


def _split_ident(value: str) -> Tuple[str, str]:
    project, _, agent = (value or "").partition("/")
    return project, agent


def parse_every(value: str) -> Optional[int]:
    m = _EVERY_RE.match((value or "").strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _parse_quiet(value: Optional[str]) -> Optional[Tuple[int, int]]:
    """'01:00-05:00' → (60, 300) minutes since local midnight."""
    if not value:
        return None
    try:
        a, b = value.split("-")
        ma, mb = _HHMM_RE.match(a.strip()), _HHMM_RE.match(b.strip())
        if not ma or not mb:
            return None
        return int(ma.group(1)) * 60 + int(ma.group(2)), int(mb.group(1)) * 60 + int(mb.group(2))
    except ValueError:
        return None


def _in_quiet(local: datetime, quiet: Tuple[int, int]) -> bool:
    minute = local.hour * 60 + local.minute
    start, end = quiet
    return start <= minute < end if start <= end else (minute >= start or minute < end)


def next_fire(schedule: Dict[str, Any], after: datetime) -> Optional[datetime]:
    """Next UTC fire time strictly after `after` (aware), honoring quiet hours."""
    if not schedule:
        return None
    tz = ZoneInfo(schedule.get("timezone") or DEFAULT_TZ)
    after = _aware(after)
    if schedule.get("every"):
        secs = parse_every(schedule["every"])
        if not secs:
            return None
        candidate = after + timedelta(seconds=secs)
    elif schedule.get("cron"):
        from croniter import croniter
        candidate = croniter(schedule["cron"], after.astimezone(tz)).get_next(datetime)
        candidate = candidate.astimezone(timezone.utc)
    else:
        return None
    quiet = _parse_quiet(schedule.get("quiet_hours"))
    if quiet:
        for _ in range(48 * 60):  # walk forward minute by minute, bounded
            if not _in_quiet(candidate.astimezone(tz), quiet):
                break
            candidate += timedelta(minutes=1)
    return candidate


def _digest_due_after(digest_at: str, tz_name: str, after: datetime) -> datetime:
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    m = _HHMM_RE.match(digest_at or DEFAULT_DIGEST_AT) or _HHMM_RE.match(DEFAULT_DIGEST_AT)
    local = _aware(after).astimezone(tz)
    target = local.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


# ── validation ─────────────────────────────────────────────────────────────

def _schema_properties(output_schema: Dict[str, Any]) -> Dict[str, Any]:
    props = output_schema.get("properties") if isinstance(output_schema, dict) else None
    return props if isinstance(props, dict) else {}


def _string_constrained(prop: Dict[str, Any]) -> bool:
    if prop.get("x-sensitive") is True:
        return True
    if "enum" in prop or "const" in prop or "pattern" in prop:
        return True
    max_len = prop.get("maxLength")
    return isinstance(max_len, int) and max_len <= 64


def _unconstrained_strings(schema: Dict[str, Any], path: str = "") -> List[str]:
    """Paths of string fields a restricted scope may not return raw (§6.3)."""
    bad = []
    if not isinstance(schema, dict):
        return bad
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "string" in types and not _string_constrained(schema):
        bad.append(path or "(root)")
    for name, prop in (schema.get("properties") or {}).items():
        bad += _unconstrained_strings(prop, f"{path}.{name}" if path else name)
    if isinstance(schema.get("items"), dict):
        bad += _unconstrained_strings(schema["items"], f"{path}[]")
    return bad


def _route_field_ok(field: str, props: Dict[str, Any]) -> bool:
    if field.startswith("item."):
        return bool(field[5:])
    return field.split(".", 1)[0] in props


def validate_task_fields(fields: Dict[str, Any], scope: Dict[str, Any]) -> List[str]:
    """Errors for a full task definition against its scope. Empty list = valid."""
    import jsonschema

    errors = []
    instructions = fields.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        errors.append("instructions: required, non-empty text")
    elif len(instructions) > MAX_INSTRUCTIONS_CHARS:
        errors.append(f"instructions: max {MAX_INSTRUCTIONS_CHARS} chars")

    schema = fields.get("output_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object" or not _schema_properties(schema):
        errors.append("output_schema: must be a JSON Schema with type=object and properties")
        schema = {}
    else:
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            errors.append(f"output_schema: invalid JSON Schema: {e.message}")
        if scope.get("restricted"):
            bad = _unconstrained_strings(schema)
            if bad:
                errors.append("output_schema: restricted scope; these string fields need enum/pattern/"
                              "maxLength<=64 or x-sensitive:true: " + ", ".join(bad))
    props = _schema_properties(schema)

    if not isinstance(fields.get("source_params", {}), dict):
        errors.append("source_params: must be an object")
    elif scope.get("adapter") == "mcp":
        src_tool = (fields.get("source_params") or {}).get("tool")
        if src_tool not in ((scope.get("tools") or {}).get("allowed") or []):
            errors.append(f"source_params.tool: required for an mcp scope, one of "
                          f"{(scope.get('tools') or {}).get('allowed') or []}")
    if fields.get("model") is not None and not isinstance(fields.get("model"), str):
        errors.append("model: must be a string (an Ollama model name)")

    routes = fields.get("routes") or []
    if not isinstance(routes, list) or len(routes) > MAX_ROUTES:
        errors.append(f"routes: must be a list of at most {MAX_ROUTES}")
        routes = []
    visible = set(scope.get("content_visible_to") or [])
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            errors.append(f"routes[{i}]: must be an object")
            continue
        action = r.get("then")
        if action not in ROUTE_ACTIONS:
            errors.append(f"routes[{i}].then: one of {sorted(ROUTE_ACTIONS)}")
        cond = r.get("if")
        if cond is not None:
            if not isinstance(cond, dict) or cond.get("op") not in ROUTE_OPS or not cond.get("field"):
                errors.append(f"routes[{i}].if: needs field + op in {sorted(ROUTE_OPS)}")
            elif not _route_field_ok(cond["field"], props):
                errors.append(f"routes[{i}].if.field '{cond['field']}': not in output_schema (or item.<meta>)")
        if r.get("template") and action != "notify":
            for ph in _PLACEHOLDER_RE.findall(r.get("template") or ""):
                if not _route_field_ok(ph, props):
                    errors.append(f"routes[{i}].template: unknown field {{{ph}}}")
        if action == "notify":
            to = r.get("to") or ""
            p, a = _split_ident(to)
            if not p or not a:
                errors.append(f"routes[{i}].to: required for notify, as 'project/agent'")
            if r.get("category", "info") not in ("info", "task"):
                errors.append(f"routes[{i}].category: info or task")
            for ph in _PLACEHOLDER_RE.findall(r.get("template") or ""):
                if not _route_field_ok(ph, props):
                    errors.append(f"routes[{i}].template: unknown field {{{ph}}}")
            if scope.get("restricted") and to not in visible:
                # Fine — delivery strips x-sensitive fields + item content for them.
                pass

    schedule = fields.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            errors.append("schedule: must be an object")
        else:
            if bool(schedule.get("every")) == bool(schedule.get("cron")):
                errors.append("schedule: exactly one of every ('15m') or cron ('0 7 * * *')")
            if schedule.get("every"):
                secs = parse_every(schedule["every"])
                if not secs:
                    errors.append("schedule.every: like '15m', '2h', '1d'")
                elif secs < MIN_EVERY_SECONDS:
                    errors.append(f"schedule.every: minimum {MIN_EVERY_SECONDS // 60}m")
            if schedule.get("cron"):
                try:
                    from croniter import croniter
                    if not croniter.is_valid(schedule["cron"]):
                        errors.append("schedule.cron: invalid cron expression")
                except ImportError:
                    errors.append("schedule.cron: cron support unavailable on this server")
            try:
                ZoneInfo(schedule.get("timezone") or DEFAULT_TZ)
            except Exception:
                errors.append("schedule.timezone: unknown IANA timezone")
            if schedule.get("quiet_hours") and not _parse_quiet(schedule["quiet_hours"]):
                errors.append("schedule.quiet_hours: like '01:00-05:00'")
            if schedule.get("digest_at") and not _HHMM_RE.match(schedule["digest_at"]):
                errors.append("schedule.digest_at: like '07:00'")
            if schedule.get("canary_every", "daily") not in ("daily", "run"):
                errors.append("schedule.canary_every: daily or run")

    canaries = fields.get("canaries") or []
    if not isinstance(canaries, list) or len(canaries) > MAX_CANARIES:
        errors.append(f"canaries: list of at most {MAX_CANARIES}")
        canaries = []
    for i, c in enumerate(canaries):
        locator_ok = isinstance(c, dict) and (isinstance(c.get("source_params"), dict)
                                              or (isinstance(c.get("content"), str) and c["content"].strip()))
        if not locator_ok or not isinstance(c.get("expect"), dict) or not c["expect"]:
            errors.append(f"canaries[{i}]: needs source_params (a locator) or content (a synthetic item), "
                          "and a non-empty expect")
            continue
        for f in c["expect"]:
            if f not in props:
                errors.append(f"canaries[{i}].expect: '{f}' not in output_schema")
    return errors


# ── scopes ─────────────────────────────────────────────────────────────────

SCOPE_FIELDS = {"adapter", "adapter_config", "tools", "runner_hosts_allowed", "grants", "content_visible_to",
                "restricted", "approval", "notes", "secret_kind"}


def define_scope(db, actor: str, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    if not name or not re.match(r"^[a-z0-9][a-z0-9:_.-]{1,63}$", name):
        return {"error": "scope name: lowercase letters, digits, ':_.-' (2-64 chars)"}
    unknown = set(fields) - SCOPE_FIELDS
    if unknown:
        return {"error": f"unknown scope fields: {sorted(unknown)}"}
    existing = db.di_scopes.find_one({"_id": name}) or {}
    merged = {**{k: existing.get(k) for k in SCOPE_FIELDS if k in existing}, **fields}
    if merged.get("adapter") not in ADAPTERS:
        return {"error": f"adapter: one of {sorted(ADAPTERS)}"}
    if merged.get("approval", "none") not in ("none", "first_run"):
        return {"error": "approval: none or first_run"}
    for key in ("runner_hosts_allowed", "grants", "content_visible_to"):
        val = merged.get(key) or []
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            return {"error": f"{key}: list of strings"}
        merged[key] = val
    for key in ("grants", "content_visible_to"):
        for v in merged[key]:
            p, a = _split_ident(v)
            if not p or not a:
                return {"error": f"{key}: entries are 'project/agent' ('{v}')"}
    if not merged["runner_hosts_allowed"]:
        return {"error": "runner_hosts_allowed: at least one host"}
    tools = merged.get("tools")
    if tools is not None:
        if not isinstance(tools, dict) or tools.get("provider") not in TOOL_PROVIDERS or not tools.get("url"):
            return {"error": f"tools: {{provider: one of {sorted(TOOL_PROVIDERS)}, url, credential_ref?, allowed[]}}"}
        allowed_tools = tools.get("allowed") or []
        if not isinstance(allowed_tools, list) or not all(isinstance(t, str) for t in allowed_tools):
            return {"error": "tools.allowed: list of tool names"}
    if merged.get("adapter") == "mcp" and not tools:
        return {"error": "adapter mcp needs a tools provider on the same scope"}
    merged.setdefault("adapter_config", {})
    merged.setdefault("restricted", False)
    merged.setdefault("approval", "none")
    now = _now()
    db.di_scopes.update_one(
        {"_id": name},
        {"$set": {**merged, "updated_at": now, "updated_by": actor},
         "$setOnInsert": {"created_at": now, "created_by": actor}},
        upsert=True,
    )
    db.di_audit.insert_one({"at": now, "actor": actor, "kind": "scope", "target": name,
                            "before": existing or None, "after": merged})
    return {"status": "updated" if existing else "created", "scope": name}


def scope_public(scope: Dict[str, Any]) -> Dict[str, Any]:
    """Scope as shown to agents: no adapter_config (holds credential paths)."""
    out = {k: scope.get(k) for k in ("adapter", "runner_hosts_allowed", "grants", "content_visible_to",
                                      "restricted", "approval", "notes", "secret_kind")} | {"name": scope["_id"]}
    out["type"] = scope.get("adapter")
    out["status"] = scope.get("status") or "active"
    out["params"] = {k: v for k, v in (scope.get("adapter_config") or {}).items() if k != "credential_ref"}
    out["secret_set"] = bool(scope.get("secret_enc"))
    out["secret_set_at"] = scope.get("secret_set_at")
    out["secret_set_by"] = scope.get("secret_set_by")
    out["requested_by"] = scope.get("requested_by")
    if scope.get("tools"):
        out["tools_allowed"] = (scope["tools"] or {}).get("allowed") or []
    return _jsonable(out)


# ── connections (1.5.0): agent-requested, Tom-activated, secrets in the vault ──

CONNECTION_PARAM_KEYS = {
    "imap": {"host", "port", "user"},
    "gmail": {"mailbox"},
    "mcp": {"url"},
    "file": {"roots"},
    "inline": set(),
}


def _audit(db, actor: str, kind: str, target: str, detail: Optional[Dict[str, Any]] = None) -> None:
    db.di_audit.insert_one({"at": _now(), "actor": actor, "kind": kind, "target": target, "detail": detail or {}})


def request_connection(db, who: str, name: str, conn: Dict[str, Any]) -> Dict[str, Any]:
    """Any agent may request a typed connection; it stays inactive until Tom activates it."""
    if not name or not re.match(r"^[a-z0-9][a-z0-9:_.-]{1,63}$", name):
        return {"error": "connection name: lowercase letters, digits, ':_.-' (2-64 chars)"}
    if db.di_scopes.find_one({"_id": name}):
        return {"error": f"connection '{name}' already exists; use it as scope, or pick another name"}
    if not isinstance(conn, dict):
        return {"error": "connection: {type, params, secret, tools_allowed?, notes?}"}
    ctype = conn.get("type")
    if ctype not in CONNECTION_PARAM_KEYS:
        return {"error": f"connection.type: one of {sorted(CONNECTION_PARAM_KEYS)}"}
    params = conn.get("params") or {}
    if not isinstance(params, dict):
        return {"error": "connection.params: an object"}
    unknown = set(params) - CONNECTION_PARAM_KEYS[ctype]
    missing = CONNECTION_PARAM_KEYS[ctype] - set(params) - ({"port"} if ctype == "imap" else set())
    if unknown or missing:
        return {"error": f"connection.params for {ctype}: needs {sorted(CONNECTION_PARAM_KEYS[ctype])}",
                "unknown": sorted(unknown), "missing": sorted(missing)}
    secret_kind = conn.get("secret", "none" if ctype in ("file", "inline") else "password")
    if secret_kind not in SECRET_KINDS:
        return {"error": f"connection.secret: one of {sorted(SECRET_KINDS)}"}
    if ctype == "gmail" and secret_kind != "oauth":
        return {"error": "gmail connections use secret 'oauth' (Tom's Google sign-in, gmail.readonly)"}
    doc = {
        "_id": name, "adapter": ctype, "adapter_config": params, "secret_kind": secret_kind,
        "runner_hosts_allowed": ["sage"], "grants": [who], "content_visible_to": [who],
        "restricted": False, "approval": "none", "notes": str(conn.get("notes") or "")[:1000],
        "status": "pending_secret" if secret_kind != "none" else "pending_approval",
        "requested_by": who, "created_at": _now(), "created_by": who,
    }
    if ctype == "mcp":
        allowed = conn.get("tools_allowed") or []
        if not isinstance(allowed, list) or not all(isinstance(t, str) for t in allowed) or not allowed:
            return {"error": "mcp connections need tools_allowed: the tool names you need"}
        doc["tools"] = {"provider": "mcp", "url": params["url"], "allowed": allowed}
    db.di_scopes.insert_one(doc)
    _audit(db, who, "connection_request", name, {"type": ctype, "params": params, "secret_kind": secret_kind})
    return {"status": doc["status"], "connection": name,
            "note": "Tom activates it in /admin → Connections (secret or approval); tasks on it wait until then"}


def activate_connection(db, actor: str, name: str, secret: Optional[str] = None,
                        widen: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Owner/admin only (enforced by callers). Supplying the secret IS the approval; a new
    secret replaces the old one (rotation: the next claim seals the new value)."""
    from shared_memory import vault

    scope = db.di_scopes.find_one({"_id": name})
    if not scope:
        return {"error": f"connection '{name}' not found"}
    update: Dict[str, Any] = {"status": "active", "activated_at": _now(), "activated_by": actor}
    kind = scope.get("secret_kind") or "none"
    if kind != "none":
        if secret is None and not scope.get("secret_enc"):
            return {"error": f"connection '{name}' needs a {kind} secret"}
        if secret is not None:
            try:
                update["secret_enc"] = vault.encrypt(secret)
            except vault.VaultError as e:
                return {"error": str(e)}
            update["secret_set_at"] = _now()
            update["secret_set_by"] = actor
    elif secret:
        return {"error": f"connection '{name}' takes no secret"}
    if widen:
        allowed_keys = {"grants", "content_visible_to", "runner_hosts_allowed", "restricted", "approval", "notes"}
        bad = set(widen) - allowed_keys
        if bad:
            return {"error": f"cannot change {sorted(bad)} here"}
        for key in ("grants", "content_visible_to", "runner_hosts_allowed"):
            if key in widen and (not isinstance(widen[key], list) or not all(isinstance(v, str) for v in widen[key])):
                return {"error": f"{key}: list of strings"}
        update.update(widen)
    db.di_scopes.update_one({"_id": name}, {"$set": update, "$unset": {"revoked_at": "", "revoked_by": ""}})
    _audit(db, actor, "connection_rotate" if (secret and scope.get("secret_enc")) else "connection_activate", name,
           {"secret_changed": secret is not None, "widen": widen or {}})
    started = _wake_tasks(db, name)
    return {"status": "active", "connection": name, "tasks_started": started}


def revoke_connection(db, actor: str, name: str) -> Dict[str, Any]:
    """Stops new claims immediately and destroys the stored secret; queued runs are cancelled."""
    scope = db.di_scopes.find_one({"_id": name})
    if not scope:
        return {"error": f"connection '{name}' not found"}
    now = _now()
    db.di_scopes.update_one({"_id": name}, {"$set": {"status": "revoked", "revoked_at": now, "revoked_by": actor},
                                            "$unset": {"secret_enc": ""}})
    cancelled = db.di_runs.update_many({"scope": name, "status": "queued"},
                                       {"$set": {"status": "cancelled", "finished_at": now,
                                                 "error": "connection revoked"}}).modified_count
    _audit(db, actor, "connection_revoke", name, {"queued_runs_cancelled": cancelled})
    return {"status": "revoked", "connection": name, "queued_runs_cancelled": cancelled}


def _scope_active(db, name: Optional[str]) -> bool:
    if not name:
        return True
    doc = db.di_scopes.find_one({"_id": name}, {"status": 1})
    return bool(doc) and doc.get("status") in ACTIVE


def _wake_tasks(db, name: str) -> int:
    """Start tasks that were waiting for this connection (and whose other connection is active)."""
    started = 0
    now = _now()
    for task in db.di_tasks.find({"status": "awaiting_connection", "deleted": {"$ne": True},
                                  "$or": [{"scope": name}, {"tool_scope": name}]}):
        if not (_scope_active(db, task.get("scope")) and _scope_active(db, task.get("tool_scope"))):
            continue
        update = {"status": "active", "next_run_at": now if task.get("schedule") else None}
        db.di_tasks.update_one({"_id": task["_id"]}, {"$set": update})
        task.update(update)
        if not task.get("schedule"):
            queue_run(db, task, reason="one-off (connection activated)")
        started += 1
    return started


def connections_admin(db) -> List[Dict[str, Any]]:
    rows = []
    for s in db.di_scopes.find().sort("_id", 1):
        row = scope_public(s)
        row["tasks"] = db.di_tasks.count_documents({"deleted": {"$ne": True},
                                                    "$or": [{"scope": s["_id"]}, {"tool_scope": s["_id"]}]})
        row["legacy_credential_file"] = bool((s.get("adapter_config") or {}).get("credential_ref")
                                             or (s.get("tools") or {}).get("credential_ref"))
        rows.append(row)
    return rows


def connection_audit(db, limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.di_audit.find({"kind": {"$regex": "^(connection_|scope|runner_key)"}}).sort("at", -1).limit(limit)
    return [_jsonable({k: v for k, v in r.items() if k not in ("_id", "before", "after")}) for r in rows]


def scopes_for(db, who: Optional[str], admin: bool) -> List[Dict[str, Any]]:
    query = {} if admin else {"grants": who}
    return [scope_public(s) for s in db.di_scopes.find(query).sort("_id", 1)]


# ── tasks ──────────────────────────────────────────────────────────────────

def _can_edit(task: Dict[str, Any], who: str, admin: bool) -> bool:
    return admin or task.get("owner") == who


def _write_version(db, task: Dict[str, Any], actor: str, changed: List[str]) -> None:
    db.di_task_versions.insert_one({
        "_id": f"{task['_id']}@v{task['version']}",
        "task_id": task["_id"], "version": task["version"], "at": _now(), "actor": actor,
        "changed": changed,
        "definition": {k: task.get(k) for k in EDITABLE_FIELDS | {"name", "scope"}},
    })


def validate_task_tools(db, fields: Dict[str, Any], task_scope: Dict[str, Any], owner: str,
                        admin: bool) -> List[str]:
    """Tools a task may call = runner credential (enforced by the provider) ∩ the tool
    scope's allowed list (Tom) ∩ the task's own list (owner); the owner must be granted
    the tool scope (§6.1)."""
    tools = fields.get("tools") or []
    if not tools:
        return []
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools) or len(tools) > MAX_TOOLS:
        return [f"tools: list of at most {MAX_TOOLS} tool names"]
    mtc = fields.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    errors = []
    if not isinstance(mtc, int) or not 1 <= mtc <= MAX_TOOL_CALLS:
        errors.append(f"max_tool_calls: 1..{MAX_TOOL_CALLS}")
    name = fields.get("tool_scope") or task_scope.get("_id")
    tscope = task_scope if name == task_scope.get("_id") else db.di_scopes.find_one({"_id": name})
    if not tscope or not tscope.get("tools"):
        return errors + [f"tool_scope '{name}': no such scope with a tools provider"]
    if not admin and owner not in (tscope.get("grants") or []):
        errors.append(f"tool_scope '{name}' is not granted to {owner}")
    if not set(tscope.get("runner_hosts_allowed") or []) & set(task_scope.get("runner_hosts_allowed") or []):
        errors.append(f"tool_scope '{name}' and scope '{task_scope.get('_id')}' share no runner host")
    allowed = set((tscope["tools"] or {}).get("allowed") or [])
    missing = sorted(set(tools) - allowed)
    if missing:
        errors.append(f"tools not allowed by scope '{name}': {missing}; allowed: {sorted(allowed)}")
    return errors


def create_task(db, who: str, admin: bool, fields: Dict[str, Any]) -> Dict[str, Any]:
    name = (fields.get("name") or "").strip()
    if not name or len(name) > 80:
        return {"error": "name: required, max 80 chars"}
    scope = db.di_scopes.find_one({"_id": fields.get("scope")})
    conn_result = None
    if not scope and fields.get("connection"):
        conn_result = request_connection(db, who, fields.get("scope") or "", fields["connection"])
        if conn_result.get("error"):
            return {"error": "connection request failed", "details": [conn_result["error"]]}
        scope = db.di_scopes.find_one({"_id": fields.get("scope")})
    if not scope:
        return {"error": f"scope '{fields.get('scope')}' not found; memory_runners(action='scopes') lists yours, "
                         "or pass connection={type, params, secret} to request a new one"}
    if not admin and who not in (scope.get("grants") or []):
        return {"error": f"scope '{scope['_id']}' is not granted to {who}"}
    if scope.get("status") == "revoked":
        return {"error": f"connection '{scope['_id']}' is revoked"}
    if db.di_tasks.count_documents({"owner": who, "deleted": {"$ne": True}}) >= MAX_TASKS_PER_AGENT:
        return {"error": f"task cap reached ({MAX_TASKS_PER_AGENT}); delete one first"}
    if db.di_tasks.find_one({"owner": who, "name": name, "deleted": {"$ne": True}}):
        return {"error": f"you already have a task named '{name}'"}
    defn = {
        "instructions": fields.get("instructions"),
        "output_schema": fields.get("output_schema"),
        "source_params": fields.get("source_params") or {},
        "model": fields.get("model") or DEFAULT_MODEL,
        "routes": fields.get("routes") or [],
        "schedule": fields.get("schedule"),
        "canaries": fields.get("canaries") or [],
        "description": fields.get("description") or "",
        "per_item": bool(fields.get("per_item", True)),
        "expect_items": bool(fields.get("expect_items", False)),
        "tools": fields.get("tools") or [],
        "tool_scope": fields.get("tool_scope"),
        "max_tool_calls": fields.get("max_tool_calls") or DEFAULT_MAX_TOOL_CALLS,
    }
    errors = validate_task_fields(defn, scope) + validate_task_tools(db, defn, scope, who, admin)
    if errors:
        return {"error": "invalid task", "details": errors}
    now = _now()
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    needs_approval = bool(defn["schedule"]) and scope.get("approval") == "first_run"
    waiting = not (_scope_active(db, scope["_id"]) and _scope_active(db, defn.get("tool_scope")))
    task = {
        "_id": task_id, "name": name, "scope": scope["_id"], "owner": who, **defn,
        "version": 1,
        "status": "awaiting_connection" if waiting else ("awaiting_approval" if needs_approval else "active"),
        "cursor": None, "consecutive_failures": 0,
        "next_run_at": None, "last_run_at": None, "last_success_at": None,
        "last_canary_local_date": None, "digest_next_at": None,
        "created_at": now, "updated_at": now, "created_by": who, "updated_by": who,
    }
    if defn["schedule"]:
        task["next_run_at"] = now if not (needs_approval or waiting) else None
        task["digest_next_at"] = _digest_due_after(defn["schedule"].get("digest_at") or DEFAULT_DIGEST_AT,
                                                   defn["schedule"].get("timezone"), now)
    db.di_tasks.insert_one(task)
    _write_version(db, task, who, sorted(defn))
    out = {"status": "created", "task_id": task_id, "version": 1, "task_status": task["status"]}
    if not defn["schedule"] or fields.get("run_now"):
        if not needs_approval and not waiting:
            out["run_id"] = queue_run(db, task, reason="one-off" if not defn["schedule"] else "run_now")
    if waiting:
        out["note"] = "waiting for Tom to activate the connection in /admin → Connections; starts automatically"
        if conn_result:
            out["connection_status"] = conn_result["status"]
    elif needs_approval:
        out["note"] = "scope requires Tom's approval before the first scheduled run"
    return out


def get_task(db, task_id: str) -> Optional[Dict[str, Any]]:
    return db.di_tasks.find_one({"_id": task_id, "deleted": {"$ne": True}})


def task_public(task: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in task.items() if k != "_id"}
    out["task_id"] = task["_id"]
    return _jsonable(out)


def list_tasks(db, who: str, admin: bool, all_tasks: bool = False,
               project_admin: bool = False) -> List[Dict[str, Any]]:
    """Caller's own tasks; all_tasks widens to everyone's for an admin, or to
    the caller's project for a project admin (projects.admins)."""
    query: Dict[str, Any] = {"deleted": {"$ne": True}}
    if all_tasks and admin:
        pass
    elif all_tasks and project_admin:
        query["owner"] = {"$regex": f"^{re.escape(_split_ident(who)[0])}/"}
    else:
        query["owner"] = who
    rows = []
    for t in db.di_tasks.find(query).sort("created_at", 1):
        rows.append(_jsonable({
            "task_id": t["_id"], "name": t["name"], "owner": t["owner"], "scope": t["scope"],
            "status": t["status"], "version": t["version"], "schedule": t.get("schedule"),
            "model": t.get("model"), "next_run_at": t.get("next_run_at"),
            "last_run_at": t.get("last_run_at"), "last_success_at": t.get("last_success_at"),
            "consecutive_failures": t.get("consecutive_failures", 0),
        }))
    return rows


def edit_task(db, task_id: str, who: str, admin: bool, changes: Dict[str, Any]) -> Dict[str, Any]:
    task = get_task(db, task_id)
    if not task:
        return {"error": f"task {task_id} not found"}
    if not _can_edit(task, who, admin):
        return {"error": f"only the owner ({task['owner']}) or an admin can edit this task"}
    if not isinstance(changes, dict) or not changes:
        return {"error": f"changes: an object with any of {sorted(EDITABLE_FIELDS)}"}
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        return {"error": f"not editable: {sorted(unknown)}; editable: {sorted(EDITABLE_FIELDS)}"}
    scope = db.di_scopes.find_one({"_id": task["scope"]}) or {}
    merged = {k: task.get(k) for k in EDITABLE_FIELDS}
    merged.update(changes)
    errors = validate_task_fields(merged, scope) + validate_task_tools(db, merged, scope, task["owner"], admin)
    if errors:
        return {"error": "invalid change", "details": errors}
    now = _now()
    update = {**changes, "version": task["version"] + 1, "updated_at": now, "updated_by": who}
    if CANARY_RESET_FIELDS & set(changes):
        update["last_canary_local_date"] = None
    if "schedule" in changes:
        sched = changes["schedule"]
        update["next_run_at"] = now if sched else None
        update["digest_next_at"] = (_digest_due_after(sched.get("digest_at") or DEFAULT_DIGEST_AT,
                                                      sched.get("timezone"), now) if sched else None)
    reapproval = (scope.get("approval") == "first_run" and merged.get("schedule")
                  and bool(APPROVAL_FIELDS & set(changes)))
    if reapproval:
        update["status"] = "awaiting_approval"
        update["next_run_at"] = None
    db.di_tasks.update_one({"_id": task_id}, {"$set": update})
    task.update(update)
    _write_version(db, task, who, sorted(changes))
    out = {"status": "edited", "task_id": task_id, "version": task["version"], "task_status": task["status"]}
    if reapproval:
        out["note"] = "this scope requires Tom's re-approval after instruction/schema/source edits"
    return out


def set_task_status(db, task_id: str, who: str, admin: bool, action: str) -> Dict[str, Any]:
    task = get_task(db, task_id)
    if not task:
        return {"error": f"task {task_id} not found"}
    if action == "approve":
        if not admin:
            return {"error": "approve needs an owner/admin role"}
        if task["status"] != "awaiting_approval":
            return {"error": f"task is {task['status']}, not awaiting_approval"}
    elif not _can_edit(task, who, admin):
        return {"error": f"only the owner ({task['owner']}) or an admin can {action} this task"}
    now = _now()
    if action in ("resume", "approve"):
        if task["status"] == "awaiting_approval" and action == "resume":
            return {"error": "task is awaiting Tom's approval"}
        if task["status"] == "awaiting_connection":
            return {"error": "task is waiting for its connection to be activated"}
        update = {"status": "active", "consecutive_failures": 0,
                  "next_run_at": now if task.get("schedule") else None}
    elif action == "pause":
        update = {"status": "paused", "next_run_at": None}
    elif action == "delete":
        update = {"deleted": True, "status": "deleted", "next_run_at": None}
        db.di_runs.update_many({"task_id": task_id, "status": "queued"},
                               {"$set": {"status": "cancelled", "finished_at": now, "error": "task deleted"}})
    else:
        return {"error": f"unknown action {action}"}
    update.update({"updated_at": now, "updated_by": who})
    db.di_tasks.update_one({"_id": task_id}, {"$set": update})
    db.di_audit.insert_one({"at": now, "actor": who, "kind": f"task_{action}", "target": task_id})
    return {"status": action + ("d" if action.endswith("e") else "ed"), "task_id": task_id}


def task_history(db, task_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = db.di_task_versions.find({"task_id": task_id}).sort("version", -1).limit(limit)
    return [_jsonable({k: v for k, v in r.items() if k != "_id"}) for r in rows]


# ── runs ───────────────────────────────────────────────────────────────────

# Claim order: runs of SCHEDULED tasks (lane 0) before one-offs (lane 1), then
# oldest first. A scheduled task is an ongoing job (e.g. live support triage);
# one-off batches must not delay it (nimbusSupport msg_e8373eadba52, 2026-10-13).
LANE_SCHEDULED, LANE_ONE_OFF = 0, 1


def run_lane(task: Dict[str, Any]) -> int:
    return LANE_SCHEDULED if task.get("schedule") else LANE_ONE_OFF


def queue_run(db, task: Dict[str, Any], reason: str, now: Optional[datetime] = None) -> str:
    now = now or _now()
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    forced = reason.startswith("run_now")
    canary_due = (forced or _canary_due(task, now)) and bool(task.get("canaries"))
    db.di_runs.insert_one({
        "_id": run_id, "task_id": task["_id"], "task_version": task["version"], "owner": task["owner"],
        "scope": task["scope"], "status": "queued", "reason": reason, "queued_at": now,
        "lane": run_lane(task), "canary_due": canary_due, "expire_at": now + timedelta(days=RUN_TTL_DAYS),
    })
    return run_id


def _canary_due(task: Dict[str, Any], now: datetime) -> bool:
    sched = task.get("schedule") or {}
    if not sched or sched.get("canary_every") == "run":
        return True
    every = parse_every(sched.get("every") or "")
    if sched.get("canary_every") is None and every and every >= 3600:
        return True
    local_date = now.astimezone(ZoneInfo(sched.get("timezone") or DEFAULT_TZ)).date().isoformat()
    return task.get("last_canary_local_date") != local_date


def run_public(run: Dict[str, Any], include_items: bool = False) -> Dict[str, Any]:
    out = {k: v for k, v in run.items() if k not in ("_id", "items", "expire_at")}
    out["run_id"] = run["_id"]
    if include_items:
        out["items"] = run.get("items", [])
    return _jsonable(out)


def list_runs(db, task_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    rows = db.di_runs.find({"task_id": task_id}).sort("queued_at", -1).limit(limit)
    return [run_public(r) for r in rows]


def cancel_run(db, run_id: str, who: str, admin: bool) -> Dict[str, Any]:
    run = db.di_runs.find_one({"_id": run_id})
    if not run:
        return {"error": f"run {run_id} not found"}
    if not admin and run.get("owner") != who:
        return {"error": "not your run"}
    if run["status"] not in ("queued", "claimed"):
        return {"error": f"run is already {run['status']}"}
    db.di_runs.update_one({"_id": run_id, "status": run["status"]},
                          {"$set": {"status": "cancelled", "finished_at": _now(), "error": f"cancelled by {who}"}})
    return {"status": "cancelled", "run_id": run_id}


# ── runners ────────────────────────────────────────────────────────────────

def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_runner_key(db, actor: str, runner_id: str, host: str) -> Dict[str, Any]:
    if not re.match(r"^[a-z0-9][a-z0-9_.-]{1,40}$", runner_id or ""):
        return {"error": "runner_id: lowercase letters, digits, '_.-'"}
    raw = "jr_" + secrets.token_urlsafe(32)
    db.di_runner_keys.update_one(
        {"_id": runner_id},
        {"$set": {"key_hash": _hash(raw), "host": host, "active": True, "created_at": _now(), "created_by": actor}},
        upsert=True,
    )
    db.di_audit.insert_one({"at": _now(), "actor": actor, "kind": "runner_key", "target": runner_id})
    return {"status": "created", "runner_id": runner_id, "host": host, "key": raw,
            "note": "shown once; store it on the runner host (mode 600)"}


def authenticate_runner(db, raw_key: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw_key:
        return None
    return db.di_runner_keys.find_one({"key_hash": _hash(raw_key), "active": True})


def register_runner(db, key_doc: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    models = [m for m in (body.get("models") or []) if isinstance(m, str)][:200]
    scopes = [s for s in (body.get("scopes_served") or []) if isinstance(s, str)][:100]
    adapters = [a for a in (body.get("adapters") or []) if a in ADAPTERS]
    pub = body.get("public_key")
    from shared_memory import vault
    if pub is not None and not vault.valid_public_key(pub):
        return {"error": "public_key: base64 raw X25519 public key"}
    now = _now()
    db.di_runners.update_one(
        {"_id": key_doc["_id"]},
        {"$set": {"host": key_doc.get("host"), "models": models, "scopes_served": scopes, "adapters": adapters,
                  "public_key": pub, "version": str(body.get("version") or "")[:40], "last_heartbeat": now},
         "$setOnInsert": {"registered_at": now}},
        upsert=True,
    )
    return {"status": "registered", "runner_id": key_doc["_id"]}


def heartbeat_runner(db, key_doc: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    db.di_runners.update_one({"_id": key_doc["_id"]}, {"$set": {"last_heartbeat": now}})
    run_id = body.get("run_id")
    extended = False
    if run_id:
        res = db.di_runs.update_one(
            {"_id": run_id, "runner_id": key_doc["_id"], "status": "claimed"},
            {"$set": {"lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        )
        extended = bool(res.matched_count)
    return {"status": "ok", "lease_extended": extended}


def claim_run(db, key_doc: Dict[str, Any]) -> Dict[str, Any]:
    runner = db.di_runners.find_one({"_id": key_doc["_id"]}) or {}
    host = key_doc.get("host")
    served = set(runner.get("scopes_served") or [])
    adapters = set(runner.get("adapters") or [])
    allowed = []
    for s in db.di_scopes.find({"runner_hosts_allowed": host, "status": {"$in": list(ACTIVE)}}):
        legacy = bool((s.get("adapter_config") or {}).get("credential_ref"))
        if s["_id"] in served or (not legacy and s.get("adapter") in adapters):
            allowed.append(s["_id"])
    if not allowed:
        return {}
    now = _now()
    run = db.di_runs.find_one_and_update(
        {"status": "queued", "scope": {"$in": allowed}},
        {"$set": {"status": "claimed", "runner_id": key_doc["_id"], "claimed_at": now,
                  "lease_until": now + timedelta(seconds=LEASE_SECONDS)}},
        sort=[("lane", 1), ("queued_at", 1)],
        return_document=True,
    )
    if not run:
        return {}
    task = db.di_tasks.find_one({"_id": run["task_id"]}) or {}
    scope = db.di_scopes.find_one({"_id": run["scope"]}) or {}
    tname = task.get("tool_scope")
    if tname and not _scope_active(db, tname):
        db.di_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "finished_at": now,
                                                             "error": f"tool connection {tname} is not active"}})
        return {}
    try:
        scope_secret = _seal_for(scope, runner)
    except Exception as e:
        db.di_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "finished_at": now,
                                                             "error": f"secret unavailable: {e}"}})
        return {}
    tool_scope = None
    if task.get("tools") or scope.get("adapter") == "mcp":
        tname = task.get("tool_scope") or scope.get("_id")
        tdoc = scope if tname == scope.get("_id") else (db.di_scopes.find_one({"_id": tname}) or {})
        tcfg = dict(tdoc.get("tools") or {})
        allowed = set(tcfg.get("allowed") or [])
        tcfg["allowed"] = sorted(allowed & set(task.get("tools") or []))
        source_tool = (task.get("source_params") or {}).get("tool") if scope.get("adapter") == "mcp" else None
        if source_tool and source_tool in allowed:
            tcfg["source_tool"] = source_tool
        tool_scope = {"name": tname, "tools": tcfg, "secret_sealed": _seal_for(tdoc, runner)}
    return _jsonable({
        "run": {"run_id": run["_id"], "task_id": run["task_id"], "task_version": run["task_version"],
                "canary_due": run.get("canary_due", False)},
        "task": {k: task.get(k) for k in ("name", "instructions", "output_schema", "source_params", "model",
                                          "per_item", "canaries", "expect_items", "tools", "max_tool_calls")},
        "tool_scope": tool_scope,
        "scope": {"name": scope.get("_id"), "adapter": scope.get("adapter"),
                  "adapter_config": scope.get("adapter_config") or {}, "secret_sealed": scope_secret},
        "cursor": task.get("cursor"),
    })


def _seal_for(scope: Dict[str, Any], runner: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The scope's secret sealed to this runner, or None when the scope has no vault secret."""
    if not scope or not scope.get("secret_enc"):
        return None
    from shared_memory import vault
    if not runner.get("public_key"):
        raise RuntimeError("runner registered no public_key; upgrade the runner")
    return vault.seal(vault.decrypt(scope["secret_enc"]), runner["public_key"])


def _fail_task_run(db, run: Dict[str, Any], task: Dict[str, Any], error: str, now: datetime) -> Dict[str, Any]:
    db.di_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "finished_at": now,
                                                         "error": error[:2000]}})
    failures = task.get("consecutive_failures", 0) + 1
    limit = (task.get("schedule") or {}).get("pause_after_failures", DEFAULT_PAUSE_AFTER_FAILURES)
    update = {"consecutive_failures": failures, "last_run_at": now}
    paused = bool(task.get("schedule")) and failures >= limit
    if paused:
        update.update({"status": "paused", "next_run_at": None, "paused_reason": f"{failures} consecutive failures"})
    db.di_tasks.update_one({"_id": task["_id"]}, {"$set": update})
    return {"paused": paused, "failures": failures}


def fail_run(db, key_doc: Dict[str, Any], body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict]]:
    run = db.di_runs.find_one({"_id": body.get("run_id"), "runner_id": key_doc["_id"], "status": "claimed"})
    if not run:
        return {"error": "run not found or not claimed by this runner"}, []
    task = db.di_tasks.find_one({"_id": run["task_id"]}) or {"_id": run["task_id"]}
    error = str(body.get("error") or "runner reported failure")
    info = _fail_task_run(db, run, task, error, _now())
    return {"status": "failed", **info}, [_failure_notice(task, run["_id"], error, info)]


def _failure_notice(task: Dict[str, Any], run_id: str, error: str, info: Dict[str, Any]) -> Dict[str, Any]:
    head = "AUTO-PAUSED after %d failures" % info["failures"] if info.get("paused") else "run failed"
    body = (f"Delegated task '{task.get('name')}' ({task.get('_id')}): {head}.\n"
            f"run {run_id}: {error[:500]}\n"
            f"Inspect: memory_schedule(action='runs', task_id='{task.get('_id')}')"
            + ("; resume with memory_schedule(action='resume') after fixing." if info.get("paused") else ""))
    return {"to": [task["owner"]] if task.get("owner") else [], "subject": f"Delegated task {head}: {task.get('name')}",
            "body": body, "category": "info", "kind": "delegated_failure"}


# ── routing ────────────────────────────────────────────────────────────────

def _lookup(field: str, output: Dict[str, Any], meta: Dict[str, Any]):
    if field.startswith("item."):
        return meta.get(field[5:])
    cur: Any = output
    for part in field.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _cond_true(cond: Optional[Dict[str, Any]], output, meta) -> bool:
    if not cond:
        return True
    val = _lookup(cond["field"], output, meta)
    op, ref = cond["op"], cond.get("value")
    try:
        if op == "eq":
            return val == ref
        if op == "ne":
            return val != ref
        if op == "in":
            return (val in ref) if not isinstance(val, list) else any(v in ref for v in val)
        if op == "not_in":
            return (val not in ref) if not isinstance(val, list) else not any(v in ref for v in val)
        if op == "exists":
            return val not in (None, "", [], {})
        if op == "true":
            return val is True
        if op == "false":
            return val is False
        if val is None:
            return False
        return {"lt": val < ref, "lte": val <= ref, "gt": val > ref, "gte": val >= ref}[op]
    except TypeError:
        return False


def choose_route(routes: List[Dict[str, Any]], output, meta) -> Dict[str, Any]:
    for r in routes or []:
        if _cond_true(r.get("if"), output, meta):
            return r
    return {"then": "digest"}


def visible_payload(output_schema, output, meta, target: str, scope) -> Dict[str, Any]:
    """Fields the target may see: everything for content_visible_to, else non-sensitive output only."""
    if target in (scope.get("content_visible_to") or []):
        return {"output": output, "item": meta}
    props = _schema_properties(output_schema)
    safe = {k: v for k, v in (output or {}).items() if not (props.get(k) or {}).get("x-sensitive")}
    return {"output": safe, "item": {"item_ref": meta.get("item_ref")}}


def _fill(template: str, view: Dict[str, Any]) -> str:
    def sub(m):
        field = m.group(1)
        v = view["item"].get(field[5:]) if field.startswith("item.") else _lookup(field, view["output"], {})
        return "" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v))
    return _PLACEHOLDER_RE.sub(sub, template)


def digest_line(route, task, scope, output, meta) -> str:
    """One digest bullet: the route's template if given, else item ref + subject/from + visible scalar fields."""
    view = visible_payload(task.get("output_schema"), output, meta, task.get("owner"), scope)
    if route.get("template"):
        return _fill(route["template"], view)[:400]
    item = view["item"]
    head = [str(item.get("item_ref") or "")]
    for k in ("date", "from", "subject"):
        if item.get(k):
            head.append(str(item[k])[:120])
    fields = [f"{k}={v}" for k, v in (view["output"] or {}).items() if isinstance(v, (str, int, float, bool))]
    return " | ".join(head + fields)[:400]


def render_notice(route, task, scope, output, meta) -> str:
    target = route.get("to")
    view = visible_payload(task.get("output_schema"), output, meta, target, scope)
    template = route.get("template")
    if template:
        text = _fill(template, view)
    else:
        lines = [f"{k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in view["item"].items()
                 if v not in (None, "")]
        lines += [f"{k}: {json.dumps(v) if isinstance(v, (dict, list)) else v}" for k, v in view["output"].items()]
        text = "\n".join(lines)
    return (f"[delegated task '{task.get('name')}' v{task.get('version')} · local model {task.get('model')} · "
            f"unverified model output]\n{text}")[:6000]


def _check_canaries(task, canary_items) -> Dict[str, Any]:
    expected = len(task.get("canaries") or [])
    failed = []
    for it in canary_items:
        idx = it.get("canary")
        spec = (task.get("canaries") or [])[idx] if isinstance(idx, int) and idx < expected else None
        if spec is None:
            continue
        out = it.get("output") or {}
        if it.get("error") or not out:
            failed.append({"canary": idx, "reason": it.get("error") or "no output"})
            continue
        for field, want in spec["expect"].items():
            got = out.get(field)
            ok = (got in want or (isinstance(got, list) and any(g in want for g in got))) \
                if isinstance(want, list) else (got == want or (isinstance(got, list) and want in got))
            if not ok:
                failed.append({"canary": idx, "field": field, "expected": want, "got": got})
    seen = {it.get("canary") for it in canary_items}
    for idx in range(expected):
        if idx not in seen:
            failed.append({"canary": idx, "reason": "not returned (locator found nothing?)"})
    return {"ran": True, "expected": expected, "passed": expected - len({f["canary"] for f in failed}),
            "failed": failed}


def complete_run(db, key_doc: Dict[str, Any], body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict]]:
    """Record results, apply routes. Returns (response, notices_to_send)."""
    import jsonschema

    run = db.di_runs.find_one({"_id": body.get("run_id"), "runner_id": key_doc["_id"], "status": "claimed"})
    if not run:
        return {"error": "run not found or not claimed by this runner"}, []
    task = db.di_tasks.find_one({"_id": run["task_id"]})
    if not task:
        db.di_runs.update_one({"_id": run["_id"]}, {"$set": {"status": "failed", "error": "task gone"}})
        return {"error": "task gone"}, []
    scope = db.di_scopes.find_one({"_id": run["scope"]}) or {}
    now = _now()
    items = body.get("items") or []
    if not isinstance(items, list) or len(items) > MAX_ITEMS_PER_RUN + MAX_CANARIES:
        return {"error": f"items: list of at most {MAX_ITEMS_PER_RUN}"}, []
    evidence = body.get("source_evidence")
    examined = int(body.get("items_examined") or 0)
    real = [it for it in items if isinstance(it, dict) and it.get("canary") is None]
    canary_items = [it for it in items if isinstance(it, dict) and it.get("canary") is not None]

    notices: List[Dict[str, Any]] = []
    if examined == 0 and not evidence:
        info = _fail_task_run(db, run, task, "0 items and no source evidence (did the source look?)", now)
        return {"status": "failed", **info}, [_failure_notice(task, run["_id"], "0 items, no source evidence", info)]
    if examined == 0 and task.get("expect_items"):
        info = _fail_task_run(db, run, task, "0 items but expect_items=true", now)
        return {"status": "failed", **info}, [_failure_notice(task, run["_id"], "0 items, expect_items", info)]

    validator = jsonschema.Draft202012Validator(task.get("output_schema") or {})
    route_counts: Dict[str, int] = {}
    stored = []
    failed_items = 0
    for it in real:
        meta = dict(it.get("meta") or {})
        meta["item_ref"] = it.get("item_ref")
        out = it.get("output")
        err = it.get("error")
        if not err and out is not None:
            problems = [e.message for e in validator.iter_errors(out)][:3]
            if problems:
                err = "schema: " + "; ".join(problems)
        if err or out is None:
            failed_items += 1
            stored.append({"item_ref": it.get("item_ref"), "meta": meta, "error": (err or "no output")[:500],
                           "tool_calls": (it.get("tool_calls") or [])[:MAX_TOOL_CALLS]})
            route_counts["error"] = route_counts.get("error", 0) + 1
            continue
        route = choose_route(task.get("routes"), out, meta)
        action = route["then"]
        route_counts[action] = route_counts.get(action, 0) + 1
        stored.append({"item_ref": it.get("item_ref"), "meta": meta, "output": out, "route": action,
                       "to": route.get("to"), "tool_calls": (it.get("tool_calls") or [])[:MAX_TOOL_CALLS],
                       "digest_line": digest_line(route, task, scope, out, meta) if action == "digest" else None})
        if action == "notify":
            notices.append({
                "to": [route["to"]], "category": route.get("category", "info"),
                "subject": f"[{task['name']}] " + str(meta.get("subject") or out.get("classification") or "item")[:120],
                "body": render_notice(route, task, scope, out, meta), "kind": "delegated_result",
            })

    canary = _check_canaries(task, canary_items) if run.get("canary_due") else {"ran": False}
    status = "done"
    db.di_runs.update_one({"_id": run["_id"]}, {"$set": {
        "status": status, "finished_at": now, "items": stored, "items_examined": examined,
        "items_failed": failed_items, "route_counts": route_counts, "canary": canary,
        "tool_calls_total": sum(len(i.get("tool_calls") or []) for i in stored),
        "source_evidence": evidence, "cursor_in": task.get("cursor"), "cursor_out": body.get("cursor_out"),
        "model": body.get("model") or task.get("model"),
        "duration_s": round((now - _aware(run.get("claimed_at") or now)).total_seconds(), 1),
    }})
    task_update = {"last_run_at": now, "last_success_at": now, "consecutive_failures": 0}
    if body.get("cursor_out") is not None:
        task_update["cursor"] = body["cursor_out"]
    if canary.get("ran"):
        sched = task.get("schedule") or {}
        task_update["last_canary_local_date"] = now.astimezone(
            ZoneInfo(sched.get("timezone") or DEFAULT_TZ)).date().isoformat()
    db.di_tasks.update_one({"_id": task["_id"]}, {"$set": task_update})
    failed_rows = [i for i in stored if i.get("error")]
    if failed_rows and task.get("schedule"):  # one-offs: the "job done" notice already counts errors
        # The cursor still advances past these items, so this notice is the only
        # thing that keeps one (e.g. a customer email) from vanishing silently
        # (nimbusSupport msg_03088a74ebf4). An action category: someone must handle them.
        lines = []
        for i in failed_rows[:25]:
            m = i.get("meta") or {}
            ident = " · ".join(str(m[k])[:120] for k in ("from", "subject", "date") if m.get(k))
            lines.append(f"- {i['item_ref']}" + (f" ({ident})" if ident else "") + f": {i['error'][:200]}")
        more = f"\n…and {len(failed_rows) - 25} more" if len(failed_rows) > 25 else ""
        notices.append({
            "to": [task["owner"]], "category": "task", "kind": "delegated_failure",
            "subject": f"[{task['name']}] {len(failed_rows)} item(s) FAILED, not routed",
            "body": (f"Task '{task['name']}' ({task['_id']}) run {run['_id']}: {len(failed_rows)} of {len(stored)} "
                     f"item(s) got no valid model output, so no route ran for them and the source cursor has "
                     f"moved past them. Handle them another way:\n" + "\n".join(lines) + more
                     + f"\nDetails: memory_job(run_id='{run['_id']}', action='result')."),
        })
    if canary.get("failed"):
        notices.append({
            "to": [task["owner"]], "category": "info", "kind": "delegated_failure",
            "subject": f"Delegated task canary MISS: {task['name']}",
            "body": (f"Task '{task['name']}' ({task['_id']}) run {run['_id']}: {len(canary['failed'])} canary "
                     f"problem(s): {json.dumps(canary['failed'])[:1500]}\nRouting still ran; check the instructions."),
        })
    if not task.get("schedule"):
        notices.append({
            "to": [task["owner"]], "category": "info", "kind": "delegated_result",
            "subject": f"Delegated job done: {task['name']}",
            "body": (f"One-off task '{task['name']}' run {run['_id']} done: {examined} examined, routes "
                     f"{json.dumps(route_counts)}"
                     + (f" ({failed_items} FAILED: no valid output, not routed)" if failed_items else "")
                     + f". Results: memory_job(run_id='{run['_id']}', action='result')."),
        })
    return {"status": status, "routed": route_counts, "canary": canary}, notices


# ── scheduler loop: queue due runs, expire leases, daily digests ──────────

def schedule_due(db, now: Optional[datetime] = None) -> int:
    now = now or _now()
    queued = 0
    for task in db.di_tasks.find({"status": "active", "deleted": {"$ne": True},
                                  "next_run_at": {"$ne": None, "$lte": now}}):
        busy = db.di_runs.find_one({"task_id": task["_id"], "status": {"$in": ["queued", "claimed"]}})
        if not busy:
            queue_run(db, task, reason="schedule", now=now)
            queued += 1
        nxt = next_fire(task.get("schedule") or {}, now)
        db.di_tasks.update_one({"_id": task["_id"]}, {"$set": {"next_run_at": nxt}})
    return queued


def expire_leases(db, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now = now or _now()
    notices = []
    for run in db.di_runs.find({"status": "claimed", "lease_until": {"$lt": now}}):
        task = db.di_tasks.find_one({"_id": run["task_id"]}) or {"_id": run["task_id"]}
        info = _fail_task_run(db, run, task, "lease expired (runner died or hung)", now)
        notices.append(_failure_notice(task, run["_id"], "lease expired (runner died or hung)", info))
    return notices


def digest_due(db, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now = now or _now()
    notices = []
    for task in db.di_tasks.find({"deleted": {"$ne": True}, "schedule": {"$ne": None},
                                  "digest_next_at": {"$ne": None, "$lte": now}}):
        since = now - timedelta(days=1)
        runs = list(db.di_runs.find({"task_id": task["_id"], "queued_at": {"$gte": since}}))
        done = [r for r in runs if r["status"] == "done"]
        failed = [r for r in runs if r["status"] == "failed"]
        examined = sum(r.get("items_examined", 0) for r in done)
        counts: Dict[str, int] = {}
        digest_lines = []
        for r in done:
            for k, v in (r.get("route_counts") or {}).items():
                counts[k] = counts.get(k, 0) + v
            for it in r.get("items") or []:
                if it.get("route") == "digest" and len(digest_lines) < 100:
                    line = it.get("digest_line")
                    if not line:  # runs recorded before digest_line existed
                        line = digest_line({}, task, db.di_scopes.find_one({"_id": task["scope"]}) or {},
                                           it.get("output") or {}, it.get("meta") or {})
                    digest_lines.append("- " + line)
        canaries = [r.get("canary") for r in done if (r.get("canary") or {}).get("ran")]
        canary_txt = ("none ran" if not canaries else
                      "; ".join(f"{c['passed']}/{c['expected']} passed" for c in canaries))
        cursors = [r.get("cursor_out") for r in done if r.get("cursor_out") is not None]
        body = (f"Daily digest for delegated task '{task['name']}' ({task['_id']}), status {task['status']}.\n"
                f"Runs (24h): {len(runs)} total, {len(done)} done, {len(failed)} failed. "
                f"Items examined: {examined}. Routes: {json.dumps(counts)}. Canaries: {canary_txt}.\n"
                f"Cursor: {cursors[0] if cursors else '-'} → {cursors[-1] if cursors else '-'}.\n"
                + (("Items routed to digest:\n" + "\n".join(digest_lines)) if digest_lines else "No digest items."))
        notices.append({"to": [task["owner"]], "category": "info", "kind": "delegated_digest",
                        "subject": f"Digest: {task['name']} ({examined} items, {len(failed)} failed runs)",
                        "body": body[:8000]})
        sched = task.get("schedule") or {}
        db.di_tasks.update_one({"_id": task["_id"]}, {"$set": {"digest_next_at": _digest_due_after(
            sched.get("digest_at") or DEFAULT_DIGEST_AT, sched.get("timezone"), now)}})
    return notices


async def send_notices(db, notices: List[Dict[str, Any]]) -> int:
    """Persist + push each notice as a system message to every target."""
    from shared_memory.config import ACTION_CATEGORIES
    from shared_memory.tools.messaging import _build_announce_packet, _notify_inbox_for_send

    sent = 0
    for n in notices:
        for target in n.get("to") or []:
            project, agent = _split_ident(target)
            if not project or not agent:
                continue
            now = _now()
            category = n.get("category", "info")
            doc = {
                "_id": f"msg_{uuid.uuid4().hex[:12]}",
                "to_instance": agent, "to_project": project,
                "from_instance": "delegation", "from_project": "junto", "from_session": None,
                "subject": n.get("subject"), "message": n.get("body"),
                "priority": "normal", "category": category,
                "obligation": "open" if category in ACTION_CATEGORIES else None,
                "reply_to": None, "in_response_to": None, "chain_depth": 0,
                "require_human": False, "sent_by_human": False, "human_interacted": False,
                "user_originated": False, "status": "pending", "push_suppressed": False,
                "push_suppress_reason": None, "emission_count": 0, "recency_bypass": False,
                "is_system_notice": True, "system_notice_kind": n.get("kind", "delegated_result"),
                "created_at": now, "delivered_at": None, "received_at": None, "completed_at": None,
                "expire_at": None if category in ACTION_CATEGORIES else now + timedelta(hours=48),
            }
            try:
                db.messages.insert_one(doc)
                sent += 1
            except Exception as e:
                log.error("delegation: notice insert failed for %s: %s", target, e)
                continue
            try:
                await _notify_inbox_for_send(project, agent, _build_announce_packet(doc))
            except Exception as e:  # persisted; pull still works
                log.warning("delegation: push failed for %s: %s", target, e)
    return sent


async def tick(db) -> Dict[str, int]:
    if db is None:
        return {}
    queued = schedule_due(db)
    notices = expire_leases(db) + digest_due(db)
    sent = await send_notices(db, notices) if notices else 0
    return {"queued": queued, "notices": sent}


async def _loop() -> None:
    from shared_memory.clients import get_mongo

    log.info("delegation: scheduler loop started (tick=%ss)", LOOP_SECONDS)
    while True:
        try:
            await asyncio.sleep(LOOP_SECONDS)
            await tick(get_mongo())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the loop die
            log.error("delegation: loop error: %s", e)


def ensure_indexes(db) -> None:
    db.di_tasks.create_index([("status", 1), ("next_run_at", 1)])
    db.di_tasks.create_index([("owner", 1), ("name", 1)])
    db.di_runs.create_index([("status", 1), ("scope", 1), ("queued_at", 1)])
    db.di_runs.create_index([("status", 1), ("lane", 1), ("queued_at", 1)])
    # Runs queued before lanes existed: a missing lane sorts BEFORE 0, so give them one.
    for run in db.di_runs.find({"status": "queued", "lane": {"$exists": False}}, {"task_id": 1}):
        task = db.di_tasks.find_one({"_id": run["task_id"]}, {"schedule": 1}) or {}
        db.di_runs.update_one({"_id": run["_id"]}, {"$set": {"lane": run_lane(task)}})
    db.di_runs.create_index([("task_id", 1), ("queued_at", -1)])
    db.di_runs.create_index("expire_at", expireAfterSeconds=0)
    db.di_task_versions.create_index([("task_id", 1), ("version", -1)])
    db.di_runner_keys.create_index("key_hash")


def start_delegation() -> None:
    """Start the scheduler loop. Idempotent (app_lifespan runs per client session)."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    from shared_memory.clients import get_mongo
    db = get_mongo()
    if db is not None:
        try:
            ensure_indexes(db)
        except Exception as e:
            log.error("delegation: index setup failed: %s", e)
    _loop_task = asyncio.create_task(_loop(), name="delegation_loop")
