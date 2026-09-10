"""The diffing, attribution, review-selection and precedent logic."""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import diff
from ptm.models import Case, Precedent, Verdict


def case(case_id="c1", outcome="deny", **payload) -> Case:
    payload.setdefault("amount_gbp", 100)
    return Case(case_id=case_id, domain="expenses", decided_at=datetime(2025, 1, 1),
                payload=payload, actual_outcome=outcome)


def verdict(outcome="approve", clause="", confidence=0.9) -> Verdict:
    return Verdict(outcome=outcome, rationale="because", confidence=confidence,
                   policy_clause=clause)


# ------------------------------------------------------------- attribution
class TestAttribute:
    def test_candidate_clause_wins_when_policies_differ(self):
        assert diff.attribute(verdict("approve", "2.1"), verdict("deny", "1.1")) == "clause 2.1"

    def test_relaxed_clause_is_credited_when_candidate_cites_nothing(self):
        """The common case: a restriction stops firing, so nothing is cited."""
        assert diff.attribute(verdict("approve", ""), verdict("deny", "1.1")) == "clause 1.1 relaxed"

    def test_agreeing_policies_mean_the_reviewer_deviated(self):
        assert diff.attribute(verdict("deny", "5.1"), verdict("deny", "5.1")) == diff.DEVIATION

    def test_deviation_beats_a_cited_clause(self):
        """Order matters: if both policies agree, the proposal changed nothing here.

        Crediting the clause the candidate cites on its way to the same answer
        would blame the new policy for an outcome the old one also rejected.
        """
        assert diff.attribute(verdict("deny", "5.1"), verdict("deny", "")) == diff.DEVIATION

    def test_unexplained_without_a_baseline(self):
        assert diff.attribute(verdict("approve", ""), None) == diff.UNEXPLAINED

    def test_clause_survives_without_a_baseline(self):
        assert diff.attribute(verdict("approve", "3.1"), None) == "clause 3.1"


# -------------------------------------------------------------------- flips
class TestFlips:
    def test_agreement_is_not_a_flip(self, expenses):
        cases = [case("c1", "approve")]
        assert diff.flips(cases, {"c1": verdict("approve")}, expenses) == []

    def test_missing_verdict_is_not_a_flip(self, expenses):
        assert diff.flips([case("c1", "approve")], {}, expenses) == []

    def test_direction_and_impact_come_from_the_domain(self, expenses):
        [flip] = diff.flips([case("c1", "deny", amount_gbp=250)],
                            {"c1": verdict("approve")}, expenses)
        assert flip.direction == "loosening"
        assert flip.impact == 250

    def test_tightening_is_the_other_direction(self, expenses):
        [flip] = diff.flips([case("c1", "approve")], {"c1": verdict("deny")}, expenses)
        assert flip.direction == "tightening"

    def test_segments_are_captured_from_the_hydrated_payload(self, expenses):
        """grade is a point-in-time fact, so it must be read at decision time."""
        [flip] = diff.flips([case("c1", "deny", category="travel", grade=3)],
                            {"c1": verdict("approve")}, expenses)
        assert flip.segments == {"category": "travel", "grade": "3"}

    def test_baseline_outcome_is_recorded(self, expenses):
        [flip] = diff.flips([case("c1", "deny")], {"c1": verdict("approve")}, expenses,
                            baseline={"c1": verdict("partial", "9.9")})
        assert flip.baseline_outcome == "partial"
        assert flip.attribution == "clause 9.9 relaxed"


# ---------------------------------------------------------- review selection
class TestSelectForReview:
    def test_respects_max_reviews(self, expenses):
        flips = diff.flips(
            [case(f"c{i}", "deny", amount_gbp=1000 + i) for i in range(40)],
            {f"c{i}": verdict("approve") for i in range(40)}, expenses)
        assert len(diff.select_for_review(flips, expenses)) == expenses.review.max_reviews

    def test_orders_by_impact_descending(self, expenses):
        flips = diff.flips(
            [case("small", "deny", amount_gbp=10), case("big", "deny", amount_gbp=9000)],
            {"small": verdict("approve"), "big": verdict("approve")}, expenses)
        assert [f.case_id for f in diff.select_for_review(flips, expenses)] == ["big", "small"]

    def test_confident_low_impact_tightening_is_not_reviewed(self, expenses):
        """The selector exists to spend humans only where they are needed."""
        flips = diff.flips([case("c1", "approve", amount_gbp=5)],
                           {"c1": verdict("deny", confidence=0.99)}, expenses)
        assert diff.select_for_review(flips, expenses) == []

    def test_low_confidence_is_reviewed_even_when_cheap(self, expenses):
        flips = diff.flips([case("c1", "approve", amount_gbp=5)],
                           {"c1": verdict("deny", confidence=0.10)}, expenses)
        assert len(diff.select_for_review(flips, expenses)) == 1

    def test_loosening_is_always_reviewed(self, expenses):
        flips = diff.flips([case("c1", "deny", amount_gbp=1)],
                           {"c1": verdict("approve", confidence=1.0)}, expenses)
        assert len(diff.select_for_review(flips, expenses)) == 1


