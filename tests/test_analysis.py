"""Coverage, cohorts and comparison - the arithmetic the model only explains."""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import insert_case, insert_fact
from ptm import analysis, store
from ptm.models import Flip, Precedent, Verdict


def judged(db, case_id, outcome="approve", clause="1.1", version="v2", run="r1"):
    db.save_verdicts(run, "expenses", version,
                     {case_id: Verdict(outcome=outcome, rationale="r", confidence=0.9,
                                       policy_clause=clause)})


def flipped(db, case_id, impact=100.0, direction="loosening", clause="1.1",
            version="v2", run="r1"):
    db.save_flips(run, "expenses", version, [
        Flip(case_id=case_id, decided_at=datetime(2025, 6, 1), actual_outcome="deny",
             new_outcome="approve", rationale="r", confidence=0.9, policy_clause=clause,
             impact=impact, direction=direction)])


# ------------------------------------------------------------------ coverage


def test_coverage_counts_every_declared_clause(db, expenses):
    db.record_run("r1", "expenses", "v2", "actual", 2, 0, 0)
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    insert_case(db, "c2", "2025-06-02T10:00:00", {}, "deny")
    judged(db, "c1", clause="1.1")
    judged(db, "c2", clause="1.1")

    cov = analysis.clause_coverage(expenses, "v2")
    assert cov["declared"] == len(expenses.declared_clauses("v2"))
    assert cov["exercised"] == 1
    by_clause = {c["clause"]: c for c in cov["clauses"]}
    assert by_clause["1.1"]["cases"] == 2
    assert by_clause["1.1"]["exercised"] is True
    assert by_clause["2.1"]["exercised"] is False
    assert "2.1" in cov["unexercised"]


def test_coverage_reports_cases_no_clause_decided(db, expenses):
    """A fall-through is the policy failing to reach its own population."""
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    judged(db, "c1", clause="")
    cov = analysis.clause_coverage(expenses, "v2")
    assert cov["decided_by_no_clause"] == 1
    assert cov["no_clause_share"] == 1.0
    assert cov["exercised"] == 0


def test_coverage_flags_a_clause_the_policy_never_declared(db, expenses):
    """The judge citing a clause the text lacks means the two have drifted."""
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    judged(db, "c1", clause="99.9")
    cov = analysis.clause_coverage(expenses, "v2")
    assert cov["undeclared"] == [{"clause": "99.9", "cases": 1}]


def test_coverage_of_an_unreplayed_policy_is_all_zero(db, expenses):
    cov = analysis.clause_coverage(expenses, "v2")
    assert cov["cases"] == 0 and cov["exercised"] == 0
    assert cov["unexercised"] == expenses.declared_clauses("v2")


def test_coverage_counts_a_case_once_across_overlapping_runs(db, expenses):
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    judged(db, "c1", clause="1.1", run="r1")
    judged(db, "c1", clause="1.1", run="r2")
    cov = analysis.clause_coverage(expenses, "v2")
    assert cov["cases"] == 1
    assert {c["clause"]: c["cases"] for c in cov["clauses"]}["1.1"] == 1


# ------------------------------------------------------------------- cohorts


def _population(db):
    """Ten cases: five 'meals' that all flip, five 'travel' that never do."""
    db.record_run("r1", "expenses", "v2", "actual", 10, 5, 500.0)
    for i in range(5):
        insert_case(db, f"m{i}", f"2025-06-0{i + 1}T10:00:00",
                    {"category": "meals", "amount_gbp": 100}, "deny")
        judged(db, f"m{i}")
        flipped(db, f"m{i}", impact=100.0)
    for i in range(5):
        insert_case(db, f"t{i}", f"2025-07-0{i + 1}T10:00:00",
                    {"category": "travel", "amount_gbp": 50}, "approve")
        judged(db, f"t{i}")


def test_cohorts_split_the_change_by_attribute(db, expenses):
    _population(db)
    out = analysis.cohort_impact(expenses, "v2", "category")
    by = {c["cohort"]: c for c in out["cohorts"]}
    assert by["meals"]["cases"] == 5 and by["meals"]["flips"] == 5
    assert by["meals"]["flip_rate"] == 1.0
    assert by["travel"]["flips"] == 0
    assert out["baseline_flip_rate"] == 0.5


def test_disproportion_is_relative_to_the_population(db, expenses):
    _population(db)
    by = {c["cohort"]: c for c in analysis.cohort_impact(expenses, "v2", "category")["cohorts"]}
    assert by["meals"]["disproportion"] == 2.0, "twice the population rate"
    assert by["travel"]["disproportion"] == 0.0


def test_tightening_counts_against_a_cohorts_net_impact(db, expenses):
    db.record_run("r1", "expenses", "v2", "actual", 2, 2, 0)
    insert_case(db, "a", "2025-06-01T10:00:00", {"category": "meals"}, "deny")
    insert_case(db, "b", "2025-06-02T10:00:00", {"category": "meals"}, "approve")
    judged(db, "a"); judged(db, "b")
    flipped(db, "a", impact=100.0, direction="loosening")
    flipped(db, "b", impact=40.0, direction="tightening", run="r2")
    cats = {c["cohort"]: c for c in analysis.cohort_impact(expenses, "v2", "category")["cohorts"]}
    assert cats["meals"]["net_impact"] == 60.0, "100 loosening less 40 tightening"


