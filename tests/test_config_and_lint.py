"""Domain configuration, and the lint that keeps it honest.

The lint matters more than it looks: :func:`ptm.judge.offline_verdict` swallows
exceptions by design, so a rule naming a field that does not exist does not
crash - it silently never matches, and the replay comes out wrong while looking
completely healthy.
"""

from __future__ import annotations

import textwrap

import pytest

from ptm.config import available_domains, load_domain
from ptm.lint import check_domain, payload_fields, template_fields
from ptm.lint import main as lint_main


class TestDomainContract:
    def test_shipped_domains_load(self):
        assert set(available_domains()) == {"expenses", "refunds"}

    def test_outcomes_are_ordered_generous_to_strict(self, expenses):
        assert expenses.direction("deny", "approve") == "loosening"
        assert expenses.direction("approve", "deny") == "tightening"
        assert expenses.direction("approve", "approve") == "lateral"

    def test_unknown_outcomes_are_lateral_not_a_crash(self, expenses):
        assert expenses.direction("approve", "nonsense") == "lateral"

    def test_validate_outcome_rejects_what_is_not_in_the_contract(self, expenses):
        assert expenses.validate_outcome("approve") == "approve"
        with pytest.raises(ValueError, match="invalid outcome"):
            expenses.validate_outcome("maybe")

    def test_unknown_policy_version_names_what_is_available(self, expenses):
        with pytest.raises(KeyError, match="v99"):
            expenses.policy_text("v99")

    def test_render_case_leaves_missing_fields_blank(self, expenses):
        rendered = expenses.render_case({"case_id": "exp-1"})
        assert "exp-1" in rendered
        assert "{grade}" not in rendered

    def test_impact_of_survives_junk(self, expenses):
        assert expenses.impact_of({"amount_gbp": "120.5"}) == 120.5
        assert expenses.impact_of({"amount_gbp": None}) == 0.0
        assert expenses.impact_of({"amount_gbp": "not a number"}) == 0.0
        assert expenses.impact_of({}) == 0.0

    def test_segments_are_stringified_so_buckets_do_not_split(self, expenses):
        """grade arrives as an int from the seeder and a str from hydration."""
        assert expenses.segments_of({"category": "meals", "grade": 3})["grade"] == "3"
        assert expenses.segments_of({"category": "meals", "grade": "3"})["grade"] == "3"

    def test_missing_segment_values_become_unknown(self, expenses):
        assert expenses.segments_of({})["category"] == "unknown"

    def test_conflict_signature_bands_the_impact_field(self, expenses):
        base = {"category": "travel", "receipt": "no", "director_approval": "no"}
        near = expenses.conflict_signature({**base, "amount_gbp": 104})
        also = expenses.conflict_signature({**base, "amount_gbp": 111})
        far = expenses.conflict_signature({**base, "amount_gbp": 900})
        assert near == also
        assert near != far

    def test_conflict_signature_is_none_when_disabled(self, expenses):
        bare = expenses.model_copy(deep=True)
        bare.conflicts.key = []
        assert bare.conflict_signature({"category": "travel"}) is None

    def test_clauses_are_parsed_from_the_policy_markdown(self, expenses):
        assert "1.1" in expenses.clauses("v1")
        assert "6.1" in expenses.clauses("v2"), "the new seniority exemption"

    def test_in_force_names_a_real_version(self):
        for name in available_domains():
            domain = load_domain(name)
            assert domain.in_force in domain.policies

    def test_the_judge_is_shown_the_point_in_time_fact(self, expenses):
        """It must be rendered, not merely present in the payload - otherwise the
        judge is told to respect a value it never sees."""
        assert expenses.pit_field in template_fields(expenses)

    def test_payload_fields_add_the_point_in_time_fact(self, expenses):
        assert payload_fields(expenses) >= template_fields(expenses)


class TestShippedDomainsLintClean:
    @pytest.mark.parametrize("name", ["expenses", "refunds"])
    def test_no_errors(self, name):
        errors = [p for p in check_domain(name) if p.level == "ERROR"]
        assert not errors, "\n".join(str(p) for p in errors)

    @pytest.mark.parametrize("name", ["expenses", "refunds"])
    def test_every_rule_cites_a_clause_that_exists(self, name):
        domain = load_domain(name)
        for version, rules in domain.offline_rules.items():
            declared = set(domain.clauses(version))
            for rule in rules:
                assert str(rule["clause"]) in declared

    def test_exit_code_is_zero(self, capsys):
        assert lint_main(["expenses", "refunds"]) == 0


