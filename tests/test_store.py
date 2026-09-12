"""Persistence: idempotency, scoping, and the latest-row-wins queries.

Manual replays deliberately overlap backfills, and Airflow retries reuse a run
id. Both mean the write path has to be safe to repeat, and the read path has to
collapse duplicates rather than double-count them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from ptm import cost, diff, store
from ptm.models import Case, Flip, Precedent, Verdict


def flip(case_id="c1", impact=100.0, clause="1.1", attribution="clause 1.1",
         direction="loosening") -> Flip:
    return Flip(case_id=case_id, decided_at=datetime(2025, 1, 1), actual_outcome="deny",
                new_outcome="approve", rationale="r", confidence=0.9, policy_clause=clause,
                impact=impact, direction=direction, attribution=attribution,
                segments={"category": "travel"}, baseline_outcome="deny")


def verdict(outcome="approve", clause="1.1") -> Verdict:
    return Verdict(outcome=outcome, rationale="r", confidence=0.9, policy_clause=clause)


def add_case(case_id="c1", outcome="deny", decided="2025-01-01T00:00:00"):
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?)",
                  (case_id, "expenses", "emp-1", decided,
                   '{"amount_gbp": 100, "category": "travel"}', outcome, "recorded"))


def save(run_id="r1", version="v2", flips=None, verdicts=None, **kwargs):
    store.save_replay(run_id, "expenses", version, "actual", 10, flips or [flip()],
                      100.0, verdicts or {"c1": verdict()}, ledger=cost.zero(), **kwargs)


class TestIdempotency:
    def test_replaying_the_same_run_id_does_not_duplicate(self, fresh_db):
        save()
        save()
        assert store.query("SELECT COUNT(*) n FROM flips")[0]["n"] == 1
        assert store.query("SELECT COUNT(*) n FROM verdicts")[0]["n"] == 1
        assert store.query("SELECT COUNT(*) n FROM runs")[0]["n"] == 1

    def test_a_case_that_stops_flipping_disappears(self, fresh_db):
        """A retry after a policy edit must not leave a stale flip in the UI."""
        save(flips=[flip("c1"), flip("c2")])
        assert store.query("SELECT COUNT(*) n FROM flips")[0]["n"] == 2
        save(flips=[flip("c1")])
        assert [r["case_id"] for r in store.query("SELECT case_id FROM flips")] == ["c1"]

    def test_segment_stats_are_replaced_not_accumulated(self, fresh_db):
        segments = [{"field": "category", "value": "travel", "cases": 10, "flips": 1,
                     "loosening": 1, "tightening": 0,
                     "impact_loosening": 100.0, "impact_tightening": 0.0}]
        save(segments=segments)
        save(segments=segments)
        [row] = store.segment_breakdown("expenses", "v2")
        assert row["cases"] == 10 and row["flips"] == 1

    def test_distinct_runs_accumulate(self, fresh_db):
        save(run_id="r1", flips=[flip("c1")])
        save(run_id="r2", flips=[flip("c2")], verdicts={"c2": verdict()})
        assert store.query("SELECT COUNT(*) n FROM flips")[0]["n"] == 2


class TestLatestWins:
    def test_flips_for_policy_keeps_only_the_newest_row_per_case(self, fresh_db):
        add_case()
        save(run_id="backfill", flips=[flip("c1", impact=100.0)])
        save(run_id="manual", flips=[flip("c1", impact=999.0)])
        rows = store.flips_for_policy("expenses", "v2")
        assert len(rows) == 1
        assert rows[0]["impact"] == 999.0

    def test_flips_carry_attribution_and_segments_back(self, fresh_db):
        add_case()
        save()
        [row] = store.flips_for_policy("expenses", "v2")
        assert row["attribution"] == "clause 1.1"
        assert row["baseline_outcome"] == "deny"
        assert '"category": "travel"' in row["segments"]

    def test_other_policy_versions_are_not_mixed_in(self, fresh_db):
        add_case()
        save(run_id="r-v2", version="v2")
        save(run_id="r-v1", version="v1")
        assert len(store.flips_for_policy("expenses", "v2")) == 1
        assert len(store.flips_for_policy("expenses", "v1")) == 1


class TestMarkReviewed:
    def test_marks_only_the_version_reviewed(self, fresh_db):
        add_case()
        save(run_id="r-v2", version="v2")
        save(run_id="r-v1", version="v1")
        store.mark_reviewed("expenses", "v2", ["c1"])
        assert store.flips_for_policy("expenses", "v2")[0]["reviewed"] == 1
        assert store.flips_for_policy("expenses", "v1")[0]["reviewed"] == 0

    def test_empty_list_is_a_no_op(self, fresh_db):
        add_case()
        save()
        store.mark_reviewed("expenses", "v2", [])
        assert store.flips_for_policy("expenses", "v2")[0]["reviewed"] == 0


class TestClauseBreakdown:
    def test_groups_by_attribution_and_flags_deviations(self, fresh_db):
        save(flips=[
            flip("c1", attribution="clause 1.1 relaxed"),
            flip("c2", attribution="clause 1.1 relaxed"),
            flip("c3", attribution=diff.DEVIATION),
        ], verdicts={"c1": verdict(), "c2": verdict(), "c3": verdict()})
        rows = {r["clause"]: r for r in store.clause_breakdown("expenses", "v2")}
        assert rows["clause 1.1 relaxed"]["flips"] == 2
        assert rows["clause 1.1 relaxed"]["policy_driven"] is True
        assert rows[diff.DEVIATION]["policy_driven"] is False

    def test_falls_back_to_the_raw_clause_for_pre_attribution_rows(self, fresh_db):
        """A row written before attribution existed still lands somewhere useful."""
        save()
        with store.conn() as c:
            c.execute("UPDATE flips SET attribution = ''")
        [row] = store.clause_breakdown("expenses", "v2")
        assert row["clause"] == "clause 1.1"


class TestBaselinePersistence:
    def test_baseline_verdicts_are_kept_under_their_own_version(self, fresh_db):
        save(baseline_version="v1", baseline_verdicts={"c1": verdict("deny", "5.1")})
        assert store.latest_verdicts("expenses", "v2")["c1"]["outcome"] == "approve"
        assert store.latest_verdicts("expenses", "v1")["c1"]["outcome"] == "deny"

    def test_both_passes_survive_a_retry(self, fresh_db):
        """The two passes share a run, so they would collide on the verdicts key."""
        for _ in range(2):
            save(baseline_version="v1", baseline_verdicts={"c1": verdict("deny", "5.1")})
        assert store.query("SELECT COUNT(*) n FROM verdicts")[0]["n"] == 2

    def test_the_run_records_what_it_was_diffed_against(self, fresh_db):
        save(baseline_version="v1", baseline_verdicts={"c1": verdict("deny")})
        assert store.query("SELECT baseline_version FROM runs")[0]["baseline_version"] == "v1"

    def test_no_baseline_writes_no_baseline_rows(self, fresh_db):
        save()
        assert store.latest_verdicts("expenses", "v1") == {}


class TestCompareVersions:
    def test_reports_only_cases_judged_under_both(self, fresh_db):
        add_case("shared")
        add_case("only_v2")
        save(run_id="r1", version="v2",
             verdicts={"shared": verdict("approve"), "only_v2": verdict("approve")},
             flips=[])
        save(run_id="r2", version="v1", verdicts={"shared": verdict("deny", "5.1")}, flips=[])
        result = store.compare_versions("expenses", "v1", "v2")
        assert result["compared"] == 1
        assert result["differ"] == 1
        assert result["differences"][0]["case_id"] == "shared"

    def test_agreement_is_counted_not_listed(self, fresh_db):
        add_case("shared")
        save(run_id="r1", version="v2", verdicts={"shared": verdict("approve")}, flips=[])
        save(run_id="r2", version="v1", verdicts={"shared": verdict("approve")}, flips=[])
        result = store.compare_versions("expenses", "v1", "v2")
        assert (result["agree"], result["differ"]) == (1, 0)

    def test_includes_the_recorded_outcome_for_context(self, fresh_db):
        add_case("shared", outcome="partial")
        save(run_id="r1", version="v2", verdicts={"shared": verdict("approve")}, flips=[])
        save(run_id="r2", version="v1", verdicts={"shared": verdict("deny")}, flips=[])
        assert store.compare_versions(
            "expenses", "v1", "v2")["differences"][0]["actual_outcome"] == "partial"


class TestCostLedger:
    def test_sums_across_runs(self, fresh_db):
        ledger = cost.estimate(40_000, 10, "anthropic:claude-sonnet-5")
        for run_id in ("r1", "r2"):
            store.save_replay(run_id, "expenses", "v2", "actual", 10, [], 0.0,
                              {}, ledger=ledger)
        total = store.cost_ledger("expenses", "v2")
        assert total["runs"] == 2
        assert total["cost_usd"] == pytest.approx(ledger["estimated_cost_usd"] * 2, abs=1e-3)
        assert total["models"] == ["anthropic:claude-sonnet-5"]

    def test_offline_runs_cost_nothing(self, fresh_db):
        save()
        assert store.cost_ledger("expenses", "v2")["cost_usd"] == 0
        assert store.cost_ledger("expenses", "v2")["models"] == ["offline"]


class TestStability:
    def test_saves_samples_and_the_headline(self, fresh_db):
        samples = [{"case_id": "c1", "sample_idx": i, "outcome": o, "confidence": 0.8}
                   for i, o in enumerate(["approve", "deny", "approve"])]
        report = {"cases_sampled": 1, "samples_per_case": 3, "unstable_cases": 1,
                  "disagreement_rate": 1.0}
        store.save_stability("s1", "expenses", "v2", samples, report,
                             ledger=cost.zero("anthropic:claude-sonnet-5"))
        latest = store.latest_stability("expenses", "v2")
        assert latest["disagreement_rate"] == 1.0
        assert store.query("SELECT COUNT(*) n FROM judge_samples")[0]["n"] == 3

    def test_none_when_never_measured(self, fresh_db):
        assert store.latest_stability("expenses", "v2") is None


class TestPrecedents:
    def test_a_precedent_is_unique_per_domain_and_case(self, fresh_db):
        for outcome in ("approve", "deny"):
            store.save_precedent(Precedent(
                case_id="c1", domain="expenses", correct_outcome=outcome,
                ruled_by="finance.lead", established_at=datetime(2025, 6, 1)))
        [row] = store.load_precedents("expenses")
        assert row.correct_outcome == "deny", "the later ruling must win"

    def test_payload_join_skips_precedents_with_no_case(self, fresh_db):
        store.save_precedent(Precedent(
            case_id="ghost", domain="expenses", correct_outcome="approve",
            ruled_by="finance.lead", established_at=datetime(2025, 6, 1)))
        assert store.precedents_with_payload("expenses") == []
        add_case("ghost")
        assert len(store.precedents_with_payload("expenses")) == 1


class TestSchema:
    def test_init_db_is_repeatable(self, fresh_db):
        store.init_db()
        store.init_db()

    def test_migrations_add_columns_to_an_older_database(self, monkeypatch, tmp_path):
        """A database seeded by an earlier version must survive the upgrade,
        because CREATE TABLE IF NOT EXISTS will not alter what already exists."""
        path = tmp_path / "legacy.db"
        legacy = sqlite3.connect(path)
        legacy.executescript("""
            CREATE TABLE flips (
                run_id TEXT NOT NULL, domain TEXT NOT NULL, policy_version TEXT NOT NULL,
                case_id TEXT NOT NULL, actual_outcome TEXT NOT NULL, new_outcome TEXT NOT NULL,
                direction TEXT NOT NULL, impact REAL NOT NULL, confidence REAL NOT NULL,
                rationale TEXT NOT NULL, reviewed INTEGER DEFAULT 0,
                PRIMARY KEY (run_id, case_id));
        """)
        legacy.commit()
        legacy.close()

        monkeypatch.setattr(store, "DB_PATH", path)
        store.init_db()
        columns = {r["name"] for r in store.query("SELECT name FROM pragma_table_info('flips')")}
        assert {"policy_clause", "segments", "attribution", "baseline_outcome"} <= columns

    def test_a_case_with_no_facts_still_loads(self, fresh_db):
        add_case()
        [case] = store.load_cases("expenses", until=datetime(2026, 9, 1))
        assert isinstance(case, Case)
        assert case.actual_rationale == "recorded"


class TestLoadingBySpecificIds:
    """The precedent gate needs *these* cases, not "some cases".

    Loading everything and filtering is subject to the default limit, so past
    that many cases the gate would judge a subset of the precedent set and
    still report a pass. A regression suite that silently checks less than it
    reports is worse than none.
    """

    def populate(self, n=30):
        for i in range(n):
            add_case(f"c{i:03d}", decided=f"2025-01-{i % 28 + 1:02d}T00:00:00")

    def test_returns_exactly_the_requested_cases(self, fresh_db):
        self.populate()
        got = store.load_cases("expenses", until=datetime(2026, 1, 1),
                               case_ids=["c003", "c017"])
        assert sorted(c.case_id for c in got) == ["c003", "c017"]

    def test_ignores_the_limit_that_would_have_truncated_them(self, fresh_db):
        self.populate()
        wanted = [f"c{i:03d}" for i in range(30)]
        got = store.load_cases("expenses", until=datetime(2026, 1, 1),
                               limit=5, case_ids=wanted)
        assert len(got) == 30, "a limit must not silently shrink an explicit id set"

    def test_a_missing_case_is_absent_rather_than_invented(self, fresh_db):
        """The caller can then refuse, which is what the gate does."""
        self.populate(3)
        got = store.load_cases("expenses", until=datetime(2026, 1, 1),
                               case_ids=["c000", "gone"])
        assert [c.case_id for c in got] == ["c000"]

    def test_handles_more_ids_than_sqlite_takes_parameters(self, fresh_db):
        """SQLite caps bound parameters, so the id list has to be chunked."""
        self.populate(0)
        wanted = [f"c{i:04d}" for i in range(1200)]
        for case_id in wanted:
            add_case(case_id)
        got = store.load_cases("expenses", until=datetime(2026, 1, 1), case_ids=wanted)
        assert len(got) == 1200

    def test_an_empty_id_list_returns_nothing_not_everything(self, fresh_db):
        self.populate()
        assert store.load_cases("expenses", until=datetime(2026, 1, 1), case_ids=[]) == []

    def test_still_hydrates_point_in_time_facts(self, fresh_db):
        add_case("c1", decided="2025-06-01T00:00:00")
        with store.conn() as c:
            c.executemany(
                "INSERT INTO subject_facts VALUES (?,?,?,?)",
                [("emp-1", "grade", "1", "2024-01-01T00:00:00"),
                 ("emp-1", "grade", "3", "2025-09-01T00:00:00")])
        [case] = store.load_cases("expenses", until=datetime(2026, 1, 1), case_ids=["c1"])
        assert case.payload["grade"] == "1", "the later promotion must not leak backwards"


class TestSegmentsAcrossOverlappingRuns:
    """Manual replays overlap backfills on purpose, so the blast radius has to
    collapse duplicates the way every other read model here does. Summing the
    per-run aggregates counts a case once per run that saw it."""

    def segments_for(self, case_ids, run_id, flips_=()):
        store.save_replay(
            run_id, "expenses", "v2", "actual", len(case_ids), list(flips_), 0.0,
            {c: verdict() for c in case_ids}, ledger=cost.zero(),
            segments=[{"field": "category", "value": "travel", "cases": len(case_ids),
                       "flips": len(flips_), "loosening": len(flips_), "tightening": 0,
                       "impact_loosening": 100.0 * len(flips_), "impact_tightening": 0.0}],
            case_segments=[{"case_id": c, "field": "category", "value": "travel"}
                           for c in case_ids])

    def test_a_second_run_over_the_same_cases_does_not_double_the_denominator(self, fresh_db):
        for case_id in ("c1", "c2", "c3"):
            add_case(case_id)
        self.segments_for(["c1", "c2", "c3"], "backfill", [flip("c1")])
        [row] = store.segment_breakdown("expenses", "v2")
        assert (row["cases"], row["flips"]) == (3, 1)

        self.segments_for(["c1", "c2", "c3"], "manual", [flip("c1")])
        [row] = store.segment_breakdown("expenses", "v2")
        assert (row["cases"], row["flips"]) == (3, 1), "the manual run counted them twice"

    def test_a_partially_overlapping_run_does_not_skew_the_rate(self, fresh_db):
        """The damaging version: a capped manual run over a subset used to
        inflate one segment's denominator but not another's."""
        for case_id in ("c1", "c2", "c3", "c4"):
            add_case(case_id)
        self.segments_for(["c1", "c2", "c3", "c4"], "backfill", [flip("c1")])
        self.segments_for(["c3", "c4"], "capped", [])
        [row] = store.segment_breakdown("expenses", "v2")
        assert row["cases"] == 4

    def test_distinct_cases_still_accumulate(self, fresh_db):
        """Deduplication must not turn a backfill's disjoint months into one."""
        for case_id in ("c1", "c2"):
            add_case(case_id)
        self.segments_for(["c1"], "month-1")
        self.segments_for(["c2"], "month-2")
        [row] = store.segment_breakdown("expenses", "v2")
        assert row["cases"] == 2

    def test_falls_back_to_the_per_run_rows_for_an_older_database(self, fresh_db):
        """A database written before case_segments existed still has to render."""
        add_case()
        save(segments=[{"field": "category", "value": "travel", "cases": 10, "flips": 1,
                        "loosening": 1, "tightening": 0,
                        "impact_loosening": 100.0, "impact_tightening": 0.0}])
        assert not store.query("SELECT * FROM case_segments")
        [row] = store.segment_breakdown("expenses", "v2")
        assert row["cases"] == 10


