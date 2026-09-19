"""A draft's whole life: written, listed, and then decided about.

Two things the project told people to do and gave them no way to do. The lint
says to discard a draft "with ptm.proposal.discard", which was a Python
function and not a command. The .gitignore says to adopt one by moving it into
``include/policies/`` and registering it in the domain YAML, which is three
fiddly edits in two directories where the usual slip is registering the policy
and forgetting the rules - leaving an offline replay that approves everything.

Both are commands now, and adoption asks who is doing it, because that is the
step the whole pipeline defers to a person.
"""

from __future__ import annotations

import shutil
from datetime import datetime

import pytest
import yaml

from ptm import config, diff, proposal, report, store
from ptm.models import Precedent


@pytest.fixture
def workspace(tmp_path, monkeypatch, seeded):
    """A copy of the real include/ tree, so the shipped fixture is never edited."""
    include = tmp_path / "include"
    shutil.copytree(config.INCLUDE_DIR, include)
    monkeypatch.setattr(config, "INCLUDE_DIR", include)
    config.load_domain.cache_clear()
    yield include
    config.load_domain.cache_clear()


def write_draft(workspace, version="v2-draft1", clause="8.1"):
    """A draft with an offline rule citing a clause the base policy lacks."""
    folder = workspace / "drafts" / "expenses"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{version}.md").write_text(
        f"# Expense Reimbursement Policy - {version}\n\n"
        f"## 8. Added by this draft\n{clause} Claims above GBP 500 are denied.\n",
        encoding="utf-8")
    (folder / f"{version}.rules.yaml").write_text(
        yaml.safe_dump([{"when": "amount_gbp > 500", "outcome": "deny",
                         "clause": clause, "because": "over the cap"}]),
        encoding="utf-8")
    config.load_domain.cache_clear()
    return version


class TestListing:
    def test_an_empty_domain_says_how_to_get_a_draft(self, workspace):
        assert "no drafts" in proposal.describe_drafts(
            "expenses", proposal.drafts_on_disk("expenses"))

    def test_a_draft_on_disk_is_listed_without_a_provenance_row(self, workspace):
        """Files with no row were written by a hand that bypassed the DAG, and
        hiding them would be the worst of the two options."""
        write_draft(workspace)
        rows = proposal.drafts_on_disk("expenses")
        assert [r["version"] for r in rows] == ["v2-draft1"]
        assert rows[0]["available"] and not rows[0]["recorded"]

    def test_a_discarded_draft_keeps_its_provenance(self, workspace):
        write_draft(workspace)
        store.save_draft("expenses", "v2-draft1", "v2", {"summary": "cap large claims"})
        proposal.discard("expenses", "v2-draft1")
        row = proposal.drafts_on_disk("expenses")[0]
        assert not row["available"] and row["recorded"]
        assert row["summary"] == "cap large claims", \
            "what was proposed and why outlives the files"
        assert "discarded" in proposal.describe_drafts("expenses", [row])


