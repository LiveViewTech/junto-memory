"""Delegated-inference MCP tools (interface:delegated-inference-v0 §1).

Thin wrappers over shared_memory.delegation: identity + role come from the
session; every rule lives in delegation.py so the HTTP runner API and the
admin UI share it.
"""

import json
from typing import Any, Dict, List, Optional

from shared_memory import delegation as di
from shared_memory.app import mcp
from shared_memory.clients import get_mongo
from shared_memory.helpers import normalize_project, require_session
from shared_memory.state import active_sessions
from shared_memory.tools.projects import _is_project_admin

ADMIN_ROLES = {"admin", "owner"}


def _caller(session_id: str):
    """(error_json | None, who 'project/agent', is_admin, db)."""
    error = require_session(session_id)
    if error:
        return json.dumps({"error": error}), None, False, None
    info = active_sessions[session_id]
    who = di.ident(normalize_project(info.get("project", "")), info.get("claude_instance", ""))
    db = get_mongo()
    if db is None:
        return json.dumps({"error": "MongoDB unavailable"}), None, False, None
    return None, who, info.get("role") in ADMIN_ROLES, db


def _out(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


@mcp.tool()
async def memory_delegate(
    session_id: str,
    name: str,
    scope: str,
    instructions: str,
    output_schema: Dict[str, Any],
    source_params: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    routes: Optional[List[Dict[str, Any]]] = None,
    schedule: Optional[Dict[str, Any]] = None,
    canaries: Optional[List[Dict[str, Any]]] = None,
    per_item: bool = True,
    expect_items: bool = False,
    description: str = "",
    run_now: bool = False,
    tools: Optional[List[str]] = None,
    tool_scope: Optional[str] = None,
    max_tool_calls: int = 8,
    connection: Optional[Dict[str, Any]] = None,
) -> str:
    """Create a task for a LOCAL model worker (runs on Tom's hardware, not a cloud model).

    You own the task and can edit everything about it later with
    memory_schedule(action="edit"). Contract: interface:delegated-inference-v0.

    How it runs: a runner fetches items from the scope's data source using
    source_params, sends each item to the local model with your instructions,
    validates the reply against output_schema, and the server applies your
    routes to each result (notify an agent, put it in the daily digest, or drop
    it). No schedule = one-off: it runs once now and you get a notice when done.

    Args:
        session_id: Your session ID.
        name: Short task name, unique among your tasks.
        scope: A data scope granted to you (memory_runners(action="scopes") lists them).
        instructions: The prompt for the local model: what to decide/extract and how.
            Item content is appended after it as untrusted data.
        output_schema: JSON Schema (type=object) for ONE item's result. Use enums for
            anything you route on. Mark raw-content fields "x-sensitive": true.
        source_params: Adapter parameters (imap: folder, gm_raw, max_items, max_chars;
            file: glob; inline: items). See the spec §9.
        model: Ollama model on the runner (default gpt-oss:20b; qwen3:14b is faster but obeys injected text in plausible mail).
        routes: Ordered rules, first match wins, no match = digest:
            [{"if": {"field": "classification", "op": "in", "value": ["customer_issue"]},
              "then": "notify", "to": "nimbus/nimbusSupport", "category": "task",
              "template": "{item.subject} — {classification} ({confidence})"},
             {"if": {"field": "classification", "op": "in", "value": ["spam_scam"]}, "then": "drop"}]
            ops: eq ne in not_in lt lte gt gte exists true false. Fields: output_schema
            properties, or item.<metadata key> from the adapter.
        schedule: {"every": "15m"} or {"cron": "0 7 * * *"}, plus optional timezone
            (default America/Chicago), quiet_hours "01:00-05:00", digest_at "07:00",
            canary_every daily|run, pause_after_failures (default 3).
        canaries: Known items with expected results, re-checked daily:
            [{"source_params": {...locator...}, "expect": {"classification": "spam_scam"}}].
            Include at least one that should be NOTIFIED (a positive control).
        per_item: One model call per item (default True).
        expect_items: Fail a run that finds 0 items (sweeps). Leave False for polls.
        description: Free text for humans.
        run_now: For a scheduled task, also run once immediately.
        tools: READ tools the local model may call while working on each item, e.g.
            ["frame_lookup"]. Must be in the tool scope's allowed list
            (memory_runners(action="scopes") shows tools_allowed). Every call is logged.
        tool_scope: Scope that provides the tools, if different from `scope`.
        max_tool_calls: Per-item tool-call budget (1-20, default 8).
        connection: Request a NEW data connection named `scope` as part of this task:
            {"type": "imap"|"gmail"|"mcp"|"file"|"inline", "params": {...},
             "secret": "password"|"token"|"oauth"|"none", "tools_allowed": [...] (mcp), "notes": "..."}.
            imap params {host, port, user}; gmail {mailbox}; mcp {url}; file {roots}.
            You never handle the secret: Tom supplies it in /admin → Connections, and
            the task starts automatically once he does (status awaiting_connection until then).

    Returns: {task_id, version, task_status, run_id?} or {error, details}.
    """
    err, who, admin, db = _caller(session_id)
    if err:
        return err
    return _out(di.create_task(db, who, admin, {
        "name": name, "scope": scope, "instructions": instructions, "output_schema": output_schema,
        "source_params": source_params, "model": model, "routes": routes, "schedule": schedule,
        "canaries": canaries, "per_item": per_item, "expect_items": expect_items,
        "description": description, "run_now": run_now,
        "tools": tools, "tool_scope": tool_scope, "max_tool_calls": max_tool_calls,
        "connection": connection,
    }))


@mcp.tool()
async def memory_schedule(
    session_id: str,
    action: str,
    task_id: Optional[str] = None,
    changes: Optional[Dict[str, Any]] = None,
    limit: int = 20,
    all_tasks: bool = False,
) -> str:
    """Manage your local-model tasks (created with memory_delegate).

    Args:
        session_id: Your session ID.
        action: list | get | edit | pause | resume | run_now | delete | history | runs | approve
            - list: your tasks (all_tasks=True: admins see everyone's, project admins their project's)
            - get: full definition incl. instructions, schema, routes, cursor
            - edit: apply `changes` (any of instructions, output_schema, source_params, model,
              routes, schedule, canaries, description, per_item, expect_items, tools,
              tool_scope, max_tool_calls). Versioned;
              takes effect on the next run.
            - pause / resume / delete; run_now queues one run immediately
            - history: previous definition versions
            - runs: recent runs with evidence (items examined, routes, canary, cursor, errors)
            - approve: Tom/admin only, for scopes that require approval
        task_id: Required for everything except list.
        changes: For edit.
        limit: For history / runs.
        all_tasks: For list; admins (all) and project admins (own project, read-only) only.
    """
    err, who, admin, db = _caller(session_id)
    if err:
        return err
    # Project admins (projects.admins) READ their project's tasks; edits stay owner/admin.
    my_project, my_agent = who.split("/", 1)
    proj_admin = not admin and _is_project_admin(db, my_project, my_agent)
    if action == "list":
        out: Dict[str, Any] = {"tasks": di.list_tasks(db, who, admin, all_tasks, proj_admin)}
        if all_tasks and not (admin or proj_admin):
            out["all_tasks_ignored"] = "admins/project admins only; showing your own tasks"
        return _out(out)
    if not task_id:
        return _out({"error": f"task_id required for {action}"})
    task = di.get_task(db, task_id)
    if not task:
        return _out({"error": f"task {task_id} not found"})
    readable = admin or task["owner"] == who or (proj_admin and task["owner"].startswith(f"{my_project}/"))
    if action == "get":
        return _out(di.task_public(task) if readable else {"error": "not your task"})
    if action == "edit":
        return _out(di.edit_task(db, task_id, who, admin, changes or {}))
    if action in ("pause", "resume", "delete", "approve"):
        return _out(di.set_task_status(db, task_id, who, admin, action))
    if action == "run_now":
        if not (admin or task["owner"] == who):
            return _out({"error": "not your task"})
        if task["status"] not in ("active", "paused"):
            return _out({"error": f"task is {task['status']}"})
        return _out({"status": "queued", "run_id": di.queue_run(db, task, reason=f"run_now by {who}")})
    if action == "history":
        return _out({"versions": di.task_history(db, task_id, limit)} if readable else {"error": "not your task"})
    if action == "runs":
        return _out({"runs": di.list_runs(db, task_id, limit)} if readable else {"error": "not your task"})
    return _out({"error": "action: list|get|edit|pause|resume|run_now|delete|history|runs|approve"})


@mcp.tool()
async def memory_job(session_id: str, run_id: str, action: str = "status") -> str:
    """One run of a local-model task: status | result | cancel.

    result returns each item's metadata, model output, and the route it took.
    """
    err, who, admin, db = _caller(session_id)
    if err:
        return err
    if action == "cancel":
        return _out(di.cancel_run(db, run_id, who, admin))
    run = db.di_runs.find_one({"_id": run_id})
    if not run:
        return _out({"error": f"run {run_id} not found"})
    if not admin and run.get("owner") != who:
        return _out({"error": "not your run"})
    if action == "status":
        return _out(di.run_public(run))
    if action == "result":
        return _out(di.run_public(run, include_items=True))
    return _out({"error": "action: status|result|cancel"})


@mcp.tool()
async def memory_runners(
    session_id: str,
    action: str = "list",
    name: Optional[str] = None,
    fields: Optional[Dict[str, Any]] = None,
    runner_id: Optional[str] = None,
    host: Optional[str] = None,
) -> str:
    """Local inference runners and the data scopes they serve.

    Args:
        session_id: Your session ID.
        action:
            - list: runners (host, models, scopes served, last heartbeat)
            - scopes: connections/scopes you can build tasks on (admins: all), with status
            - request_connection: ask for a new connection: name + fields {type, params,
              secret, tools_allowed?, notes?} (see memory_delegate's connection arg). Tom
              activates it in /admin; you never see the secret.
            - define_scope: OWNER/ADMIN only. name + fields {adapter, adapter_config,
              runner_hosts_allowed, grants, content_visible_to, restricted, approval, notes}
            - create_runner_key: OWNER/ADMIN only. runner_id + host. Returns the key once.
    """
    err, who, admin, db = _caller(session_id)
    if err:
        return err
    if action == "list":
        rows = []
        for r in db.di_runners.find().sort("_id", 1):
            rows.append({"runner_id": r["_id"], "host": r.get("host"), "models": r.get("models"),
                         "scopes_served": r.get("scopes_served"), "version": r.get("version"),
                         "last_heartbeat": r.get("last_heartbeat")})
        return _out({"runners": rows})
    if action == "scopes":
        return _out({"scopes": di.scopes_for(db, who, admin)})
    if action == "request_connection":
        return _out(di.request_connection(db, who, name or "", fields or {}))
    if action == "define_scope":
        if not admin:
            return _out({"error": "define_scope needs an owner/admin role (scopes are Tom's)"})
        return _out(di.define_scope(db, who, name or "", fields or {}))
    if action == "create_runner_key":
        if not admin:
            return _out({"error": "create_runner_key needs an owner/admin role"})
        return _out(di.create_runner_key(db, who, runner_id or "", host or ""))
    return _out({"error": "action: list|scopes|request_connection|define_scope|create_runner_key"})
