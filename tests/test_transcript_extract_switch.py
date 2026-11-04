"""Transcript-extraction off switch (query_config `transcript_extract`, Tom 2026-11-03).

Pins:
- the flag exists, is a bool and defaults OFF;
- memory_record_learning rejects a `transcript-extract`-tagged write when the
  effective flag for the target project is off, before any storage work;
- an untagged write is never gated;
- with the flag on, the gate lets the write through;
- memory_health reports features.transcript_extract from the server default.
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory import query_config
from shared_memory.state import active_sessions
from shared_memory.tools import diagnostics, storage


class _PastGate(Exception):
    """Raised by the patched get_chroma: proves the write got past the gate."""


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    active_sessions.clear()
    active_sessions["sess_te"] = {
        "claude_instance": "extractor", "project": "junto", "role": "agent",
        "allowed_projects": ["junto"], "started_at": "2026-11-03T00:00:00+00:00",
    }

    async def _chroma():
        raise _PastGate()

    monkeypatch.setattr(storage, "get_chroma", _chroma)
    monkeypatch.setattr(storage, "get_mongo", lambda: None)
    monkeypatch.setattr(diagnostics, "get_mongo", lambda: None)
    yield
    active_sessions.clear()


def _flag(monkeypatch, value):
    def _cfg(db, project=None):
        cfg = query_config._default_config_dict()
        cfg["transcript_extract"] = value
        return cfg
    monkeypatch.setattr(query_config, "get_effective_config", _cfg)


def test_flag_registered_and_defaults_off():
    assert query_config.CONFIG_KEYS["transcript_extract"] is bool
    assert query_config._default_config_dict()["transcript_extract"] is False
    assert query_config.get_effective_config(None, None)["transcript_extract"] is False


@pytest.mark.asyncio
async def test_tagged_write_rejected_when_off(monkeypatch):
    _flag(monkeypatch, False)
    out = json.loads(await storage.memory_record_learning(
        "sess_te", "t", "d", project="ha", tags=["transcript-extract", "kind:decision"]))
    assert out["code"] == "transcript_extract_disabled"
    assert "'ha'" in out["error"]


@pytest.mark.asyncio
async def test_untagged_write_not_gated(monkeypatch):
    _flag(monkeypatch, False)
    with pytest.raises(_PastGate):
        await storage.memory_record_learning("sess_te", "t", "d", project="ha", tags=["other"])


@pytest.mark.asyncio
async def test_tagged_write_allowed_when_on(monkeypatch):
    _flag(monkeypatch, True)
    with pytest.raises(_PastGate):
        await storage.memory_record_learning(
            "sess_te", "t", "d", project="ha", tags=["transcript-extract"])


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_health_reports_feature(monkeypatch, value):
    _flag(monkeypatch, value)
    out = json.loads(await diagnostics.memory_health(include_storage=False))
    assert out["features"] == {"transcript_extract": value}
