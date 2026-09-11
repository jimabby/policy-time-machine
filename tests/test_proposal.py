"""Drafting the next version of the policy, and then checking it.

The only place in this project where a machine writes rather than judges, so
the tests are weighted accordingly: most of them are about what it refuses to
write, and about the draft never being mistakable for a policy a person signed.
"""

from __future__ import annotations

import shutil
from datetime import datetime

import pytest

from ptm import config, diff, proposal, report, store
from ptm.judge import offline_verdict
from ptm.models import ClauseEdit, PolicyPatch, Precedent


@pytest.fixture
def include_copy(tmp_path, monkeypatch):
    """The real include/ directory, copied somewhere a draft may be written.

    Drafts are files, and a test that writes one into the repository would
    change what every later test - and the next demo - sees.
    """
    target = tmp_path / "include"
    shutil.copytree(config.INCLUDE_DIR, target,
                    ignore=shutil.ignore_patterns("*.db", "*.db-wal", "*.db-shm", "drafts"))
    monkeypatch.setattr(config, "INCLUDE_DIR", target)
    config.load_domain.cache_clear()
    yield target
    config.load_domain.cache_clear()


@pytest.fixture
def drafted(include_copy, replayed):
    """A written draft of expenses/v2, from the offline proposer."""
    domain = config.load_domain("expenses")
    found = proposal.evidence(domain, "v2")
    patch = proposal.offline_patch(domain, "v2", found)
    version = proposal.next_version(domain, "v2")
    written = proposal.materialise(
        "expenses", version,
        proposal.apply_to_markdown(domain, "v2", patch, version),
        proposal.rules_for(domain, "v2", patch, found))
    return {"patch": patch, "version": version, "written": written, "evidence": found}


class TestTheEvidence:
    def test_it_reads_what_the_pipeline_already_measured(self, replayed):
        """A proposer arguing from figures nobody else can see is a proposer
        nobody can check."""
        found = proposal.evidence(replayed["domain"], "v2")
        assert found["cases"] > 0
        assert any(row["clause"].startswith("clause") for row in found["clauses"])
        assert found["dials"], "expenses v2 has numeric thresholds to consider"

    def test_the_prompt_puts_reversed_rulings_in_front_of_the_drafter(self, replayed):
        store.save_precedent(Precedent(
            case_id=replayed["flips"][0].case_id, domain="expenses",
            correct_outcome=replayed["flips"][0].actual_outcome, ruled_by="finance.lead",
            note="Keep it as it was.", established_at=datetime(2026, 1, 1)))
        found = proposal.evidence(replayed["domain"], "v2")
        prompt = proposal.build_prompt(replayed["domain"], "v2", found)
        assert "finance.lead ruled" in prompt
        assert "Keep it as it was." in prompt
        assert "strongest evidence" in prompt


class TestTheOfflineProposer:
    def test_it_proposes_the_setting_that_reverses_the_fewest_rulings(self, replayed):
        domain = replayed["domain"]
        found = proposal.evidence(domain, "v2")
        patch = proposal.offline_patch(domain, "v2", found)
        assert patch.edits, "there is a dial to move and rulings to satisfy"
        edit = patch.edits[0]
        assert edit.current_text and edit.proposed_text != edit.current_text
        assert "reverses" in edit.rationale

    def test_the_amendment_actually_improves_what_it_claims_to(self, replayed):
        """The proposer optimises the gate directly, which is exactly why its
        output is not trusted - but it does at least have to be true."""
        domain = replayed["domain"]
        precedents = store.load_precedents("expenses")
        cases = store.load_cases("expenses", until=datetime(2026, 9, 1),
                                 case_ids=[p.case_id for p in precedents])
        found = proposal.evidence(domain, "v2")
        patch = proposal.offline_patch(domain, "v2", found)
        from ptm import rules as rules_engine

        amended = rules_engine.with_rules(
            domain, "v2", proposal.rules_for(domain, "v2", patch, found) or [])
        before = diff.precedent_violations(
            {c.case_id: offline_verdict(c, domain, "v2") for c in cases}, precedents)
        after = diff.precedent_violations(
            {c.case_id: offline_verdict(c, amended, "v2") for c in cases}, precedents)
        assert len(after) <= len(before)

    def test_it_refuses_rather_than_reword_a_clause_that_states_no_number(self, replayed, tmp_path, monkeypatch):
        """A clause saying "the threshold in the schedule" has nothing to move,
        and a proposer that silently appends a sentence instead has written a
        policy nobody asked for."""
        domain = replayed["domain"]
        found = proposal.evidence(domain, "v2")
        monkeypatch.setattr(proposal, "_clause_body", lambda *a, **k: "No number here.")
        patch = proposal.offline_patch(domain, "v2", found)
        assert patch.edits == []
        assert "cannot be written" in patch.risks

    def test_with_no_dials_it_says_so_instead_of_inventing_an_edit(self, expenses, replayed):
        stripped = expenses.model_copy(update={"offline_rules": {"v2": [], "v1": []}})
        patch = proposal.offline_patch(stripped, "v2", {"dials": [], "violations": [],
                                                        "clauses": []})
        assert patch.edits == []
        assert "no numeric threshold" in patch.risks

    def test_every_patch_states_what_it_could_break(self, replayed):
        domain = replayed["domain"]
        found = proposal.evidence(domain, "v2")
        patch = proposal.offline_patch(domain, "v2", found)
        assert patch.risks, "a draft with no stated risk is a draft nobody checked"


