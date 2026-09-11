"""Reading the policy before paying to replay it.

The division of labour matters: :mod:`ptm.lint` checks the offline *fixtures*
against the policy, and this checks the **policy markdown** - the artefact the
real judge is shown and the one the lint never looks inside.

Both halves of the calibration are tested here. A check that fires on the
shipped policies would be noise; a check that fires on nothing would be
decoration. So: no blocking findings on what ships, and a real finding on each
malformation, written out as a policy file rather than described.
"""

from __future__ import annotations

import pytest

from ptm import preflight, report
from ptm.config import DomainConfig


def policy(tmp_path, monkeypatch, text: str, outcomes=("approve", "deny")) -> DomainConfig:
    """A one-version domain whose policy is exactly ``text``."""
    from ptm import config

    (tmp_path / "policies").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policies" / "vx.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "INCLUDE_DIR", tmp_path)
    return DomainConfig(name="scratch", label="scratch decisions", outcomes=list(outcomes),
                        case_template="Amount: {amount}\n", policies={"vx": "policies/vx.md"})


def kinds(findings) -> set[str]:
    return {f.kind for f in findings}


class TestBlockingProblems:
    def test_a_clause_number_used_twice_merges_two_rules_into_one_bucket(self, tmp_path, monkeypatch):
        """Attribution groups on the clause identifier, so a duplicate makes the
        breakdown name the wrong sentence to edit."""
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 A claim is approved.
1.1 A claim is denied.
""")
        findings = preflight.structural(domain, "vx")
        assert "duplicate" in kinds(findings)
        assert preflight.blocking(findings)

    def test_a_reference_to_a_clause_that_does_not_exist_is_blocking(self, tmp_path, monkeypatch):
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Approved unless clause 9.4 applies.
""")
        finding = next(f for f in preflight.structural(domain, "vx") if f.kind == "unreachable")
        assert finding.clause == "9.4"
        assert finding.severity == "error"

    def test_a_policy_with_no_numbered_clauses_at_all_is_blocking(self, tmp_path, monkeypatch):
        """Every change it causes would come back attributed to nothing, which
        is the one output this pipeline exists to produce."""
        domain = policy(tmp_path, monkeypatch, "# P\n\nClaims must be approved promptly.\n")
        findings = preflight.structural(domain, "vx")
        assert findings[0].kind == "unnumbered"
        assert findings[0].severity == "error"

    def test_a_whole_section_number_is_a_legitimate_reference(self, tmp_path, monkeypatch):
        """Policies say "unless clause 3 applies", meaning the section. Treating
        that as a dangling reference would fire on every well-written policy."""
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Approved unless clause 3 applies.
## 3. Three
3.1 Denied.
""")
        assert not preflight.blocking(preflight.structural(domain, "vx"))


class TestWarnings:
    def test_a_rule_written_as_prose_with_no_clause_number_is_flagged(self, tmp_path, monkeypatch):
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Approved on the usual terms.

Anything over the limit must be denied.
""")
        findings = [f for f in preflight.structural(domain, "vx") if f.kind == "unnumbered"]
        assert findings and "must be denied" in findings[0].detail

    def test_narration_without_an_obligation_is_not_mistaken_for_a_rule(self, tmp_path, monkeypatch):
        """Every shipped policy opens with a rationale paragraph. Flagging those
        is how a panel earns being ignored."""
        domain = policy(tmp_path, monkeypatch, """# P

Rationale: the team spends more on chasing paperwork than it recovers.

## 1. One
1.1 Approved on the usual terms, otherwise denied.
""")
        assert "unnumbered" not in kinds(preflight.structural(domain, "vx"))

    def test_a_clause_defined_only_by_pointing_at_another_version_is_empty(self, tmp_path, monkeypatch):
        """The judge is shown one policy at a time, so it cannot follow the
        pointer - the clause decides nothing at judging time."""
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Approved on the usual terms, otherwise denied.
## 2. Two
2.1 Unchanged from v1.
""")
        finding = next(f for f in preflight.structural(domain, "vx") if f.kind == "unreachable")
        assert finding.clause == "2.1"
        assert finding.severity == "warning"

    def test_a_clause_that_says_unchanged_and_then_says_what_it_is_is_fine(self, tmp_path, monkeypatch):
        """The problem is being defined by reference to a document the judge
        cannot see - not the word "unchanged"."""
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Approved on the usual terms, otherwise denied.
## 2. Two
2.1 Unchanged - approved on proper notice.
""")
        assert "unreachable" not in kinds(preflight.structural(domain, "vx"))

    def test_an_outcome_the_policy_never_mentions_is_flagged(self, tmp_path, monkeypatch):
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 Every case is approved.
""", outcomes=("approve", "escalate"))
        finding = next(f for f in preflight.structural(domain, "vx")
                       if f.kind == "undefined_outcome")
        assert "escalate" in finding.detail

    def test_outcomes_are_matched_on_stems_not_on_identifiers(self, tmp_path, monkeypatch):
        """Outcomes are identifiers (``no_refund``, ``deny``) and policies are
        prose (``no refund is payable``, ``the case is denied``). Exact matching
        finds nothing and reports every domain as broken."""
        domain = policy(tmp_path, monkeypatch, """# P
## 1. One
1.1 The case is approved, or denied where no refund is payable.
""", outcomes=("approve", "deny", "no_refund"))
        assert "undefined_outcome" not in kinds(preflight.structural(domain, "vx"))


class TestTheShippedPolicies:
    @pytest.mark.parametrize("domain_name,version",
                             [("expenses", "v1"), ("expenses", "v2"),
                              ("refunds", "v1"), ("refunds", "v2")])
    def test_nothing_shipped_would_block_a_replay(self, seeded, domain_name, version):
        assert report.preflight(domain_name, version)["blocking"] == 0

    def test_it_does_find_the_one_real_problem_in_the_shipped_candidate(self, seeded):
        """v2's discretion clause reads "Unchanged from v1." - and a judge shown
        only v2 has nothing to apply. A check that finds nothing in a real
        document is not evidence the document is clean."""
        findings = report.preflight("expenses", "v2")["findings"]
        assert [f["clause"] for f in findings] == ["7.1"]

    def test_the_read_model_says_what_it_did_not_check(self, seeded):
        assert "structural only" in report.preflight("expenses", "v2")["caveat"]


class TestTheLintRunsItToo:
    def test_one_command_covers_the_policy_as_well_as_the_fixtures(self, seeded):
        """Two halves of the same drift, and nobody remembers to run two
        commands."""
        from ptm import lint

        messages = [p.message for p in lint.check_domain("expenses")]
        assert any("[unreachable]" in m for m in messages)
