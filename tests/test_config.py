"""Domain configuration - the only place domain knowledge is allowed to live."""

from __future__ import annotations

import pytest

from ptm.config import DomainConfig, ReviewPolicy, available_domains, load_domain


def test_both_shipped_domains_load():
    assert set(available_domains()) >= {"expenses", "refunds"}


def test_outcomes_are_ordered_generous_to_strict(expenses, refunds):
    assert expenses.outcomes == ["approve", "partial", "deny"]
    assert refunds.outcomes == ["full_refund", "partial_refund", "no_refund"]


def test_every_declared_policy_file_exists(expenses, refunds):
    for domain in (expenses, refunds):
        for version in domain.policies:
            assert domain.policy_text(version).strip(), f"{domain.name}/{version} is empty"


def test_an_unknown_policy_version_names_the_ones_that_exist(expenses):
    with pytest.raises(KeyError) as exc:
        expenses.policy_text("v99")
    assert "v1" in str(exc.value) and "v2" in str(exc.value)


def test_an_unknown_domain_lists_the_available_ones():
    with pytest.raises(FileNotFoundError) as exc:
        load_domain("not_a_domain")
    assert "expenses" in str(exc.value)


def test_render_case_leaves_missing_keys_blank(expenses):
    out = expenses.render_case({"case_id": "exp-1", "amount_gbp": 42})
    assert "exp-1" in out and "42" in out
    assert "{submitted_by}" not in out, "an absent key must not leak its placeholder"


def test_impact_of_reads_the_declared_field(expenses):
    assert expenses.impact_of({"amount_gbp": 250.5}) == 250.5
    assert expenses.impact_of({"amount_gbp": "250.5"}) == 250.5, "SQLite returns text"
    assert expenses.impact_of({}) == 0.0
    assert expenses.impact_of({"amount_gbp": None}) == 0.0
    assert expenses.impact_of({"amount_gbp": "not a number"}) == 0.0


def test_a_domain_with_no_impact_field_has_no_impact():
    d = DomainConfig(name="x", label="x", outcomes=["a", "b"], case_template="{a}",
                     policies={"v1": "policies/expenses/v1.md"})
    assert d.impact_of({"anything": 10}) == 0.0


def test_review_policy_defaults_are_stingy():
    """Humans are the scarce resource; the default must not route thousands."""
    r = ReviewPolicy()
    assert r.max_reviews == 8
    assert r.always_review_directions == ["loosening"]


def test_offline_rules_reference_only_template_fields(expenses, refunds):
    """A rule naming a field the case template never renders is a silent bug.

    The judge is shown the template; the offline evaluator is given the payload.
    They should be talking about the same facts.
    """
    import re
    for domain in (expenses, refunds):
        rendered = set(re.findall(r"\{(\w+)\}", domain.case_template))
        for version, rules in domain.offline_rules.items():
            for rule in rules:
                names = set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", rule["when"]))
                unknown = names - rendered - {"and", "or", "not", "in", "if", "else",
                                              "yes", "no", "abs", "len", "min", "max",
                                              "float", "int", "str", "true", "false"}
                # Values compared against, e.g. 'travel' or 'premium', are quoted
                # and so are not bare names; anything left should be a real field.
                unknown = {u for u in unknown if f"'{u}'" not in rule["when"]}
                assert not unknown, f"{domain.name}/{version} rule references {unknown}"
