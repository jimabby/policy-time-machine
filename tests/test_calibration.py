"""Scoring the judge against the humans who ruled - the one accuracy number here.

Everything else measures the judge against itself. These tests are about the
three ways that scoring could quietly lie: counting a case nobody judged as
agreement, reporting confidence as meaningful when it predicts nothing, and
presenting a floor measured on the hardest cases as an overall accuracy.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import calibration, report, store
from ptm.models import Precedent, Verdict


def precedent(case_id: str, outcome: str, who: str = "finance.lead") -> Precedent:
    return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                     ruled_by=who, established_at=datetime(2026, 1, 1))


def verdict(outcome: str, confidence: float = 0.9, clause: str = "1.1") -> Verdict:
    return Verdict(outcome=outcome, rationale="because", confidence=confidence,
                   policy_clause=clause)


class TestScoring:
    def test_it_counts_agreement_with_the_human_not_with_the_recorded_outcome(self, expenses):
        result = calibration.score(
            expenses, "v2",
            {"a": verdict("approve"), "b": verdict("deny"), "c": verdict("partial")},
            [precedent("a", "approve"), precedent("b", "approve"), precedent("c", "partial")])
        assert result.judged == 3
        assert result.agreed == 2
        assert result.accuracy == pytest.approx(2 / 3, abs=0.001)

    def test_a_precedent_with_no_verdict_is_unjudged_rather_than_agreed(self, expenses):
        """The exact failure the gate exists to prevent, in reporting form. A
        precedent nobody judged counted as agreement would make an unrun check
        look like a passed one."""
        result = calibration.score(expenses, "v2", {"a": verdict("approve")},
                                   [precedent("a", "approve"), precedent("never-judged", "deny")])
        assert result.judged == 1
        assert result.unjudged == ["never-judged"]
        assert result.accuracy == 1.0, "the unjudged case is out of the denominator too"

    def test_the_band_widens_the_smaller_the_precedent_set(self, expenses):
        """Eight rulings is a small sample and the accuracy has to say so, or a
        judge scored on one lucky case reads as a judge that is right."""
        result = calibration.score(expenses, "v2", {"a": verdict("approve")},
                                   [precedent("a", "approve")])
        assert result.accuracy == 1.0
        assert result.accuracy_lo < 0.3, "one case is not evidence of anything"


class TestConfidence:
    def test_being_confident_and_wrong_is_reported_as_overconfidence(self, expenses):
        result = calibration.score(
            expenses, "v2",
            {"a": verdict("deny", 0.95), "b": verdict("deny", 0.95)},
            [precedent("a", "approve"), precedent("b", "deny")])
        assert result.accuracy == 0.5
        assert result.overconfidence > 0.4
        assert result.expected_calibration_error > 0.4

    def test_buckets_group_by_the_confidence_the_judge_claimed(self, expenses):
        result = calibration.score(
            expenses, "v2",
            {"a": verdict("approve", 0.95), "b": verdict("deny", 0.5)},
            [precedent("a", "approve"), precedent("b", "deny")])
        assert [b.n for b in result.buckets] == [1, 1]
        assert all(b.accuracy == 1.0 for b in result.buckets)

    def test_a_threshold_that_does_not_predict_correctness_is_called_out(self, expenses):
        """``review.below_confidence`` routes the review budget on the judge's
        own claim about itself. If that claim predicts nothing, the routing is
        random and the panel has to say so rather than print a number."""
        verdicts, precedents = {}, []
        for i in range(10):  # right half the time on each side of the threshold
            verdicts[f"hi{i}"] = verdict("approve", 0.9)
            verdicts[f"lo{i}"] = verdict("approve", 0.5)
            precedents.append(precedent(f"hi{i}", "approve" if i % 2 else "deny"))
            precedents.append(precedent(f"lo{i}", "approve" if i % 2 else "deny"))
        result = calibration.score(expenses, "v2", verdicts, precedents)
        assert not result.threshold_separates
        assert "does NOT separate" in calibration.describe(result)

    def test_a_threshold_that_does_predict_correctness_is_credited(self, expenses):
        verdicts, precedents = {}, []
        for i in range(30):
            verdicts[f"hi{i}"] = verdict("approve", 0.95)
            precedents.append(precedent(f"hi{i}", "approve"))
        for i in range(30):
            verdicts[f"lo{i}"] = verdict("approve", 0.5)
            precedents.append(precedent(f"lo{i}", "deny"))
        result = calibration.score(expenses, "v2", verdicts, precedents)
        assert result.threshold_separates


class TestWhereItGoesWrong:
    def test_errors_are_grouped_by_the_pair_of_outcomes(self, expenses):
        """Error concentrated in one outcome is a prompt problem with a fix.
        Error spread evenly is a judge that does not understand the policy."""
        result = calibration.score(
            expenses, "v2",
            {"a": verdict("deny"), "b": verdict("deny"), "c": verdict("approve")},
            [precedent("a", "approve"), precedent("b", "approve"), precedent("c", "approve")])
        assert result.confusion[0]["ruled"] == "approve"
        assert result.confusion[0]["judged"] == "deny"
        assert result.confusion[0]["n"] == 2
        assert result.confusion[0]["case_ids"] == ["a", "b"]


class TestTheReadModel:
    def test_it_says_nothing_is_measured_rather_than_reporting_success(self, fresh_db, expenses):
        """A calibration panel showing 100% from zero cases is the most
        misleading thing this API could return."""
        result = report.calibration("expenses", "v2")
        assert result["measured"] is False
        assert "no human rulings" in result["hint"]

    def test_it_scores_the_verdicts_already_on_file(self, replayed):
        store.save_precedent(precedent("exp-0001", "approve"))
        result = report.calibration("expenses", "v2")
        # exp-0001 was judged by the replay fixture, so there is something to score.
        assert result["measured"] is True
        assert result["judged"] >= 1
        assert "floor" in result["caveat"]

    def test_the_summary_always_repeats_the_sampling_caveat(self, expenses):
        result = calibration.score(expenses, "v2", {"a": verdict("approve")},
                                   [precedent("a", "approve")])
        assert "floor on the judge's accuracy" in calibration.describe(result)
