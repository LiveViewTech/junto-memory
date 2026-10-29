"""Modality shape flag on the read surfaces (Tom-approved 2026-08-10, from
coordinator@nimbus msg_b91f7992752a).

WHAT IT IS: a SHAPE flag — "this doc's body declares a proposal or a
deployment state, open it" — which is TRUE for every doc it fires on
regardless of whether extraction actually inverted the modality.

WHAT IT IS NOT: a defect predictor. The best measured discriminator for "this
claim is actually wrong" was 22% precision (phi4 judging its own output), so a
warning worded that way would be wrong ~4 times in 5. The distinction is the
whole design and these tests pin it.

Pins:
- both marker classes fire (proposal AND the committed-but-not-deployed class
  that the original screen was blind to);
- the deployment class stays TIGHT — a loose `NOT (YET )?(ON|IN)` matched 104
  docs and was junk;
- the window is a DISJUNCTION over title+body, not title-weighted (two
  reviewers independently mis-read it that way);
- the /recall marker goes INSIDE one_line, because sub's T1 rater sees
  one_line as its only view of a candidate;
- one_line still respects its 120-char contract WITH the prefix attached.
"""

import pytest

from shared_memory.__main__ import _recall_one_line
from shared_memory.facets import SHAPE_LABELS, modality_shape


class TestMarkerClasses:
    def test_proposal_class_fires(self):
        assert modality_shape(
            "apex 301 step 3 is unratified",
            "As of today the redirect WOULD move the whole app.",
        ) == "proposal"

    def test_deployment_class_fires(self):
        """The class BOTH confirmed nimbus instances actually failed on, and
        the one the original screen had no vocabulary for at all."""
        assert modality_shape(
            "/meural/ is now a CROSS-DOMAIN redirect target",
            "As of 2026-08-03 (commit 351fba6c, NOT yet on production — sits "
            "in a 24-commit delta behind the production freeze), the pages "
            "redirect cross-domain.",
        ) == "deployment"

    def test_plain_measured_fact_does_not_fire(self):
        assert modality_shape(
            "MqttStatusHandler reconnects after CONNACK",
            "Measured on the .47 bench: 68 successful reconnects today.",
        ) is None

    def test_deployment_markers_stay_tight(self):
        """A loose NOT (YET )?(ON|IN) matched 104 corpus docs and was visibly
        junk. These are the exact shapes that made it junk."""
        for body in (
            "The cache lives in the mosquitto process, not in upstream auth service.",
            "MqttLogger is defined but not in rootLogger.",
            "Motion data is on Azure File Share, not in Blob Storage.",
        ):
            assert modality_shape("A title", body) is None, body


class TestWindowGeometry:
    def test_marker_in_body_fires_under_an_indicative_title(self):
        """NOT title-weighted. Two reviewers independently claimed a confident
        title would mask a hedged body; it cannot — the window is a
        disjunction, so including the title only ever ADDS matches."""
        assert modality_shape(
            "The redirect is now live and serving",   # indicative, no marker
            "x" * 100 + " this is a PROPOSAL and has not been applied",
        ) == "proposal"

    def test_marker_in_title_alone_fires(self):
        assert modality_shape(
            "PROPOSAL, NOT APPLIED - no apex 301 exists",
            "Ordinary body text with nothing notable in it.",
        ) == "proposal"

    def test_marker_past_the_window_does_not_fire(self):
        """A real, unfixed limitation — pinned so nobody reads a clean result
        as 'no proposal docs are missed'."""
        assert modality_shape("A title", "x" * 400 + " PROPOSED") is None

    def test_proposal_wins_ties(self):
        """'unratified' is a stronger statement about the world than 'not yet
        deployed'; the reader needs the stronger one first."""
        assert modality_shape(
            "t", "This is UNRATIFIED and also NOT YET DEPLOYED.",
        ) == "proposal"

    def test_empty_inputs_are_safe(self):
        assert modality_shape("", "") is None
        assert modality_shape(None, None) is None


class TestRecallOneLine:
    def test_unflagged_one_line_is_unchanged(self):
        assert _recall_one_line("A short claim.", "body") == "A short claim."

    def test_flag_rides_inside_one_line(self):
        """sub's T1 rater sees "[{type}] {title} — {one_line}" as its ONLY
        view of a candidate, so a sibling key alone would need consumer code
        changes to have any effect."""
        out = _recall_one_line("The redirect is implemented.", "body", "deployment")
        assert out.startswith("⚠" + SHAPE_LABELS["deployment"])
        assert "open body" in out
        assert "The redirect is implemented." in out

    def test_prefix_eats_the_budget_rather_than_extending_it(self):
        """The header-size contract wins over claim completeness."""
        out = _recall_one_line("z" * 400, "body", "proposal")
        assert len(out) <= 120
        assert out.startswith("⚠")
        assert out.endswith("...")

    def test_unflagged_long_claim_still_capped(self):
        out = _recall_one_line("z" * 400, "body")
        assert len(out) <= 120


@pytest.mark.parametrize("shape", ["proposal", "deployment"])
def test_every_shape_has_a_label(shape):
    """A missing label would KeyError inside the one_line builder, which sits
    on the /recall hot path."""
    assert SHAPE_LABELS[shape]
