"""A rule an earlier rule has already eaten.

``offline_rules`` are tried in order and the first match decides the case, so a
general restriction written above the exemption it was meant to carve out of
makes that exemption unreachable. Every check this project had passed such a
rule: it parses, it reads real fields, it cites a real clause. It simply never
fires, so its outcome is never produced and its clause never cited - and the
replay is then confidently wrong about every case it was written for, in exactly
the silent way a misspelled field name is.

The proof is static and one-directional on purpose. It reports a shadow only
where it can show one, so it never fires on a rule set somebody has thought
about; it will happily miss a rule made unreachable by two earlier rules
between them, which is a harder question than this needs to answer.
"""

from __future__ import annotations

import pytest

from ptm import lint
from ptm.config import load_domain
from ptm.safe_eval import describe_shadowed, implies, shadowed


class TestImplication:
    @pytest.mark.parametrize("narrow,wide", [
        ("amount > 100", "amount > 75"),
        ("amount > 75", "amount >= 75"),
        ("amount == 80", "amount > 75"),
        ("amount < 10", "amount < 50"),
        ("amount <= 10", "amount < 50"),
        ("amount > 75 and receipt == 'no'", "amount > 75"),
        ("a > 1 and b == 'x'", "b == 'x' and a > 0"),
        ("75 < amount", "amount > 50"),
        ("amount > 75", "amount > 75"),
    ])
    def test_it_holds_where_every_case_of_one_is_a_case_of_the_other(self, narrow, wide):
        assert implies(narrow, wide)

    @pytest.mark.parametrize("narrow,wide", [
        ("amount > 75", "amount > 100"),
        ("amount >= 75", "amount > 75"),
        ("amount > 75", "amount > 75 and receipt == 'no'"),
        ("category == 'meals'", "category == 'travel'"),
        ("grade >= 3", "grade < 3"),
        ("amount > 75 or receipt == 'no'", "amount > 75"),
    ])
    def test_it_does_not_hold_otherwise(self, narrow, wide):
        assert not implies(narrow, wide)

    def test_the_exemption_in_the_shipped_fixture_is_not_shadowed(self):
        """The case this had to get right or it would be useless.

        expenses/v2 states the receipt threshold twice - ``grade < 3`` denies and
        ``grade >= 3`` approves - which is the exact shape a crude subset check
        reports as a shadow. Neither implies the other, and a check that said so
        would have made the shipped policy unlintable.
        """
        assert not implies("amount_gbp > 75 and receipt == 'no' and grade >= 3",
                           "amount_gbp > 75 and receipt == 'no' and grade < 3")

    def test_an_unparsable_expression_implies_nothing(self):
        """Reported by check_expression, not guessed at here. A syntax error
        turned into a claim about reachability is two wrong findings."""
        assert not implies("this is not ((", "a > 1")
        assert not implies("a > 1", "this is not ((")


class TestShadowedRules:
    def test_an_identical_rule_later_in_the_list_can_never_fire(self):
        found = shadowed([
            {"when": "amount > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount > 75", "outcome": "deny", "clause": "1.1"},
        ])
        assert [row["rule_index"] for row in found] == [1]
        # Same answer from the same clause: dead weight, not a wrong result.
        assert found[0]["harmless"] is True

    def test_a_narrower_rule_below_a_general_one_is_the_dangerous_case(self):
        found = shadowed([
            {"when": "amount > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount > 100 and receipt == 'no'", "outcome": "approve",
             "clause": "6.1"},
        ])
        assert len(found) == 1
        assert found[0]["shadowed_by"] == 0
        assert found[0]["harmless"] is False
        assert found[0]["shadowed_by_outcome"] == "deny"

    def test_the_specific_rule_first_is_not_a_shadow(self):
        """The fix, asserted as a fix: order is the whole difference."""
        assert not shadowed([
            {"when": "amount > 100 and receipt == 'no'", "outcome": "approve",
             "clause": "6.1"},
            {"when": "amount > 75", "outcome": "deny", "clause": "1.1"},
        ])

    def test_a_rule_with_no_condition_is_somebody_else_s_finding(self):
        """``when`` missing is reported by the lint's own check; reporting it
        here as well would put two findings on one mistake."""
        assert shadowed([{"when": "", "outcome": "deny"}, {"when": "", "outcome": "deny"}]) == []

    def test_the_description_names_the_rule_that_ate_it(self):
        text = describe_shadowed(shadowed([
            {"when": "amount > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount > 100", "outcome": "approve", "clause": "6.1"},
        ]), "demo/v9")
        assert "rule[1]" in text and "rule[0]" in text
        assert "Move it earlier" in text

    def test_nothing_shadowed_says_so_rather_than_printing_nothing(self):
        assert "no rule" in describe_shadowed([], "demo/v9")


