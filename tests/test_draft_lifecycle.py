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

import pytest
import yaml

from ptm import config, proposal, report, store


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
