"""Park-summary shape rule — pre-model, tag-driven (2026-08-10).

end_session auto-writes a park-summary whose body is a list of pointers to
that session's learnings. It has no operation OF ITS OWN, so `reference` is
correct by construction — and phi4 gets it wrong reliably by latching onto
whichever pointer it read hardest (librarian: 8/8 overrides in one batch).

Deciding in CODE rather than by prompt edit is the point: it cannot move
phi4's behaviour because phi4 is not asked, which sidesteps the bakeoff
constraint that has gated recipe work for weeks.

⚠️ HISTORY WORTH KEEPING: the first cut of this was a CONTENT heuristic
(>=3 doc-id pointers + pointer-line density). Measured against the real
corpus it scored 5/8 recall with 2 FALSE POSITIVES on 6 ordinary analytical
learnings. The tag is exact. Precision > recall here — a wrong `reference`
on a real analytical doc is worse than missing an untagged digest.
"""

from shared_memory.facets import is_park_summary


class TestIsParkSummary:
    def test_list_tags_fire(self):
        assert is_park_summary(["park-summary", "server-team"]) is True

    def test_json_string_tags_fire(self):
        """Chroma stores tags as a JSON string, not a list."""
        assert is_park_summary('["park-summary", "frames-team"]') is True

    def test_ordinary_learning_tags_do_not_fire(self):
        assert is_park_summary(["facets", "shelf-life", "calibration"]) is False
        assert is_park_summary('["deploy", "ops", "junto-memory"]') is False

    def test_empty_and_missing_are_safe(self):
        assert is_park_summary(None) is False
        assert is_park_summary([]) is False
        assert is_park_summary("") is False
        assert is_park_summary("not json at all") is False

    def test_non_iterable_does_not_raise(self):
        assert is_park_summary(12345) is False

    def test_substring_does_not_false_fire(self):
        """'park-summary-ish' is not 'park-summary'."""
        assert is_park_summary(["park-summaries"]) is False
