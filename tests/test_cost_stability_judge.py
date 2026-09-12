"""The cost ledger, the judge's noise floor, and the offline judge itself."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from ptm import cost, stability
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Case


class TestCost:
    def test_known_model_uses_its_own_price(self):
        assert cost.price_for("anthropic:claude-sonnet-5") == (3.00, 15.00)

    def test_unknown_model_falls_back_rather_than_failing(self):
        assert cost.price_for("some:unreleased-model") == cost.DEFAULT_PRICE

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("PTM_PRICE", "1.50/6.00")
        assert cost.price_for("anthropic:claude-sonnet-5") == (1.50, 6.00)

    def test_a_malformed_override_does_not_break_a_replay(self, monkeypatch):
        monkeypatch.setenv("PTM_PRICE", "not-a-price")
        assert cost.price_for("anthropic:claude-sonnet-5") == (3.00, 15.00)

    def test_estimate_is_arithmetic_we_can_check_by_hand(self):
        # 4,000,000 chars / 4 = 1,000,000 input tokens at USD 3.00.
        result = cost.estimate(4_000_000, 1, "anthropic:claude-sonnet-5", response_chars=0)
        assert result["estimated_input_tokens"] == 1_000_000
        assert result["estimated_cost_usd"] == pytest.approx(3.00)

    def test_output_tokens_scale_with_request_count(self):
        one = cost.estimate(1000, 1, "anthropic:claude-sonnet-5")
        ten = cost.estimate(1000, 10, "anthropic:claude-sonnet-5")
        assert ten["estimated_output_tokens"] == 10 * one["estimated_output_tokens"]

    def test_offline_ledger_is_actually_zero(self):
        zero = cost.zero()
        assert zero["estimated_cost_usd"] == 0.0
        assert zero["judge_model"] == "offline"

    def test_backfill_forecast_scales_with_case_count(self):
        small = cost.estimate_backfill(100, 1400, 400, "anthropic:claude-sonnet-5")
        large = cost.estimate_backfill(1000, 1400, 400, "anthropic:claude-sonnet-5")
        assert large["estimated_cost_usd"] > small["estimated_cost_usd"] * 9

    def test_forecast_matches_a_measured_replay(self, replayed):
        """The forecast is only useful if it lands near what the real prompts cost."""
        domain = replayed["domain"]
        measured = cost.estimate(
            sum(len(build_prompt(c, domain, "v2")) for c in replayed["cases"]),
            len(replayed["cases"]), "anthropic:claude-sonnet-5")
        case_chars = sum(len(domain.render_case(c.payload))
                         for c in replayed["cases"]) // len(replayed["cases"])
        forecast = cost.estimate_backfill(len(replayed["cases"]),
                                          len(domain.policy_text("v2")), case_chars,
                                          "anthropic:claude-sonnet-5")
        ratio = forecast["estimated_cost_usd"] / measured["estimated_cost_usd"]
        assert 0.8 < ratio < 1.2, f"forecast is off by {ratio:.2f}x"


def case(case_id, day) -> Case:
    return Case(case_id=case_id, domain="expenses",
                decided_at=datetime(2025, 1, 1) + timedelta(days=day),
                payload={"amount_gbp": 10}, actual_outcome="approve")


class TestSampling:
    def test_is_deterministic_for_a_given_seed(self):
        cases = [case(f"c{i}", i) for i in range(200)]
        first = [c.case_id for c in stability.sample_cases(cases, 20, seed=7)]
        again = [c.case_id for c in stability.sample_cases(cases, 20, seed=7)]
        assert first == again

    def test_a_different_seed_gives_a_different_sample(self):
        cases = [case(f"c{i}", i) for i in range(200)]
        assert ([c.case_id for c in stability.sample_cases(cases, 20, seed=7)]
                != [c.case_id for c in stability.sample_cases(cases, 20, seed=8)])

    def test_spreads_across_the_period_rather_than_clustering(self):
        """A sample drawn from one month says nothing about the other 23."""
        cases = [case(f"c{i}", i) for i in range(240)]
        picked = stability.sample_cases(cases, 12, seed=7)
        span = max(c.decided_at for c in picked) - min(c.decided_at for c in picked)
        assert span > timedelta(days=180)

    def test_asking_for_more_than_exists_returns_everything(self):
        cases = [case(f"c{i}", i) for i in range(5)]
        assert len(stability.sample_cases(cases, 50)) == 5

    def test_zero_or_negative_returns_everything(self):
        cases = [case(f"c{i}", i) for i in range(5)]
        assert len(stability.sample_cases(cases, 0)) == 5

    def test_returns_the_requested_count(self):
        cases = [case(f"c{i}", i) for i in range(200)]
        assert len(stability.sample_cases(cases, 25)) == 25


def sample(case_id, idx, outcome, confidence=0.8):
    return {"case_id": case_id, "sample_idx": idx, "outcome": outcome,
            "confidence": confidence}


class TestAnalyse:
    def test_a_self_consistent_judge_scores_zero(self):
        samples = [sample("c1", i, "approve") for i in range(3)]
        report = stability.analyse(samples, 3)
        assert report.disagreement_rate == 0.0
        assert report.unstable_cases == 0

    def test_disagreement_is_counted_per_case(self):
        samples = ([sample("c1", 0, "approve"), sample("c1", 1, "deny"),
                    sample("c1", 2, "approve")]
                   + [sample("c2", i, "deny") for i in range(3)])
        report = stability.analyse(samples, 3)
        assert report.cases_sampled == 2
        assert report.unstable_cases == 1
        assert report.disagreement_rate == 0.5

    def test_agreement_reflects_the_modal_share(self):
        samples = [sample("c1", 0, "approve"), sample("c1", 1, "approve"),
                   sample("c1", 2, "deny"), sample("c1", 3, "deny")]
        [row] = stability.analyse(samples, 4).unstable
        assert row["agreement"] == 0.5
        assert row["samples"] == 4

    def test_confidence_spread_is_reported(self):
        samples = [sample("c1", 0, "approve", 0.95), sample("c1", 1, "deny", 0.35)]
        [row] = stability.analyse(samples, 2).unstable
        assert row["confidence_spread"] == pytest.approx(0.60)

    def test_worst_agreement_is_listed_first(self):
        samples = (
            [sample("steady", 0, "approve"), sample("steady", 1, "approve"),
             sample("steady", 2, "deny")]
            + [sample("coinflip", 0, "approve"), sample("coinflip", 1, "deny"),
               sample("coinflip", 2, "partial")]
        )
        report = stability.analyse(samples, 3)
        assert report.unstable[0]["case_id"] == "coinflip"

    def test_a_single_sample_cannot_be_unstable(self):
        """A judge task that failed must not be counted as a disagreement."""
        report = stability.analyse([sample("c1", 0, "approve")], 1)
        assert report.cases_sampled == 1
        assert report.unstable_cases == 0

    def test_no_samples_is_not_a_division_by_zero(self):
        report = stability.analyse([], 3)
        assert report.cases_sampled == 0
        assert report.disagreement_rate == 0.0

    def test_describe_says_the_check_is_inert_at_one_sample(self):
        text = stability.describe(stability.analyse([sample("c1", 0, "approve")], 1))
        assert "at least 2 samples" in text

    def test_describe_relates_noise_to_the_flip_rate(self):
        samples = [sample("c1", 0, "approve"), sample("c1", 1, "deny")]
        text = stability.describe(stability.analyse(samples, 2), flip_rate=0.25)
        assert "judge noise" in text

    def test_describe_handles_an_empty_report(self):
        assert "nothing to say" in stability.describe(stability.analyse([], 2))


class TestOfflineJudge:
    def test_returns_a_valid_outcome_for_every_shipped_case(self, seeded, expenses):
        from ptm import store

        cases = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=200)
        for c in cases:
            for version in ("v1", "v2"):
                verdict = offline_verdict(c, expenses, version)
                assert verdict.outcome in expenses.outcomes
                assert 0.0 <= verdict.confidence <= 1.0

    def test_is_deterministic(self, seeded, expenses):
        from ptm import store

        [c] = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=1)
        assert (offline_verdict(c, expenses, "v2").model_dump()
                == offline_verdict(c, expenses, "v2").model_dump())

    def test_a_rule_referencing_a_missing_field_is_skipped_not_fatal(self, expenses):
        """The behaviour that makes ptm.lint necessary, pinned deliberately."""
        rigged = expenses.model_copy(deep=True)
        rigged.offline_rules = {"v2": [
            {"when": "field_that_does_not_exist > 1", "outcome": "deny", "clause": "1.1"},
            {"when": "amount_gbp > 5", "outcome": "partial", "clause": "2.1"},
        ]}
        verdict = offline_verdict(case("c1", 0), rigged, "v2")
        assert verdict.outcome == "partial", "the broken rule must not match, nor crash"

    def test_falls_back_to_the_most_generous_outcome(self, expenses):
        bare = expenses.model_copy(deep=True)
        bare.offline_rules = {"v2": []}
        assert offline_verdict(case("c1", 0), bare, "v2").outcome == bare.outcomes[0]

    def test_the_prompt_carries_the_policy_and_the_decision_date(self, seeded, expenses):
        from ptm import store

        [c] = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=1)
        prompt = build_prompt(c, expenses, "v2")
        assert "GBP 75" in prompt, "the v2 receipt threshold"
        assert c.decided_at.date().isoformat() in prompt
        for outcome in expenses.outcomes:
            assert outcome in prompt


class TestFlipConfirmation:
    """An error bar on one flip, rather than on the whole replay.

    The aggregate disagreement rate says how noisy the judge is. It does not
    say whether *this* flip - the one a human is about to turn into permanent
    precedent - is real.
    """

    def samples(self, case_id, *outcomes):
        return [{"case_id": case_id, "sample_idx": i, "outcome": o, "confidence": 0.9}
                for i, o in enumerate(outcomes)]

    def test_a_flip_the_judge_repeats_is_confirmed(self):
        [c] = stability.confirm(self.samples("c1", "approve", "approve", "approve"),
                                {"c1": "approve"})
        assert c.stable is True and c.agreement == 1.0

    def test_a_flip_the_judge_will_not_repeat_is_not(self):
        [c] = stability.confirm(self.samples("c1", "approve", "deny", "approve"),
                                {"c1": "approve"})
        assert c.stable is False
        assert c.outcomes == {"approve": 2, "deny": 1}
        assert c.modal_outcome == "approve"

    def test_self_consistent_but_contradicting_the_replay_is_not_confirmation(self):
        """A judge can be perfectly repeatable and still disagree with the run
        that recorded the flip. Calling that confirmed would launder a
        contradiction into a precedent."""
        [c] = stability.confirm(self.samples("c1", "deny", "deny", "deny"),
                                {"c1": "approve"})
        assert c.stable is False
        assert c.modal_outcome == "deny" and c.recorded_outcome == "approve"

    def test_reports_the_least_trustworthy_first(self):
        rows = stability.confirm(
            self.samples("solid", "approve", "approve")
            + self.samples("shaky", "approve", "deny"),
            {"solid": "approve", "shaky": "approve"})
        assert [c.case_id for c in rows] == ["shaky", "solid"]

    def test_an_unrecorded_case_is_judged_on_self_consistency_alone(self):
        [c] = stability.confirm(self.samples("c1", "deny", "deny"), {})
        assert c.stable is True

    def test_nothing_to_confirm_says_so(self):
        assert "no flips re-judged" in stability.describe_confirmations([])

    def test_describes_what_was_held_back(self):
        rows = stability.confirm(self.samples("c1", "approve", "deny"), {"c1": "approve"})
        text = stability.describe_confirmations(rows)
        assert "1 did not" in text and "c1" in text
        assert "held back from the human queue" in text

    def test_describes_a_clean_pass(self):
        rows = stability.confirm(self.samples("c1", "approve", "approve"), {"c1": "approve"})
        assert "every flip reproduced" in stability.describe_confirmations(rows)


class TestChoosingWhichFlipsToConfirm:
    def flip(self, case_id, impact):
        from ptm.models import Flip

        return Flip(case_id=case_id, decided_at=datetime(2025, 1, 1),
                    actual_outcome="deny", new_outcome="approve", rationale="r",
                    confidence=0.9, policy_clause="1.1", impact=impact)

    def test_spends_the_budget_on_the_flips_that_will_be_acted_on(self):
        flips = [self.flip("small", 10), self.flip("big", 900), self.flip("mid", 100)]
        assert [f.case_id for f in stability.flips_to_confirm(flips, 2)] == ["big", "mid"]

    def test_zero_means_all_of_them(self):
        flips = [self.flip("a", 1), self.flip("b", 2)]
        assert len(stability.flips_to_confirm(flips, 0)) == 2

    def test_asking_for_more_than_exist_is_not_an_error(self):
        assert len(stability.flips_to_confirm([self.flip("a", 1)], 50)) == 1
