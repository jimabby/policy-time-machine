"""Prompt construction and the offline rule evaluator."""

from __future__ import annotations

import logging

from conftest import case
from ptm import judge


def test_prompt_carries_the_policy_the_outcomes_and_the_decision_date(expenses):
    c = case("exp-1", payload={"amount_gbp": 90, "category": "meals", "grade": 2},
             decided="2025-03-04T09:00:00")
    p = judge.build_prompt(c, expenses, "v2")
    assert "2025-03-04" in p, "the judge must be told which day it is reasoning as of"
    assert "approve, partial, deny" in p
    assert "GBP 75" in p, "the actual v2 policy text is inlined"
    assert "grade 3 or above" in p
    assert "Do not" in p and "afterwards" in p, "the no-hindsight instruction is load-bearing"


def test_prompt_renders_missing_payload_keys_as_blank_not_an_error(expenses):
    p = judge.build_prompt(case("exp-1", payload={"amount_gbp": 10}), expenses, "v1")
    assert "Submitted by:" in p, "a sparse payload still renders the template"


def test_first_matching_rule_wins(expenses):
    # Over 1000 with no director approval (5.1) AND no receipt (1.1); 5.1 is listed first.
    v = judge.offline_verdict(
        case(payload={"amount_gbp": 1500, "director_approval": "no", "receipt": "no",
                      "grade": 1, "category": "software"}), expenses, "v2")
    assert v.policy_clause == "5.1"


def test_no_matching_rule_falls_through_to_the_most_generous_outcome(expenses):
    v = judge.offline_verdict(
        case(payload={"amount_gbp": 10, "receipt": "yes", "director_approval": "no",
                      "category": "software", "grade": 1, "days_notice": 30, "alcohol": "no"}),
        expenses, "v2")
    assert v.outcome == "approve"
    assert v.confidence == 0.6, "a fall-through is not a confident decision"
    assert v.policy_clause == ""


def test_string_numerics_in_the_payload_are_coerced(expenses):
    """Point-in-time facts arrive from SQLite as text; comparisons must still work."""
    v = judge.offline_verdict(
        case(payload={"amount_gbp": 200, "receipt": "no", "grade": "1",
                      "director_approval": "no", "category": "software"}), expenses, "v2")
    assert v.outcome == "deny" and v.policy_clause == "1.1"


def test_the_grade_3_exemption_applies_point_in_time(expenses):
    """The same claim resolves differently by grade - this is the trap in one test."""
    payload = {"amount_gbp": 200, "receipt": "no", "director_approval": "no",
               "category": "software"}
    assert judge.offline_verdict(case(payload={**payload, "grade": 2}), expenses, "v2").outcome == "deny"
    assert judge.offline_verdict(case(payload={**payload, "grade": 3}), expenses, "v2").outcome == "approve"


def test_v1_and_v2_disagree_on_the_raised_receipt_threshold(expenses):
    payload = {"amount_gbp": 50, "receipt": "no", "grade": 1, "director_approval": "no",
               "category": "software", "days_notice": 30, "alcohol": "no"}
    assert judge.offline_verdict(case(payload=payload), expenses, "v1").outcome == "deny"
    assert judge.offline_verdict(case(payload=payload), expenses, "v2").outcome == "approve"


def test_rules_have_no_access_to_builtins(expenses, monkeypatch):
    """The evaluator is a local fixture, but it should not hand out __import__."""
    monkeypatch.setattr(expenses, "offline_rules",
                        {"vx": [{"when": "__import__('os')", "outcome": "approve"}]})
    v = judge.offline_verdict(case(payload={}), expenses, "vx")
    assert v.outcome == "approve" and v.confidence == 0.6, "the rule failed, so it fell through"


def test_a_broken_rule_is_logged_rather_than_swallowed(expenses, monkeypatch, caplog):
    """Regression: a typo'd field name silently changed every result."""
    judge._WARNED.clear()
    monkeypatch.setattr(expenses, "offline_rules",
                        {"vx": [{"when": "amount_gpb > 10", "outcome": "deny"}]})
    with caplog.at_level(logging.WARNING):
        judge.offline_verdict(case(payload={"amount_gbp": 100}), expenses, "vx")
    assert "amount_gpb" in caplog.text
    assert "did not evaluate" in caplog.text


def test_a_broken_rule_is_logged_only_once_per_rule(expenses, monkeypatch, caplog):
    """A 600-case replay must not emit 600 identical warnings."""
    judge._WARNED.clear()
    monkeypatch.setattr(expenses, "offline_rules",
                        {"vx": [{"when": "nope > 10", "outcome": "deny"}]})
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            judge.offline_verdict(case(f"c{i}", payload={"amount_gbp": 100}), expenses, "vx")
    assert caplog.text.count("did not evaluate") == 1


def test_an_unknown_policy_version_has_no_rules(expenses):
    v = judge.offline_verdict(case(payload={"amount_gbp": 9999}), expenses, "v99")
    assert v.outcome == "approve", "no rules means nothing matches"
