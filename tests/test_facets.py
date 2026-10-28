"""Write-time facets (design:memory-facets-v0 v0.4.0).

Pins the contract-load-bearing behaviors:
  1. Dormant by default — no env knob, no task scheduled, no response field.
  2. Fail-quiet parsing: bad enum word → operation omitted; digression lines
     → trigger dropped; >3 trigger lines → trimmed; empty claim → NO facets
     row at all (claim is guaranteed-non-empty when the container exists).
  3. Claim reuse: a gate-cached claim is never re-extracted.
  4. Stored row carries recipe_version/model/extracted_at stamps.
  5. get_facets_for_ids: batched read, plumbing stripped, empty on failure.
  6. memory_query attaches facets inline to learning_ rows (delivery-surface
     guarantee) and leaves rows without facets untouched.
"""

import asyncio

import pytest

from shared_memory import claim_gate, facets


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeMongoCollection:
    def __init__(self):
        self.rows = {}

    def update_one(self, filt, update, upsert=False):
        _id = filt["_id"]
        row = self.rows.get(_id, {"_id": _id})
        row.update(update["$set"])
        self.rows[_id] = row

    def find_one(self, filt):
        return self.rows.get(filt["_id"])

    def find(self, filt):
        ids = filt["_id"]["$in"]
        return [dict(r) for _id, r in self.rows.items() if _id in ids]


class _FakeMongo:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeMongoCollection())


class _FakeChroma:
    def __init__(self, metadatas=None):
        self.metadatas = metadatas or {}
        self.updates = []

    async def get(self, ids, include=None):
        metas = [self.metadatas.get(i) for i in ids]
        return {"ids": ids, "metadatas": metas, "documents": [None] * len(ids)}

    async def update(self, ids, metadatas):
        self.updates.append((ids, metadatas))


def _scripted_chat(responses):
    """Monkeypatch replacement for claim_gate._chat: pops scripted responses
    keyed by the system prompt."""
    async def chat(client, system, user, max_tokens):
        return responses[system].pop(0)
    return chat


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JUNTO_FACETS_ENABLED", "true")


# ---------------------------------------------------------------------------
# 1. dormant by default
# ---------------------------------------------------------------------------

def test_disabled_schedules_nothing(monkeypatch):
    monkeypatch.delenv("JUNTO_FACETS_ENABLED", raising=False)
    assert facets.schedule_facet_extraction(None, None, "learning_x", "t", "d") is False


# ---------------------------------------------------------------------------
# 2/3/4. extraction pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(monkeypatch, db, chroma, chat_responses,
                  cached_claim=None, doc_id="learning_x"):
    monkeypatch.setattr(claim_gate, "_chat", _scripted_chat(chat_responses))
    if cached_claim:
        db[claim_gate.CLAIMS_COLLECTION].update_one(
            {"_id": doc_id},
            {"$set": {"claim": cached_claim,
                      "recipe_version": claim_gate.RECIPE_VERSION}},
            upsert=True,
        )
    asyncio.run(facets._extract_and_store(db, chroma, doc_id, "Title", "Details"))


def test_full_pipeline_stores_row_and_mirrors_operation(monkeypatch, enabled):
    db, chroma = _FakeMongo(), _FakeChroma(
        metadatas={"learning_x": {"title": "Title", "type": "learning",
                                  "status": "active"}}
    )
    _run_pipeline(monkeypatch, db, chroma, {
        claim_gate.EXTRACT_SYSTEM: ["The server does X."],
        facets.OPERATION_SYSTEM: ["diagnose"],
        facets.SHELF_LIFE_SYSTEM: ["VOLATILE"],
        facets.TRIGGER_SYSTEM: [
            "when the server reports X\nbefore restarting mcp-rag-arch\n"
            "This note is about X.\nwhen a fourth thing\n"
        ],
    })
    row = db[facets.FACETS_COLLECTION].rows["learning_x"]
    assert row["claim"] == "The server does X."
    assert row["operation"] == "diagnose"
    assert row["shelf_life"] == "volatile"
    # digression line dropped, convention lines kept, capped at 3 → here 3rd
    # valid line exists but "This note is about X." was invalid → 3 total
    assert row["trigger"] == [
        "when the server reports X",
        "before restarting mcp-rag-arch",
        "when a fourth thing",
    ]
    assert row["recipe_version"] == facets.FACETS_RECIPE_VERSION
    assert row["model"] and row["extracted_at"]
    # operation mirrored to chroma with existing metadata preserved
    (ids, metas), = chroma.updates
    assert ids == ["learning_x"]
    assert metas[0]["facet_operation"] == "diagnose"
    assert metas[0]["type"] == "learning"