class TestClearingDerivedResults:
    def test_drops_recomputable_rows_for_that_domain(self, fresh_db):
        add_case()
        save()
        store.clear_domain_results("expenses")
        assert store.query("SELECT COUNT(*) n FROM flips")[0]["n"] == 0
        assert store.query("SELECT COUNT(*) n FROM runs")[0]["n"] == 0

    def test_keeps_precedent_which_is_the_one_durable_artefact(self, fresh_db):
        add_case()
        save()
        store.save_precedent(Precedent(
            case_id="c1", domain="expenses", correct_outcome="approve",
            ruled_by="a.human", established_at=datetime(2025, 1, 1)))
        store.clear_domain_results("expenses")
        assert len(store.load_precedents("expenses")) == 1

    def test_leaves_another_domain_alone(self, fresh_db):
        add_case()
        save()
        store.save_replay("r-other", "refunds", "v2", "actual", 1, [], 0.0, {},
                          ledger=cost.zero())
        store.clear_domain_results("expenses")
        assert store.query("SELECT COUNT(*) n FROM runs WHERE domain='refunds'")[0]["n"] == 1


class TestFlipStability:
    def confirmation(self, case_id="c1", stable=True):
        from ptm.models import FlipConfirmation

        return FlipConfirmation(
            case_id=case_id, samples=3,
            outcomes={"approve": 3} if stable else {"approve": 2, "deny": 1},
            modal_outcome="approve", agreement=1.0 if stable else 0.667,
            stable=stable, recorded_outcome="approve")

    def test_tags_the_flip_row_so_review_selection_can_see_it(self, fresh_db):
        add_case()
        save()
        store.save_flip_stability("expenses", "v2", [self.confirmation(stable=False)])
        [row] = store.query("SELECT stability FROM flips")
        assert row["stability"] == "unstable"

    def test_reads_back_what_the_judge_actually_said(self, fresh_db):
        add_case()
        save()
        store.save_flip_stability("expenses", "v2", [self.confirmation(stable=False)])
        measured = store.flip_stability("expenses", "v2")
        assert measured["c1"]["outcomes"] == {"approve": 2, "deny": 1}
        assert measured["c1"]["stable"] is False

    def test_re_measuring_replaces_rather_than_accumulates(self, fresh_db):
        add_case()
        save()
        store.save_flip_stability("expenses", "v2", [self.confirmation(stable=False)])
        store.save_flip_stability("expenses", "v2", [self.confirmation(stable=True)])
        measured = store.flip_stability("expenses", "v2")
        assert len(measured) == 1 and measured["c1"]["stable"] is True


