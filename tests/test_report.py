"""The API surface the Diff Explorer reads.

The FastAPI plugin is only routing on top of :mod:`ptm.report`, so covering
these functions covers every endpoint without installing a web framework.
"""

from __future__ import annotations

import pytest

from ptm import diff, report


class TestDomains:
    def test_lists_the_shipped_domains_with_their_contracts(self, seeded):
        by_name = {d["name"]: d for d in report.domains()}
        assert set(by_name) == {"expenses", "refunds"}
        expenses = by_name["expenses"]
        assert expenses["outcomes"] == ["approve", "partial", "deny"]
        assert expenses["in_force"] == "v1"
        assert expenses["segment_fields"] == ["category", "grade"]
        assert expenses["policies"] == ["v1", "v2"]


class TestNotFound:
    """The plugin turns these into 404s; anything else becomes an opaque 500."""

    @pytest.mark.parametrize("call", [
        lambda: report.summary("nope", "v2"),
        lambda: report.flips("nope", "v2"),
        lambda: report.clauses("nope", "v2"),
        lambda: report.segments("nope", "v2"),
        lambda: report.compare("nope", "v1", "v2"),
        lambda: report.cost_report("nope", "v2"),
        lambda: report.stability("nope", "v2"),
        lambda: report.conflicts("nope"),
        lambda: report.precedents("nope"),
    ])
    def test_unknown_domain(self, seeded, call):
        with pytest.raises(LookupError, match="Unknown domain"):
            call()

    @pytest.mark.parametrize("call", [
        lambda: report.summary("expenses", "v99"),
        lambda: report.flips("expenses", "v99"),
        lambda: report.clauses("expenses", "v99"),
        lambda: report.segments("expenses", "v99"),
        lambda: report.cost_report("expenses", "v99"),
        lambda: report.stability("expenses", "v99"),
    ])
    def test_unknown_version(self, seeded, call):
        with pytest.raises(LookupError, match="Unknown policy version"):
            call()

    def test_compare_rejects_an_unknown_version_on_either_side(self, seeded):
        with pytest.raises(LookupError):
            report.compare("expenses", "v1", "v99")
        with pytest.raises(LookupError):
            report.compare("expenses", "v99", "v2")


class TestSummary:
    def test_reconciles_with_the_replay_it_describes(self, replayed):
        summary = report.summary("expenses", "v2")
        assert summary["cases"] == len(replayed["cases"])
        assert summary["flips"] == len(replayed["flips"])

    def test_separates_policy_driven_change_from_reviewer_deviation(self, replayed):
        summary = report.summary("expenses", "v2")
        assert summary["deviation_flips"] > 0
        assert (summary["policy_driven_flips"] + summary["deviation_flips"]
                == summary["flips"])

    def test_records_what_the_candidate_was_diffed_against(self, replayed):
        assert report.summary("expenses", "v2")["baseline_versions"] == ["v1"]

    def test_direction_counts_sum_to_the_flip_total(self, replayed):
        summary = report.summary("expenses", "v2")
        assert sum(d["n"] for d in summary["by_direction"]) == summary["flips"]

    def test_an_unreplayed_version_reports_zeroes_not_an_error(self, replayed):
        summary = report.summary("refunds", "v1")
        assert summary["flips"] == 0


class TestClausesAndSegments:
    def test_clause_rows_account_for_every_flip(self, replayed):
        rows = report.clauses("expenses", "v2")
        assert sum(r["flips"] for r in rows) == len(replayed["flips"])

    def test_the_deviation_bucket_is_flagged_as_not_policy_driven(self, replayed):
        rows = {r["clause"]: r for r in report.clauses("expenses", "v2")}
        assert rows[diff.DEVIATION]["policy_driven"] is False
        assert all(r["policy_driven"] for c, r in rows.items() if c != diff.DEVIATION)

    def test_the_new_seniority_exemption_is_attributed_to_its_own_clause(self, replayed):
        """Clause 6.1 is the headline of the proposal; before attribution existed
        its effect was invisible, folded into an unattributed default."""
        rows = {r["clause"]: r for r in report.clauses("expenses", "v2")}
        assert rows["clause 6.1"]["flips"] > 0

    def test_segments_carry_denominators_and_rates(self, replayed):
        rows = report.segments("expenses", "v2")
        assert rows
        for row in rows:
            assert row["cases"] > 0
            assert row["flip_rate"] == pytest.approx(row["flips"] / row["cases"], abs=1e-3)

    def test_every_declared_segment_field_appears(self, replayed):
        fields = {r["field"] for r in report.segments("expenses", "v2")}
        assert fields == set(replayed["domain"].segment_fields)

    def test_the_point_in_time_segment_is_present(self, replayed):
        """grade is resolved per case at its decision date, not from today."""
        grades = {r["value"] for r in report.segments("expenses", "v2")
                  if r["field"] == "grade"}
        assert {"1", "2", "3"} <= grades


