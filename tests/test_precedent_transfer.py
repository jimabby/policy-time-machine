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


class TestRecordingARulingWithoutAirflow:
    """The half of the loop that needed a scheduler, and should not have.

    :func:`ptm.store.save_precedent` had three callers: the HITL task in
    ``ptm_dags.adjudicate``, the simulated reviewer in ``ptm.selftest``, and
    the import path above - which can only file a ruling somebody else already
    made. So ``manage.py`` would import your history, replay it, measure its
    coverage, export a bundle and export the rulings held against it, and there
    was no way to *make* one. The project's claim is that a human ruling becomes
    the check the next proposal faces; making one was the last thing that should
    have required standing up Airflow.
    """

    def test_it_records_the_ruling(self, ruled):
        result = precedents.record_precedent(
            "expenses", "exp-0003", "deny", "risk.lead", note="over the band")
        assert result["outcome"] == "deny"
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0003"].correct_outcome == "deny"
        assert held["exp-0003"].ruled_by == "risk.lead"
        assert held["exp-0003"].note == "over the band"

    def test_it_captures_the_circumstances_and_not_only_the_answer(self, ruled):
        """A ruling recording only the answer cannot be re-read later.

        ``policy_version`` and ``judged_outcome`` are what make staleness
        computable - see :func:`ptm.diff.stale_precedents` - and the DAG records
        them because it has just shown them to a reviewer. A shell path that
        left them blank would produce rulings that quietly read as stale beside
        ones made in the UI.
        """
        from ptm import cost, diff
        from ptm.config import load_domain
        from ptm.judge import offline_verdict

        domain = load_domain("expenses")
        cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
        candidate = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
        baseline = {c.case_id: offline_verdict(c, domain, "v1") for c in cases}
        flips = diff.flips(cases, candidate, domain, baseline=baseline)
        store.save_replay("pytest__rule", "expenses", "v2", "actual", len(cases), flips,
                          0.0, candidate, ledger=cost.zero(), baseline_version="v1",
                          baseline_verdicts=baseline)

        flipped = flips[0].case_id
        result = precedents.record_precedent("expenses", flipped, "deny", "risk.lead")
        assert result["policy_version"] == "v2"
        assert result["judged_outcome"], "the verdict being overturned was not recorded"
        held = {p.case_id: p for p in store.load_precedents("expenses")}[flipped]
        assert held.policy_version == "v2"
        assert held.established_by_run, "a ruling with no provenance"

    def test_a_case_this_database_does_not_have_is_refused(self, ruled):
        """The opposite of the import path, deliberately.

        An import accepts rulings for cases not yet loaded, because receiving a
        regression suite before the history is the normal way round. Nobody
        settles a case first-hand that they cannot read, so here a case id that
        is not on file is a typo.
        """
        with pytest.raises(LookupError, match="no case"):
            precedents.record_precedent("expenses", "exp-9999", "deny", "risk.lead")

    def test_an_outcome_the_domain_does_not_have_is_refused(self, ruled):
        with pytest.raises(LookupError, match="invalid outcome"):
            precedents.record_precedent("expenses", "exp-0003", "maybe", "risk.lead")

    def test_an_unattributed_ruling_is_refused(self, ruled):
        """Precedent is the one output that cannot be recomputed, so a ruling
        nobody's name is on is one nobody can be asked about."""
        with pytest.raises(LookupError, match="who made it"):
            precedents.record_precedent("expenses", "exp-0003", "deny", "   ")

    def test_it_does_not_overwrite_an_existing_ruling(self, ruled):
        with pytest.raises(LookupError, match="already been ruled"):
            precedents.record_precedent("expenses", "exp-0002", "approve", "someone.else")
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0002"].correct_outcome == "deny", "the ruling was replaced"

    def test_replace_takes_the_new_one_and_archives_the_old(self, ruled):
        before = len(store.precedent_history("expenses"))
        result = precedents.record_precedent(
            "expenses", "exp-0002", "approve", "someone.else", replace=True)
        assert result["replaced"] == "deny"
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0002"].correct_outcome == "approve"
        assert len(store.precedent_history("expenses")) == before + 1

    def test_pinning_a_version_with_no_recorded_change_is_refused(self, ruled):
        """Saying which candidate you ruled against is a claim, so it is checked."""
        with pytest.raises(LookupError, match="no recorded change"):
            precedents.record_precedent("expenses", "exp-0003", "deny", "risk.lead",
                                        version="v2")

    def test_the_ruling_is_exportable_like_any_other(self, ruled):
        """A ruling made here must be indistinguishable from one made in the UI."""
        precedents.record_precedent("expenses", "exp-0003", "deny", "risk.lead",
                                    note="over the band")
        payload = precedents.export_precedents("expenses")
        recorded = [p for p in payload["precedents"] if p["case_id"] == "exp-0003"]
        assert recorded and recorded[0]["note"] == "over the band"