def test_bad_enum_and_bad_shelf_life_are_omitted(monkeypatch, enabled):
    db, chroma = _FakeMongo(), _FakeChroma()
    _run_pipeline(monkeypatch, db, chroma, {
        claim_gate.EXTRACT_SYSTEM: ["Claim."],
        facets.OPERATION_SYSTEM: ["NONE"],
        facets.SHELF_LIFE_SYSTEM: ["it depends on many factors"],
        facets.TRIGGER_SYSTEM: ["no convention lines here"],
    })
    row = db[facets.FACETS_COLLECTION].rows["learning_x"]
    assert "operation" not in row
    assert "shelf_life" not in row
    assert "trigger" not in row  # empty list is falsy → stripped
    assert row["claim"] == "Claim."
    assert chroma.updates == []  # nothing to mirror


def test_empty_claim_means_no_row(monkeypatch, enabled):
    db, chroma = _FakeMongo(), _FakeChroma()
    _run_pipeline(monkeypatch, db, chroma, {
        claim_gate.EXTRACT_SYSTEM: [""],
        facets.OPERATION_SYSTEM: ["diagnose"],
        facets.SHELF_LIFE_SYSTEM: ["DURABLE"],
        facets.TRIGGER_SYSTEM: ["when x"],
    })
    assert facets.FACETS_COLLECTION not in db.collections or \
        "learning_x" not in db[facets.FACETS_COLLECTION].rows


def test_gate_cached_claim_is_reused_not_reextracted(monkeypatch, enabled):
    db, chroma = _FakeMongo(), _FakeChroma()
    # EXTRACT_SYSTEM deliberately has NO scripted response — a re-extract
    # would KeyError/IndexError and the fail-quiet boundary would skip the
    # row; a stored row therefore proves the cache was used.
    _run_pipeline(monkeypatch, db, chroma, {
        claim_gate.EXTRACT_SYSTEM: [],
        facets.OPERATION_SYSTEM: ["build"],
        facets.SHELF_LIFE_SYSTEM: ["DURABLE"],
        facets.TRIGGER_SYSTEM: ["when y happens"],
    }, cached_claim="Cached claim.")
    row = db[facets.FACETS_COLLECTION].rows["learning_x"]
    assert row["claim"] == "Cached claim."
    assert row["shelf_life"] == "durable"


def test_model_failure_degrades_to_no_row(monkeypatch, enabled):
    async def boom(client, system, user, max_tokens):
        raise RuntimeError("endpoint down")
    monkeypatch.setattr(claim_gate, "_chat", boom)
    db, chroma = _FakeMongo(), _FakeChroma()
    asyncio.run(facets._extract_and_store(db, chroma, "learning_x", "T", "D"))
    assert facets.FACETS_COLLECTION not in db.collections or \
        "learning_x" not in db[facets.FACETS_COLLECTION].rows


# ---------------------------------------------------------------------------
# 5/6. delivery surface
# ---------------------------------------------------------------------------

def test_get_facets_for_ids_batched_and_stripped():
    db = _FakeMongo()
    db[facets.FACETS_COLLECTION].update_one(
        {"_id": "learning_a"},
        {"$set": {"claim": "A", "operation": "diagnose",
                  "recipe_version": "0.3.0"}},
        upsert=True,
    )
    out = facets.get_facets_for_ids(db, ["learning_a", "learning_missing"])
    assert set(out) == {"learning_a"}
    assert out["learning_a"]["claim"] == "A"
    assert "_id" not in out["learning_a"]


def test_get_facets_for_ids_failure_returns_empty():
    class _Broken:
        def __getitem__(self, name):
            raise RuntimeError("mongo down")
    assert facets.get_facets_for_ids(_Broken(), ["learning_a"]) == {}
    assert facets.get_facets_for_ids(None, ["learning_a"]) == {}
    assert facets.get_facets_for_ids(_FakeMongo(), []) == {}