class TestSeedingIsIdempotent:
    """The compose file seeds on every container start. A re-seed that always
    fired would throw away the replay you ran before restarting."""

    def test_a_second_seed_is_a_no_op(self, fresh_db):
        from ptm.seed import seed_domain

        first = seed_domain("expenses")
        second = seed_domain("expenses")
        assert first["cases"] == 600
        assert "skipped" in second and second["cases"] == 600

    def test_existing_results_survive_a_restart(self, fresh_db):
        from ptm.seed import seed_domain

        seed_domain("expenses")
        save(run_id="before-restart")
        seed_domain("expenses")
        assert store.query("SELECT COUNT(*) n FROM runs")[0]["n"] == 1

    def test_forcing_a_reseed_clears_results_computed_against_the_old_cases(self, fresh_db):
        """Aggregates joined onto a regenerated fixture would mix two histories."""
        from ptm.seed import seed_domain

        seed_domain("expenses")
        save(run_id="stale")
        seed_domain("expenses", force=True)
        assert store.query("SELECT COUNT(*) n FROM runs")[0]["n"] == 0
        assert store.query("SELECT COUNT(*) n FROM cases")[0]["n"] == 600

    def test_forcing_a_reseed_keeps_precedent(self, fresh_db):
        from ptm.seed import seed_domain

        seed_domain("expenses")
        store.save_precedent(Precedent(
            case_id="exp-0001", domain="expenses", correct_outcome="approve",
            ruled_by="a.human", established_at=datetime(2025, 1, 1)))
        seed_domain("expenses", force=True)
        assert len(store.load_precedents("expenses")) == 1

    def test_the_cli_reports_what_it_did(self, fresh_db, capsys):
        from ptm.seed import main

        assert main(["expenses"]) == 0
        assert main(["expenses"]) == 0
        assert "skipped" in capsys.readouterr().out