class TestAdoption:
    def test_it_refuses_a_version_that_is_not_a_draft(self, workspace):
        with pytest.raises(LookupError, match="is not a draft"):
            proposal.adopt("expenses", "v2", "jim")

    def test_it_refuses_to_adopt_anonymously(self, workspace):
        write_draft(workspace)
        with pytest.raises(ValueError, match="who adopted it"):
            proposal.adopt("expenses", "v2-draft1", "  ")

    def test_it_refuses_to_write_over_an_existing_version(self, workspace):
        write_draft(workspace)
        with pytest.raises(LookupError, match="already has a policy version"):
            proposal.adopt("expenses", "v2-draft1", "jim", as_version="v2")

    def test_the_adopted_draft_becomes_an_ordinary_policy_version(self, workspace):
        write_draft(workspace)
        result = proposal.adopt("expenses", "v2-draft1", "jim")
        assert result["version"] == "v3"

        domain = config.load_domain("expenses")
        assert "v3" in domain.policies
        assert "v3" not in domain.draft_versions, "it is nobody's draft any more"
        assert domain.policy_text("v3")
        assert domain.clauses("v3") == ["8.1"]

    def test_the_offline_rules_come_with_it(self, workspace):
        """The slip this replaces: registering the policy and forgetting the
        rules leaves PTM_OFFLINE=1 approving every case under the new version."""
        write_draft(workspace)
        proposal.adopt("expenses", "v2-draft1", "jim")
        rules = config.load_domain("expenses").offline_rules["v3"]
        assert [r["clause"] for r in rules] == ["8.1"]

    def test_the_domain_yaml_keeps_every_comment(self, workspace):
        """It is mostly comments explaining decisions a person made, and a
        yaml.safe_dump round trip would delete all of them."""
        path = workspace / "domains" / "expenses.yaml"
        before = [line for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip().startswith("#")]
        write_draft(workspace)
        proposal.adopt("expenses", "v2-draft1", "jim")
        after = [line for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip().startswith("#")]
        assert after == before
        assert yaml.safe_load(path.read_text(encoding="utf-8")), "and it still parses"

    def test_the_adopted_text_no_longer_claims_nobody_approved_it(self, workspace):
        write_draft(workspace)
        proposal.adopt("expenses", "v2-draft1", "jim (finance)")
        text = config.load_domain("expenses").policy_text("v3")
        assert proposal.DRAFT_STAMP not in text
        assert "jim (finance)" in text and "v2-draft1" in text, \
            "who adopted it and what it came from both survive"

    def test_the_draft_files_are_gone_and_the_adoption_is_on_record(self, workspace):
        write_draft(workspace)
        store.save_draft("expenses", "v2-draft1", "v2", {"summary": "cap large claims"})
        proposal.adopt("expenses", "v2-draft1", "jim")

        row = proposal.drafts_on_disk("expenses")[0]
        assert not row["available"] and row["adopted_as"] == "v3"
        assert row["summary"] == "cap large claims", "adoption adds, it does not overwrite"
        # Adopted and discarded both leave the folder empty and are the
        # opposite decision, so the read model has to tell them apart.
        assert report.drafts("expenses")[0]["state"] == "adopted"

    def test_adopting_does_not_put_the_policy_in_force(self, workspace):
        """A version existing and a version governing are different claims."""
        write_draft(workspace)
        before = config.load_domain("expenses").in_force
        proposal.adopt("expenses", "v2-draft1", "jim")
        assert config.load_domain("expenses").in_force == before


class TestTheCommandLine:
    def test_list_discard_and_adopt_all_have_an_exit_code(self, workspace, capsys):
        write_draft(workspace)
        assert proposal.main(["expenses", "--list"]) == 0
        assert "v2-draft1" in capsys.readouterr().out

        assert proposal.main(["expenses", "--adopt", "v2-draft1", "--by", "jim"]) == 0
        assert "adopted v2-draft1 as v3" in capsys.readouterr().out

        write_draft(workspace, "v3-draft1")
        assert proposal.main(["expenses", "--discard", "v3-draft1"]) == 0
        assert "no longer a policy version" in capsys.readouterr().out

    def test_a_missing_argument_is_an_error_and_not_a_traceback(self, workspace, capsys):
        assert proposal.main(["expenses", "--adopt"]) == 2
        assert proposal.main(["expenses", "--discard"]) == 2
        assert proposal.main(["expenses", "--discard", "nope"]) == 2
        assert proposal.main(["expenses", "--wat"]) == 2

    def test_drafting_still_works_the_way_it_did(self, workspace, capsys):
        """The flags are additive; the original invocation is unchanged."""
        assert proposal.main(["expenses", "v2"]) == 0
        out = capsys.readouterr().out
        assert "not written" in out, "without --write nothing touches the disk"
        assert not (workspace / "drafts").exists()


