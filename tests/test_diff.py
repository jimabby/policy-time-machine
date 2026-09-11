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


class TestReviewBudget:
    """Who gets the scarcest resource in the system.

    Deviations are reliably the largest flips by money, so ranking on impact
    alone hands them most of the queue - and the precedents that come back then
    fail the gate for a candidate that had nothing to do with them.
    """

    def flip(self, case_id, impact, attribution="clause 1.1", stability="",
             direction="loosening"):
        return diff.Flip(case_id=case_id, decided_at=datetime(2025, 1, 1),
                         actual_outcome="deny", new_outcome="approve", rationale="r",
                         confidence=0.9, policy_clause="1.1", impact=impact,
                         direction=direction, attribution=attribution,
                         stability=stability)

    def domain_with(self, expenses, **review):
        d = expenses.model_copy(deep=True)
        for key, value in review.items():
            setattr(d.review, key, value)
        return d

    def test_deviations_do_not_take_the_whole_queue(self, expenses):
        """The exact failure: the three biggest flips are all deviations."""
        flips = [self.flip(f"dev{i}", 10_000 - i, attribution=diff.DEVIATION)
                 for i in range(8)]
        flips += [self.flip(f"pol{i}", 100 - i) for i in range(8)]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=8))
        sent_deviations = [f for f in chosen if f.attribution == diff.DEVIATION]
        assert len(chosen) == 8
        assert len(sent_deviations) == expenses.review.max_deviation_reviews

    def test_deviations_still_get_their_reserved_slots(self, expenses):
        """Capping them is not the same as hiding them: a deviation ruling
        settles a case the policy already in force gets wrong."""
        flips = [self.flip(f"pol{i}", 1000 - i) for i in range(20)]
        flips += [self.flip("dev0", 5, attribution=diff.DEVIATION)]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=8))
        assert "dev0" in {f.case_id for f in chosen}

    def test_the_budget_is_filled_even_when_one_side_runs_short(self, expenses):
        flips = [self.flip(f"pol{i}", 1000 - i) for i in range(8)]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=8))
        assert len(chosen) == 8

    def test_a_zero_cap_keeps_deviations_out_entirely(self, expenses):
        flips = [self.flip(f"dev{i}", 10_000, attribution=diff.DEVIATION) for i in range(3)]
        flips += [self.flip(f"pol{i}", 10 - i) for i in range(3)]
        chosen = diff.select_for_review(
            flips, self.domain_with(expenses, max_reviews=8, max_deviation_reviews=0))
        assert all(f.attribution != diff.DEVIATION for f in chosen)

    def test_an_unconfirmed_flip_never_reaches_a_human(self, expenses):
        """Precedent is permanent; a verdict the judge will not reproduce must
        not be written into it."""
        flips = [self.flip("shaky", 9_999, stability="unstable"),
                 self.flip("solid", 10, stability="stable")]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=8))
        assert [f.case_id for f in chosen] == ["solid"]

    def test_an_unmeasured_flip_is_not_assumed_guilty(self, expenses):
        flips = [self.flip("never_measured", 500, stability="")]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=8))
        assert [f.case_id for f in chosen] == ["never_measured"]

    def test_the_exclusion_can_be_turned_off(self, expenses):
        flips = [self.flip("shaky", 9_999, stability="unstable")]
        chosen = diff.select_for_review(
            flips, self.domain_with(expenses, exclude_unstable=False))
        assert [f.case_id for f in chosen] == ["shaky"]

    def test_still_returns_the_highest_impact_first(self, expenses):
        flips = [self.flip("a", 10), self.flip("b", 900), self.flip("c", 50)]
        chosen = diff.select_for_review(flips, self.domain_with(expenses, max_reviews=3))
        assert [f.case_id for f in chosen] == ["b", "c", "a"]


class TestDeviationReport:
    def test_lists_only_flips_both_policies_agree_on(self, replayed):
        rows = diff.deviations(replayed["flips"])
        assert rows, "the fixture seeds reviewer deviation on purpose"
        assert all(f.attribution == diff.DEVIATION for f in rows)

    def test_is_ordered_by_money(self, replayed):
        impacts = [f.impact for f in diff.deviations(replayed["flips"])]
        assert impacts == sorted(impacts, reverse=True)

    def test_and_the_rest_are_the_proposal_s_own(self, replayed):
        total = len(replayed["flips"])
        assert len(diff.deviations(replayed["flips"])) < total


class TestCaseSegmentRows:
    def test_one_row_per_case_and_field(self, replayed):
        rows = diff.case_segment_rows(replayed["cases"], replayed["domain"])
        expected = len(replayed["cases"]) * len(replayed["domain"].segment_fields)
        assert len(rows) == expected

    def test_captures_the_point_in_time_value(self, replayed):
        """Segments on a slowly-changing fact are as of the decision date."""
        rows = diff.case_segment_rows(replayed["cases"], replayed["domain"])
        grades = {r["value"] for r in rows if r["field"] == "grade"}
        assert len(grades) > 1, "the fixture promotes people mid-period"

    def test_empty_without_declared_fields(self, replayed):
        bare = replayed["domain"].model_copy(deep=True)
        bare.segment_fields = []
        assert diff.case_segment_rows(replayed["cases"], bare) == []
