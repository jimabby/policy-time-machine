"""Turning a drafted amendment into a policy, and testing whether it works."""

from __future__ import annotations

from datetime import datetime

import pytest

from conftest import insert_case
from ptm import amend, judge
from ptm.models import Case, Flip, Precedent, Verdict

AMENDMENT = {
    "feasible": True,
    "rationale": "Narrow clause 5.1 so it stops reversing the finance lead's rulings.",
    "edits": [{"clause": "5.1", "current_text": "Any claim over GBP 1000 requires approval.",
               "proposed_text": "Any claim over GBP 1000 requires approval, unless already paid.",
               "reason": "exp-0478 was ruled approve."}],
    "residual_risk": "Claims just over the threshold now pass unreviewed.",
}
VIOLATIONS = [
    {"case_id": "exp-0478", "established_outcome": "approve", "proposed_outcome": "deny",
     "ruled_by": "finance.lead", "established_at": "2026-01-01"},
]


def test_candidate_name_is_stable(expenses):
    assert amend.candidate_name("v2") == "v2+fix"
    assert amend.candidate_name("v2+fix") == "v2+fix", "re-running replaces, never multiplies"


def test_amended_text_keeps_the_parent_readable_beside_the_change(expenses):
    text = amend.amended_text(expenses, "v2", AMENDMENT)
    assert "GBP 75" in text, "the parent policy survives intact"
    assert "## Amendment to v2" in text
    assert "unless already paid" in text
    assert "Previously: Any claim over GBP 1000 requires approval." in text
    assert "Claims just over the threshold" in text


def test_amended_text_handles_a_new_clause(expenses):
    text = amend.amended_text(expenses, "v2", {
        "rationale": "r", "edits": [{"clause": "", "proposed_text": "A brand new rule."}]})
    assert "amends clause new" in text
    assert "Previously" not in text


def test_the_carve_out_precedes_the_rules_it_overrides(expenses):
    rules = amend.offline_carve_out(VIOLATIONS, expenses.rules_for("v2"))
    assert rules[0]["when"] == "case_id == 'exp-0478'"
    assert rules[0]["outcome"] == "approve"
    assert rules[0]["clause"] == amend.CARVE_OUT_CLAUSE
    assert rules[1:] == expenses.rules_for("v2"), "the parent's rules follow, unchanged"


def test_the_carve_out_actually_changes_the_verdict(expenses):
    """First match wins, so the carve-out must beat the clause that offended."""
    payload = {"case_id": "exp-0478", "amount_gbp": 1500, "director_approval": "no",
               "receipt": "yes", "grade": 1, "category": "software"}
    case = Case(case_id="exp-0478", domain="expenses", decided_at=datetime(2025, 6, 1),
                payload=payload, actual_outcome="approve")
    assert judge.offline_verdict(case, expenses, "v2").outcome == "deny"

    expenses.offline_rules["carved"] = amend.offline_carve_out(VIOLATIONS, expenses.rules_for("v2"))
    try:
        v = judge.offline_verdict(case, expenses, "carved")
        assert v.outcome == "approve" and v.policy_clause == amend.CARVE_OUT_CLAUSE
    finally:
        expenses.offline_rules.pop("carved")


def test_materialise_registers_a_judgeable_version(db, expenses):
    name = amend.materialise(expenses, "v2", AMENDMENT, VIOLATIONS, "run-1")
    assert name == "v2+fix"

    row = db.load_policy_version("expenses", "v2+fix")
    assert row["parent"] == "v2"
    assert row["forced_by"] == ["exp-0478"]
    assert "## Amendment to v2" in row["text"]
    # And the engine can now resolve it like any hand-written version.
    assert "unless already paid" in expenses.policy_text("v2+fix")
    assert expenses.rules_for("v2+fix")[0]["clause"] == amend.CARVE_OUT_CLAUSE
    assert "v2+fix" in expenses.all_versions()