class TestTheRulingsMadeAgainstADraft:
    """Adopting a draft must not orphan the rulings made while looking at it.

    A reviewer settles a case under ``v2-draft1``; adoption copies that exact
    text into ``include/policies/expenses/v3.md`` and deletes the draft. The
    ruling still named ``v2-draft1``, so :func:`ptm.diff.stale_precedents`
    reported it as ``version_gone`` - "what the reviewer was shown cannot be
    recovered" - permanently, while the gate went on enforcing it. The sentence
    was false the moment it was printed, and adoption is the only operation in
    the project that knows both names for the same document.
    """

    def _ruling(self, version, case_id="exp-0001", outcome="deny", by="finance.lead"):
        return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                         ruled_by=by, note="ruled while looking at the draft",
                         established_at=datetime(2026, 3, 1),
                         policy_version=version, judged_outcome="approve",
                         judged_clause="8.1")

    def test_a_ruling_made_under_the_draft_survives_adoption(self, workspace, fresh_db):
        write_draft(workspace)
        store.save_precedent(self._ruling("v2-draft1"))

        domain = config.load_domain("expenses")
        before = diff.stale_precedents(store.load_precedents("expenses"), domain,
                                       "v2-draft1")
        assert before == [], "the ruling is about the draft it names, before adoption"

        result = proposal.adopt("expenses", "v2-draft1", "jim")
        domain = proposal.reload_domain("expenses")
        after = diff.stale_precedents(store.load_precedents("expenses"),
                                      domain, result["version"])
        assert after == [], (
            "the adopted policy is the text the reviewer was shown, so the ruling is "
            f"not stale against it; got {after}")
        assert result["repointed_precedents"] == ["exp-0001"]
        assert [p.policy_version for p in store.load_precedents("expenses")] == ["v3"]

    def test_without_the_re_filing_it_would_be_version_gone(self, workspace, fresh_db):
        """The bug itself, pinned by the mechanism rather than by the symptom.

        A ruling naming a version the domain no longer has is exactly what
        ``version_gone`` is for - and it is the right answer for a draft
        somebody *discarded*. What made it wrong after adoption is that the
        version did not go anywhere; it was renamed.
        """
        write_draft(workspace)
        store.save_precedent(self._ruling("v2-draft1"))
        proposal.discard("expenses", "v2-draft1")

        domain = proposal.reload_domain("expenses")
        stale = diff.stale_precedents(store.load_precedents("expenses"), domain, "v2")
        assert [r["reason"] for r in stale] == ["version_gone"], (
            "a discarded draft really is gone, and the ruling really is unreadable")

    def test_the_archive_moves_with_the_ruling(self, workspace, fresh_db):
        """``precedent_history`` carries the same field and the same check reads it.

        Moving the live ruling and leaving its archive behind would strand the
        record of what an earlier reviewer said under a version nothing can
        resolve - which is the half of the durable artefact that exists
        precisely so a supersession cannot happen silently.
        """
        write_draft(workspace)
        store.save_precedent(self._ruling("v2-draft1", outcome="deny"))
        store.save_precedent(self._ruling("v2-draft1", outcome="partial", by="ops.lead"))

        proposal.adopt("expenses", "v2-draft1", "jim")
        archived = store.precedent_history("expenses")
        assert archived, "the first ruling was superseded and kept"
        assert {r["policy_version"] for r in archived} == {"v3"}

    def test_it_re_files_nothing_it_was_not_asked_to(self, workspace, fresh_db):
        """Rulings made against a real version are none of adoption's business."""
        write_draft(workspace)
        store.save_precedent(self._ruling("v2", case_id="exp-0002"))
        store.save_precedent(self._ruling("v2-draft1", case_id="exp-0003"))

        result = proposal.adopt("expenses", "v2-draft1", "jim")
        assert result["repointed_precedents"] == ["exp-0003"]
        by_case = {p.case_id: p.policy_version
                   for p in store.load_precedents("expenses")}
        assert by_case == {"exp-0002": "v2", "exp-0003": "v3"}

    def test_re_filing_is_a_rename_and_not_a_supersession(self, workspace, fresh_db):
        """Nothing about the ruling changed, so nothing may be archived.

        Routing this through ``save_precedent`` would have been the obvious
        implementation and would invent a supersession that never happened -
        making a re-adjudication count, which the Explorer shows beside every
        ruling, read as two.
        """
        write_draft(workspace)
        store.save_precedent(self._ruling("v2-draft1"))
        proposal.adopt("expenses", "v2-draft1", "jim")

        assert store.revision_counts("expenses") == {}, (
            "a rename must not look like somebody ruling a second time")
        [ruling] = store.load_precedents("expenses")
        assert (ruling.correct_outcome, ruling.ruled_by) == ("deny", "finance.lead")
        assert ruling.established_at == datetime(2026, 3, 1)

    def test_repointing_is_a_no_op_when_there_is_nothing_to_move(self, fresh_db):
        assert store.repoint_precedents("expenses", "", "v3") == []
        assert store.repoint_precedents("expenses", "v3", "v3") == []
        assert store.repoint_precedents("expenses", "v2-draft9", "v3") == []