def test_a_field_no_case_carries_is_one_unknown_cohort(db, expenses):
    db.record_run("r1", "expenses", "v2", "actual", 1, 0, 0)
    insert_case(db, "a", "2025-06-01T10:00:00", {"category": "meals"}, "deny")
    judged(db, "a")
    (row,) = analysis.cohort_impact(expenses, "v2", "no_such_field")["cohorts"]
    assert row["cohort"] == "unknown", "a missing field is a cohort, not an error"
    assert row["cases"] == 1


def test_cohorts_use_the_point_in_time_value(db, expenses):
    """An employee promoted later must be counted in the grade they then held."""
    insert_fact(db, "emp-1", "grade", "2", "2024-01-01T00:00:00")
    insert_fact(db, "emp-1", "grade", "3", "2025-07-01T00:00:00")
    insert_case(db, "before", "2025-06-01T10:00:00", {}, "deny", "emp-1")
    insert_case(db, "after", "2025-08-01T10:00:00", {}, "deny", "emp-1")
    db.record_run("r1", "expenses", "v2", "actual", 2, 0, 0)
    judged(db, "before"); judged(db, "after")

    by = {c["cohort"]: c for c in analysis.cohort_impact(expenses, "v2", "grade")["cohorts"]}
    assert by["2"]["cases"] == 1 and by["3"]["cases"] == 1


def test_cohorts_ignore_cases_this_policy_never_judged(db, expenses):
    insert_case(db, "judged", "2025-06-01T10:00:00", {"category": "meals"}, "deny")
    insert_case(db, "not_judged", "2025-06-02T10:00:00", {"category": "meals"}, "deny")
    db.record_run("r1", "expenses", "v2", "actual", 1, 0, 0)
    judged(db, "judged")
    out = analysis.cohort_impact(expenses, "v2", "category")
    assert out["cases"] == 1, "an unreplayed case is not evidence either way"


def test_disproportionate_ignores_tiny_cohorts(db, expenses):
    _population(db)
    assert analysis.disproportionate(analysis.cohort_report(expenses, "v2")) == [], \
        "five cases is below the 20-case floor, however extreme the rate"
    loud = analysis.disproportionate(analysis.cohort_report(expenses, "v2"), min_cases=1)
    assert [c["cohort"] for c in loud] == ["meals"]


def test_cohort_report_covers_every_declared_field(db, expenses):
    _population(db)
    assert [b["field"] for b in analysis.cohort_report(expenses, "v2")] == expenses.cohort_fields


# ---------------------------------------------------------------- comparison


def test_compare_reports_both_sides_and_their_disagreements(db, expenses):
    insert_case(db, "c1", "2025-06-01T10:00:00", {"amount_gbp": 100}, "deny")
    db.record_run("r1", "expenses", "v1", "actual", 1, 0, 0)
    db.record_run("r2", "expenses", "v2", "actual", 1, 1, 100.0)
    judged(db, "c1", outcome="deny", version="v1", run="r1")
    judged(db, "c1", outcome="approve", version="v2", run="r2")

    c = analysis.compare(expenses, "v1", "v2")
    assert c["left"]["version"] == "v1" and c["right"]["version"] == "v2"
    assert c["compared_cases"] == 1
    assert c["disagreements"] == 1
    assert c["disagreement_cases"] == ["c1"]


def test_a_policy_that_reverses_precedent_is_never_called_safer(db, expenses):
    """Regression: "fewer violations" read as an endorsement of a failing policy."""
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    db.record_run("r1", "expenses", "v1", "actual", 1, 0, 0)
    db.record_run("r2", "expenses", "v2", "actual", 1, 0, 0)
    judged(db, "c1", outcome="deny", version="v1", run="r1")
    judged(db, "c1", outcome="partial", version="v2", run="r2")
    db.save_precedent(Precedent(case_id="c1", domain="expenses", correct_outcome="approve",
                                ruled_by="lead", established_at=datetime(2026, 1, 1)))

    verdict = analysis.compare(expenses, "v1", "v2")["verdict"]
    assert "neither can ship" in verdict
    assert "safer" not in verdict and "fewer" not in verdict


def test_compare_names_the_one_policy_that_fails(db, expenses):
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    db.record_run("r1", "expenses", "v1", "actual", 1, 0, 0)
    db.record_run("r2", "expenses", "v2", "actual", 1, 0, 0)
    judged(db, "c1", outcome="approve", version="v1", run="r1")
    judged(db, "c1", outcome="deny", version="v2", run="r2")
    db.save_precedent(Precedent(case_id="c1", domain="expenses", correct_outcome="approve",
                                ruled_by="lead", established_at=datetime(2026, 1, 1)))
    c = analysis.compare(expenses, "v1", "v2")
    assert c["right"]["violations"] == 1 and c["left"]["violations"] == 0
    assert "v2 cannot ship" in c["verdict"]


def test_compare_warns_that_precedents_carry_their_own_framing(db, expenses):
    insert_case(db, "c1", "2025-06-01T10:00:00", {}, "deny")
    judged(db, "c1", version="v1", run="r1")
    db.save_precedent(Precedent(case_id="c1", domain="expenses", correct_outcome="approve",
                                ruled_by="lead", established_at=datetime(2026, 1, 1)))
    assert "never asked" in analysis.compare(expenses, "v1", "v2")["caveat"]


def test_compare_has_no_caveat_without_precedents(db, expenses):
    assert analysis.compare(expenses, "v1", "v2")["caveat"] == ""
