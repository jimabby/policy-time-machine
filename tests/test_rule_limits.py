"""What a rule expression may *cost*, as opposed to what it may reach.

:mod:`ptm.safe_eval` walks the expression instead of calling ``eval``, so the
object graph is genuinely gone. That bounds what a rule can do and says nothing
about how long it can take, and a rule is evaluated on a worker holding a mapped
task slot - by rules a model wrote, for a draft the proposer published.

There was one cost bound, ``MAX_EXPONENT``, and it did not hold: every exponent
in ``((b**64)**64)**64`` is a legal 64 while the base grows at each step. That
expression took 15 seconds and one nesting further is the hang the ceiling was
written to prevent. Sequence repetition had no bound at all.
"""

from __future__ import annotations

import time

import pytest

from ptm import safe_eval


def timed(expression: str, scope: dict | None = None) -> float:
    start = time.perf_counter()
    with pytest.raises(safe_eval.RuleError):
        safe_eval.evaluate(expression, scope or {})
    return time.perf_counter() - start


class TestItRefusesWhatItCannotAfford:
    def test_a_power_of_a_power_is_refused_outright(self):
        """Not size-checked after the fact - refused on shape, so the ceiling
        cannot be walked past by adding another level."""
        assert timed("((10**64)**64)**64 > 0") < 1.0
        assert timed("(((10**64)**64)**64)**64 > 0") < 1.0

    def test_the_refusal_names_the_construct_rather_than_the_size(self):
        with pytest.raises(safe_eval.RuleError, match="raises a power to a power"):
            safe_eval.evaluate("(2**3)**4 > 0", {})

    def test_string_repetition_is_capped_before_it_allocates(self):
        """'x' * 10**9 is a gigabyte by the time a check on the result runs."""
        assert timed("str(0) * 20000000 == ''") < 1.0

    def test_list_repetition_is_capped_too(self):
        assert timed("[1] * 10**9 == []") < 1.0

    def test_a_computed_exponent_is_still_bounded(self):
        with pytest.raises(safe_eval.RuleError, match="ceiling is 64"):
            safe_eval.evaluate("10**64**64 > 0", {})

    def test_the_repetition_cap_reads_from_a_field_too(self):
        """The count does not have to be a literal: a payload carries numbers."""
        with pytest.raises(safe_eval.RuleError, match="past the ceiling"):
            safe_eval.evaluate("len(note * amount_gbp) > 0",
                               {"note": "x" * 1000, "amount_gbp": 100000})


class TestTheLintSeesItToo:
    """A rule rejected at judging time has already been written to a draft and
    shipped. check_expression is what rejects it on arrival instead."""

    def test_check_refuses_the_shape_the_evaluator_refuses(self):
        assert safe_eval.check_expression("((10**64)**64)**64 > 0", {"x"})

    def test_validate_rejects_a_generated_rule_that_would_hang_a_worker(self, expenses):
        from ptm import rules

        problems = rules.validate(
            [{"when": "(amount_gbp**64)**64 > 0", "outcome": "deny", "clause": "1.1"}],
            expenses, "v2")
        assert any("power to a power" in p for p in problems)


class TestTheRulesPeopleActuallyWriteStillRun:
    """A cost ceiling that refuses real rules is worse than the hang."""

    @pytest.mark.parametrize("expression,scope,expected", [
        ("amount_gbp > 75", {"amount_gbp": 100}, True),
        ("grade >= 3 and receipt == 'no'", {"grade": 3, "receipt": "no"}, True),
        ("amount_gbp ** 2 > 100", {"amount_gbp": 11}, True),
        ("round(amount_gbp / 3, 2) < 30", {"amount_gbp": 80}, True),
        ("category in ['travel', 'meals']", {"category": "meals"}, True),
        ("max(1, days_notice) * 2 >= 14", {"days_notice": 7}, True),
        ("len(str(amount_gbp)) > 2", {"amount_gbp": 1000}, True),
    ])
    def test_it_evaluates(self, expression, scope, expected):
        assert safe_eval.evaluate(expression, scope) is expected

    def test_every_shipped_rule_still_passes_the_lint(self):
        """The real fixtures, not a parallel set: a ceiling that quietly broke
        the demo's own rules would show up here and nowhere else."""
        from ptm.config import available_domains
        from ptm.lint import check_domain

        for name in available_domains():
            errors = [p for p in check_domain(name) if p.level == "ERROR"]
            assert not errors, errors