class TestAdoptingDryRun:
    """The one irreversible act in the project, with a way to look first."""

    def test_it_reports_every_edit_and_makes_none_of_them(self, workspace, fresh_db):
        write_draft(workspace)
        store.save_precedent(
            Precedent(case_id="exp-0001", domain="expenses", correct_outcome="deny",
                      ruled_by="finance.lead", established_at=datetime(2026, 3, 1),
                      policy_version="v2-draft1"))
        yaml_before = (workspace / "domains" / "expenses.yaml").read_text(encoding="utf-8")

        plan = proposal.adopt("expenses", "v2-draft1", "jim", dry_run=True)

        assert plan["dry_run"] and plan["version"] == "v3"
        assert plan["repointed_precedents"] == ["exp-0001"]
        assert plan["offline_rules"] == 1
        assert not (workspace / "policies" / "expenses" / "v3.md").exists()
        assert (workspace / "drafts" / "expenses" / "v2-draft1.md").exists()
        assert (workspace / "domains" / "expenses.yaml").read_text(encoding="utf-8") \
            == yaml_before
        assert [p.policy_version for p in store.load_precedents("expenses")] \
            == ["v2-draft1"], "a dry run does not touch the durable artefact either"

    def test_it_refuses_exactly_what_the_real_thing_refuses(self, workspace, fresh_db):
        """A dry run that accepts what adoption would reject is worse than none."""
        write_draft(workspace)
        with pytest.raises(LookupError):
            proposal.adopt("expenses", "v2", "jim", dry_run=True)
        with pytest.raises(ValueError):
            proposal.adopt("expenses", "v2-draft1", "  ", dry_run=True)
        with pytest.raises(LookupError):
            proposal.adopt("expenses", "v2-draft1", "jim", as_version="v2", dry_run=True)

    def test_the_plan_matches_what_adopting_then_does(self, workspace, fresh_db):
        write_draft(workspace)
        plan = proposal.adopt("expenses", "v2-draft1", "jim", dry_run=True)
        done = proposal.adopt("expenses", "v2-draft1", "jim")
        shared = ("draft", "version", "by", "policy", "domain_yaml", "offline_rules",
                  "repointed_precedents")
        assert {k: plan[k] for k in shared} == {k: done[k] for k in shared}
        assert plan["would_remove"] == done["removed"]

    def test_the_cli_says_would_rather_than_did(self, workspace, fresh_db, capsys):
        write_draft(workspace)
        assert proposal.main(
            ["expenses", "--adopt", "v2-draft1", "--by", "jim", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "would adopt v2-draft1 as v3" in out
        assert "nothing was written" in out
        assert not (workspace / "policies" / "expenses" / "v3.md").exists()
