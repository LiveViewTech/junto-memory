"""memory_get_spec read-path resolution (defect reported by frames-team@nimbus
2026-06-14).

Three defects, all here:
  1. get_spec read ONLY the collection named by the `project` ARG, so a
     project-scoped spec (every state: spec lives in proj_<project>) was
     invisible when the caller omitted project -> 404 (coordinator case).
  2. get_spec never filtered status=="active", so an archived/orphaned ghost
     doc sitting at a live id was served as current (frames-team case: a
     shared_patterns ghost shadowed the real proj_nimbus spec).
  3. define_spec accepted a malformed payload where tool-call XML leaked into
     content (the original cause of the ghost).

Fix: when project is omitted, get_spec tries the caller's OWN project first,
then shared; skips non-active docs; and falls back to the live doc when an
explicit version request matches the current version. define_spec no longer
rejects the malformed-content signature — reconciled 2026-08-26 to the shared
strip-and-recover posture every free-text writer uses (backlog_8d33a63e2626).
"""

import json

import pytest

from shared_memory import auth as auth_mod
from shared_memory.state import active_sessions
from shared_memory.tools import specs


class _FakeCol:
    def __init__(self, name):
        self.name = name
        self.docs = {}  # id -> (document, metadata)

    async def get(self, ids=None, include=None, where=None):
        oids, docs, metas = [], [], []
        items = ([(i, self.docs[i]) for i in ids if i in self.docs]
                 if ids else list(self.docs.items()))
        for i, (d, m) in items:
            oids.append(i); docs.append(d); metas.append(m)
        return {"ids": oids, "documents": docs, "metadatas": metas}

    async def upsert(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = (d, m)

    async def add(self, ids, documents, metadatas):
        for i, d, m in zip(ids, documents, metadatas):
            self.docs[i] = (d, m)


def _spec_meta(version, spec_type, status="active", project="", owner="frames-team",
               updated="2026-06-13T00:00:00+00:00", name="state:frames-team"):
    return {
        "type": "spec", "spec_name": name, "spec_version": version,
        "spec_type": spec_type, "spec_owner": owner, "status": status,
        "project": project, "tags": "[]", "created": "2026-01-01", "updated": updated,
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(auth_mod, "AUTH_ENABLED", False)
    active_sessions.clear()
    yield
    active_sessions.clear()


def _install(session_id="s1", project="nimbus"):
    active_sessions[session_id] = {
        "claude_instance": "frames-team", "project": project, "role": "agent",
        "allowed_projects": [project], "started_at": "2026-06-14T00:00:00+00:00",
    }


def _patch_chroma(monkeypatch, collections):
    async def _get_chroma():
        return object()

    async def _get_project_collection(client, project):
        name = f"proj_{project}"
        return collections.setdefault(name, _FakeCol(name))

    async def _get_shared_collection(client, kind):
        name = f"shared_{kind}"
        return collections.setdefault(name, _FakeCol(name))

    monkeypatch.setattr(specs, "get_chroma", _get_chroma)
    monkeypatch.setattr(specs, "get_project_collection", _get_project_collection)
    monkeypatch.setattr(specs, "get_shared_collection", _get_shared_collection)
    monkeypatch.setattr(specs, "get_mongo", lambda: None)


async def test_project_spec_resolves_when_project_omitted_ignoring_shared_ghost(monkeypatch):
    """The frames-team case: a shared archived ghost must NOT shadow the live
    proj_nimbus spec when the caller omits project."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], ["CURRENT NIMBUS STATE"],
                      [_spec_meta("1.0.133", "agent_state", "active", "nimbus")])
    shared = _FakeCol("shared_patterns")
    await shared.upsert(["spec_state_frames-team"], ["old apr-22 ghost"],
                        [_spec_meta("1.0.0", "interface", "archived", "")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = shared

    _install()
    res = json.loads(await specs.memory_get_spec(session_id="s1", name="state:frames-team"))
    assert res.get("version") == "1.0.133", res
    assert res.get("spec_type") == "agent_state"
    assert res["content"] == "CURRENT NIMBUS STATE"


async def test_archived_ghost_alone_is_not_served(monkeypatch):
    """If ONLY the archived ghost exists, get_spec must report not-found, not
    serve the ghost as current."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    shared = _FakeCol("shared_patterns")
    await shared.upsert(["spec_state_frames-team"], ["ghost"],
                        [_spec_meta("1.0.0", "interface", "archived", "")])
    cols["shared_patterns"] = shared
    cols["proj_nimbus"] = _FakeCol("proj_nimbus")  # empty

    _install()
    res = json.loads(await specs.memory_get_spec(session_id="s1", name="state:frames-team"))
    assert "error" in res and "not found" in res["error"].lower(), res


async def test_project_spec_found_when_no_shared_doc(monkeypatch):
    """The coordinator case: no shared doc at all — must resolve from the
    caller's project, not 404."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], ["coord-style"],
                      [_spec_meta("1.0.103", "agent_state", "active", "nimbus")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = _FakeCol("shared_patterns")  # empty

    _install()
    res = json.loads(await specs.memory_get_spec(session_id="s1", name="state:frames-team"))
    assert res.get("version") == "1.0.103", res


async def test_stale_active_shared_duplicate_loses_to_newer_project(monkeypatch):
    """The server-team case: shared has an ACTIVE-but-stale duplicate (status
    filter won't catch it). The newer project copy must win by recency."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], ["CURRENT v1.1.6"],
                      [_spec_meta("1.1.6", "agent_state", "active", "nimbus",
                                  updated="2026-06-13T22:00:00+00:00")])
    shared = _FakeCol("shared_patterns")
    await shared.upsert(["spec_state_frames-team"], ["stale v1.0.2"],
                        [_spec_meta("1.0.2", "agent_state", "active", "",
                                    updated="2026-02-01T00:00:00+00:00")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = shared

    _install()
    res = json.loads(await specs.memory_get_spec(session_id="s1", name="state:frames-team"))
    assert res.get("version") == "1.1.6", res
    assert res["content"] == "CURRENT v1.1.6"


async def test_newer_shared_spec_not_regressed_to_older_project_shadow(monkeypatch):
    """The media-delivery-contract case: a genuinely-shared spec whose shared
    copy is AHEAD must NOT be regressed to an older project shadow."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_design_media-delivery-contract"], ["proj v2.1.1"],
                      [_spec_meta("2.1.1", "design", "active", "nimbus",
                                  updated="2026-03-01T00:00:00+00:00",
                                  name="design:media-delivery-contract")])
    shared = _FakeCol("shared_patterns")
    await shared.upsert(["spec_design_media-delivery-contract"], ["shared v2.2.0"],
                        [_spec_meta("2.2.0", "design", "active", "",
                                    updated="2026-06-10T00:00:00+00:00",
                                    name="design:media-delivery-contract")])
    cols["proj_nimbus"] = proj
    cols["shared_patterns"] = shared

    _install()
    res = json.loads(await specs.memory_get_spec(
        session_id="s1", name="design:media-delivery-contract"))
    assert res.get("version") == "2.2.0", res
    assert res["content"] == "shared v2.2.0"


async def test_explicit_current_version_falls_back_to_live_doc(monkeypatch):
    """Requesting the CURRENT version label (never archived to history) must
    return the live doc, not 'version not found'."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], ["live"],
                      [_spec_meta("1.0.133", "agent_state", "active", "nimbus")])
    cols["proj_nimbus"] = proj
    cols["shared_context"] = _FakeCol("shared_context")  # empty history

    _install()
    res = json.loads(await specs.memory_get_spec(
        session_id="s1", name="state:frames-team", version="1.0.133"))
    assert res.get("version") == "1.0.133", res
    assert res["content"] == "live"


async def test_explicit_missing_version_still_404s(monkeypatch):
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_frames-team"], ["live"],
                      [_spec_meta("1.0.133", "agent_state", "active", "nimbus")])
    cols["proj_nimbus"] = proj
    cols["shared_context"] = _FakeCol("shared_context")

    _install()
    res = json.loads(await specs.memory_get_spec(
        session_id="s1", name="state:frames-team", version="9.9.9"))
    assert "error" in res and "9.9.9" in res["error"], res


async def test_list_specs_include_versions_finds_intact_history(monkeypatch):
    """list_specs(include_versions) must surface the FULL archived history, not
    fall back to [current] — the false 'no history' that made a clobbered state
    spec look unrecoverable. Regression for the multi-key where-filter bug."""
    cols = {}
    _patch_chroma(monkeypatch, cols)
    proj = _FakeCol("proj_nimbus")
    await proj.upsert(["spec_state_billing-team"], ["current"],
                      [_spec_meta("1.2.30", "agent_state", "active", "nimbus",
                                  name="state:billing-team")])
    cols["proj_nimbus"] = proj
    hist = _FakeCol("shared_context")
    for v in ("1.2.27", "1.2.28", "1.2.29"):
        await hist.upsert(
            [f"spec_history_state_billing-team_{v.replace('.', '_')}"], [f"old {v}"],
            [{"type": "spec", "spec_name": "state:billing-team", "spec_version": v,
              "status": "archived", "archived_at": "2026-06-13"}])
    cols["shared_context"] = hist

    _install()
    res = json.loads(await specs.memory_list_specs(
        session_id="s1", project="nimbus", include_versions=True))
    out = {s["name"]: s for s in res["specs"]}
    av = set(out["state:billing-team"]["all_versions"])
    assert av >= {"1.2.27", "1.2.28", "1.2.29", "1.2.30"}, av


async def test_define_spec_recovers_malformed_xml_leak(monkeypatch):
    """define_spec used to REJECT content where a tool-call serialization leaked
    in (the class that created the original ghost). Reconciled 2026-08-26
    (backlog_8d33a63e2626) to the shared RECOVERY posture every free-text writer
    uses: strip + re-route + warn, so no work is lost and no retry is needed."""
    collections = {}
    _patch_chroma(monkeypatch, collections)
    _install()
    bad = ('## Current Task\nreal content...\n</content>'
           '<parameter name="spec_type">agent_state</parameter>'
           '<parameter name="project">nimbus</parameter></invoke>')
    res = json.loads(await specs.memory_define_spec(
        session_id="s1", name="state:frames-team", content=bad,
        spec_type="agent_state", project="nimbus", owner="frames-team"))
    # Recovery, not rejection: the write succeeds and flags the malformed client.
    assert "error" not in res, res
    assert res["status"].endswith("_with_recovery"), res
    assert res.get("write_lint"), res
    # The stored doc is clean — the envelope tail is gone.
    stored_doc, _meta = collections["proj_nimbus"].docs["spec_state_frames-team"]
    assert "</content>" not in stored_doc and "</invoke>" not in stored_doc, stored_doc
    # A swallowed non-project param (spec_type) is preserved in-doc, not lost.
    assert "agent_state" in stored_doc, stored_doc