BASE = """
name: broken
label: broken domain
impact_field: amount_gbp
impact_unit: GBP
outcomes: [approve, deny]
policies: {{v1: policies/broken/v1.md}}
in_force: v1
case_template: |
  Amount: {{amount_gbp}}
  Receipt: {{receipt}}
offline_rules:
  v1:
{rules}
"""

GOOD_RULE = """
    - when: "receipt == 'no'"
      outcome: deny
      clause: "1.1"
"""


class TestLintCatchesDrift:
    """Each of these is a way an offline fixture can silently stop matching the
    policy it claims to implement, with no error at runtime."""

    @pytest.fixture
    def broken(self, tmp_path, monkeypatch):
        import ptm.config as config

        monkeypatch.setattr(config, "INCLUDE_DIR", tmp_path)
        config.load_domain.cache_clear()
        yield tmp_path
        config.load_domain.cache_clear()

    def write(self, tmp_path, rules=GOOD_RULE, extra="",
              policy="1.1 A receipt is required.\n"):
        """Write a deliberately broken domain.

        ``rules`` is indented into the template; ``extra`` is appended after the
        template is assembled, so it can add top-level keys without disturbing
        the indentation of everything above it.
        """
        body = BASE.format(rules=textwrap.indent(textwrap.dedent(rules).strip(), "    "))
        body += extra
        (tmp_path / "domains").mkdir(exist_ok=True)
        (tmp_path / "policies" / "broken").mkdir(parents=True, exist_ok=True)
        (tmp_path / "policies" / "broken" / "v1.md").write_text(policy, encoding="utf-8")
        (tmp_path / "domains" / "broken.yaml").write_text(body, encoding="utf-8")
        return body

    def errors(self):
        return [str(p) for p in check_domain("broken") if p.level == "ERROR"]

    def test_the_template_itself_lints_clean(self, broken):
        """Guards the guards. If the base template were malformed, every test
        below would 'pass' on an unrelated config-load error."""
        self.write(broken)
        assert self.errors() == []

    def test_a_rule_naming_a_field_that_does_not_exist(self, broken):
        self.write(broken, rules="""
            - when: "employee_grade > 2"
              outcome: deny
              clause: "1.1"
        """)
        assert any("unknown field" in e for e in self.errors())

    def test_a_rule_citing_a_clause_the_policy_lacks(self, broken):
        self.write(broken, rules="""
            - when: "receipt == 'no'"
              outcome: deny
              clause: "9.9"
        """)
        assert any("does not contain" in e for e in self.errors())

    def test_a_rule_that_does_not_parse(self, broken):
        self.write(broken, rules="""
            - when: "amount_gbp >"
              outcome: deny
              clause: "1.1"
        """)
        assert any("does not parse" in e for e in self.errors())

    def test_an_outcome_outside_the_contract(self, broken):
        self.write(broken, rules="""
            - when: "receipt == 'no'"
              outcome: reject
              clause: "1.1"
        """)
        assert any("not one of" in e for e in self.errors())

    def test_a_confidence_outside_zero_to_one(self, broken):
        self.write(broken, rules="""
            - when: "receipt == 'no'"
              outcome: deny
              clause: "1.1"
              confidence: 3.0
        """)
        assert any("outside 0..1" in e for e in self.errors())

    def test_a_rule_with_no_when_expression(self, broken):
        self.write(broken, rules="""
            - outcome: deny
              clause: "1.1"
        """)
        assert any("no 'when'" in e for e in self.errors())

    def test_a_segment_field_that_is_not_a_case_field(self, broken):
        self.write(broken, extra="segment_fields: [nonexistent]\n")
        assert any("segment_fields" in e for e in self.errors())

    def test_a_conflict_key_that_is_not_a_case_field(self, broken):
        self.write(broken, extra="conflicts:\n  key: [missing_field]\n")
        assert any("conflicts.key" in e for e in self.errors())

    def test_an_impact_field_the_template_never_renders(self, broken):
        self.write(broken, extra="impact_field: not_rendered\n")
        assert any("impact_field" in e for e in self.errors())

    def test_a_point_in_time_field_the_judge_never_sees(self, broken):
        """A pit_field missing from the template means the judge is asked to
        respect a fact it is never shown."""
        self.write(broken, extra="pit_field: tier\n")
        assert any("pit_field" in e for e in self.errors())

    def test_offline_rules_for_a_version_that_does_not_exist(self, broken):
        self.write(broken, extra="  v9:\n    - when: \"receipt == 'no'\"\n"
                                 "      outcome: deny\n      clause: \"1.1\"\n")
        assert any("no matching policy version" in e for e in self.errors())

    def test_lint_exits_non_zero_on_errors(self, broken, capsys):
        self.write(broken, rules="""
            - when: "employee_grade > 2"
              outcome: deny
              clause: "9.9"
        """)
        assert lint_main(["broken"]) == 1

    def test_a_domain_that_will_not_load_is_reported_not_raised(self, broken):
        (broken / "domains").mkdir(exist_ok=True)
        (broken / "domains" / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
        problems = check_domain("broken")
        assert problems and problems[0].level == "ERROR"
        assert "will not load" in str(problems[0])


class TestConflictKeyAgainstTheStoredRecord:
    """The check that used to be unreachable.

    payload_fields is the rendered template *plus* the point-in-time fact, and
    a pit_field missing from the template is already an error - so "in the
    payload but not rendered" could never be true, and the warning it guarded
    never fired for any domain. It is tested against pit_field directly now,
    because the failure it describes is real: conflict detection reads the case
    as filed, and the point-in-time fact is merged in per case, not stored.
    """

    def domain_keyed_on(self, expenses, key):
        d = expenses.model_copy(deep=True)
        d.conflicts.key = key
        return d

    def check(self, monkeypatch, domain):
        import ptm.config as config
        import ptm.lint as lint

        monkeypatch.setattr(config, "load_domain", lambda name: domain)
        monkeypatch.setattr(lint, "load_domain", lambda name: domain)
        return [str(p) for p in check_domain("expenses")]

    def test_rejects_the_point_in_time_fact_as_a_conflict_key(self, expenses, monkeypatch):
        problems = self.check(monkeypatch, self.domain_keyed_on(expenses, ["category", "grade"]))
        assert any("ERROR" in p and "conflicts.key" in p and "point-in-time" in p
                   for p in problems), problems

    def test_accepts_a_key_that_is_in_the_stored_record(self, expenses, monkeypatch):
        problems = self.check(monkeypatch, self.domain_keyed_on(expenses, ["category", "receipt"]))
        assert not any("conflicts.key" in p for p in problems), problems

    def test_the_shipped_domains_pass_it(self):
        """refunds used to key on tier, which is its pit_field."""
        for name in ("expenses", "refunds"):
            assert not [p for p in check_domain(name)
                        if p.level == "ERROR"], f"{name} has lint errors"


class TestHelperShadowing:
    def test_warns_when_a_case_field_hides_a_rule_helper(self, expenses, monkeypatch):
        """offline_verdict swallows exceptions, so a rule calling a shadowed
        helper does not crash - it silently never matches."""
        import ptm.config as config
        import ptm.lint as lint

        d = expenses.model_copy(deep=True)
        d.case_template = d.case_template + "\n  Length: {len}"
        monkeypatch.setattr(config, "load_domain", lambda name: d)
        monkeypatch.setattr(lint, "load_domain", lambda name: d)
        problems = [str(p) for p in check_domain("expenses")]
        assert any("shadow the rule helpers" in p for p in problems), problems

    def test_quiet_for_the_shipped_domains(self):
        for name in ("expenses", "refunds"):
            assert not [p for p in check_domain(name) if "shadow" in p.message]

    def test_a_helper_call_is_not_reported_as_an_unknown_field(self, expenses, monkeypatch):
        """The lint must know abs() is a helper, not a missing payload key."""
        import ptm.config as config
        import ptm.lint as lint

        d = expenses.model_copy(deep=True)
        d.offline_rules = {"v2": [{"when": "abs(amount_gbp) > 10", "outcome": "deny",
                                   "clause": "1.1"}]}
        monkeypatch.setattr(config, "load_domain", lambda name: d)
        monkeypatch.setattr(lint, "load_domain", lambda name: d)
        assert not [p for p in check_domain("expenses")
                    if p.level == "ERROR" and "unknown field" in p.message]
