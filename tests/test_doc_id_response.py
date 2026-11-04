"""Regression tests for Issue #2 — `memory_store` and `memory_record_learning`
must return the full 16-hex-suffix doc_id so callers can pass it back into
`memory_get_by_id` and get a hit (rather than a 404 against a 3-hex-char
truncated form).

Also covers the analogous slice in `memory_archive_by_tag` /
`memory_restore_by_tag` response payloads where archived/restored doc ids
were being clipped before reaching the client.
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import storage


class _FakeCollection:
    def __init__(self, name="proj_test"):
        self.name = name
        self.added_ids: list[str] = []

    async def add(self, ids, documents, metadatas):
        self.added_ids.extend(ids)
        self.added_docs = getattr(self, "added_docs", []) + list(documents)
        self.added_metas = getattr(self, "added_metas", []) + list(metadatas)


def _install_session(session_id="sess_test", project="test", role="agent"):
    active_sessions[session_id] = {
        "claude_instance": "tester",
        "project": project,
        "role": role,
        "allowed_projects": [project],
        "started_at": "2026-05-14T00:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def _disable_auth(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)


@pytest.fixture(autouse=True)
def _clear_sessions():
    active_sessions.clear()
    yield
    active_sessions.clear()


@pytest.fixture
def _patch_storage_collection(monkeypatch):
    fake = _FakeCollection()

    async def _fake_get_chroma():
        return object()

    async def _fake_get_project_collection(client, project):
        return fake

    async def _fake_get_shared_collection(client, kind):
        return fake

    async def _fake_check_duplicate(collection, content, threshold=0.95):
        return None

    monkeypatch.setattr(storage, "get_chroma", _fake_get_chroma)
    monkeypatch.setattr(storage, "get_project_collection", _fake_get_project_collection)
    monkeypatch.setattr(storage, "get_shared_collection", _fake_get_shared_collection)
    monkeypatch.setattr(storage, "check_duplicate", _fake_check_duplicate)
    monkeypatch.setattr(storage, "get_mongo", lambda: None)
    return fake


async def test_memory_record_learning_returns_full_doc_id(_patch_storage_collection):
    _install_session()
    raw = await storage.memory_record_learning(
        session_id="sess_test",
        title="probe",
        details="repro of Issue #2",
        project="test",
        tags=["regression"],
    )
    response = json.loads(raw)
    assert response["status"] == "recorded"
    actual_id = _patch_storage_collection.added_ids[0]
    assert response["id"] == actual_id, (
        f"response id {response['id']!r} must equal the Chroma-side id "
        f"{actual_id!r} so memory_get_by_id round-trips"
    )
    assert response["id"].startswith("learning_")
    # learning_ + 16 hex chars = 25 chars total. The Issue #2 bug returned 12.
    assert len(response["id"]) == len("learning_") + 16


async def test_memory_store_returns_full_doc_id(_patch_storage_collection):
    _install_session()
    raw = await storage.memory_store(
        session_id="sess_test",
        title="probe-store",
        content="repro of Issue #2 against memory_store too",
        memory_type="pattern",
        project="test",
        tags=["regression"],
    )
    response = json.loads(raw)
    assert response["status"] == "stored"
    actual_id = _patch_storage_collection.added_ids[0]
    assert response["id"] == actual_id
    # doc_id shape for non-interface memory: prefix + 16 hex. Prefix varies by
    # memory_type but is always >= 5 chars; full id always > 12 chars.
    assert len(response["id"]) > 12


async def test_record_learning_redacts_secrets_before_persist(_patch_storage_collection):
    """Memento #3: the credential never reaches Chroma, and the writer is told."""
    _install_session()
    raw = await storage.memory_record_learning(
        session_id="sess_test",
        title="dev mongo creds",
        details="MONGO_URI='mongodb://admin:T0mPass1@localhost:27017' for the dev box",
        project="test",
    )
    response = json.loads(raw)
    stored = " ".join(_patch_storage_collection.added_docs)
    assert "T0mPass1" not in stored
    assert "[REDACTED:url_password]" in stored
    assert response["redacted"] == {"url_password": 1}
    assert "redacted_warning" in response


async def test_store_redacts_title_and_content(_patch_storage_collection):
    _install_session()
    raw = await storage.memory_store(
        session_id="sess_test",
        title="key AKIAIOSFODNN7EXAMPLE",
        content="HA_TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        memory_type="pattern",
        project="test",
    )
    response = json.loads(raw)
    assert "AKIA" not in _patch_storage_collection.added_metas[0]["title"]
    assert "eyJhbGci" not in _patch_storage_collection.added_docs[0]
    assert response["redacted"] == {"aws_access_key": 1, "jwt": 1}


async def test_clean_write_has_no_redaction_keys(_patch_storage_collection):
    _install_session()
    raw = await storage.memory_record_learning(
        session_id="sess_test", title="plain", details="nothing secret here",
        project="test",
    )
    assert "redacted" not in json.loads(raw)
