"""Snapshot retention, stranded runs, the re-run diff, and the evidence round-trip.

Four things that arrived together because they are the same gap seen from four
sides. ``replay_snapshots`` was the one table with no retention and the heaviest
row in the schema; a run recorded before judging and never resolved held
coverage open forever with no command to clear it; two runs of one version could
not be compared at all, so *the number moved and I do not know why* had no
answer; and the evidence a replay archived could leave a database but not enter
one.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from ptm import config, diff, ingest, provenance, replay, report, store
from ptm.models import Verdict


def case(case_id: str = "ret-1", amount: int = 50, receipt: str = "yes") -> dict:
    """One importable expenses case.

    Every field the domain's ``case_template`` names, ``note`` and
    ``submitted_by`` included. Leaving those two out does not fail loudly: the
    import rejects the row, nothing is written, and a replay over the empty
    database then succeeds with zero cases - so the tests below pass while
    asserting nothing at all. They did, until this docstring was needed.
    """
    return {"case_id": case_id, "subject_id": "s-1", "decided_at": "2025-03-01T00:00:00",
            "actual_outcome": "approve",
            "payload": {"case_id": case_id, "submitted_by": "Example", "grade": 2,
                        "category": "travel", "amount_gbp": amount, "receipt": receipt,
                        "days_notice": 30, "director_approval": "no", "alcohol": "no",
                        "client_driven": "no", "note": "Retention fixture"}}


def imported(*rows: dict) -> None:
    """Import cases and refuse to continue if the batch was rejected."""
    result = ingest.import_cases("expenses", list(rows) or [case()], write=True)
    assert result["committed"], result["rejected"]


def verdict(outcome: str = "deny", clause: str = "1.1") -> Verdict:
    return Verdict(outcome=outcome, rationale="because", confidence=0.9, policy_clause=clause)


def age_everything(days: int = 120) -> None:
    """Backdate every run and snapshot, so a retention clause can see them.

    Retention compares ``created_at < cutoff`` strictly, and at ``days=0`` the
    cutoff can land in the same clock tick as a snapshot written microseconds
    earlier - Windows' system clock granularity is coarse enough that it does.
    Backdating makes these tests about the retention rule rather than about how
    fast the machine running them is; the first version of this file passed
    alone and failed in sequence for exactly that reason.
    """
    stamp = (store.now_utc().replace(tzinfo=None) - timedelta(days=days)).isoformat()
    with store.conn() as c:
        c.execute("UPDATE runs SET started_at=?", (stamp,))
        c.execute("UPDATE replay_snapshots SET created_at=?", (stamp,))


@pytest.fixture
def replayed_once(fresh_db):
    """One imported case, replayed once, with its snapshot archived."""
    imported()
    result = replay.replay("expenses", "v2")
    return result["run_id"]


@pytest.fixture
def replayed_long_ago(replayed_once):
    """The same, backdated past the default retention window."""
    age_everything()
    return replayed_once


class TestSnapshotRetention:
    def test_a_fresh_snapshot_is_left_alone(self, replayed_once):
        """Recent evidence is the evidence anybody actually opens."""
        assert store.trim_snapshots_preview("expenses", days=90) == 0
        assert store.trim_snapshots("expenses", days=90) == 0
        assert "inputs" in provenance.snapshots("expenses", "v2", True)[0]

    def test_trimming_keeps_the_run_verifiable(self, replayed_long_ago):
        """The property the first attempt at this got wrong.

        Dropping the whole ``policy`` key took the judge configuration with it,
        and every trimmed run then reported its own policy as unavailable - so a
        retention pass turned verifiable history into warnings. Coverage staying
        complete and unstale is the assertion that matters here.
        """
        assert store.trim_snapshots("expenses", days=90) == 1
        after = provenance.coverage("expenses", "v2")
        assert after["complete"], after["runs"][0]["stale_reasons"]
        assert not any(r["stale"] for r in after["runs"])

        snapshot = provenance.snapshots("expenses", "v2", True)[0]
        assert snapshot["trimmed"] is True
        assert "inputs" not in snapshot and "verdicts" not in snapshot
        assert snapshot["policy"] == {"judge": {"model": "offline"}}
        for key in ("policy_hash", "baseline_hash", "input_hash", "case_ids"):
            assert key in snapshot, f"{key} is what keeps the run checkable"

    def test_trimming_is_idempotent(self, replayed_long_ago):
        assert store.trim_snapshots("expenses", days=90) == 1
        assert store.trim_snapshots("expenses", days=90) == 0

    def test_a_trimmed_run_still_answers_the_review_workspace(self, replayed_long_ago):
        """It used to raise KeyError('inputs') and take the panel down with it."""
        store.trim_snapshots("expenses", days=90)
        review = report.review_case("expenses", "v2", "ret-1")
        assert review["candidate"]["policy_text"], "falls back to the live policy"
        assert review["archived_inputs"] is False, (
            "a trimmed snapshot has no archived inputs, and saying otherwise would "
            "label today's case data as the record of what the judge was shown")

    def test_a_superseded_snapshot_is_deleted_rather_than_trimmed(self, replayed_long_ago):
        """A run nothing points at any more is not evidence, it is a leftover."""
        with store.conn() as c:
            c.execute("DELETE FROM replay_cases")
        counted = store.prune_preview("expenses", days=90)
        assert counted["replay_snapshots"] == 1
        assert counted[store.TRIMMED_KEY] == 0
        store.prune("expenses", days=90)
        assert provenance.snapshots("expenses", "v2") == []

    def test_a_pending_run_survives_retention_at_any_age(self, fresh_db):
        """Deleting an unresolved question is not the same as answering it."""
        imported()
        domain = config.load_domain("expenses")
        cases = store.load_cases("expenses", until=store.now_utc())
        provenance.begin("never-finished", "expenses", "v2",
                         provenance.capture(domain, "v2", cases))
        age_everything()
        store.prune("expenses", days=90)
        assert [r["status"] for r in provenance.snapshots("expenses", "v2")] == ["pending"]

    def test_the_dry_run_counts_what_the_write_does(self, replayed_long_ago):
        counted = store.prune_preview("expenses", days=90)[store.TRIMMED_KEY]
        assert counted == store.prune("expenses", days=90)[store.TRIMMED_KEY]

    def test_prune_reports_trimming_outside_the_removal_total(self, replayed_long_ago):
        from ptm import prune

        result = store.prune_preview("expenses", days=90)
        text = prune.describe(result, "expenses", 90, dry_run=True)
        removed = sum(v for k, v in result.items()
                      if k not in ("cutoff", store.TRIMMED_KEY))
        assert f"would remove {removed:,} row(s)" in text
        assert "trimmed" in text and "would keep the run and its hashes" in text


class TestStrandedRuns:
    def test_a_pending_run_holds_coverage_open(self, fresh_db):
        imported()
        domain = config.load_domain("expenses")
        cases = store.load_cases("expenses", until=store.now_utc())
        provenance.begin("stranded", "expenses", "v2",
                         provenance.capture(domain, "v2", cases))
        assert not provenance.coverage("expenses", "v2")["complete"]

    def test_resolving_releases_it(self, fresh_db):
        imported()
        replay.replay("expenses", "v2")
        domain = config.load_domain("expenses")
        cases = store.load_cases("expenses", until=store.now_utc())
        provenance.begin("stranded", "expenses", "v2",
                         provenance.capture(domain, "v2", cases))
        assert not provenance.coverage("expenses", "v2")["complete"]

        resolved = provenance.resolve_pending("expenses")
        assert len(resolved) == 1
        assert provenance.coverage("expenses", "v2")["complete"]

    def test_older_than_spares_a_replay_still_in_flight(self, fresh_db):
        """The guard that makes this safe against a running scheduler."""
        imported()
        domain = config.load_domain("expenses")
        cases = store.load_cases("expenses", until=store.now_utc())
        provenance.begin("in-flight", "expenses", "v2",
                         provenance.capture(domain, "v2", cases))
        assert provenance.resolve_pending("expenses", older_than_hours=24) == []
        assert provenance.resolve_pending("expenses") != []

    def test_resolving_never_touches_a_run_that_published(self, replayed_once):
        provenance.resolve_pending("expenses")
        assert [r["status"] for r in provenance.snapshots("expenses", "v2")] == ["complete"]

    def test_the_offline_replay_cli_resolves_its_own_failure(self, fresh_db, monkeypatch):
        """A crash between begin() and save_replay() must not strand the run.

        The DAG has an ``on_failure_callback`` doing this and the CLI had
        nothing, so a RuleError in a drafted rule set - or a Ctrl-C - left a
        pending row that nothing could clear.
        """
        imported()

        def explode(*args, **kwargs):
            raise KeyboardInterrupt("Ctrl-C half way through")

        monkeypatch.setattr(replay, "offline_verdict", explode)
        with pytest.raises(KeyboardInterrupt):
            replay.replay("expenses", "v2")
        assert [r["status"] for r in provenance.snapshots("expenses", "v2")] == ["failed"]

    def test_the_cli_reports_what_it_resolved(self, fresh_db, capsys):
        imported()
        domain = config.load_domain("expenses")
        cases = store.load_cases("expenses", until=store.now_utc())
        provenance.begin("stranded", "expenses", "v2",
                         provenance.capture(domain, "v2", cases))
        assert provenance.main(["expenses", "--resolve"]) == 0
        assert "marked 1 stranded run(s) failed" in capsys.readouterr().out

    def test_a_version_is_required_without_resolve(self, fresh_db):
        with pytest.raises(SystemExit) as exit_info:
            provenance.main(["expenses"])
        assert exit_info.value.code == 2


class TestComparingTwoRunsOfOneVersion:
    def test_an_identical_rerun_reports_no_movement(self, fresh_db):
        imported()
        replay.replay("expenses", "v2")
        replay.replay("expenses", "v2")
        result = report.rerun("expenses", "v2")
        assert result["differ"] == 0 and result["compared"] == 1
        assert result["changed_between"] == {
            "policy": "same", "baseline_policy": "same", "inputs": "same"}
        assert "reproducible over this history" in report.describe_rerun(result)

    def test_it_names_the_data_when_the_data_moved(self, fresh_db):
        """The finding this comparison exists to make: not the policy, the cases."""
        imported(case(amount=50, receipt="yes"))
        replay.replay("expenses", "v2")
        with store.conn() as c:
            payload = json.loads(c.execute("SELECT payload FROM cases").fetchone()["payload"])
            payload.update({"amount_gbp": 900, "receipt": "no"})
            c.execute("UPDATE cases SET payload=?", (json.dumps(payload),))
        replay.replay("expenses", "v2")

        result = report.rerun("expenses", "v2")
        assert result["outcome_changed"] == 1
        assert result["changed_between"]["inputs"] == "changed"
        assert result["changed_between"]["policy"] == "same"
        text = report.describe_rerun(result)
        assert "the historical cases changed between them" in text
        assert "the policy text and judge configuration were identical" in text

    def test_a_clause_only_move_is_counted_apart_from_an_outcome_move(self, fresh_db):
        """Same answer, different sentence credited - which moves attribution."""
        imported()
        cases = store.load_cases("expenses", until=store.now_utc())
        for run, clause in (("run-a", "1.1"), ("run-b", "2.1")):
            verdicts = {c.case_id: verdict("deny", clause) for c in cases}
            store.save_replay(run, "expenses", "v2", "actual", len(cases),
                              diff.flips(cases, verdicts, config.load_domain("expenses")),
                              0.0, verdicts)
        runs = [r["run_id"] for r in store.replay_runs("expenses", "v2")]
        result = store.compare_runs("expenses", "v2", runs[1], runs[0])
        assert result["outcome_changed"] == 0
        assert result["clause_only_changed"] == 1
        assert result["differ"] == 1

    def test_cases_only_one_run_saw_are_listed_never_counted(self, fresh_db):
        """A different window is not a disagreement."""
        imported(case("a"), case("b"))
        cases = store.load_cases("expenses", until=store.now_utc())
        both = {c.case_id: verdict() for c in cases}
        store.save_replay("wide", "expenses", "v2", "actual", len(cases), [], 0.0, both)
        store.save_replay("narrow", "expenses", "v2", "actual", 1, [], 0.0,
                          {cases[0].case_id: verdict()})
        # Through scoped_run_id, because that is what save_replay stored and what
        # replay_runs hands back - a raw id matches nothing and would make this
        # pass for having compared two empty sets.
        result = store.compare_runs("expenses", "v2",
                                    store.scoped_run_id("expenses", "wide"),
                                    store.scoped_run_id("expenses", "narrow"))
        assert result["compared"] == 1
        assert len(result["only_left"]) == 1 and result["only_right"] == []

    def test_two_runs_sharing_no_cases_is_a_refusal_not_agreement(self, fresh_db):
        """The reading this must never produce.

        A backfill writes one run per month, each covering a disjoint window, so
        two adjacent runs share no case at all. The first version of this said
        "every case came back the same, citing the same clause - this version is
        reproducible over this history", which is true of no cases and is a
        clean bill of health issued for having compared nothing.
        """
        imported(case("a"), case("b"))
        cases = {c.case_id: c for c in store.load_cases("expenses", until=store.now_utc())}
        for run, case_id in (("jan", "a"), ("feb", "b")):
            store.save_replay(run, "expenses", "v2", "actual", 1, [], 0.0,
                              {case_id: verdict()})
        assert len(cases) == 2
        result = store.compare_runs("expenses", "v2",
                                    store.scoped_run_id("expenses", "jan"),
                                    store.scoped_run_id("expenses", "feb"))
        assert result["compared"] == 0
        text = report.describe_rerun(
            {**result, "domain": "expenses", "policy_version": "v2"})
        assert "no case in common" in text
        assert "reproducible" not in text, "comparing nothing is not evidence"

    def test_the_default_pair_prefers_runs_that_actually_overlap(self, fresh_db):
        """Otherwise the default on any backfilled history compares two months.

        The shape is the real one: a backfill wrote two disjoint monthly runs,
        then somebody re-ran one window by hand. The newest run overlaps the
        *older* of the two, not the row directly beneath it.
        """
        imported(case("a"), case("b"))
        for run, ids in (("jan", ["a"]), ("feb", ["b"]), ("jan-again", ["a"])):
            store.save_replay(run, "expenses", "v2", "actual", len(ids), [], 0.0,
                              {case_id: verdict() for case_id in ids})
        # Explicit ordering: the two backfill runs land in the same second in
        # real life as well, so leaving it to the timestamp would make this test
        # depend on how run ids happen to sort.
        with store.conn() as c:
            for run, minutes_ago in (("jan", 30), ("feb", 29), ("jan-again", 1)):
                c.execute("UPDATE runs SET started_at=? WHERE run_id=?",
                          ((store.now_utc().replace(tzinfo=None)
                            - timedelta(minutes=minutes_ago)).isoformat(),
                           store.scoped_run_id("expenses", run)))
        result = report.rerun("expenses", "v2")
        assert result["compared"] == 1, (
            "the default should pair the re-run with the run it overlaps, not with "
            "whichever row happens to sit next to it")
        assert result["left"] == store.scoped_run_id("expenses", "jan")

    def test_an_unknown_cause_is_said_out_loud(self, fresh_db):
        """A run with no snapshot cannot rule a cause in or out, and says so."""
        imported()
        cases = store.load_cases("expenses", until=store.now_utc())
        for run in ("bare-a", "bare-b"):
            store.save_replay(run, "expenses", "v2", "actual", len(cases), [], 0.0,
                              {c.case_id: verdict() for c in cases})
        runs = [r["run_id"] for r in store.replay_runs("expenses", "v2")]
        result = store.compare_runs("expenses", "v2", runs[1], runs[0])
        assert result["changed_between"]["policy"] == "unknown"
        assert "cannot be checked" in report.describe_rerun(
            {**result, "domain": "expenses", "policy_version": "v2"})

    def test_one_run_is_reported_as_nothing_to_compare_not_as_an_error(
            self, replayed_once):
        """A version replayed once is the ordinary state of a version replayed once.

        This raised at first, and that made the dashboard's own panel 404 on a
        fresh database - the shape of a broken page rather than of a measurement
        nobody has taken yet. Every other read model here answers "not
        measured"; so does this.
        """
        result = report.rerun("expenses", "v2")
        assert result["available"] is False
        assert result["runs_available"] == 1
        assert result["compared"] == 0 and result["differ"] == 0
        assert "needs two" in result["hint"]
        assert "needs two" in report.describe_rerun(result)

    def test_an_unknown_run_is_refused(self, fresh_db):
        imported()
        replay.replay("expenses", "v2")
        replay.replay("expenses", "v2")
        with pytest.raises(LookupError, match="not a recorded run"):
            report.rerun("expenses", "v2", "nonsense", "also-nonsense")

    def test_the_run_list_says_which_runs_are_still_current(self, fresh_db):
        imported()
        replay.replay("expenses", "v2")
        replay.replay("expenses", "v2")
        rows = report.replay_runs("expenses", "v2")
        assert len(rows) == 2
        assert [r["current_cases"] for r in rows] == [1, 0], (
            "the newer run supersedes the older one for that case")
        assert "snapshot" not in rows[0], "the blob is megabytes; this is a list"
        assert "--rerun" in report.describe_runs(rows, "expenses", "v2")


class TestTheEvidenceRoundTrip:
    def test_an_export_reads_back_into_another_database(self, replayed_once, tmp_path):
        exported = provenance.export_snapshots("expenses", "v2")
        assert exported["format"] == provenance.SNAPSHOT_FORMAT
        assert len(exported["snapshots"]) == 1

        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(exported), encoding="utf-8")
        with store.conn() as c:
            c.execute("DELETE FROM replay_snapshots")
        result = provenance.import_snapshots(
            "expenses", json.loads(path.read_text(encoding="utf-8")))
        assert result["added"] == 1
        assert provenance.snapshots("expenses", "v2", True)[0]["inputs"]

    def test_importing_twice_adds_nothing(self, replayed_once):
        exported = provenance.export_snapshots("expenses", "v2")
        assert provenance.import_snapshots("expenses", exported)["added"] == 0
        assert provenance.import_snapshots("expenses", exported)["already_here"] == 1

    def test_a_file_of_the_wrong_shape_is_refused_by_name(self, fresh_db):
        with pytest.raises(LookupError, match="ptm-snapshots/1"):
            provenance.import_snapshots("expenses", {"format": "something-else"})

    def test_another_domains_evidence_is_refused(self, fresh_db):
        with pytest.raises(LookupError, match="refunds"):
            provenance.import_snapshots("expenses", {
                "format": provenance.SNAPSHOT_FORMAT, "domain": "refunds", "snapshots": []})

    def test_an_incomplete_row_is_refused_rather_than_half_imported(self, fresh_db):
        with pytest.raises(LookupError, match="missing required fields"):
            provenance.import_snapshots("expenses", {
                "format": provenance.SNAPSHOT_FORMAT, "domain": "expenses",
                "snapshots": [{"run_id": "half"}]})


class TestTheDomainNameCannotReachOutOfItsFolder:
    @pytest.mark.parametrize("name", [
        "../../etc/passwd", r"..\..\secrets", "a/b", ".", "..", ".hidden", "", "   "])
    def test_a_name_that_is_not_one_path_segment_is_refused(self, name):
        assert not config.is_safe_name(name)
        with pytest.raises(LookupError):
            report._domain(name)

    def test_the_shipped_domains_still_load(self):
        for name in config.available_domains():
            assert config.load_domain(name).name == name

    def test_a_malformed_domain_is_a_lookup_error_not_a_500(self, tmp_path, monkeypatch):
        """It used to reach FastAPI as an unhandled pydantic traceback."""
        monkeypatch.setattr(config, "INCLUDE_DIR", tmp_path)
        (tmp_path / "domains").mkdir()
        (tmp_path / "domains" / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
        config.load_domain.cache_clear()
        with pytest.raises(LookupError, match="configured but unreadable"):
            report._domain("broken")
        config.load_domain.cache_clear()


class TestManageExposesTheWholeLoop:
    """Every command ``manage.py`` offers resolves to a module with a ``main``."""

    def test_every_command_points_at_a_real_entry_point(self):
        import importlib

        import manage

        for command, (module_name, _flags) in manage.COMMANDS.items():
            module = importlib.import_module(module_name)
            assert hasattr(module, "main"), f"{command} -> {module_name} has no main()"

    def test_the_commands_a_real_history_needs_are_all_there(self):
        """The gap this closed: measuring your own cases and then being unable to
        get the bundle out, move the rulings, clear a stranded run or reclaim the
        disk without setting PTM_DB by hand."""
        import manage

        assert {"import", "replay", "coverage", "snapshots", "evidence",
                "resolve", "export", "rulings", "prune"} == set(manage.COMMANDS)


class TestTheMakefileFollowsTheDatabaseYouChose:
    def test_env_is_parameterised_rather_than_hardcoded(self):
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[1] / "Makefile").read_text(
            encoding="utf-8")
        assert "DB ?= ./include/ptm.db" in text, "the demo default is unchanged"
        assert "PTM_DB=$(DB)" in text, (
            "every measurement target reads $(DB); hardcoding include/ptm.db meant "
            "`make export` silently measured the demo after `make import-cases`")
        assert "PTM_DB=./include/ptm.db" not in text


def test_replay_cases_is_deliberately_not_pruned():
    """It looks like it belongs in the retention plan and does not.

    Its primary key is (domain, policy_version, case_id), so it is bounded by
    cases times versions rather than growing per run - and a row in it is what
    makes its run the current answer. Pruning it would unpick the thing it
    records, which is why the omission is asserted rather than left to be
    noticed and 'fixed'.
    """
    assert "replay_cases" not in {table for table, _ in store._PRUNE}
    assert "replay_cases" in store.DERIVED_TABLES, "a re-seed still clears it"


def test_the_retention_plan_covers_every_table_that_grows_per_run():
    pruned = {table for table, _ in store._PRUNE}
    assert {"verdict_cache", "judge_samples", "verdicts", "flips",
            "cross_checks", "replay_snapshots"} <= pruned


