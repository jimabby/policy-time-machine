"""A rule that parses perfectly and is refused by every case it meets.

This is the failure that had no check. ``amount_gbp > '75'`` - the threshold
quoted, which is the likeliest single mistake in a rule set a model wrote -
parses, reads a real field, cites a real clause, is refused by nothing static
and raises the moment it meets a numeric ``amount_gbp``.
:func:`ptm.judge.offline_verdict` catches that and moves on, so the rule decides
nothing: its clause is never cited and every case it was written for takes the
default outcome. The replay is quietly wrong rather than visibly broken, which
is the one outcome this project refuses everywhere else.

Nothing that reads the rule can see it, because there is nothing wrong with the
rule as text. The only check that works is running it, which is what
:func:`ptm.lint.probe` does - and what these tests are about.

Two halves, and they have to hold together: :mod:`ptm.safe_eval` has to *report*
the refusal as a :class:`RuleError` rather than letting a ``TypeError`` escape,
or the probe has nothing to say beyond "something went wrong".
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import lint, rules
from ptm.config import load_domain
from ptm.judge import NO_RULE_RATIONALE, offline_verdict
from ptm.models import Case
from ptm.safe_eval import RuleError, check_expression, evaluate

QUOTED = "amount_gbp > '75' and receipt == 'no'"


def rule(when: str, clause: str = "1.1") -> list[dict]:
    return [{"when": when, "outcome": "deny", "clause": clause,
             "because": "probe fixture", "confidence": 0.9}]


class TestTheEvaluatorReportsRatherThanRaises:
    """Every refusal arrives as a RuleError, so a caller can say which rule.

    The helper call has done this since ``int('1' * 100000)``; the operators
    did not, and the gap was the same one.
    """

    @pytest.mark.parametrize("expression,scope", [
        ("amount > '75'", {"amount": 100}),
        ("amount < 'x'", {"amount": 100}),
        ("amount + 'x'", {"amount": 100}),
        ("amount / 0", {"amount": 100}),
        ("amount % 0", {"amount": 100}),
        ("amount // 0", {"amount": 100}),
        ("-category", {"category": "travel"}),
        ("amount in 5", {"amount": 1}),
    ])
    def test_an_operator_refusing_its_operands_is_a_rule_error(self, expression, scope):
        with pytest.raises(RuleError):
            evaluate(expression, scope)

    def test_the_message_names_the_operand_and_its_type(self):
        """Actionable, because the diagnosis *is* the type: the rule author has
        to see that 75 arrived as a string before the fix is obvious."""
        with pytest.raises(RuleError, match=r"'75' \(str\)"):
            evaluate("amount > '75'", {"amount": 100.0})

    def test_a_refusal_from_inside_a_helper_still_arrives_as_one(self):
        with pytest.raises(RuleError):
            evaluate("int('9' * 100000)", {})

    def test_the_static_check_still_passes_it(self):
        """Which is the entire problem, and why the probe has to exist. There is
        nothing wrong with this expression as text - it is wrong about a value
        it cannot see until it is run."""
        assert check_expression(QUOTED, known={"amount_gbp", "receipt"}) == []


@pytest.fixture(scope="module")
def cases(seeded):
    """The real fixture, loaded the way the lint loads it."""
    found = lint.probe_cases(load_domain("expenses"))
    assert found, "the probe needs the seeded fixture to have anything to say"
    return found


class TestTheProbeCatchesIt:
    def test_a_quoted_threshold_is_refused_by_every_case(self, cases):
        row = lint.probe(rule(QUOTED), cases)[0]
        assert row["always_refused"]
        assert row["matched"] == 0
        assert row["refused"] == row["probed"]
        assert "str" in row["reason"]

    def test_a_sound_rule_fires_and_is_not_reported(self, cases):
        row = lint.probe(rule("amount_gbp > 500"), cases)[0]
        assert row["matched"] > 0
        assert not row["always_refused"]
        assert row["refused"] == 0

    def test_a_rule_the_history_has_no_case_for_is_not_an_error(self, cases):
        """Dead against this fixture, which is a different claim from wrong. It
        is reported - as a warning, by the lint - and never rejected."""
        row = lint.probe(rule("amount_gbp > 99999999"), cases)[0]
        assert row["matched"] == 0
        assert not row["always_refused"]

    def test_it_is_standalone_rather_than_first_match_wins(self, cases):
        """A shadowed rule still reports the cases it *would* match. Whether an
        earlier rule eats it is safe_eval.shadowed's question, already asked -
        answering it here too would report one fault twice under two names."""
        pair = rule("amount_gbp > 100") + rule("amount_gbp > 500")
        rows = lint.probe(pair, cases)
        assert rows[1]["matched"] > 0

    def test_no_cases_is_no_evidence_rather_than_a_finding_of_zero(self):
        """The one thing this project refuses to print anywhere else: a number
        drawn from a measurement never taken. A checkout with no history must
        produce no probe findings, not a dead-rule report for every rule."""
        assert lint.probe(rule("amount_gbp > 500"), []) == []


class TestTheLintReportsIt:
    def test_the_shipped_fixtures_are_clean(self, seeded):
        """The probe must not cry wolf on the rules this repository ships."""
        for domain in ("expenses", "refunds"):
            errors = [p for p in lint.check_domain(domain) if p.level == "ERROR"]
            assert errors == []

    def test_a_refused_rule_is_an_error_naming_the_reason(self, seeded, monkeypatch):
        domain = load_domain("expenses")
        patched = domain.model_copy(
            update={"offline_rules": {**domain.offline_rules, "v2": rule(QUOTED)}})
        monkeypatch.setattr(lint, "load_domain", lambda _name: patched)
        errors = [p for p in lint.check_domain("expenses") if p.level == "ERROR"]
        assert errors, "a rule refused by every case must fail the lint"
        assert any("can never fire" in p.message and "str" in p.message for p in errors)


class TestValidateRejectsGeneratedOnes:
    """``propose_<domain>`` has a model write these, which is why it matters."""

    def test_a_quoted_threshold_is_rejected(self, seeded, expenses):
        problems = rules.validate(rule(QUOTED), expenses, "v2")
        assert problems and any("can never fire" in p for p in problems)

    def test_a_sound_rule_is_accepted(self, seeded, expenses):
        assert rules.validate(rule("amount_gbp > 500"), expenses, "v2") == []

    def test_a_rule_matching_nothing_is_not_grounds_for_rejection(self, seeded, expenses):
        """A rule set is ordered and first-match-wins, so one entry dropped takes
        the whole set with it - and a fixture with no case for a legitimate rule
        is an ordinary thing for a fixture to be. The lint warns; this does not
        reject."""
        assert rules.validate(rule("amount_gbp > 99999999"), expenses, "v2") == []

    def test_passing_no_cases_skips_the_probe(self, expenses):
        """So a caller with no database still gets the static checks."""
        assert rules.validate(rule(QUOTED), expenses, "v2", cases=[]) == []


class TestWhyItMattered:
    def test_the_judge_would_have_silently_defaulted(self, seeded, expenses):
        """The failure in one assertion: a rule that should deny a GBP 500
        no-receipt claim, and a verdict of 'approve' from no rule at all."""
        candidate = rules.with_rules(expenses, "v2", rule(QUOTED))
        case = Case(case_id="probe-1", domain="expenses", subject_id="e1",
                    decided_at=datetime(2025, 1, 1), actual_outcome="deny",
                    payload={"amount_gbp": 500, "receipt": "no", "category": "meals"})
        verdict = offline_verdict(case, candidate, "v2")
        assert verdict.outcome == "approve"
        assert verdict.rationale == NO_RULE_RATIONALE
        assert verdict.policy_clause == ""
