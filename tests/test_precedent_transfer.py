"""Moving the only thing here that cannot be recomputed.

Every other output of this project is derivable from the cases and the policy,
which is why :func:`ptm.store.clear_domain_results` drops all of it and keeps
the rulings. Rulings are what a re-seed must survive and what retention must
never touch - and there was no way to get them out of the database or into
another one short of copying the whole SQLite file.

Two behaviours are load-bearing here and both are refusals. An import does not
overwrite a ruling somebody made in this database, because two people
disagreeing is settled by them rather than by whichever import ran last. And a
file whose shape this cannot read is refused by name rather than half-applied,
because a partial precedent set is a regression suite that passes for the wrong
reason.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from ptm import precedents, store
from ptm.models import Precedent


def ruling(case_id: str, outcome: str = "approve", by: str = "finance.lead") -> Precedent:
    return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                     ruled_by=by, note=f"{by} ruled {outcome}.",
                     established_at=datetime(2026, 1, 1), policy_version="v2",
                     judged_outcome="deny", judged_clause="1.1")


@pytest.fixture
def ruled(fresh_db):
    from ptm.seed import seed_domain

    seed_domain("expenses")
    store.save_precedent(ruling("exp-0001", "approve"))
    store.save_precedent(ruling("exp-0002", "deny", by="ops.manager"))
    # A ruling that replaced another, so the archive has something in it.
    store.save_precedent(ruling("exp-0001", "deny", by="ops.manager"))
    return fresh_db


class TestExport:
    def test_it_carries_the_rulings_and_what_they_replaced(self, ruled):
        payload = precedents.export_precedents("expenses")
        assert payload["format"] == precedents.FORMAT
        assert {p["case_id"] for p in payload["precedents"]} == {"exp-0001", "exp-0002"}
        # exp-0001 was ruled twice, so the earlier answer is in the archive.
        assert [row["case_id"] for row in payload["superseded"]] == ["exp-0001"]
        assert payload["superseded"][0]["correct_outcome"] == "approve"

    def test_the_reviewer_s_own_words_survive(self, ruled):
        """The only free text here written by the person accountable for the
        decision, and the thing a drafter is shown beside the ruling."""
        payload = precedents.export_precedents("expenses")
        assert all(p["note"] for p in payload["precedents"])

    def test_an_unknown_domain_is_a_lookup_error(self, ruled):
        with pytest.raises((LookupError, FileNotFoundError)):
            precedents.export_precedents("nosuchdomain")


class TestImport:
    def test_a_round_trip_into_an_empty_database_restores_everything(self, ruled, tmp_path,
                                                                    monkeypatch):
        payload = precedents.export_precedents("expenses")
        elsewhere = tmp_path / "other.db"
        monkeypatch.setattr(store, "DB_PATH", elsewhere)
        store.init_db()

        result = precedents.import_precedents("expenses", payload)
        assert sorted(result["added"]) == ["exp-0001", "exp-0002"]
        assert result["archived_rulings"] == 1
        restored = {p.case_id: p.correct_outcome
                    for p in store.load_precedents("expenses")}
        assert restored == {"exp-0001": "deny", "exp-0002": "deny"}
        assert store.precedent_history("expenses")

    def test_re_importing_the_same_file_changes_nothing(self, ruled):
        payload = precedents.export_precedents("expenses")
        result = precedents.import_precedents("expenses", payload)
        assert result["added"] == [] and result["replaced"] == []
        assert sorted(result["already_agreed"]) == ["exp-0001", "exp-0002"]
        assert result["archived_rulings"] == 0

    def test_a_different_answer_is_reported_and_not_applied(self, ruled):
        payload = precedents.export_precedents("expenses")
        payload["precedents"][0]["correct_outcome"] = "partial"
        payload["precedents"][0]["ruled_by"] = "someone.else"
        result = precedents.import_precedents("expenses", payload)
        assert result["replaced"] == []
        assert [row["case_id"] for row in result["conflicted"]] == \
            [payload["precedents"][0]["case_id"]]
        conflict = result["conflicted"][0]
        assert conflict["incoming"] == "partial" and conflict["incoming_ruled_by"] == \
            "someone.else"

    def test_replace_takes_the_incoming_one_and_archives_this_one(self, ruled):
        payload = precedents.export_precedents("expenses")
        payload["precedents"][0]["correct_outcome"] = "partial"
        before = len(store.precedent_history("expenses"))
        result = precedents.import_precedents("expenses", payload, replace=True)
        assert len(result["replaced"]) == 1
        # The ruling it replaced is kept, like every other supersession here.
        assert len(store.precedent_history("expenses")) == before + 1

    def test_a_file_of_another_shape_is_refused_by_name(self, ruled):
        with pytest.raises(LookupError, match="format"):
            precedents.import_precedents("expenses", {"precedents": []})

    def test_another_domain_s_rulings_are_refused(self, ruled):
        payload = precedents.export_precedents("expenses")
        payload["domain"] = "refunds"
        with pytest.raises(LookupError, match="case ids are per domain|not the ones"):
            precedents.import_precedents("expenses", payload)

    def test_an_outcome_this_domain_does_not_have_is_rejected_not_stored(self, ruled):
        payload = precedents.export_precedents("expenses")
        payload["precedents"].append({
            **payload["precedents"][0], "case_id": "exp-0003",
            "correct_outcome": "escalate"})
        result = precedents.import_precedents("expenses", payload)
        assert [row["case_id"] for row in result["rejected"]] == ["exp-0003"]
        assert "exp-0003" not in {p.case_id for p in store.load_precedents("expenses")}

    def test_a_ruling_with_no_case_here_is_imported_and_named(self, ruled):
        """Recorded, because a ruling is a fact about a case and an unseeded
        database is the normal way to receive one - but named, because the gate
        refuses to run against a precedent whose case it cannot load."""
        payload = precedents.export_precedents("expenses")
        payload["precedents"].append({
            **payload["precedents"][0], "case_id": "exp-does-not-exist"})
        result = precedents.import_precedents("expenses", payload)
        assert "exp-does-not-exist" in result["no_case_on_file"]
        assert "exp-does-not-exist" in result["added"]


class TestTheCli:
    def test_export_to_a_file_then_import_it_back(self, ruled, tmp_path, capsys):
        path = tmp_path / "rulings.json"
        assert precedents.main(["expenses", "-o", str(path)]) == 0
        assert "exported 2 ruling(s)" in capsys.readouterr().err
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["format"] == precedents.FORMAT

        assert precedents.main(["expenses", "--import", str(path)]) == 0
        assert "already said the same thing" in capsys.readouterr().out

    def test_export_to_stdout_is_json(self, ruled, capsys):
        assert precedents.main(["expenses"]) == 0
        assert json.loads(capsys.readouterr().out)["domain"] == "expenses"

    def test_an_unresolved_conflict_exits_non_zero(self, ruled, tmp_path, capsys):
        path = tmp_path / "rulings.json"
        precedents.main(["expenses", "-o", str(path)])
        capsys.readouterr()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["precedents"][0]["correct_outcome"] = "partial"
        path.write_text(json.dumps(payload), encoding="utf-8")

        assert precedents.main(["expenses", "--import", str(path)]) == 1
        assert "NOT imported" in capsys.readouterr().out

    def test_replace_resolves_it(self, ruled, tmp_path, capsys):
        path = tmp_path / "rulings.json"
        precedents.main(["expenses", "-o", str(path)])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["precedents"][0]["correct_outcome"] = "partial"
        path.write_text(json.dumps(payload), encoding="utf-8")
        capsys.readouterr()
        assert precedents.main(["expenses", "--import", str(path), "--replace"]) == 0

    def test_a_missing_file_is_an_error_not_a_traceback(self, ruled, tmp_path, capsys):
        assert precedents.main(["expenses", "--import", str(tmp_path / "gone.json")]) == 2
        err = capsys.readouterr().err
        assert err.startswith("ERROR") and "Traceback" not in err

    def test_import_needs_a_file(self, ruled, capsys):
        assert precedents.main(["expenses", "--import"]) == 2
        assert "needs a file to read" in capsys.readouterr().err

    def test_dash_o_needs_a_file(self, ruled, capsys):
        assert precedents.main(["expenses", "-o"]) == 2
        assert "needs a file to write" in capsys.readouterr().err

    def test_naming_no_domain_is_an_error(self, ruled, capsys):
        assert precedents.main([]) == 2
        assert "name a domain" in capsys.readouterr().err

    def test_an_unknown_domain_is_an_error_not_a_traceback(self, ruled, capsys):
        assert precedents.main(["nosuchdomain"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("ERROR") and "Traceback" not in err


class TestTheArchiveIsNeverOverwritten:
    def test_importing_an_archive_twice_adds_it_once(self, ruled):
        payload = precedents.export_precedents("expenses")
        before = len(store.precedent_history("expenses"))
        store.import_precedent_history(
            [{**row, "domain": "expenses"} for row in payload["superseded"]])
        store.import_precedent_history(
            [{**row, "domain": "expenses"} for row in payload["superseded"]])
        assert len(store.precedent_history("expenses")) == before

    def test_two_environments_archives_merge(self, ruled):
        payload = precedents.export_precedents("expenses")
        elsewhere = dict(payload["superseded"][0])
        elsewhere["superseded_at"] = "2020-01-01T00:00:00"
        elsewhere["ruled_by"] = "someone.elsewhere"
        added = store.import_precedent_history([{**elsewhere, "domain": "expenses"}])
        assert added == 1