class TestTheShippedFixturesAreClean:
    @pytest.mark.parametrize("name", ["expenses", "refunds"])
    def test_no_shipped_rule_is_unreachable(self, name, seeded):
        domain = load_domain(name)
        for version, rules in sorted(domain.offline_rules.items()):
            found = shadowed(rules)
            assert not found, describe_shadowed(found, f"{name}/{version}")

    @pytest.mark.parametrize("name", ["expenses", "refunds"])
    def test_the_lint_passes_them(self, name, seeded):
        assert not [p for p in lint.check_domain(name) if p.level == "ERROR"]


class TestTheLintReportsIt:
    """Wired into the lint, because that is the command CI runs."""

    def test_a_disagreeing_shadow_is_an_error(self, monkeypatch, seeded):
        domain = load_domain("expenses")
        patched = domain.model_copy(deep=True)
        patched.offline_rules["v2"] = [
            {"when": "amount_gbp > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount_gbp > 100 and receipt == 'no'", "outcome": "approve",
             "clause": "6.1"},
        ]
        monkeypatch.setattr(lint, "load_domain", lambda name: patched)
        errors = [p for p in lint.check_domain("expenses") if p.level == "ERROR"]
        assert any("can never fire" in p.message for p in errors)
        assert any("Move the specific rule above the general one" in p.message
                   for p in errors)

    def test_a_duplicate_is_only_a_warning(self, monkeypatch, seeded):
        domain = load_domain("expenses")
        patched = domain.model_copy(deep=True)
        patched.offline_rules["v2"] = [
            {"when": "amount_gbp > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount_gbp > 75", "outcome": "deny", "clause": "1.1"},
        ]
        monkeypatch.setattr(lint, "load_domain", lambda name: patched)
        problems = lint.check_domain("expenses")
        assert not [p for p in problems if p.level == "ERROR" and "never fire" in p.message]
        assert any("dead weight" in p.message for p in problems if p.level == "WARN")


class TestGeneratedRuleSetsAreRejected:
    """The prolific source. ``propose_<domain>`` has a model write these."""

    def test_validate_refuses_a_rule_set_with_an_unreachable_rule(self, seeded):
        from ptm import rules as rules_engine

        domain = load_domain("expenses")
        problems = rules_engine.validate([
            {"when": "amount_gbp > 75", "outcome": "deny", "clause": "1.1"},
            {"when": "amount_gbp > 100 and receipt == 'no'", "outcome": "approve",
             "clause": "1.1"},
        ], domain, "v2")
        assert any("can never fire" in problem for problem in problems)

    def test_it_accepts_the_same_rules_in_the_right_order(self, seeded):
        from ptm import rules as rules_engine

        domain = load_domain("expenses")
        assert not rules_engine.validate([
            {"when": "amount_gbp > 100 and receipt == 'no'", "outcome": "approve",
             "clause": "1.1"},
            {"when": "amount_gbp > 75", "outcome": "deny", "clause": "1.1"},
        ], domain, "v2")
