"""Flip detection, review selection, and the precedent gate."""

from __future__ import annotations

from datetime import datetime

from conftest import case
from ptm import diff
from ptm.models import Flip, Precedent, Verdict


def v(outcome, confidence=0.9, clause="1.1"):
    return Verdict(outcome=outcome, rationale="because", confidence=confidence, policy_clause=clause)


def flip(case_id="c1", impact=100.0, confidence=0.9, direction="loosening", clause="1.1"):
    return Flip(case_id=case_id, decided_at=datetime(2025, 6, 1), actual_outcome="deny",
                new_outcome="approve", rationale="r", confidence=confidence,
                policy_clause=clause, impact=impact, direction=direction)


# ------------------------------------------------------------------- flips


def test_agreement_is_not_a_flip(expenses):
    cases = [case("c1", outcome="approve")]
    assert diff.flips(cases, {"c1": v("approve")}, expenses) == []


def test_disagreement_is_a_flip_with_direction_and_impact(expenses):
    cases = [case("c1", payload={"amount_gbp": 250}, outcome="deny")]
    (f,) = diff.flips(cases, {"c1": v("approve")}, expenses)
    assert f.new_outcome == "approve"
    assert f.direction == "loosening", "deny -> approve is more generous"
    assert f.impact == 250.0
    assert f.policy_clause == "1.1"


def test_a_case_with_no_verdict_is_skipped_not_flipped(expenses):
    assert diff.flips([case("c1")], {}, expenses) == []


def test_direction_uses_the_domains_outcome_ordering(expenses):
    assert expenses.direction("deny", "approve") == "loosening"
    assert expenses.direction("approve", "deny") == "tightening"
    assert expenses.direction("approve", "approve") == "lateral"
    assert expenses.direction("approve", "nonsense") == "lateral", "unknown outcomes cannot be ranked"


def test_direction_is_domain_agnostic(refunds):
    assert refunds.direction("no_refund", "full_refund") == "loosening"
    assert refunds.direction("full_refund", "partial_refund") == "tightening"


# --------------------------------------------------------- review selection


def test_low_confidence_flips_survive_a_crowd_of_expensive_ones(expenses):
    """Regression: sorting the pool by impact alone starved the ambiguous cases.

    An ambiguous case is the more useful precedent - it settles the drafting,
    not just the invoice - so it must not be crowded out by big-ticket claims.
    """
    costly = [flip(f"big-{i}", impact=10_000 + i, confidence=0.99, direction="tightening")
              for i in range(20)]
    unsure = flip("unsure", impact=12.0, confidence=0.30)
    picked = {f.case_id for f in diff.select_for_review(costly + [unsure], expenses)}
    assert "unsure" in picked


def test_selection_respects_max_reviews(expenses):
    many = [flip(f"c{i}", impact=float(i)) for i in range(50)]
    assert len(diff.select_for_review(many, expenses)) == expenses.review.max_reviews


def test_selection_never_repeats_a_case(expenses):
    # Qualifies on all three grounds at once.
    both = flip("c1", impact=100_000.0, confidence=0.1, direction="loosening")
    picked = diff.select_for_review([both], expenses)
    assert [f.case_id for f in picked] == ["c1"]


def test_uninteresting_flips_are_not_reviewed(expenses):
    """High confidence, small money, and tightening: nobody needs to see it."""
    dull = flip("dull", impact=1.0, confidence=0.99, direction="tightening")
    assert diff.select_for_review([dull], expenses) == []


def test_loosening_is_always_reviewable(expenses):
    quiet = flip("loose", impact=1.0, confidence=0.99, direction="loosening")
    assert [f.case_id for f in diff.select_for_review([quiet], expenses)] == ["loose"]


def test_selection_prefers_bigger_money_within_a_ground(expenses):
    small = flip("small", impact=500.0, confidence=0.2)
    large = flip("large", impact=900.0, confidence=0.2)
    picked = diff.select_for_review([small, large], expenses)
    assert picked[0].case_id == "large"


# ------------------------------------------------------------ precedent gate


def precedent(case_id="c1", outcome="approve"):
    return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                     ruled_by="finance.lead", note="n", established_at=datetime(2026, 1, 1))


def test_agreeing_with_precedent_is_not_a_violation():
    assert diff.precedent_violations({"c1": v("approve")}, [precedent("c1", "approve")]) == []


def test_reversing_a_human_ruling_is_a_violation():
    (x,) = diff.precedent_violations({"c1": v("deny")}, [precedent("c1", "approve")])
    assert x["established_outcome"] == "approve"
    assert x["proposed_outcome"] == "deny"
    assert x["ruled_by"] == "finance.lead"


def test_cases_without_precedent_are_ignored():
    assert diff.precedent_violations({"other": v("deny")}, [precedent("c1", "approve")]) == []


# ---------------------------------------------------------------- summarise


def test_summarise_nets_loosening_against_tightening(expenses):
    flips_ = [
        flip("a", impact=100.0, direction="loosening"),
        flip("b", impact=40.0, direction="loosening"),
        flip("c", impact=30.0, direction="tightening"),
    ]
    s = diff.summarise(flips_, total=10, domain=expenses)
    assert s["flips"] == 3
    assert s["flip_rate"] == 0.3
    assert s["impact_loosening"] == 140.0
    assert s["impact_tightening"] == 30.0
    assert s["net_impact"] == 110.0
    assert s["impact_unit"] == "GBP"


def test_summarise_survives_an_empty_population(expenses):
    s = diff.summarise([], total=0, domain=expenses)
    assert s["flip_rate"] == 0.0 and s["net_impact"] == 0.0