class TestTheRulingCli:
    def test_it_records_and_reports(self, ruled, capsys):
        assert precedents.main(
            ["expenses", "--rule", "exp-0003", "deny", "--by", "risk.lead"]) == 0
        assert "risk.lead ruled exp-0003" in capsys.readouterr().out

    def test_a_note_may_begin_with_a_dash(self, ruled, capsys):
        """A reviewer's reason is free text they wrote. A minus sign can open a
        sentence, and a flag scan that reads a leading dash as the next option
        would report that --note needs a value it had just been given."""
        assert precedents.main(
            ["expenses", "--rule", "exp-0003", "deny", "--by", "risk.lead",
             "--note", "-40 GBP under the old band"]) == 0
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0003"].note.startswith("-40 GBP")

    def test_a_note_is_not_mistaken_for_a_switch(self, ruled, capsys):
        """`--note "--replace"` is an odd note and not a request to overwrite.

        A membership test over the raw argument list read it as the switch that
        replaces somebody else's ruling, which is the one act this module is
        built to make people ask for twice.
        """
        assert precedents.main(
            ["expenses", "--rule", "exp-0002", "approve", "--by", "x",
             "--note", "--replace"]) == 2
        assert "already been ruled" in capsys.readouterr().err
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0002"].correct_outcome == "deny", "the ruling was replaced"

    def test_a_conflict_exits_non_zero_without_writing(self, ruled, capsys):
        assert precedents.main(
            ["expenses", "--rule", "exp-0002", "approve", "--by", "x"]) == 2
        assert "already been ruled" in capsys.readouterr().err
        held = {p.case_id: p for p in store.load_precedents("expenses")}
        assert held["exp-0002"].correct_outcome == "deny"

    def test_replace_resolves_it(self, ruled, capsys):
        assert precedents.main(
            ["expenses", "--rule", "exp-0002", "approve", "--by", "x", "--replace"]) == 0
        assert "replaced the earlier ruling" in capsys.readouterr().out

    def test_a_missing_outcome_is_usage_rather_than_a_traceback(self, ruled, capsys):
        assert precedents.main(["expenses", "--rule", "exp-0003", "--by", "x"]) == 2
        err = capsys.readouterr().err
        assert "needs a case id and an outcome" in err and "Traceback" not in err

    def test_ruling_and_importing_are_different_acts(self, ruled, tmp_path, capsys):
        """Both write precedents and they mean opposite things: one is a
        judgement made here, the other is a file of judgements made elsewhere.
        Silently doing one when asked for both is how a regression suite gets a
        row nobody remembers agreeing to."""
        path = tmp_path / "rulings.json"
        path.write_text(json.dumps(precedents.export_precedents("expenses"),
                                   default=str), encoding="utf-8")
        assert precedents.main(["expenses", "--rule", "exp-0003", "deny",
                                "--by", "x", "--import", str(path)]) == 2
        assert "different acts" in capsys.readouterr().err

    def test_the_export_path_is_untouched(self, ruled, capsys):
        """The flags added for --rule must not change what the other forms do."""
        assert precedents.main(["expenses"]) == 0
        assert json.loads(capsys.readouterr().out)["format"] == precedents.FORMAT
