"""Tenant isolation for project-scoped API keys
(interface:project-scope-enforcement-v0, backlog_8cd9708f4ad2).

A key scoped to [alpha] must not reach project beta: explicit project args
are rejected at the MCP layer (R1), and "all projects" listings / doc-id
lookups only see alpha + shared (R3-R5). Unscoped sessions, keyless
soft-auth sessions and auth-disabled servers are unchanged.
"""

import json
from types import SimpleNamespace

import pytest
from mcp import types

import shared_memory.auth as auth
from shared_memory.project_scope import build_call_tool_handler_with_project_scope
from shared_memory.state import active_sessions, file_locks

SCOPED = {"claude_instance": "amy", "project": "alpha", "role": "agent",
          "allowed_projects": ["alpha"], "last_activity": "2099-01-01T00:00:00+00:00",
          "task": "", "started": "2099-01-01T00:00:00+00:00"}
OPEN = {"claude_instance": "owner", "project": "alpha", "role": "owner",
        "allowed_projects": [], "last_activity": "2099-01-01T00:00:00+00:00",
        "task": "", "started": "2099-01-01T00:00:00+00:00"}


@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    active_sessions["s_scoped"] = dict(SCOPED)
    active_sessions["s_open"] = dict(OPEN)
    active_sessions["s_beta"] = dict(SCOPED, claude_instance="bob", project="beta",
                                     allowed_projects=["beta"])
    yield
    for k in ("s_scoped", "s_open", "s_beta"):
        active_sessions.pop(k, None)


# ── helpers ──────────────────────────────────────────────────────────────

def test_helpers(sessions, monkeypatch):
    s = active_sessions["s_scoped"]
    assert auth.scoped_projects(s) == ["alpha"]
    assert auth.scoped_projects(active_sessions["s_open"]) is None
    assert auth.project_allowed(s, "Alpha") and auth.project_allowed(s, None)
    assert not auth.project_allowed(s, "beta")
    assert auth.collection_allowed(s, "proj_alpha") and auth.collection_allowed(s, "shared_context")
    assert not auth.collection_allowed(s, "proj_beta")
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert auth.scoped_projects(s) is None and auth.project_allowed(s, "beta")


# ── R1 wrapper ───────────────────────────────────────────────────────────

def _req(name, args):
    return SimpleNamespace(params=SimpleNamespace(name=name, arguments=args))


async def _call(name, args):
    seen = {}

    async def inner(req):
        seen["called"] = True
        return "ok"
    out = await build_call_tool_handler_with_project_scope(inner)(_req(name, args))
    return out, seen.get("called", False)


def _is_rejection(out):
    root = getattr(out, "root", None)
    return (isinstance(root, types.CallToolResult) and root.isError
            and root.content[0].text.startswith("ERROR: Tenant isolation"))


async def test_wrapper_allows_own_project(sessions):
    out, called = await _call("memory_query", {"session_id": "s_scoped", "project": "ALPHA"})
    assert called and out == "ok"


async def test_wrapper_rejects_other_project(sessions):
    out, called = await _call("memory_get_spec", {"session_id": "s_scoped", "project": "beta"})
    assert not called and _is_rejection(out)
    assert "'beta'" in out.root.content[0].text


async def test_wrapper_rejects_projects_list(sessions):
    out, called = await _call("memory_sync_pull", {"session_id": "s_scoped", "projects": ["alpha", "beta"]})
    assert not called and _is_rejection(out)


async def test_send_message_cross_project_is_exempt(sessions):
    out, called = await _call("memory_send_message",
                              {"session_id": "s_scoped", "to_project": "beta", "project": "beta"})
    assert called


async def test_unscoped_and_missing_session_pass_through(sessions):
    assert (await _call("memory_query", {"session_id": "s_open", "project": "beta"}))[1]
    assert (await _call("memory_query", {"session_id": "nope", "project": "beta"}))[1]


