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

import ast
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


class TestNestingIsBounded:
    """The ceiling that closes the one refusal this module used to leak.

    :mod:`ptm.safe_eval` promises that everything it declines arrives as a
    :class:`RuleError`, and :func:`_apply` exists to keep that promise for the
    operators. The walkers themselves were unbounded: ``amount_gbp > 1+1+1+...``
    four thousand terms long parses, whitelists clean, stays far under every
    size and exponent ceiling, and exhausts the Python stack. What came back
    was ``RecursionError``, which is not a ``RuleError`` and so walked past
    every ``except RuleError`` in the project.

    The caller that mattered is :func:`ptm.rules.validate`, the gate in front of
    rules a *model* wrote - so ``propose_<domain>``'s designed behaviour, report
    the rule and write the draft without it, became a task dying on a traceback.
    """

    @staticmethod
    def deep(terms: int = 4000) -> str:
        """A condition that nests ``terms`` deep without a single parenthesis.

        Parentheses are refused by CPython's own tokenizer at a few hundred, so
        a test built on those would prove the tokenizer works. A left-nested
        chain of ``BinOp`` is the shape that actually reaches the walkers.
        """
        return "amount_gbp > " + "+".join(["1"] * terms)

    def test_check_expression_reports_it_rather_than_raising(self):
        problems = safe_eval.check_expression(self.deep())
        assert problems and "nests" in problems[0]

    @pytest.mark.parametrize("call", [
        lambda expr: safe_eval.evaluate(expr, {"amount_gbp": 5}),
        safe_eval.names_in,
        safe_eval.depth_of,
        safe_eval.parse,
    ])
    def test_every_entry_point_refuses_by_the_same_door(self, call):
        """Each of these goes through parse(), which is where the check lives."""
        with pytest.raises(safe_eval.RuleError):
            call(self.deep())

    def test_a_recursion_error_never_escapes(self):
        """The assertion is the exception *type*, which is the whole bug.

        ``RecursionError`` subclasses ``RuntimeError``, so a caller catching
        ``Exception`` swallowed it and a caller catching ``RuleError`` did not.
        Both were wrong in different directions.
        """
        try:
            safe_eval.evaluate(self.deep(), {"amount_gbp": 5})
        except safe_eval.RuleError:
            pass
        except RecursionError:  # pragma: no cover - the bug this closes
            pytest.fail("RecursionError escaped safe_eval")

    def test_the_generated_rule_gate_reports_instead_of_crashing(self, expenses):
        problems = rules.validate(
            [{"when": self.deep(), "outcome": "approve", "clause": "1.1",
              "because": "x"}], expenses, "v2")
        assert problems, "validate accepted a rule it cannot evaluate"
        assert any("nests" in p for p in problems)

    def test_an_ordinary_rule_is_nowhere_near_the_ceiling(self):
        """The bound has to be one no real rule can trip, or it is a bug of its own."""
        assert safe_eval.depth_of("amount_gbp > 75 and receipt == 'no'") < 10

    def test_the_shipped_rules_are_nowhere_near_it_either(self, expenses):
        for ruleset in expenses.offline_rules.values():
            for rule in ruleset:
                assert safe_eval.depth_of(rule["when"]) < safe_eval.MAX_DEPTH / 2

    @pytest.mark.parametrize("expression,why", [
        ("a" + chr(0) + "b", "a null byte"),
        ("a" + chr(0xDCFF), "a lone surrogate"),
    ])
    def test_the_other_things_ast_parse_raises_are_refusals_too(self, expression, why):
        """Neither is a ``SyntaxError``, and neither was caught.

        A null byte raises ``ValueError`` and a lone surrogate raises
        ``UnicodeEncodeError``; ``RuleError`` subclasses ``ValueError``, so a
        caller catching ``RuleError`` did not catch either. Unreachable from
        YAML somebody typed, reachable from a JSON rule set a model produced -
        which is the threat model this module was rewritten for. Built with
        ``chr()`` because a source file holding a lone surrogate cannot itself
        be saved as UTF-8, which is its own small demonstration of the point.
        """
        problems = safe_eval.check_expression(expression)
        assert problems and "cannot be parsed" in problems[0], why
        with pytest.raises(safe_eval.RuleError):
            safe_eval.evaluate(expression, {})

    def test_measuring_depth_does_not_itself_recurse(self):
        """A recursive measurement would overflow on exactly its own input.

        Driven well past the interpreter's limit: if the walk below this were
        recursive, this is the call that would prove it.
        """
        assert safe_eval._measure_depth(
            ast.parse("1" + "+1" * 20000, mode="eval").body,
            ceiling=safe_eval.MAX_DEPTH) > safe_eval.MAX_DEPTH
