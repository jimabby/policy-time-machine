"""How big a change this much history could have detected.

Every other band in this project is retrospective: it says how precise a
measurement turned out to be once it had been paid for. This is the one that
comes first, and it is the one a backfill should be sized by - *how many cases
do I need to tell 20% from 24%?* A replay of one month's cases cannot separate
those at any confidence worth quoting, and discovering that from two overlapping
bands afterwards costs a backfill.

The direction that matters is the refusal. A comparison this history cannot
settle has to say so rather than produce a number somebody argues from.
"""

from __future__ import annotations

import pytest

from ptm import report, stats


class TestSampleSize:
    def test_a_smaller_difference_costs_more_cases(self):
        assert stats.sample_size(0.20, 0.30) < stats.sample_size(0.20, 0.24)
        assert stats.sample_size(0.20, 0.24) < stats.sample_size(0.20, 0.21)

    def test_it_is_symmetric_in_the_two_rates(self):
        assert stats.sample_size(0.20, 0.24) == stats.sample_size(0.24, 0.20)

    def test_a_rate_cannot_be_told_from_itself(self):
        """Zero rather than a division by zero, and zero rather than infinity:
        there is no sample size that separates a rate from itself, and the
        honest answer to "how many" is that the question is malformed."""
        assert stats.sample_size(0.2, 0.2) == 0

    def test_more_power_costs_more_cases(self):
        assert (stats.sample_size(0.20, 0.24, power=stats.Z_POWER90)
                > stats.sample_size(0.20, 0.24, power=stats.Z_POWER80))

    def test_it_lands_where_the_textbook_does(self):
        """20% against 24% at 95%/80% is a little over sixteen hundred per arm.

        Pinned loosely rather than exactly: the point is that the arithmetic is
        the ordinary two-proportion one and not something invented here, and a
        band catches a sign error without failing on a rounding convention.
        """
        assert 1500 < stats.sample_size(0.20, 0.24) < 1900


class TestDetectableDifference:
    def test_more_cases_detect_smaller_moves(self):
        assert (stats.detectable_difference(0.245, 6000)
                < stats.detectable_difference(0.245, 600)
                < stats.detectable_difference(0.245, 120))

    def test_it_inverts_the_sample_size(self):
        """The two have to agree or one of them is decoration."""
        mde = stats.detectable_difference(0.245, 600)
        assert stats.sample_size(0.245, 0.245 + mde) <= 600
        assert stats.sample_size(0.245, 0.245 + mde * 0.8) > 600

    def test_it_inverts_the_sample_size_downwards_too(self):
        """The half that was never asked, and the half that was wrong.

        The test above only ever moved *up* from the baseline, so the untested
        direction was free to be whatever the arithmetic happened to give - and
        power_report was mirroring the upward answer onto it.
        """
        mde = stats.detectable_difference(0.245, 600, direction="down")
        assert stats.sample_size(0.245, 0.245 - mde) <= 600
        assert stats.sample_size(0.245, 0.245 - mde * 0.8) > 600

    @pytest.mark.parametrize("baseline", [0.05, 0.10, 0.245, 0.40])
    def test_the_two_directions_are_not_the_same_number(self, baseline):
        """Which is the whole reason `direction` exists.

        `sample_size` is symmetric in its two *rates*; that is a different
        property from being symmetric about a fixed baseline, and it is the
        second one power_report needed and did not have. A rise and a fall of
        the same size from the same baseline sit at different variances, so
        they cost different numbers of cases.
        """
        up = stats.detectable_difference(baseline, 600, direction="up")
        down = stats.detectable_difference(baseline, 600, direction="down")
        assert down < up

    def test_a_boundary_rate_answers_with_the_direction_that_exists(self):
        """A rate of 100% cannot rise. Answering 0.0 would say this sample can
        detect no change at all, which is false and is the reading somebody
        would act on."""
        assert stats.detectable_difference(1.0, 600, direction="up") > 0
        assert stats.detectable_difference(0.0, 600, direction="down") > 0

    def test_no_cases_detects_nothing(self):
        assert stats.detectable_difference(0.245, 0) == 1.0

    @pytest.mark.parametrize("baseline", [0.0, 0.5, 1.0])
    def test_it_answers_at_the_edges_rather_than_dividing_by_zero(self, baseline):
        """A rate of 1.0 has no room above it and can still move down."""
        assert 0.0 < stats.detectable_difference(baseline, 600) < 1.0


