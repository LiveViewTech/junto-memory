"""HTTP API for delegated-inference runners (interface:delegated-inference-v0 §7).

POST /runner/api/register   {models[], scopes_served[], version}
POST /runner/api/heartbeat  {run_id?}            extends the run's lease
POST /runner/api/claim      {}                   → {run, task, scope, cursor} | {}
POST /runner/api/complete   {run_id, items[], items_examined, source_evidence, cursor_out, model}
POST /runner/api/fail       {run_id, error}

Runners are service principals, not agents: they authenticate with a runner
key (Authorization: Bearer jr_...) from di_runner_keys, never start an MCP
session, and never appear in a roster. LAN-only: tunnel-origin requests are
refused, like /admin.
"""

import json
import logging

log = logging.getLogger(__name__)


def register_runner_routes(mcp, get_mongo) -> None:
    from starlette.responses import JSONResponse

    from shared_memory import delegation as di
    from shared_memory.auth import detect_tunnel_origin, parse_bearer_token

    async def _guard(request):
        headers = [(k.lower(), v) for k, v in request.headers.raw]
        if detect_tunnel_origin(headers):
            return None, None, None, JSONResponse({"error": "runner API is LAN-only"}, status_code=403)
        db = get_mongo()
        if db is None:
            return None, None, None, JSONResponse({"error": "MongoDB unavailable"}, status_code=503)
        key_doc = di.authenticate_runner(db, parse_bearer_token(headers))
        if not key_doc:
            return None, None, None, JSONResponse({"error": "invalid runner key"}, status_code=401)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return None, None, None, JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return None, None, None, JSONResponse({"error": "body must be an object"}, status_code=400)
        return db, key_doc, body, None

    def _resp(payload):
        return JSONResponse(json.loads(json.dumps(payload, default=str)),
                            status_code=400 if isinstance(payload, dict) and payload.get("error") else 200)

    @mcp.custom_route("/runner/api/register", methods=["POST"])
    async def runner_register(request):
        db, key_doc, body, err = await _guard(request)
        return err or _resp(di.register_runner(db, key_doc, body))

    @mcp.custom_route("/runner/api/heartbeat", methods=["POST"])
    async def runner_heartbeat(request):
        db, key_doc, body, err = await _guard(request)
        return err or _resp(di.heartbeat_runner(db, key_doc, body))

    @mcp.custom_route("/runner/api/claim", methods=["POST"])
    async def runner_claim(request):
        db, key_doc, body, err = await _guard(request)
        if err:
            return err
        db.di_runners.update_one({"_id": key_doc["_id"]}, {"$set": {"last_heartbeat": di._now()}})
        return _resp(di.claim_run(db, key_doc))

    @mcp.custom_route("/runner/api/complete", methods=["POST"])
    async def runner_complete(request):
        db, key_doc, body, err = await _guard(request)
        if err:
            return err
        result, notices = di.complete_run(db, key_doc, body)
        if notices:
            try:
                await di.send_notices(db, notices)
            except Exception as e:
                log.error("runner_api: notices failed for %s: %s", body.get("run_id"), e)
        return _resp(result)

    @mcp.custom_route("/runner/api/fail", methods=["POST"])
    async def runner_fail(request):
        db, key_doc, body, err = await _guard(request)
        if err:
            return err
        result, notices = di.fail_run(db, key_doc, body)
        if notices:
            await di.send_notices(db, notices)
        return _resp(result)
