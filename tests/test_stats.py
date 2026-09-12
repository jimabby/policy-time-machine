"""Sampling error: the band that comes from the sample size and nothing else.

Three error bars get quoted around a flip rate in this project and they measure
different things - judge noise, sampling error, judge accuracy. The tests here
are mostly about keeping the second one from being read as either of the others,
and about the arithmetic behaving at the sample sizes that actually occur: a
monthly backfill run sees twenty-five cases, not six hundred.
"""

from __future__ import annotations

from ptm import diff, stats
from ptm.judge import offline_verdict


class TestTheInterval:
    def test_it_contains_the_rate_it_is_a_band_around(self):
        for successes, trials in ((0, 10), (1, 10), (5, 10), (9, 10), (10, 10), (147, 600)):
            lo, hi = stats.wilson_interval(successes, trials)
            assert lo <= successes / trials <= hi

    def test_nothing_measured_is_the_whole_range_not_a_confident_zero(self):
        """Zero of zero is not 0%. An empty interval would let an unreplayed
        month be quoted as a month in which nothing changed."""
        assert stats.wilson_interval(0, 0) == (0.0, 1.0)

    def test_it_never_leaves_zero_to_one(self):
        """The reason this is Wilson's interval and not the textbook one: at the
        small rates here the normal approximation's lower bound goes negative,
        and a negative lower bound on a count gets the whole number dismissed."""
        lo, hi = stats.wilson_interval(1, 200)
        assert lo >= 0.0
        assert stats.wilson_interval(200, 200)[1] <= 1.0

    def test_more_cases_means_a_tighter_band_at_the_same_rate(self):
        small = stats.rate(5, 20)
        large = stats.rate(150, 600)
        assert small["rate"] == large["rate"] == 0.25
        assert (small["rate_hi"] - small["rate_lo"]) > (large["rate_hi"] - large["rate_lo"])

    def test_the_band_travels_with_the_rate_as_one_object(self):
        band = stats.rate(147, 600)
        assert band["k"] == 147 and band["n"] == 600
        assert "24.5%" in stats.describe_rate(band)


class TestSeparation:
    """The test :mod:`ptm.disparity` uses before it says two groups differ."""

    def test_a_wide_looking_gap_on_a_tiny_sample_is_not_separation(self):
        """Four of twelve against 15% *looks* like more than twice the rate and
        is not evidence of anything. The test is deliberately conservative -
        stricter than a two-proportion z-test - because anything it flags is
        printed beside its two bands for a sceptical reader to check by eye."""
        assert not stats.separated(4, 12, 60, 400)

    def test_a_small_sample_can_still_separate_when_the_gap_is_enormous(self):
        """The interval is not a size cutoff and must not be mistaken for one.
        Excluding small segments is ``disparity.min_cases``'s job, and the two
        guards are separate on purpose."""
        assert stats.separated(9, 12, 60, 400)

    def test_a_real_difference_on_real_numbers_is_flagged(self):
        assert stats.separated(61, 116, 86, 484)

    def test_identical_rates_are_never_separated(self):
        assert not stats.separated(50, 100, 100, 200)


class TestTheReplaySummaryCarriesIt:
    def test_summarise_reports_the_band_around_its_own_flip_rate(self, replayed):
        summary = diff.summarise(replayed["flips"], len(replayed["cases"]),
                                 replayed["domain"])
        assert summary["flip_rate_lo"] <= summary["flip_rate"] <= summary["flip_rate_hi"]
        assert 0 < summary["flip_rate_lo"] < 1

    def test_a_month_sized_run_reports_a_visibly_wider_band(self, replayed):
        """The comparison the band exists to prevent: one month against two
        years, as though the difference between them were the policy."""
        domain = replayed["domain"]
        month = replayed["cases"][:25]
        verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in month}
        small = diff.summarise(diff.flips(month, verdicts, domain), len(month), domain)
        whole = diff.summarise(replayed["flips"], len(replayed["cases"]), domain)
        assert (small["flip_rate_hi"] - small["flip_rate_lo"]) > \
               (whole["flip_rate_hi"] - whole["flip_rate_lo"])

    def test_an_empty_replay_does_not_claim_a_zero_percent_flip_rate(self, expenses):
        summary = diff.summarise([], 0, expenses)
        assert summary["flip_rate"] == 0.0
        assert summary["flip_rate_hi"] == 1.0, "nothing measured is not nothing changed"
