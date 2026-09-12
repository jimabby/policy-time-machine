"""Do the offline rules implement the policy they stand in for?

The threshold sweep is computed from these rules, so the sweep is worth exactly
what they are worth - and until this is measured, nobody knows what that is.
The other half of these tests is the guard rail: a generated rule set is
validated with the lint's own checks before it is allowed near a replay.
"""

from __future__ import annotations

from datetime import datetime

from ptm import report, rules, store
from ptm.judge import offline_verdict
from ptm.models import DraftRule, RuleSet, Verdict


def verdict(outcome: str, clause: str = "1.1") -> Verdict:
    return Verdict(outcome=outcome, rationale="r", confidence=0.9, policy_clause=clause)


class TestAgreement:
    def test_rules_that_are_the_judge_agree_with_it_completely(self, replayed):
        """Offline this is the situation by construction, and the number is
        therefore 100% and means nothing - which is what ``inert`` is for."""
        domain = replayed["domain"]
        result = rules.agreement(domain, "v2", domain.offline_rules["v2"],
                                 replayed["cases"], replayed["candidate"])
        assert result["compared"] == len(replayed["cases"])
        assert result["rate"] == 1.0

    def test_a_disagreement_is_reported_with_both_clauses(self, replayed):
        """What the panel needs is not "84%" but which case, and which sentence
        each side thought it was applying."""
        domain = replayed["domain"]
        judged = dict(replayed["candidate"])
        case_id = replayed["cases"][0].case_id
        judged[case_id] = verdict("deny" if judged[case_id].outcome != "deny" else "approve",
                                  clause="9.9")
        result = rules.agreement(domain, "v2", domain.offline_rules["v2"],
                                 replayed["cases"], judged)
        found = next(d for d in result["disagreements"] if d["case_id"] == case_id)
        assert found["judge_clause"] == "9.9"
        assert found["rule_outcome"] != found["judge_outcome"]

    def test_cases_the_judge_never_saw_are_skipped_not_scored(self, replayed):
        domain = replayed["domain"]
        two = {c.case_id: replayed["candidate"][c.case_id] for c in replayed["cases"][:2]}
        result = rules.agreement(domain, "v2", domain.offline_rules["v2"],
                                 replayed["cases"], two)
        assert result["compared"] == 2

    def test_agreement_reached_by_defaulting_is_counted_separately(self, replayed):
        """A rule set that matches nothing still agrees with the judge on every
        case the judge also left alone. That is agreement by accident and must
        not read as evidence the rules work."""
        domain = replayed["domain"]
        result = rules.agreement(domain, "v2", [], replayed["cases"], replayed["candidate"])
        assert result["unmatched"] == len(replayed["cases"])

    def test_clause_agreement_is_reported_apart_from_outcome_agreement(self, replayed):
        """The weaker-looking number is the one the attribution panel - and
        therefore the sweep - actually rests on."""
        domain = replayed["domain"]
        result = rules.agreement(domain, "v2", domain.offline_rules["v2"],
                                 replayed["cases"], replayed["candidate"])
        assert result["clause_compared"] > 0
        assert result["clause_agreement"] == 1.0


class TestValidation:
    def test_a_rule_reading_a_field_that_does_not_exist_is_rejected(self, expenses):
        """The failure ptm.lint was written for, from a far more prolific
        source: offline_verdict swallows the exception, so the rule simply never
        matches and the replay comes out wrong while looking healthy."""
        problems = rules.validate([{"when": "not_a_field > 1", "outcome": "deny"}],
                                  expenses, "v2")
        assert any("unknown field" in p for p in problems)

    def test_an_outcome_outside_the_domain_is_rejected(self, expenses):
        problems = rules.validate([{"when": "amount_gbp > 1", "outcome": "escalate"}],
                                  expenses, "v2")
        assert any("not one of" in p for p in problems)

    def test_a_clause_the_policy_does_not_contain_is_rejected(self, expenses):
        problems = rules.validate(
            [{"when": "amount_gbp > 1", "outcome": "deny", "clause": "9.9"}], expenses, "v2")
        assert any("absent from policy" in p for p in problems)

    def test_an_expression_that_does_not_parse_is_rejected(self, expenses):
        problems = rules.validate([{"when": "amount_gbp >", "outcome": "deny"}], expenses, "v2")
        assert any("does not parse" in p for p in problems)

    def test_the_shipped_rules_pass_their_own_validation(self, expenses):
        assert rules.validate(expenses.offline_rules["v2"], expenses, "v2") == []


