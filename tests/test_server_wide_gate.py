"""Server-wide HTTP views need an unscoped key under JUNTO_REQUIRE_KEY (1.41.1).

interface:project-scope-enforcement-v0 R7: /admin reads, /dashboard/api/* and
the compact-log stats span every project, so a project-scoped key must not
read them. The HTML shells stay loadable (a browser navigation cannot send a
Bearer header); POST /hook/compact-log takes any valid key.
"""

import ast
from pathlib import Path

import pytest

import shared_memory.admin_ui as ui

SRC = Path(__file__).resolve().parent.parent / "src" / "shared_memory"

KEYS = {
    "owner": {"name": "tom", "role": "owner", "projects": []},
    "agent": {"name": "bot", "role": "agent", "projects": []},
    "scoped": {"name": "amy", "role": "agent", "projects": ["alpha"]},
}


class _Req:
    def __init__(self, headers):
        self.headers = type("H", (), {"raw": [(k.encode(), v.encode()) for k, v in headers.items()]})()


def _bearer(k):
    return _Req({"Authorization": f"Bearer {k}"})


@pytest.fixture
def require_key(monkeypatch):
    import shared_memory.auth as auth
    monkeypatch.setattr(auth, "REQUIRE_KEY", True)
    monkeypatch.setattr(auth, "validate_api_key", lambda k: KEYS.get(k))


@pytest.fixture
def open_server(monkeypatch):
    import shared_memory.auth as auth
    monkeypatch.setattr(auth, "REQUIRE_KEY", False)
    monkeypatch.setattr(auth, "validate_api_key", lambda k: KEYS.get(k))


# ── read gate (_lan_only) ──

def test_read_gate_rejects_missing_and_invalid_keys(require_key):
    assert ui._lan_only(_Req({})).status_code == 401
    assert ui._lan_only(_bearer("nope")).status_code == 401


def test_read_gate_rejects_scoped_key(require_key):
    r = ui._lan_only(_bearer("scoped"))
    assert r.status_code == 403
    assert b"requires an unscoped key" in r.body


def test_read_gate_allows_unscoped_keys_of_any_role(require_key):
    assert ui._lan_only(_bearer("owner")) is None
    assert ui._lan_only(_bearer("agent")) is None


def test_read_gate_unchanged_without_require_key(open_server):
    assert ui._lan_only(_Req({})) is None
    assert ui._lan_only(_bearer("scoped")) is None
    assert ui._lan_only(_Req({"CF-Connecting-IP": "1.2.3.4"})).status_code == 403


def test_read_gate_refuses_tunnel_even_with_owner_key(require_key):
    r = ui._lan_only(_Req({"Authorization": "Bearer owner", "CF-Connecting-IP": "1.2.3.4"}))
    assert r.status_code == 403


# ── page shell gate (_tunnel_only) ──

def test_page_shell_loads_without_key_under_require_key(require_key):
    assert ui._tunnel_only(_Req({})) is None
    assert ui._tunnel_only(_Req({"CF-Connecting-IP": "1.2.3.4"})).status_code == 403


# ── caller-owned write gate (_key_if_required) ──

def test_write_gate_takes_any_valid_key(require_key):
    assert ui._key_if_required(_Req({})).status_code == 401
    assert ui._key_if_required(_bearer("nope")).status_code == 401
    assert ui._key_if_required(_bearer("scoped")) is None
    assert ui._key_if_required(_bearer("owner")) is None


def test_write_gate_open_without_require_key(open_server):
    assert ui._key_if_required(_Req({})) is None


# ── wiring: every server-wide route calls its gate ──

def _route_gates(path):
    """{route_path: set(gate names called in its handler body)}."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "custom_route"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                calls = {c.func.id for c in ast.walk(node)
                         if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                         and c.func.id in ("_lan_only", "_tunnel_only", "_key_if_required", "_auth", "_write")}
                out[dec.args[0].value] = calls
    return out


def test_dashboard_and_compact_routes_are_gated():
    gates = _route_gates(SRC / "__main__.py")
    for route in ("/dashboard/api/sessions", "/dashboard/api/agents",
                  "/dashboard/api/messages", "/dashboard/api/backlog",
                  "/hook/compact-log/stats"):
        assert "_lan_only" in gates[route], route
    assert "_key_if_required" in gates["/hook/compact-log"]


def test_every_admin_api_route_is_gated():
    gates = _route_gates(SRC / "admin_ui.py")
    assert "_tunnel_only" in gates["/admin"]
    for route, calls in gates.items():
        if route.startswith("/admin/api/"):
            assert calls & {"_lan_only", "_auth", "_write"}, route  # _write = owner-key _auth
