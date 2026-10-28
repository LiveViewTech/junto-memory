"""The `project` field on learning read surfaces (coordinator@nimbus 2026-08-10,
msg_b91f7992752a).

MEASURED DEFECT: `memory_record_learning` never wrote a `project` key into
Chroma metadata — `memory_store` did, `memory_record_learning` did not. Corpus
sweep on 2026-08-10: **4374 of 4397 learnings (99.5%) had no key**, across every
project, since the tool shipped. Routing was NEVER affected (it goes by
collection, and the collection was always right); the READ SURFACE was — an
agent calling get_by_id on a nimbus learning saw `project: ""` and could
reasonably conclude the doc was unscoped.

Pins:
- the collection is the authority when the metadata copy is missing;
- shared collections still report "" (they genuinely have no project);
- an explicit metadata value is never overridden by the derived one;
- record_learning now writes the key going forward.
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.helpers import project_from_collection
from shared_memory.state import active_sessions
from shared_memory.tools import query

from .test_get_by_id_surface import FakeCollection, FakeDb, _install_session, _patch_chroma  # noqa: F401


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_sessions():
    active_sessions.clear()
    yield
    active_sessions.clear()


LEGACY_ID = "learning_aaaabbbbccccdddd"


def _legacy_meta(**over):
    """A learning as memory_record_learning actually wrote it: no project key."""
    meta = {"title": "Legacy learning", "type": "learning", "status": "active",
            "tags": "[]", "created": "2026-08-01", "updated": "2026-08-01",
            "claude_instance": "wordpress-team"}
    meta.update(over)
    return meta


def test_project_from_collection_derives_scope():
    assert project_from_collection("proj_nimbus") == "nimbus"
    assert project_from_collection("proj_junto") == "junto"


def test_project_from_collection_returns_empty_for_shared():
    """Shared docs have no project BY CONSTRUCTION — "" is the right answer."""
    assert project_from_collection("shared_patterns") == ""
    assert project_from_collection("") == ""


@pytest.mark.asyncio
async def test_get_by_id_falls_back_to_collection(monkeypatch):
    """The 4374-doc repair: no migration, the read surface derives it."""
    _install_session()
    _patch_chroma(monkeypatch, [FakeCollection("proj_nimbus", {
        LEGACY_ID: (_legacy_meta(), "body"),
    })])
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())
    import shared_memory.facets as facets_mod
    monkeypatch.setattr(facets_mod, "get_facets_for_ids", lambda db, ids: {})

    out = json.loads(await query.memory_get_by_id("sess_test", LEGACY_ID))
    assert out["found"] is True
    assert out["project"] == "nimbus", "collection is authoritative when meta is absent"


@pytest.mark.asyncio
async def test_shared_doc_still_reports_no_project(monkeypatch):
    _install_session()
    _patch_chroma(monkeypatch, [FakeCollection("shared_patterns", {
        LEGACY_ID: (_legacy_meta(), "body"),
    })])
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())
    import shared_memory.facets as facets_mod
    monkeypatch.setattr(facets_mod, "get_facets_for_ids", lambda db, ids: {})

    out = json.loads(await query.memory_get_by_id("sess_test", LEGACY_ID))
    assert out["project"] == ""


@pytest.mark.asyncio
async def test_explicit_metadata_wins_over_derivation(monkeypatch):
    """Never let the derived value silently overwrite a recorded one."""
    _install_session()
    _patch_chroma(monkeypatch, [FakeCollection("proj_nimbus", {
        LEGACY_ID: (_legacy_meta(project="claude_terminal"), "body"),
    })])
    monkeypatch.setattr(query, "get_mongo", lambda: FakeDb())
    import shared_memory.facets as facets_mod
    monkeypatch.setattr(facets_mod, "get_facets_for_ids", lambda db, ids: {})

    out = json.loads(await query.memory_get_by_id("sess_test", LEGACY_ID))
    assert out["project"] == "claude_terminal"


def test_record_learning_writes_the_key():
    """Forward fix — guard against the key being dropped again."""
    import inspect

    from shared_memory.tools import storage

    src = inspect.getsource(storage.memory_record_learning)
    body = src.split('"type": "learning"', 1)[1]
    assert '"project": project or ""' in body, (
        "memory_record_learning must write the project key into metadata"
    )
