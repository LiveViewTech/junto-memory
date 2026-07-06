"""Tests for header-based (Authorization: Bearer) auth — design:header-auth-v0.

Covers the pure header parser and the contextvar set/get/reset roundtrip.
The end-to-end fallback (memory_start_session pulling from the contextvar) is
exercised against the live server; here we lock down the parsing + plumbing
that the middleware depends on.
"""

import importlib

import shared_memory.auth as auth_mod

# ── parse_bearer_token ──

def test_parse_bearer_extracts_token():
    headers = [(b"authorization", b"Bearer smk_abc123")]
    assert auth_mod.parse_bearer_token(headers) == "smk_abc123"


def test_parse_bearer_case_insensitive_name_and_scheme():
    # ASGI lowercases header names, but be defensive; scheme casing varies.
    assert auth_mod.parse_bearer_token([(b"Authorization", b"bearer tok")]) == "tok"
    assert auth_mod.parse_bearer_token([(b"authorization", b"BEARER tok")]) == "tok"


def test_parse_bearer_strips_whitespace():
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer   tok  ")]) == "tok"


def test_parse_bearer_absent_returns_none():
    assert auth_mod.parse_bearer_token([(b"content-type", b"application/json")]) is None
    assert auth_mod.parse_bearer_token([]) is None
    assert auth_mod.parse_bearer_token(None) is None


def test_parse_bearer_non_bearer_scheme_returns_none():
    # A non-Bearer Authorization (e.g. Basic) must not be treated as a key.
    assert auth_mod.parse_bearer_token([(b"authorization", b"Basic dXNlcjpwdw==")]) is None


def test_parse_bearer_empty_token_returns_none():
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer ")]) is None
    assert auth_mod.parse_bearer_token([(b"authorization", b"Bearer")]) is None


# ── contextvar roundtrip ──

def test_header_api_key_default_is_none():
    assert auth_mod.get_header_api_key() is None


def test_header_api_key_set_get_reset():
    token = auth_mod.set_header_api_key("smk_xyz")
    try:
        assert auth_mod.get_header_api_key() == "smk_xyz"
    finally:
        auth_mod.reset_header_api_key(token)
    # After reset, back to the default — no leakage across requests.
    assert auth_mod.get_header_api_key() is None


# ── tunnel-origin detection (design:auth-origin-trust-v0) ──

def test_detect_tunnel_origin_true_on_cf_header():
    # cloudflared sets CF-Connecting-IP on every proxied request.
    assert auth_mod.detect_tunnel_origin([(b"cf-connecting-ip", b"203.0.113.7")]) is True


def test_detect_tunnel_origin_case_insensitive():
    assert auth_mod.detect_tunnel_origin([(b"CF-Connecting-IP", b"203.0.113.7")]) is True


def test_detect_tunnel_origin_false_for_lan_local():
    # A LAN/local direct hit carries no CF header → keyless allowed.
    assert auth_mod.detect_tunnel_origin([(b"host", b"192.168.15.240:8080")]) is False
    assert auth_mod.detect_tunnel_origin([]) is False
    assert auth_mod.detect_tunnel_origin(None) is False


def test_via_tunnel_set_get_reset():
    assert auth_mod.get_via_tunnel() is False  # default
    token = auth_mod.set_via_tunnel(True)
    try:
        assert auth_mod.get_via_tunnel() is True
    finally:
        auth_mod.reset_via_tunnel(token)
    # No leakage across requests.
    assert auth_mod.get_via_tunnel() is False


# ── REQUIRE_KEY flag (design:auth-origin-trust-v0) ──
# Rejects EVERY keyless session regardless of origin — the posture for a
# Tailscale-only server (no tunnel header) where all clients already hold keys.
# The flag is read at import, so we reload the module under a patched env and
# restore the default afterwards so other tests see the off (default) state.

def _reload_auth_with_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JUNTO_REQUIRE_KEY", raising=False)
    else:
        monkeypatch.setenv("JUNTO_REQUIRE_KEY", value)
    importlib.reload(auth_mod)


def test_require_key_defaults_off(monkeypatch):
    try:
        _reload_auth_with_env(monkeypatch, None)
        assert auth_mod.REQUIRE_KEY is False
    finally:
        monkeypatch.delenv("JUNTO_REQUIRE_KEY", raising=False)
        importlib.reload(auth_mod)


def test_require_key_truthy_values(monkeypatch):
    try:
        for v in ("true", "1", "yes", "TRUE", "Yes"):
            _reload_auth_with_env(monkeypatch, v)
            assert auth_mod.REQUIRE_KEY is True, f"{v!r} should enable REQUIRE_KEY"
    finally:
        monkeypatch.delenv("JUNTO_REQUIRE_KEY", raising=False)
        importlib.reload(auth_mod)


def test_require_key_falsey_values(monkeypatch):
    try:
        for v in ("false", "0", "no", ""):
            _reload_auth_with_env(monkeypatch, v)
            assert auth_mod.REQUIRE_KEY is False, f"{v!r} should leave REQUIRE_KEY off"
    finally:
        monkeypatch.delenv("JUNTO_REQUIRE_KEY", raising=False)
        importlib.reload(auth_mod)
