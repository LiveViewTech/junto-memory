"""memory_db query auditing (Tom-directed 2026-08-09).

WHY THIS EXISTS, since it is not the obvious rationale: the read-only flag and
the SELECT-only guard do NOT stop a determined agent — both are one env var or
one direct mysql client away, and an agent that has convinced itself an action
is necessary will find the path. What cannot be reasoned around is a record
made after the fact. Detection is the control that survives motivated
reasoning; the guards only make the unsafe path require a decision.

Pins: every query is logged with verbatim SQL and actor; a mutating statement
against a write-capable handle is logged as `db.write` at WARNING severity so
it is greppable in one query; a rejected write is logged too (an attempt is
evidence); and an audit failure must NEVER block the query.
"""

import json

import pytest

from shared_memory.state import active_sessions
from shared_memory.tools import database as db_mod


@pytest.fixture
def audit_sink(monkeypatch):
    """Capture log_audit calls made by the tool."""
    events = []

    def _fake(event_type, actor, project="", details=None, session_id=""):
        events.append({
            "event_type": event_type, "actor": actor, "project": project,
            "details": details or {}, "session_id": session_id,
        })

    import shared_memory.audit as audit_mod
    monkeypatch.setattr(audit_mod, "log_audit", _fake)
    return events


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setitem(db_mod.DB_REGISTRY, "stg_ro", {
        "type": "mysql", "host": "192.168.15.75", "port": 3306,
        "database": "picFrame", "user": "junto_staging_ro", "password": "x",
        "read_only": True, "query_timeout": 30, "max_rows": 1000,
    })
    monkeypatch.setitem(db_mod.DB_REGISTRY, "stg_rw", {
        "type": "mysql", "host": "192.168.15.75", "port": 3306,
        "database": "picFrame", "user": "junto_staging_rw", "password": "x",
        "read_only": False, "query_timeout": 30, "max_rows": 1000,
    })


@pytest.fixture
def session():
    active_sessions["s-audit"] = {
        "claude_instance": "legacy-team", "project": "nimbus",
    }
    yield "s-audit"
    active_sessions.pop("s-audit", None)


def _no_connection(monkeypatch):
    """Stop before real I/O — auditing happens before the connection."""
    monkeypatch.setattr(db_mod, "_get_db_connection",
                        lambda name: (None, "connection stubbed"))


@pytest.mark.asyncio
async def test_write_against_write_handle_is_flagged(
    audit_sink, registry, session, monkeypatch
):
    _no_connection(monkeypatch)
    await db_mod.memory_db(
        session_id=session, action="query", database="stg_rw",
        query="UPDATE betaagreements SET id=id WHERE 1=0",
    )
    ev = next(e for e in audit_sink if e["event_type"] == "db.write")
    assert ev["actor"] == "legacy-team"
    assert ev["details"]["mutating"] is True
    assert ev["details"]["read_only_handle"] is False
    assert ev["details"]["severity"] == "WARNING"
    assert ev["details"]["verb"] == "UPDATE"
    assert "betaagreements" in ev["details"]["sql"], "verbatim SQL must be kept"


@pytest.mark.asyncio
async def test_select_is_logged_as_ordinary_query(
    audit_sink, registry, session, monkeypatch
):
    _no_connection(monkeypatch)
    await db_mod.memory_db(
        session_id=session, action="query", database="stg_ro",
        query="SELECT 1",
    )
    ev = next(e for e in audit_sink if e["event_type"] == "db.query")
    assert ev["details"]["mutating"] is False
    assert ev["details"]["severity"] == "info"
    assert ev["details"]["read_only_handle"] is True


@pytest.mark.asyncio
async def test_rejected_write_is_still_audited(
    audit_sink, registry, session, monkeypatch
):
    """An ATTEMPT is evidence — the block must not erase the record."""
    _no_connection(monkeypatch)
    out = json.loads(await db_mod.memory_db(
        session_id=session, action="query", database="stg_ro",
        query="DELETE FROM betaagreements",
    ))
    assert "error" in out
    kinds = [e["event_type"] for e in audit_sink]
    assert "db.query_rejected" in kinds
    rej = next(e for e in audit_sink if e["event_type"] == "db.query_rejected")
    assert rej["details"]["verb"] == "DELETE"
    assert "DELETE FROM betaagreements" in rej["details"]["sql"]


@pytest.mark.asyncio
async def test_audit_failure_never_blocks_the_query(
    registry, session, monkeypatch
):
    """Fail-quiet contract: telemetry must not become an availability risk."""
    import shared_memory.audit as audit_mod

    def _boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(audit_mod, "log_audit", _boom)
    _no_connection(monkeypatch)
    out = json.loads(await db_mod.memory_db(
        session_id=session, action="query", database="stg_ro", query="SELECT 1",
    ))
    # Reaches the connection step (our stub) rather than dying in the audit.
    assert out.get("error") == "connection stubbed"