class TestSubstitution:
    def test_candidate_rules_never_leak_into_the_cached_domain(self, expenses):
        """load_domain is cached per process and shared by every caller. A rule
        set being scored must not become the rule set the next replay uses."""
        before = len(expenses.offline_rules["v2"])
        swapped = rules.with_rules(expenses, "v2", [])
        assert swapped.offline_rules["v2"] == []
        assert len(expenses.offline_rules["v2"]) == before

    def test_other_versions_are_left_alone(self, expenses):
        swapped = rules.with_rules(expenses, "v2", [])
        assert swapped.offline_rules["v1"] == expenses.offline_rules["v1"]

    def test_a_generated_rule_set_converts_to_the_shape_the_judge_expects(self, expenses, seeded):
        generated = RuleSet(rules=[DraftRule(when="amount_gbp > 10000", outcome="deny",
                                             clause="5.1", because="over the limit")])
        converted = rules.as_offline_rules(generated)
        assert rules.validate(converted, expenses, "v2") == []
        domain = rules.with_rules(expenses, "v2", converted)
        case = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=1)[0]
        assert offline_verdict(case, domain, "v2").outcome in expenses.outcomes


class TestTheReadModel:
    def test_it_names_the_judge_it_scored_against(self, replayed):
        """Offline the verdicts came from these same rules. Saying so is the
        difference between a measurement and a decoration."""
        result = report.rule_agreement("expenses", "v2")
        assert result["inert"] is True
        assert result["judged_by"] == ["offline"]
        assert result["rate"] == 1.0

    def test_with_nothing_replayed_it_says_so_rather_than_scoring_nothing(self, fresh_db):
        result = report.rule_agreement("expenses", "v2")
        assert result["compared"] == 0
        assert "no verdicts on file" in result["hint"]

    def test_the_prompt_can_be_pointed_at_a_draft_that_is_not_on_disk_yet(self, expenses):
        """The proposer needs rules for a draft *before* writing it: published
        without them, an offline replay of the draft would match nothing and
        report a policy that approves everything."""
        prompt = rules.build_prompt(expenses, "v2", policy_text="1.1 Everything is denied.")
        assert "Everything is denied." in prompt
        assert "amount_gbp" in prompt, "it still lists the fields a rule may read"


class TestTheAgreementGate:
    """The number the sweep rests on, finally made load-bearing.

    ``agreement`` has always been computed and nothing ever acted on it, so a
    rule set that quietly stopped implementing the policy left every threshold
    curve confidently wrong and the panel green.
    """

    def _result(self, **overrides):
        base = {"compared": 600, "rate": 0.95, "clause_agreement": 0.95,
                "clause_compared": 400, "inert": False}
        return {**base, **overrides}

    def _domain(self, expenses, **policy):
        config = expenses.model_copy(deep=True)
        for key, value in policy.items():
            setattr(config.rules, key, value)
        return config

    def test_drifted_outcomes_are_a_finding(self, expenses):
        problems = rules.gate(self._result(rate=0.5),
                              self._domain(expenses, min_outcome_agreement=0.9))
        assert problems and "below the 90.0%" in problems[0]

    def test_the_right_answer_from_the_wrong_clause_is_also_a_finding(self, expenses):
        """The weaker-looking number, and the one attribution depends on: a rule
        reaching the judge's outcome by citing a different sentence makes the
        panel name the wrong sentence to edit."""
        problems = rules.gate(self._result(clause_agreement=0.4),
                              self._domain(expenses, min_clause_agreement=0.85))
        assert problems and "wrong sentence" in problems[0]

    def test_agreement_above_the_floor_is_silent(self, expenses):
        assert not rules.gate(
            self._result(), self._domain(expenses, min_outcome_agreement=0.9,
                                         min_clause_agreement=0.85))

    def test_an_inert_measurement_never_fires_it(self, expenses):
        """Offline the verdicts scored against came from these same rules, so
        agreement is 1.0 by construction. A gate that passes because the check
        is switched off teaches people to trust a number that means nothing."""
        assert not rules.gate(self._result(rate=0.0, inert=True),
                              self._domain(expenses, min_outcome_agreement=0.9))

    def test_nothing_measured_is_not_zero_agreement(self, expenses):
        """No verdicts on file is no evidence. Failing there would make the gate
        fire loudest on a project that has not run yet."""
        assert not rules.gate(self._result(compared=0, rate=0.0),
                              self._domain(expenses, min_outcome_agreement=0.9))

    def test_a_domain_that_sets_no_floor_enforces_nothing(self, expenses):
        assert not rules.gate(self._result(rate=0.1, clause_agreement=0.1),
                              self._domain(expenses, min_outcome_agreement=0.0,
                                           min_clause_agreement=0.0))
