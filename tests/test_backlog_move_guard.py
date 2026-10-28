"""Backlog move guard — the same-project 'move' data-loss defect.

REGRESSION COVER for a silent destroyer (reported 2026-08-07, pipeline;
fixed 316b351; 14 items destroyed within op-log coverage, honest total
14-plus-an-unknown since the defect predates the instrument).

Mechanism: update_backlog_item(project=<the item's CURRENT project>) took the
move branch, resolved the target collection to the SAME collection, called
add() with an existing id (chroma silently no-ops), then delete()d the source —
destroying the only copy — and returned status:"moved" with the item's own
metadata echoed back, the most reassuring possible response to a data loss.

Why it caught people: passing project= alongside an id READS as scoping, and
IS harmless scoping on the sibling read tools (get_by_id, get_spec). The
parameter name meaning "scope" on the read tools must never mean "mutate" on
the write tool beside them.

These tests pin the three guarantees of the fix:
  1. same-project is IDENTITY — update in place, no delete, status "updated";
  2. a real cross-project move only deletes the source AFTER verifying the
     item landed (a half-failed move leaves the original intact);
  3. "moved" is reported only when something actually moved.
"""

import json

import pytest

from shared_memory.state import active_sessions
from shared_memory.tools import backlog as backlog_mod


class _FakeCollection:
    """Chroma-shaped collection. add() mimics the real silent no-op on an
    existing id — the behaviour that made the delete destructive."""

    def __init__(self, name, docs=None):
        self.name = name
        self.docs = dict(docs or {})  # id -> (document, metadata)
        self.deleted = []
        self.updated = []
        self.added = []

    async def get(self, ids=None, where=None, include=None, **kw):
        if ids is not None:
            found = [i for i in ids if i in self.docs]
            return {
                "ids": found,
                "documents": [self.docs[i][0] for i in found],
                "metadatas": [self.docs[i][1] for i in found],
            }
        return {
            "ids": list(self.docs),
            "documents": [d for d, _ in self.docs.values()],
            "metadatas": [m for _, m in self.docs.values()],
        }

    async def add(self, ids=None, documents=None, metadatas=None):
        for idx, i in enumerate(ids or []):
            self.added.append(i)
            if i in self.docs:
                continue  # chroma's silent no-op on an existing id
            self.docs[i] = (documents[idx], metadatas[idx])

    async def update(self, ids=None, documents=None, metadatas=None):
        for idx, i in enumerate(ids or []):
            self.updated.append(i)
            if i in self.docs:
                doc = documents[idx] if documents else self.docs[i][0]
                self.docs[i] = (doc, metadatas[idx])

    async def delete(self, ids=None):
        for i in ids or []:
            self.deleted.append(i)
            self.docs.pop(i, None)


ITEM_ID = "backlog_1c5237bc6566"


def _seed(collections, monkeypatch, session_id="s1"):
    """Wire the module's chroma/session/op-log surfaces to fakes."""
    active_sessions[session_id] = {
        "claude_instance": "pipeline",
        "project": "nimbus",
    }

    class _Chroma:
        async def list_collections(self):
            return list(collections.values())

    async def _get_chroma():
        return _Chroma()

    async def _get_project_collection(_chroma, project):
        name = f"proj_{project}"
        collections.setdefault(name, _FakeCollection(name))
        return collections[name]

    async def _get_shared_collection(_chroma, kind):
        name = f"shared_{kind}"
        collections.setdefault(name, _FakeCollection(name))
        return collections[name]

    monkeypatch.setattr(backlog_mod, "get_chroma", _get_chroma)
    monkeypatch.setattr(backlog_mod, "get_project_collection", _get_project_collection)
    monkeypatch.setattr(backlog_mod, "get_shared_collection", _get_shared_collection)
    monkeypatch.setattr(backlog_mod, "get_mongo", lambda: None)
    monkeypatch.setattr(backlog_mod, "emit_op_log_from_context", lambda **kw: None)

    async def _fetch_embedding(*a, **k):
        return None

    monkeypatch.setattr(backlog_mod, "fetch_embedding_for_op_log", _fetch_embedding)
    return session_id


def _item(project="nimbus"):
    return {
        ITEM_ID: (
            "# VACUOUS-PASS AUDIT\n\noriginal body",
            {
                "title": "VACUOUS-PASS AUDIT",
                "type": "backlog",
                "backlog_status": "open",
                "priority": "high",
                "project": project,
                "assigned_to": "pipeline",
                "tags": "[]",
                "created": "2026-08-06T00:00:00+00:00",
                "updated": "2026-08-06T00:00:00+00:00",
            },
        )
    }


@pytest.mark.asyncio
async def test_same_project_update_does_not_destroy_the_item(monkeypatch):
    """THE regression: project=<current> must not delete the item."""
    nimbus = _FakeCollection("proj_nimbus", _item())
    collections = {"proj_nimbus": nimbus}
    sid = _seed(collections, monkeypatch)

    result = json.loads(
        await backlog_mod.memory_update_backlog_item(
            session_id=sid,
            item_id=ITEM_ID,
            project="nimbus",  # the destroying call shape
            description="updated body",
        )
    )

    assert ITEM_ID in nimbus.docs, "item was DESTROYED by a same-project update"
    assert nimbus.deleted == [], "same-project update must never delete"
    assert result["status"] == "updated", "must not claim 'moved' when nothing moved"
    assert result["id"] == ITEM_ID
    assert "updated body" in nimbus.docs[ITEM_ID][0]


@pytest.mark.asyncio
async def test_plain_update_without_project_still_works(monkeypatch):
    nimbus = _FakeCollection("proj_nimbus", _item())
    sid = _seed({"proj_nimbus": nimbus}, monkeypatch)

    result = json.loads(
        await backlog_mod.memory_update_backlog_item(
            session_id=sid, item_id=ITEM_ID, priority="critical"
        )
    )
    assert result["status"] == "updated"
    assert ITEM_ID in nimbus.docs
    assert nimbus.docs[ITEM_ID][1]["priority"] == "critical"


@pytest.mark.asyncio
async def test_real_cross_project_move_relocates_and_reports_moved(monkeypatch):
    nimbus = _FakeCollection("proj_nimbus", _item())
    junto = _FakeCollection("proj_junto")
    sid = _seed({"proj_nimbus": nimbus, "proj_junto": junto}, monkeypatch)

    result = json.loads(
        await backlog_mod.memory_update_backlog_item(
            session_id=sid, item_id=ITEM_ID, project="junto"
        )
    )

    assert result["status"] == "moved"
    assert ITEM_ID in junto.docs, "item must land in the destination"
    assert ITEM_ID not in nimbus.docs, "source row is removed on a real move"
    assert junto.docs[ITEM_ID][1]["project"] == "junto"


@pytest.mark.asyncio
async def test_failed_landing_leaves_the_source_intact(monkeypatch):
    """A move that doesn't land must NOT delete the original."""
    nimbus = _FakeCollection("proj_nimbus", _item())

    class _BlackHole(_FakeCollection):
        async def add(self, ids=None, documents=None, metadatas=None):
            return  # accepts the write, stores nothing

    junto = _BlackHole("proj_junto")
    sid = _seed({"proj_nimbus": nimbus, "proj_junto": junto}, monkeypatch)

    result = json.loads(
        await backlog_mod.memory_update_backlog_item(
            session_id=sid, item_id=ITEM_ID, project="junto"
        )
    )

    assert "error" in result, "a non-landing move must report an error"
    assert ITEM_ID in nimbus.docs, "source must survive a failed move"
    assert nimbus.deleted == []