class TestTheMarkdown:
    def test_the_edited_clause_is_replaced_and_the_rest_is_left_alone(self, replayed):
        domain = replayed["domain"]
        patch = PolicyPatch(summary="s", edits=[ClauseEdit(
            clause="1.1", proposed_text="Nothing needs paperwork.", rationale="r")])
        text = proposal.apply_to_markdown(domain, "v2", patch, "v2-draft1")
        assert "1.1 Nothing needs paperwork." in text
        assert "## 2. Meals" in text, "the other sections survive"
        assert "GBP 75" not in text.split("## 2.")[0], "1.1's old text is gone"

    def test_a_new_clause_is_appended_rather_than_dropped(self, replayed):
        """A drafter proposing a new exemption is the normal case, and silently
        discarding it would make the draft disagree with the patch beside it."""
        domain = replayed["domain"]
        patch = PolicyPatch(summary="s", edits=[ClauseEdit(
            clause="9.1", proposed_text="A new exemption applies.", rationale="r")])
        text = proposal.apply_to_markdown(domain, "v2", patch, "v2-draft1")
        assert "9.1 A new exemption applies." in text

    def test_the_draft_says_in_its_own_text_that_nobody_approved_it(self, replayed):
        domain = replayed["domain"]
        patch = PolicyPatch(summary="raise the cap", edits=[ClauseEdit(
            clause="1.1", proposed_text="x", rationale="r")])
        text = proposal.apply_to_markdown(domain, "v2", patch, "v2-draft1")
        assert "Not approved by anyone" in text
        assert "v2-draft1" in text.splitlines()[2], "the title carries the draft's version"


