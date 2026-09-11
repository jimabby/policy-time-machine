"""Does the change land on one group harder than the rest?

The failure mode this panel has to avoid is not missing a concentration - it is
reporting so many unsupported ones that nobody reads it again. So most of these
tests are about what it declines to say: small buckets, single-valued fields,
and differences the sample size cannot support.
"""

from __future__ import annotations

from ptm import disparity, report


def row(field: str, value: str, cases: int, flips: int,
        loosening: int | None = None, tightening: int = 0, impact: float = 0.0) -> dict:
    return {"field": field, "value": value, "cases": cases, "flips": flips,
            "loosening": flips if loosening is None else loosening,
            "tightening": tightening,
            "impact_loosening": impact, "impact_tightening": 0.0}


class TestWhatItDeclinesToSay:
    def test_a_segment_below_min_cases_is_not_compared_at_all(self, expenses):
        rows = [row("category", "tiny", 9, 9), row("category", "rest", 500, 50)]
        assert disparity.analyse(rows, expenses) == []

    def test_a_field_with_one_value_has_nothing_to_compare_against(self, expenses):
        """Reporting a ratio of 1.0 would imply a comparison happened."""
        assert disparity.analyse([row("category", "only", 600, 147)], expenses) == []

    def test_a_field_the_domain_does_not_ask_about_is_ignored_not_rejected(self, expenses):
        """The whole blast radius can be handed to this."""
        rows = [row("not_a_segment", "a", 300, 200), row("not_a_segment", "b", 300, 10)]
        assert disparity.analyse(rows, expenses) == []

    def test_an_unsupported_difference_is_reported_but_marked(self, expenses):
        """Present, so it can be looked at; marked, so it is not acted on."""
        rows = [row("category", "a", 40, 16), row("category", "b", 40, 6)]
        findings = disparity.analyse(rows, expenses)
        # Two rows, two findings: with one other value to compare against, the
        # segment carrying the change and the one it passes over are the same
        # observation seen from either end.
        assert {f.value for f in findings} == {"a", "b"}
        assert not any(f.significant for f in findings)
        assert not disparity.gated(findings), "nothing unsupported may fail a run"


class TestTheComparison:
    def test_a_segment_is_compared_against_the_rest_of_its_field_pooled(self, expenses):
        """Not against the least-affected bucket, which would make the smallest
        group the protagonist of every finding."""
        rows = [row("category", "hot", 200, 100), row("category", "mid", 200, 40),
                row("category", "cold", 200, 20)]
        hot = next(f for f in disparity.analyse(rows, expenses) if f.value == "hot")
        assert hot.rest_cases == 400 and hot.rest_flips == 60
        assert hot.ratio == round(0.5 / 0.15, 3)

    def test_a_group_the_change_passes_over_is_a_finding_too(self, expenses):
        """For a loosening proposal, a group that barely moves is a benefit
        distributed unevenly - the same question asked from the other side."""
        rows = [row("category", "skipped", 200, 8), row("category", "rest", 400, 200)]
        skipped = next(f for f in disparity.analyse(rows, expenses) if f.value == "skipped")
        assert skipped.ratio < 1
        assert skipped not in disparity.gated(disparity.analyse(rows, expenses)), \
            "a distribution question is not an exposure one and must not fail a run"

    def test_the_rest_of_the_field_not_moving_at_all_is_said_in_words(self, expenses):
        rows = [row("category", "only_mover", 200, 100), row("category", "rest", 400, 0)]
        finding = next(f for f in disparity.analyse(rows, expenses) if f.value == "only_mover")
        assert finding.ratio == 0.0
        assert "does not move at all" in disparity.describe([finding], expenses)

    def test_direction_separates_being_given_something_from_losing_it(self, expenses):
        rows = [row("category", "loose", 200, 100, loosening=100),
                row("category", "tight", 200, 100, loosening=0, tightening=100),
                row("category", "rest", 400, 20)]
        by_value = {f.value: f.direction for f in disparity.analyse(rows, expenses)}
        assert by_value["loose"] == "loosening"
        assert by_value["tight"] == "tightening"

    def test_a_genuine_split_is_called_mixed_rather_than_by_its_majority(self, expenses):
        rows = [row("category", "both", 200, 100, loosening=60, tightening=40),
                row("category", "rest", 400, 20)]
        both = next(f for f in disparity.analyse(rows, expenses) if f.value == "both")
        assert both.direction == "mixed"


class TestAgainstTheShippedFixture:
    def test_it_finds_the_segment_the_shipped_proposal_actually_concentrates_on(self, replayed):
        """v2 raises the meals cap, so meals move far more than anything else.
        A check that cannot find a concentration that obvious is not a check."""
        result = report.disparity("expenses", "v2")
        found = {f["value"]: f for f in result["findings"]}
        assert "meals" in found
        assert found["meals"]["significant"] is True
        assert found["meals"]["ratio"] > 2
        assert found["meals"]["direction"] == "loosening"

    def test_the_panel_never_calls_a_concentration_unfair(self, replayed):
        """It is a request for an explanation, and the explanation is often
        good. Saying more than that would be a claim this cannot support."""
        result = report.disparity("expenses", "v2")
        assert "question, not a verdict" in result["caveat"]
        assert "not a fault" in result["summary"]