class TestThePowerReport:
    def test_it_names_the_band_that_is_not_a_finding(self):
        report_ = stats.power_report(0.245, 600)
        assert report_["detectable_rate_lo"] < 0.245 < report_["detectable_rate_hi"]

    def test_each_edge_of_the_band_comes_from_its_own_direction(self):
        """The regression this was written for.

        The band is printed as "anything between X and Y is inside this
        sample's noise and must not be reported as a change", and its lower
        edge was a mirror of the upper one. On the shipped fixture that put it
        at 17.2% when the arithmetic says 17.9%, so the report called a
        detectable *fall* noise - and `sample_size`, asked about the very same
        rate, said 490 cases would have settled it. Two functions in one module
        contradicting each other about one number.

        Asserted as self-consistency rather than against pinned percentages, so
        it keeps meaning something if the fixture changes.
        """
        report_ = stats.power_report(0.245, 600)
        down = report_["detectable_difference_down"]
        up = report_["detectable_difference_up"]
        assert down < up
        # Each edge is its own direction's answer, not a mirror of the other's.
        # Asserted against the differences rather than against a case count: the
        # edges are rounded to four places for display, and pushing an exact
        # boundary through that rounding tests the rounding. The boundary itself
        # is proved on the unrounded numbers, in TestDetectableDifference above.
        assert report_["detectable_rate_lo"] == round(0.245 - down, 4)
        assert report_["detectable_rate_hi"] == round(0.245 + up, 4)
        # And the lower edge is above where mirroring would have put it, which
        # is the bug in one line: 17.9% rather than 17.2%.
        assert report_["detectable_rate_lo"] > round(0.245 - up, 4)

    def test_the_headline_number_is_the_larger_of_the_two(self):
        """One figure gets quoted on its own, so it has to be the one that is
        safe alone - the move this sample can detect whichever way it goes."""
        report_ = stats.power_report(0.245, 600)
        assert report_["detectable_difference"] == max(
            report_["detectable_difference_up"], report_["detectable_difference_down"])
        assert report_["power"] == 0.8

    def test_a_target_this_history_cannot_settle_says_how_far_short_it_is(self):
        report_ = stats.power_report(0.245, 600, target=0.20)
        assert report_["sufficient"] is False
        assert report_["shortfall"] == report_["cases_needed"] - 600
        text = stats.describe_power(report_)
        assert "cannot be settled on this history" in text

    def test_a_target_it_can_settle_says_so(self):
        report_ = stats.power_report(0.245, 50_000, target=0.20)
        assert report_["sufficient"] is True
        assert "this history has enough" in stats.describe_power(report_)

    def test_the_reading_always_states_the_sample_size(self):
        assert "600" in stats.describe_power(stats.power_report(0.245, 600))


class TestTheReadModel:
    def test_it_sizes_against_the_measured_flip_rate(self, replayed):
        result = report.power("expenses", "v2")
        assert result["measured"] is True
        assert result["cases"] == len(replayed["cases"])
        assert 0 < result["detectable_difference"] < 1
        assert "sampling error and nothing else" in result["caveat"]

    def test_it_says_unmeasured_rather_than_reporting_a_perfect_sample(self, fresh_db):
        """Zero cases detecting everything is the most misleading thing this
        could return, and it is what a bare arithmetic answer would give."""
        result = report.power("expenses", "v2")
        assert result["measured"] is False
        assert "nothing has been replayed" in result["hint"]

    def test_an_unknown_version_is_a_lookup_error_like_every_other_read_model(self):
        with pytest.raises(LookupError):
            report.power("expenses", "v99")

    def test_the_export_bundle_carries_it_with_a_caveat(self, replayed):
        bundle = report.export_bundle("expenses", "v2")
        assert bundle["power"]["measured"] is True
        assert any("power" in caveat for caveat in bundle["caveats"])


class TestTheCli:
    def test_it_prints_the_reading(self, replayed, capsys):
        assert report.main(["expenses", "v2", "--power"]) == 0
        out = capsys.readouterr().out
        assert "can detect a move of" in out
        assert "sampling error" in out

    def test_a_target_is_carried_through(self, replayed, capsys):
        assert report.main(["expenses", "v2", "--power", "--target", "0.20"]) == 0
        assert "needs" in capsys.readouterr().out

    def test_a_target_that_is_not_a_rate_is_refused(self, replayed, capsys):
        assert report.main(["expenses", "v2", "--power", "--target", "20"]) == 2
        assert "between 0 and 1" in capsys.readouterr().err

    def test_a_target_that_is_not_a_number_is_refused(self, replayed, capsys):
        assert report.main(["expenses", "v2", "--power", "--target", "soon"]) == 2
        assert "needs a rate" in capsys.readouterr().err


class TestTheFlagFixThatCameWithIt:
    def test_dash_o_with_nothing_after_it_is_an_error_not_a_dump(self, replayed, capsys):
        """It used to print the whole bundle to the terminal: `_flag` returns ""
        for a flag with no value, `or` collapsed that to None, and None means
        stdout. The one output built to *leave* the room, silently not leaving."""
        assert report.main(["expenses", "v2", "-o"]) == 2
        assert "needs a file" in capsys.readouterr().err

    def test_a_real_path_still_writes(self, replayed, tmp_path, capsys):
        target = tmp_path / "bundle.json"
        assert report.main(["expenses", "v2", "-o", str(target)]) == 0
        assert target.exists() and target.stat().st_size > 0