# ------------------------------------------------------- precedent violations
class TestPrecedentViolations:
    def precedent(self, case_id="c1", outcome="approve"):
        return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                         ruled_by="finance.lead", established_at=datetime(2025, 6, 1))

    def test_contradiction_is_a_violation(self):
        [v] = diff.precedent_violations({"c1": verdict("deny")}, [self.precedent()])
        assert v["established_outcome"] == "approve"
        assert v["proposed_outcome"] == "deny"

    def test_agreement_is_not(self):
        assert diff.precedent_violations({"c1": verdict("approve")}, [self.precedent()]) == []

    def test_cases_without_a_precedent_are_ignored(self):
        assert diff.precedent_violations({"other": verdict("deny")}, [self.precedent()]) == []


# -------------------------------------------------------- precedent conflicts
class TestPrecedentConflicts:
    def row(self, case_id, outcome, ruled_by="a.reviewer", **payload):
        payload.setdefault("category", "travel")
        payload.setdefault("receipt", "no")
        payload.setdefault("director_approval", "no")
        payload.setdefault("amount_gbp", 100)
        return {"case_id": case_id, "correct_outcome": outcome, "ruled_by": ruled_by,
                "payload": payload}

    def test_identical_cases_ruled_differently_conflict(self, expenses):
        [conflict] = diff.precedent_conflicts(
            [self.row("c1", "approve", "alice"), self.row("c2", "deny", "bob")], expenses)
        assert conflict.outcomes == {"approve": ["c1"], "deny": ["c2"]}
        assert conflict.ruled_by == ["alice", "bob"]

    def test_identical_cases_ruled_alike_do_not(self, expenses):
        assert diff.precedent_conflicts(
            [self.row("c1", "approve"), self.row("c2", "approve")], expenses) == []

    def test_different_cases_do_not_conflict(self, expenses):
        assert diff.precedent_conflicts(
            [self.row("c1", "approve", category="travel"),
             self.row("c2", "deny", category="meals")], expenses) == []

    def test_impact_is_banded_so_near_identical_amounts_still_match(self, expenses):
        """GBP 104 and GBP 111 are the same kind of claim; GBP 900 is not."""
        assert len(diff.precedent_conflicts(
            [self.row("c1", "approve", amount_gbp=104),
             self.row("c2", "deny", amount_gbp=111)], expenses)) == 1
        assert diff.precedent_conflicts(
            [self.row("c1", "approve", amount_gbp=104),
             self.row("c2", "deny", amount_gbp=900)], expenses) == []

    def test_disabled_when_the_domain_declares_no_key(self, expenses):
        bare = expenses.model_copy(deep=True)
        bare.conflicts.key = []
        assert diff.precedent_conflicts(
            [self.row("c1", "approve"), self.row("c2", "deny")], bare) == []

    def test_widest_disagreement_is_reported_first(self, expenses):
        conflicts = diff.precedent_conflicts([
            self.row("a1", "approve", category="meals"),
            self.row("a2", "deny", category="meals"),
            self.row("b1", "approve", category="travel"),
            self.row("b2", "deny", category="travel"),
            self.row("b3", "partial", category="travel"),
        ], expenses)
        assert len(conflicts[0].outcomes) == 3


# ------------------------------------------------------------- aggregations
class TestAggregations:
    def test_clause_attribution_shares_sum_to_one(self, replayed):
        rows = diff.clause_attribution(replayed["flips"], replayed["domain"])
        assert sum(r["flips"] for r in rows) == len(replayed["flips"])
        assert pytest.approx(sum(r["share"] for r in rows), abs=1e-3) == 1.0

    def test_a_baseline_pass_leaves_nothing_unexplained(self, replayed):
        """The whole point of judging both sides: every change gets a reason."""
        rows = diff.clause_attribution(replayed["flips"], replayed["domain"])
        assert diff.UNEXPLAINED not in {r["clause"] for r in rows}

    def test_without_a_baseline_most_changes_are_unexplained(self, replayed):
        """The regression this feature exists to fix, asserted rather than assumed."""
        blind = diff.flips(replayed["cases"], replayed["candidate"], replayed["domain"])
        rows = diff.clause_attribution(blind, replayed["domain"])
        unexplained = next(r for r in rows if r["clause"] == diff.UNEXPLAINED)
        assert unexplained["share"] > 0.5

    def test_deviations_are_excluded_from_policy_driven_totals(self, replayed):
        summary = diff.summarise(replayed["flips"], len(replayed["cases"]), replayed["domain"])
        assert summary["deviation_flips"] > 0
        assert summary["policy_driven_flips"] + summary["deviation_flips"] == summary["flips"]
        assert summary["policy_driven_flips"] < summary["flips"]

    def test_segment_denominators_cover_every_replayed_case(self, replayed):
        rows = diff.segment_stats(replayed["cases"], replayed["flips"], replayed["domain"])
        for field in replayed["domain"].segment_fields:
            assert sum(r["cases"] for r in rows if r["field"] == field) == len(replayed["cases"])

    def test_segment_flips_reconcile_with_the_flip_list(self, replayed):
        rows = diff.segment_stats(replayed["cases"], replayed["flips"], replayed["domain"])
        for field in replayed["domain"].segment_fields:
            assert sum(r["flips"] for r in rows if r["field"] == field) == len(replayed["flips"])

    def test_segment_stats_empty_without_declared_fields(self, replayed):
        bare = replayed["domain"].model_copy(deep=True)
        bare.segment_fields = []
        assert diff.segment_stats(replayed["cases"], replayed["flips"], bare) == []

    def test_summarise_handles_no_cases(self, expenses):
        summary = diff.summarise([], 0, expenses)
        assert summary["flip_rate"] == 0.0
        assert summary["flips"] == 0