def test_re_materialising_replaces_rather_than_accumulates(db, expenses):
    amend.materialise(expenses, "v2", AMENDMENT, VIOLATIONS, "run-1")
    amend.materialise(expenses, "v2", AMENDMENT, VIOLATIONS, "run-2")
    assert len(db.candidate_versions("expenses")) == 1


def _setup_verify(db, expenses, precedent_outcome="approve"):
    insert_case(db, "exp-0478", "2025-06-01T10:00:00", {"amount_gbp": 1500}, "approve")
    insert_case(db, "other", "2025-06-02T10:00:00", {"amount_gbp": 90}, "deny")
    db.record_run("r1", "expenses", "v2", "actual", 2, 2, 0)
    db.save_flips("r1", "expenses", "v2", [
        Flip(case_id="exp-0478", decided_at=datetime(2025, 6, 1), actual_outcome="approve",
             new_outcome="deny", rationale="r", confidence=0.9, policy_clause="5.1",
             impact=1500.0, direction="tightening"),
        Flip(case_id="other", decided_at=datetime(2025, 6, 2), actual_outcome="deny",
             new_outcome="approve", rationale="r", confidence=0.9, policy_clause="1.1",
             impact=90.0, direction="loosening"),
    ])
    db.save_precedent(Precedent(case_id="exp-0478", domain="expenses",
                                correct_outcome=precedent_outcome, ruled_by="finance.lead",
                                established_at=datetime(2026, 1, 1)))
    amend.materialise(expenses, "v2", AMENDMENT, VIOLATIONS, "run-1")


def v(outcome):
    return Verdict(outcome=outcome, rationale="r", confidence=0.9)


def test_a_working_fix_clears_the_gate_with_no_collateral(db, expenses):
    _setup_verify(db, expenses)
    result = amend.verify(expenses, "v2+fix", "v2",
                          precedent_verdicts={"exp-0478": v("approve")},
                          collateral_verdicts={"exp-0478": v("approve"), "other": v("approve")})
    assert result["clears_gate"] is True
    assert result["collateral_changed"] == 0
    assert "surgical" in result["summary"]


def test_the_cases_the_fix_targets_are_not_counted_as_collateral(db, expenses):
    """Regression: the carved-out cases move by design and were reported as damage."""
    _setup_verify(db, expenses)
    result = amend.verify(expenses, "v2+fix", "v2",
                          precedent_verdicts={"exp-0478": v("approve")},
                          collateral_verdicts={"exp-0478": v("approve"), "other": v("approve")})
    assert result["intended_changes"] == ["exp-0478"]
    assert result["collateral_checked"] == 1, "only 'other' is collateral"
    assert "exp-0478" not in result["collateral_cases"]


def test_collateral_damage_is_reported(db, expenses):
    _setup_verify(db, expenses)
    result = amend.verify(expenses, "v2+fix", "v2",
                          precedent_verdicts={"exp-0478": v("approve")},
                          collateral_verdicts={"exp-0478": v("approve"), "other": v("deny")})
    assert result["clears_gate"] is True
    assert result["collateral_changed"] == 1
    assert result["collateral_cases"] == ["other"]
    assert "read those before adopting" in result["summary"]


def test_a_fix_that_does_not_work_says_so(db, expenses):
    _setup_verify(db, expenses)
    result = amend.verify(expenses, "v2+fix", "v2",
                          precedent_verdicts={"exp-0478": v("deny")},
                          collateral_verdicts={})
    assert result["clears_gate"] is False
    assert len(result["violations"]) == 1
    assert "still reverses" in result["summary"]
    assert "cannot ship" in result["summary"]


def test_verification_is_stored_against_the_parent_not_the_candidate(db, expenses):
    """The question is "can v2 ship?", so its answer belongs under v2."""
    _setup_verify(db, expenses)
    result = amend.verify(expenses, "v2+fix", "v2", {"exp-0478": v("approve")}, {})
    db.save_insight("expenses", "v2", "amendment_verification", result, "offline", "r1")
    assert "amendment_verification" in db.load_insights("expenses", "v2")
    assert db.load_insights("expenses", "v2+fix") == {}
