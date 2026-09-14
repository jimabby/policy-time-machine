"""Two dials at once.

A single sweep holds every other threshold still and reports a curve. The thing
a reader takes from that curve - "at 100 you get 161 flips" - is true only where
the other dials are sitting right now, and nothing on the curve says so. These
tests are about the grid saying it.
"""

from __future__ import annotations

import pytest

from ptm import report, sweep

AXIS_A = {"field": "amount_gbp", "clause": "1.1", "values": [25, 75, 150]}
AXIS_B = {"field": "days_notice", "clause": "3.1", "values": [3, 7, 14]}


class TestTheGrid:
    def test_it_is_the_product_of_the_two_axes(self, seeded, replayed):
        result = sweep.joint("expenses", "v2", AXIS_A, AXIS_B)
        assert len(result["points"]) == 9
        assert {p["first_value"] for p in result["points"]} == {25, 75, 150}
        assert {p["second_value"] for p in result["points"]} == {3, 7, 14}

    def test_the_settings_in_force_are_marked(self, seeded):
        result = sweep.joint("expenses", "v2", AXIS_A, AXIS_B)
        current = [p for p in result["points"] if p["is_current"]]
        assert len(current) == 1, "exactly one point is where the policy sits today"
        assert (current[0]["first_value"], current[0]["second_value"]) == (75, 7)

    def test_each_cell_agrees_with_the_single_sweep_at_the_same_settings(self, seeded):
        """The grid must not be a second implementation of the replay. Holding
        the second dial at its current value reproduces the single curve."""
        grid = sweep.joint("expenses", "v2", AXIS_A,
                           {**AXIS_B, "values": [7]})
        curve = sweep.sweep("expenses", "v2", "amount_gbp", [25, 75, 150], clause="1.1")
        by_value = {p["value"]: p["flips"] for p in curve["points"]}
        for point in grid["points"]:
            assert point["flips"] == by_value[point["first_value"]]

    def test_moving_the_second_dial_changes_what_the_first_one_does(self, seeded):
        """The whole reason a grid exists. If this held equal everywhere, two
        curves would say the same thing and the grid would be decoration."""
        result = sweep.joint("expenses", "v2", AXIS_A, AXIS_B)
        by_pair = {(p["first_value"], p["second_value"]): p["flips"]
                   for p in result["points"]}
        spans = {b: by_pair[(150, b)] - by_pair[(25, b)] for b in (3, 7, 14)}
        assert len(set(spans.values())) > 1, \
            "the fixture's two clauses interact; the grid has to show it"

    def test_the_interaction_is_reported_as_a_number(self, seeded):
        result = sweep.joint("expenses", "v2", AXIS_A, AXIS_B)
        interaction = result["interaction"]
        assert interaction["measured"] and not interaction["independent"]
        assert interaction["effect_max_flips"] > interaction["effect_min_flips"]

    def test_independent_dials_say_so(self, seeded, expenses):
        """A grid that was not worth running has to admit it, or it becomes a
        panel people look at out of habit."""
        single = {"field": "amount_gbp", "clause": "1.1", "values": [75]}
        result = sweep.joint("expenses", "v2", single, AXIS_B)
        assert result["interaction"]["measured"] is False, \
            "one setting on an axis cannot show an interaction either way"

    def test_the_same_dial_twice_is_refused(self, seeded):
        with pytest.raises(LookupError, match="same dial"):
            sweep.joint("expenses", "v2", AXIS_A, dict(AXIS_A))

    def test_a_field_no_rule_compares_is_refused(self, seeded):
        with pytest.raises(LookupError, match="compares"):
            sweep.joint("expenses", "v2", AXIS_A,
                        {"field": "not_a_field", "clause": "", "values": [1, 2]})


class TestTheAxisSyntax:
    @pytest.mark.parametrize("raw,expected", [
        ("1.1:amount_gbp=25,50", {"clause": "1.1", "field": "amount_gbp",
                                  "values": [25, 50]}),
        ("amount_gbp=25,50", {"clause": "", "field": "amount_gbp", "values": [25, 50]}),
        (":amount_gbp=1.5", {"clause": "", "field": "amount_gbp", "values": [1.5]}),
    ])
    def test_it_parses(self, raw, expected):
        assert sweep.parse_axis(raw) == expected

    @pytest.mark.parametrize("raw", ["amount_gbp", "=25,50"])
    def test_it_refuses_nonsense(self, raw):
        with pytest.raises(ValueError):
            sweep.parse_axis(raw)


class TestTheEndpoint:
    def test_the_grid_is_capped(self, seeded):
        """|A| x |B| full replays is cheap per point and not cheap at 400, and
        an endpoint anyone can call is the wrong place to discover that."""
        with pytest.raises(LookupError, match="grid points"):
            report.joint_sweep("expenses", "v2", "amount_gbp", ",".join("123456789"),
                               "days_notice", ",".join("123456789"))

    def test_it_parses_query_string_values(self, seeded):
        result = report.joint_sweep("expenses", "v2", "amount_gbp", "25,75",
                                    "days_notice", "3,7", "1.1", "3.1")
        assert len(result["points"]) == 4

    def test_it_says_what_the_rules_it_rests_on_are_worth(self, seeded, replayed):
        """A curve computed from the offline rules arrives with a statement
        about how far those rules have drifted from the judge, or a reader
        takes it as being about the policy."""
        result = report.joint_sweep("expenses", "v2", "amount_gbp", "25,75",
                                    "days_notice", "3,7", "1.1", "3.1")
        assert "rules_check" in result