class TestCompare:
    def test_compares_the_two_versions_the_baseline_pass_judged(self, replayed):
        result = report.compare("expenses", "v1", "v2")
        assert result["compared"] == len(replayed["cases"])
        assert result["differ"] > 0
        assert result["agree"] + result["differ"] == result["compared"]

    def test_a_version_compared_with_itself_never_differs(self, replayed):
        assert report.compare("expenses", "v2", "v2")["differ"] == 0

    def test_respects_the_limit(self, replayed):
        assert len(report.compare("expenses", "v1", "v2", limit=5)["differences"]) == 5


class TestCostAndStability:
    def test_offline_runs_cost_nothing_but_still_forecast(self, replayed):
        result = report.cost_report("expenses", "v2")
        assert result["ledger"]["cost_usd"] == 0
        assert result["forecast"]["estimated_cost_usd"] > 0
        assert result["forecast"]["cases"] == len(replayed["cases"])

    def test_the_baseline_forecast_is_double(self, replayed):
        forecast = report.cost_report("expenses", "v2")["forecast"]
        assert forecast["with_baseline_pass_usd"] == pytest.approx(
            forecast["estimated_cost_usd"] * 2, abs=1e-3)

    def test_unmeasured_stability_says_so_and_points_at_the_dag(self, replayed):
        result = report.stability("expenses", "v2")
        assert result["measured"] is False
        assert "judge_stability_expenses" in result["hint"]

    def test_a_measured_run_reports_its_disagreeing_cases(self, replayed):
        from ptm import cost, store

        samples = [{"case_id": "c1", "sample_idx": i, "outcome": o, "confidence": 0.8}
                   for i, o in enumerate(["approve", "deny", "approve"])]
        samples += [{"case_id": "c2", "sample_idx": i, "outcome": "deny", "confidence": 0.9}
                    for i in range(3)]
        store.save_stability("stab-1", "expenses", "v2", samples,
                             {"cases_sampled": 2, "samples_per_case": 3,
                              "unstable_cases": 1, "disagreement_rate": 0.5},
                             ledger=cost.zero())
        result = report.stability("expenses", "v2")
        assert result["measured"] is True
        assert result["disagreement_rate"] == 0.5
        assert set(result["disagreeing_cases"]) == {"c1"}, "c2 was consistent"
        assert result["disagreeing_cases"]["c1"] == {"approve": 2, "deny": 1}

        # The summary surfaces the same figure, so the tile cannot drift.
        assert report.summary("expenses", "v2")["stability"]["disagreement_rate"] == 0.5


class TestFlipsAndPrecedents:
    def test_flip_rows_carry_what_the_dashboard_renders(self, replayed):
        [row] = report.flips("expenses", "v2", limit=1)
        for field in ("case_id", "attribution", "baseline_outcome", "segments",
                      "payload", "decided_at", "actual_rationale", "precedent"):
            assert field in row
        assert isinstance(row["payload"], dict)
        assert isinstance(row["segments"], dict)

    def test_flips_are_ordered_by_impact(self, replayed):
        impacts = [r["impact"] for r in report.flips("expenses", "v2")]
        assert impacts == sorted(impacts, reverse=True)

    def test_the_limit_is_respected(self, replayed):
        assert len(report.flips("expenses", "v2", limit=3)) == 3

    def test_conflicts_are_empty_for_a_consistent_precedent_set(self, replayed):
        assert report.conflicts("expenses") == []

    def test_a_contradiction_is_reported(self, replayed):
        """Two rulings on materially identical claims that disagree."""
        from datetime import datetime

        from ptm import store
        from ptm.models import Precedent

        payload = ('{"category": "travel", "receipt": "no", '
                   '"director_approval": "no", "amount_gbp": 120}')
        with store.conn() as c:
            for case_id in ("twin-a", "twin-b"):
                c.execute("INSERT OR REPLACE INTO cases VALUES (?,?,?,?,?,?,?)",
                          (case_id, "expenses", "emp-x", "2025-05-01T00:00:00",
                           payload, "deny", ""))
        for case_id, outcome, who in [("twin-a", "approve", "alice"),
                                      ("twin-b", "deny", "bob")]:
            store.save_precedent(Precedent(
                case_id=case_id, domain="expenses", correct_outcome=outcome,
                ruled_by=who, established_at=datetime(2025, 6, 1)))

        [conflict] = report.conflicts("expenses")
        assert sorted(conflict["case_ids"]) == ["twin-a", "twin-b"]
        assert conflict["ruled_by"] == ["alice", "bob"]
        assert report.summary("expenses", "v2")["precedent_conflicts"] == 1