async def test_auth_disabled_passes_through(sessions, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    assert (await _call("memory_query", {"session_id": "s_scoped", "project": "beta"}))[1]


# ── Chroma fakes for R3/R4/R5 ────────────────────────────────────────────

class _Col:
    def __init__(self, name, docs):
        self.name = name
        self.docs = docs  # id -> meta
        self.updates = []

    async def get(self, ids=None, where=None, include=None):
        keys = [i for i in (ids if ids is not None else self.docs) if i in self.docs]
        if where and "type" in where and isinstance(where["type"], str):
            keys = [k for k in keys if self.docs[k].get("type") == where["type"]]
        if where and where.get("status") == "archived":
            keys = [k for k in keys if self.docs[k].get("status") == "archived"]
        return {"ids": keys, "metadatas": [dict(self.docs[k]) for k in keys],
                "documents": [f"# {self.docs[k].get('title', k)}\n\nbody" for k in keys]}

    async def query(self, query_texts=None, n_results=5, where=None, **kw):
        keys = list(self.docs)[:n_results]
        return {"ids": [keys], "documents": [["body"] * len(keys)],
                "metadatas": [[dict(self.docs[k]) for k in keys]], "distances": [[0.1] * len(keys)]}

    async def update(self, ids, metadatas=None, documents=None):
        self.updates.append(ids)
        for i, m in zip(ids, metadatas or []):
            self.docs[i] = m

    async def count(self):
        return len(self.docs)


def _cols():
    return [
        _Col("proj_alpha", {"backlog_a": {"type": "backlog", "title": "A", "project": "alpha", "tags": "[\"t\"]",
                                          "status": "active"},
                            "spec_a": {"type": "spec", "status": "active", "spec_name": "s:a", "project": "alpha"}}),
        _Col("proj_beta", {"backlog_b": {"type": "backlog", "title": "B", "project": "beta", "tags": "[\"t\"]",
                                         "status": "active"},
                           "spec_b": {"type": "spec", "status": "active", "spec_name": "s:b", "project": "beta"}}),
        _Col("shared_patterns", {"spec_s": {"type": "spec", "status": "active", "spec_name": "s:shared"}}),
        _Col("shared_context", {"backlog_s": {"type": "backlog", "title": "S", "project": "", "tags": "[\"t\"]",
                                              "status": "active"}}),
    ]


@pytest.fixture
def chroma(monkeypatch):
    cols = _cols()
    by_name = {c.name: c for c in cols}

    class _Chroma:
        async def list_collections(self):
            return cols

    async def get_chroma():
        return _Chroma()

    async def get_project_collection(_c, project):
        return by_name.setdefault(f"proj_{project}", _Col(f"proj_{project}", {}))

    async def get_shared_collection(_c, name):
        return by_name.setdefault(f"shared_{name}", _Col(f"shared_{name}", {}))

    from shared_memory.tools import backlog, functions, lifecycle, query, search, specs
    for mod in (backlog, query, search, specs, lifecycle, functions):
        monkeypatch.setattr(mod, "get_chroma", get_chroma)
        for fn, impl in (("get_project_collection", get_project_collection),
                         ("get_shared_collection", get_shared_collection)):
            if hasattr(mod, fn):
                monkeypatch.setattr(mod, fn, impl)
    monkeypatch.setattr(backlog, "get_mongo", lambda: None)
    monkeypatch.setattr(backlog, "emit_op_log_from_context", lambda **k: None)

    async def _no_emb(*a, **k):
        return None
    monkeypatch.setattr(backlog, "fetch_embedding_for_op_log", _no_emb)
    monkeypatch.setattr(lifecycle, "get_mongo", lambda: None)
    monkeypatch.setattr(lifecycle, "emit_op_log_from_context", lambda **k: None)
    return by_name


# ── R3: omitted project = all projects ───────────────────────────────────

async def test_list_backlog_scoped_sees_own_and_shared(sessions, chroma):
    from shared_memory.tools import backlog
    ids = {i["id"] for i in json.loads(await backlog.memory_list_backlog("s_scoped", limit=0))["items"]}
    assert ids == {"backlog_a", "backlog_s"}
    ids = {i["id"] for i in json.loads(await backlog.memory_list_backlog("s_open", limit=0))["items"]}
    assert ids == {"backlog_a", "backlog_b", "backlog_s"}


async def test_list_specs_scoped(sessions, chroma):
    from shared_memory.tools import specs
    names = {s["name"] for s in json.loads(await specs.memory_list_specs("s_scoped"))["specs"]}
    assert names == {"s:a", "s:shared"}


async def test_archive_by_tag_skips_other_projects(sessions, chroma):
    from shared_memory.tools import lifecycle
    await lifecycle.memory_archive_by_tag("s_scoped", tag="t", reason="r")
    assert chroma["proj_beta"].docs["backlog_b"]["status"] == "active"
    assert chroma["proj_alpha"].docs["backlog_a"]["status"] == "archived"


# ── R4: doc-id addressed ─────────────────────────────────────────────────

async def test_get_by_id_other_project_reads_not_found(sessions, chroma):
    from shared_memory.tools import query
    assert json.loads(await query.memory_get_by_id("s_scoped", "backlog_b"))["found"] is False
    assert json.loads(await query.memory_get_by_id("s_scoped", "backlog_a"))["found"] is True
    assert json.loads(await query.memory_get_by_id("s_open", "backlog_b"))["found"] is True


async def test_backlog_by_id_writes_cannot_touch_other_project(sessions, chroma):
    from shared_memory.tools import backlog
    out = await backlog.memory_update_backlog_item("s_scoped", "backlog_b", status="done")
    assert "not found" in out.lower()
    out = await backlog.memory_complete_backlog_item("s_scoped", "backlog_b")
    assert "not found" in out.lower()
    assert chroma["proj_beta"].updates == []
    out = json.loads(await backlog.memory_batch_backlog(
        "s_scoped", action="create", items=[{"title": "x", "project": "beta"}]))
    assert out["failed"] == 1 and "Tenant isolation" in out["errors"][0]["error"]


async def test_change_status_never_reveals_other_project(sessions, chroma):
    from shared_memory.tools import lifecycle
    out = json.loads(await lifecycle.memory_change_status("s_scoped", "spec_b", "deprecated"))
    assert "found_in" not in out


async def test_skill_by_id_other_project_not_found(sessions):
    from shared_memory.tools import skills

    class _Skills:
        def find_one(self, q):
            return {"_id": "skill_x", "project": "beta", "name": "x"}
    assert skills._resolve(_Skills(), "s_scoped", "skill_x", None) is None
    assert skills._resolve(_Skills(), "s_open", "skill_x", None) is not None


# ── R5: cross-project listings ───────────────────────────────────────────

async def test_search_global_scoped(sessions, chroma):
    from shared_memory.tools import search
    cols = {r["collection"] for r in json.loads(await search.memory_search_global("s_scoped", "q"))["results"]}
    assert "proj_beta" not in cols and "proj_alpha" in cols


async def test_list_projects_uses_bearer_scope(sessions, chroma, monkeypatch):
    from shared_memory.tools import search
    monkeypatch.setattr(auth, "get_header_api_key", lambda: "k")
    monkeypatch.setattr(auth, "validate_api_key", lambda k: {"projects": ["alpha"]})
    names = {p["project"] for p in json.loads(await search.memory_list_projects())["projects"]}
    assert names == {"alpha"}
    monkeypatch.setattr(auth, "get_header_api_key", lambda: None)
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    assert json.loads(await search.memory_list_projects())["projects"] == []
    monkeypatch.setattr(auth, "REQUIRE_KEY", False)
    assert len(json.loads(await search.memory_list_projects())["projects"]) == 2


async def test_get_active_work_scoped(sessions, chroma):
    from shared_memory.tools import query
    out = json.loads(await query.memory_get_active_work("s_scoped"))
    assert {w["project"] for w in out["currently_active"]} <= {"alpha"}
    out = json.loads(await query.memory_get_active_work("s_open"))
    assert "beta" in {w["project"] for w in out["currently_active"]}


class _Cursor(list):
    def sort(self, *a, **k):
        return self


class _Find:
    def __init__(self, rows):
        self.rows = rows

    def find(self, q=None, proj=None):
        return _Cursor(r for r in self.rows if all(r.get(k) == v for k, v in (q or {}).items()
                                                   if not isinstance(v, dict)))

    def distinct(self, field, q):
        allowed = q["project"]["$in"]
        return [r[field] for r in self.rows if r.get("project") in allowed]


async def test_list_agents_and_status_scoped(sessions, monkeypatch):
    from shared_memory.tools import messaging
    rows = [{"instance": "amy", "project": "alpha"}, {"instance": "bob", "project": "beta"}]
    db = SimpleNamespace(agent_directory=_Find(rows), registered_agents=_Find([]),
                         agent_status=_Find([{"instance": "amy"}, {"instance": "bob"}]))
    monkeypatch.setattr(messaging, "get_mongo", lambda: db)
    out = json.loads(await messaging.memory_list_agents("s_scoped"))
    assert {a["instance"] for a in out["agents"]} == {"amy"}
    out = json.loads(await messaging.memory_get_agent_status("s_scoped"))
    assert {a["instance"] for a in out["agents"]} == {"amy"}
    out = json.loads(await messaging.memory_get_agent_status("s_open"))
    assert {a["instance"] for a in out["agents"]} == {"amy", "bob"}


async def test_list_alerts_scoped(sessions, monkeypatch):
    from shared_memory.tools import push_control_api as pca
    monkeypatch.setattr(pca, "get_mongo", lambda: object())
    monkeypatch.setattr(pca.push_control, "list_alerts", lambda **k: [
        {"agent_project": "alpha"}, {"agent_project": "beta"}])
    out = json.loads(await pca.memory_list_alerts("s_scoped"))
    assert [a["agent_project"] for a in out["alerts"]] == ["alpha"]


async def test_get_locks_scoped(sessions):
    from shared_memory.tools import locking
    file_locks["/a"] = {"session_id": "s_open", "claude_instance": "owner", "locked_at": "2099-01-01T00:00:00+00:00"}
    file_locks["/b"] = {"session_id": "s_beta", "claude_instance": "bob", "locked_at": "2099-01-01T00:00:00+00:00"}
    try:
        out = json.loads(await locking.memory_get_locks("s_scoped"))
        assert {lk["file"] for lk in out["locks"]} == {"/a"}
    finally:
        file_locks.pop("/a", None)
        file_locks.pop("/b", None)


def test_list_projects_still_registered_as_a_tool():
    # Regression: the _caller_scope helper was once inserted between
    # @mcp.tool() and memory_list_projects, registering the helper as a tool
    # and dropping memory_list_projects. Every registered tool is memory_*.
    import shared_memory.tools  # noqa: F401
    from shared_memory.app import mcp
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "memory_list_projects" in names
    assert not [n for n in names if not n.startswith("memory_")]