class TestWritingItDown:
    def test_a_draft_becomes_an_ordinary_policy_version(self, drafted):
        """This is what makes it replayable, sweepable and gateable with no new
        code path anywhere."""
        domain = config.load_domain("expenses")
        assert drafted["version"] in domain.policies
        assert domain.policy_text(drafted["version"]).startswith("<!-- Drafted by")

    def test_but_never_an_indistinguishable_one(self, drafted):
        domain = config.load_domain("expenses")
        assert drafted["version"] in domain.draft_versions
        assert "v2" not in domain.draft_versions
        # And the API says so too, or the dashboard could not label it either.
        listed = next(d for d in report.domains() if d["name"] == "expenses")
        assert drafted["version"] in listed["policies"]
        assert listed["draft_versions"] == [drafted["version"]]
        assert report.summary("expenses", drafted["version"])["is_draft"] is True
        assert report.summary("expenses", "v2")["is_draft"] is False

    def test_it_carries_rules_so_an_offline_replay_of_it_means_something(self, drafted):
        """Without them the offline judge matches nothing and returns the most
        generous outcome for every case - a wildly permissive policy nobody
        wrote, reported as the draft's effect."""
        domain = config.load_domain("expenses")
        assert domain.offline_rules[drafted["version"]]
        case = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=1)[0]
        assert offline_verdict(case, domain, drafted["version"]).outcome in domain.outcomes

    def test_a_hand_written_version_is_never_shadowed_by_a_draft(self, include_copy, replayed):
        """The YAML is the file somebody is accountable for, so it wins."""
        proposal.materialise("expenses", "v2", "# not the real v2\n1.1 Everything denied.\n")
        domain = config.load_domain("expenses")
        assert "not the real v2" not in domain.policy_text("v2")
        assert "v2" not in domain.draft_versions

    def test_drafts_are_numbered_rather_than_overwritten(self, drafted):
        domain = config.load_domain("expenses")
        assert proposal.next_version(domain, "v2") == "v2-draft2"

    def test_a_draft_written_by_another_process_still_takes_its_number(self, drafted):
        """The domain config in this process can predate a draft written
        elsewhere, and a name collision here does not fail - it silently
        overwrites somebody's draft. So the filesystem is consulted too."""
        stale = config.load_domain("expenses")
        (config.INCLUDE_DIR / config.DRAFTS_DIR / "expenses" / "v2-draft2.md").write_text(
            "# written by another worker\n1.1 Denied.\n", encoding="utf-8")
        assert "v2-draft2" not in stale.policies, "the stale config cannot see it"
        assert proposal.next_version(stale, "v2") == "v2-draft3"

    def test_a_draft_is_as_cheap_to_discard_as_it_was_to_make(self, drafted):
        assert proposal.discard("expenses", drafted["version"])
        assert drafted["version"] not in config.load_domain("expenses").policies


class TestVerification:
    def test_it_reports_what_the_draft_fixes_and_what_it_breaks(self, drafted):
        result = proposal.verify("expenses", drafted["version"], "v1")
        assert result["precedents"] > 0
        assert set(result) >= {"fixed", "introduced", "violations", "base_violations"}
        assert not (set(result["fixed"]) & set(result["introduced"]))

    def test_the_policy_in_force_is_read_rather_than_re_judged(self, drafted):
        """Both sides of the comparison have to be answered by the same judge.
        Re-evaluating the in-force policy offline while the draft was judged by
        a model would put two different judges either side of the difference."""
        result = proposal.verify("expenses", drafted["version"], "v1")
        assert result["base_from_stored_verdicts"] == result["checked"]
        assert result["base_computed_offline"] == 0

    def test_where_nothing_is_stored_it_falls_back_and_says_which(self, drafted):
        """A version nothing has judged has to be evaluated here, and the result
        has to admit it rather than present a mixed comparison as like-for-like."""
        version = drafted["version"]  # nothing has ever judged the draft itself
        result = proposal.verify("expenses", version, version)
        assert result["base_computed_offline"] == result["checked"]
        assert result["base_from_stored_verdicts"] == 0

    def test_it_says_it_only_checked_the_precedent_set(self, drafted):
        """A draft that reverses no ruling has passed the regression suite, not
        been measured."""
        assert "precedent set only" in proposal.verify("expenses", drafted["version"], "v1")["caveat"]

    def test_with_no_rulings_on_file_it_says_so_rather_than_passing(self, include_copy, fresh_db):
        result = proposal.verify("expenses", "v2", "v1")
        assert result["precedents"] == 0
        assert "not been checked against anything" in result["hint"]


class TestProvenance:
    def test_a_saved_draft_keeps_the_patch_and_the_verification(self, drafted, fresh_db):
        """Six weeks later somebody opens the file and asks who wrote it and
        why. The markdown cannot answer that; this row can."""
        store.save_draft("expenses", drafted["version"], "v2",
                         drafted["patch"].model_dump(),
                         verification={"precedents": 3, "violations": []},
                         drafted_by="offline")
        row = report.drafts("expenses")[0]
        assert row["base_version"] == "v2"
        assert row["drafted_by"] == "offline"
        assert row["verification"]["precedents"] == 3
        assert row["patch"]["summary"] == drafted["patch"].summary

    def test_a_draft_whose_files_are_gone_is_marked_rather_than_linked(self, include_copy, fresh_db):
        """Provenance outlives the file, and a row pointing at a policy version
        that no longer resolves has to say so."""
        store.save_draft("expenses", "v2-draft9", "v2", {"summary": "gone"})
        assert report.drafts("expenses")[0]["available"] is False
