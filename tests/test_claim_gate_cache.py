"""Claim cache stickiness across recipe bumps (v1.0.3 amendment).

The librarian repairs defective claims by hand — meta-openers, truncations,
factual inversions. A recipe bump invalidates cached claims lazily, which
would silently overwrite that manual work with a fresh machine extraction.
A correction outranks a re-run: the repair exists BECAUSE the extractor
was wrong. Ratified with sub alongside EXTRACT_MAX_TOKENS 120 -> 400.
"""

from shared_memory import claim_gate


class _FakeDB:
    def __init__(self, row):
        self._row = row
        self.CLAIMS = self

    def __getitem__(self, _name):
        return self

    def find_one(self, _q):
        return self._row


def test_hand_repaired_claim_survives_a_recipe_bump():
    row = {
        "_id": "learning_x", "claim": "hand-written correct claim",
        "recipe_version": "1.0.2",            # stale on purpose
        "claim_cleaned_by": "librarian",
    }
    assert claim_gate._cached_claim(_FakeDB(row), "learning_x") == (
        "hand-written correct claim"
    ), "a recipe bump must not discard a hand repair"


def test_machine_claim_is_invalidated_by_a_recipe_bump():
    row = {"_id": "learning_y", "claim": "stale machine claim",
           "recipe_version": "1.0.2"}
    assert claim_gate._cached_claim(_FakeDB(row), "learning_y") is None


def test_current_version_machine_claim_is_served():
    row = {"_id": "learning_z", "claim": "fresh machine claim",
           "recipe_version": claim_gate.RECIPE_VERSION}
    assert claim_gate._cached_claim(_FakeDB(row), "learning_z") == (
        "fresh machine claim"
    )


def test_missing_row_is_a_miss():
    assert claim_gate._cached_claim(_FakeDB(None), "nope") is None


def test_contract_constants_match_ratified_v103():
    assert claim_gate.RECIPE_VERSION == "1.0.3"
    assert claim_gate.EXTRACT_MAX_TOKENS == 400
    assert claim_gate.CLASSIFY_MAX_TOKENS == 8, "classify stage untouched"
    assert claim_gate.CONTENT_HEAD_CHARS == 3000, "input head untouched"
