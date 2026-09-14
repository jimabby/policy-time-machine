"""The rule evaluator, and what it refuses.

``offline_rules`` stopped being only hand-written YAML when ``propose_<domain>``
started asking a model for them: the drafter writes rules, they go to disk, and
every later replay of that draft evaluates them on a worker. So the question
these tests exist for is not "does a threshold comparison work" - it is whether
an expression that arrives from a model can do anything but compare numbers.

The escape asserted against below is the real one. Before :mod:`ptm.safe_eval`,
the check in front of ``eval`` collected :class:`ast.Name` nodes, and an
expression built entirely from attribute access and subscripting has none - so
it was reported as clean and then handed the interpreter.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import rules, safe_eval
from ptm.judge import offline_verdict
from ptm.models import Case

#: Reaches ``os.system`` through the object graph, reading no bare name at all.
ESCAPE = ("().__class__.__base__.__subclasses__()[-1].__init__.__globals__"
          "['__builtins__']['__import__']('os').system('true')==0")

#: The same idea via a comprehension, which is how it is usually written.
ESCAPE_COMPREHENSION = (
    "[c for c in ().__class__.__base__.__subclasses__() "
    "if c.__name__=='BuiltinImporter'][0].load_module('os').system('true')==0")


def case(**payload) -> Case:
    return Case(case_id="c", domain="expenses", decided_at=datetime(2025, 1, 1),
                payload=payload, actual_outcome="approve")


class TestWhatItRefuses:
    """Every one of these used to evaluate. That is the point of the module."""

    @pytest.mark.parametrize("expression", [
        ESCAPE,
        ESCAPE_COMPREHENSION,
        "().__class__ is not None",
        "amount.__class__.__name__ == 'int'",
        "(lambda: 1)() == 1",
        "[1, 2][0] == 1",
        "{'a': 1}['a'] == 1",
        "f'{amount}' == '10'",
        "(x := 5) > 1",
        "[n for n in (1, 2)][0] == 1",
    ])
    def test_the_evaluator_refuses(self, expression):
        with pytest.raises(safe_eval.RuleError):
            safe_eval.evaluate(expression, {"amount": 10})

    @pytest.mark.parametrize("expression", [ESCAPE, ESCAPE_COMPREHENSION])
    def test_validate_reports_it_rather_than_passing_it_through(self, expression, expenses):
        """The check in front of the evaluator has to see it too.

        Containment and reporting are separate jobs here: the evaluator is what
        makes the rule safe, and this is what makes it *visible*, so a generated
        rule set is rejected on arrival instead of silently matching nothing.
        """
        problems = rules.validate(
            [{"when": expression, "outcome": "approve", "clause": "1.1"}], expenses, "v2")
        assert problems, "an expression the evaluator will not run must be reported"

    def test_a_refused_rule_does_not_decide_a_case(self, expenses):
        """offline_verdict swallows the refusal, which must mean 'no match'."""
        candidate = rules.with_rules(expenses, "v2", [
            {"when": ESCAPE, "outcome": "deny", "clause": "1.1"},
            {"when": "amount_gbp > 5", "outcome": "partial", "clause": "2.1"},
        ])
        verdict = offline_verdict(case(amount_gbp=10), candidate, "v2")
        assert verdict.outcome == "partial", \
            "a rule the evaluator refuses must fall through to the next one"

    def test_an_unbounded_exponent_cannot_hang_a_worker(self):
        with pytest.raises(safe_eval.RuleError):
            safe_eval.evaluate("9**9**9 > 0", {})
        assert safe_eval.check_expression("2**1000 > 0", set()), \
            "a literal exponent above the ceiling is knowable before the rule ever runs"


class TestWhatItStillDoes:
    """The refusals are worthless if they cost the rules anyone actually writes."""

    @pytest.mark.parametrize("expression,expected", [
        ("amount_gbp > 75 and receipt == 'no'", True),
        ("amount_gbp > 75 and receipt == 'yes'", False),
        ("amount_gbp > 500 or grade >= 3", True),
        ("not (amount_gbp < 10)", True),
        ("10 < amount_gbp < 200", True),
        ("category in ('travel', 'meals')", True),
        ("category not in ('software',)", True),
        ("abs(amount_gbp - 100) <= 0", True),
        ("max(amount_gbp, 10) == 100", True),
        ("round(amount_gbp / 3, 2) > 33", True),
        ("(amount_gbp if grade >= 3 else 0) > 50", True),
        ("amount_gbp % 2 == 0", True),
        ("amount_gbp ** 2 > 9000", True),
    ])
    def test_the_real_language_evaluates(self, expression, expected):
        scope = {"amount_gbp": 100, "receipt": "no", "grade": 3, "category": "travel"}
        assert safe_eval.evaluate(expression, scope) is expected or \
               safe_eval.evaluate(expression, scope) == expected

    def test_every_shipped_rule_still_runs(self, expenses):
        """The fixtures are the regression suite for the evaluator's coverage."""
        known = {"amount_gbp", "receipt", "grade", "category", "days_notice",
                 "director_approval", "alcohol", "client_driven", "note",
                 "case_id", "submitted_by"}
        for version, ruleset in expenses.offline_rules.items():
            for rule in ruleset:
                assert not safe_eval.check_expression(rule["when"], known), \
                    f"{version}: the evaluator will not run a rule that ships"

    def test_short_circuit_does_not_evaluate_the_far_side(self):
        """`and` must stop at the first falsehood, or a guard clause is pointless."""
        assert safe_eval.evaluate("present == 1 and absent == 2", {"present": 2}) is False

    def test_a_missing_field_is_a_refusal_not_a_match(self):
        with pytest.raises(safe_eval.RuleError):
            safe_eval.evaluate("nonexistent > 1", {"amount": 1})

    def test_names_in_reports_fields_but_not_helpers(self):
        assert safe_eval.names_in("max(amount, 10) > cap") == {"amount", "cap"}
