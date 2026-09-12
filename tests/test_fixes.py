"""Regressions for things that were silently wrong.

Each of these passed the whole suite before, which is the point: none of them
crashed, threw, or showed up as a red test. They produced a confident number
that was not the number it claimed to be.
"""

from __future__ import annotations

from datetime import datetime

from ptm import cost, disparity, store
from ptm.config import load_domain


def segment_row(field, value, cases, flips, loosening=None, tightening=0):
    loosening = flips if loosening is None else loosening
    return {"field": field, "value": value, "cases": cases, "flips": flips,
            "loosening": loosening, "tightening": tightening,
            "impact_loosening": loosening * 10.0, "impact_tightening": tightening * 10.0}


class TestDisparityReportsTheGroupItSkips:
    """``passed_over`` tested ``0.0 < ratio``, and a segment the change misses
    entirely has a ratio of exactly 0.0 - so the strongest pass-over there is
    was the one case that could never be reported."""

    def test_a_segment_nothing_moves_in_is_a_finding(self, expenses):
        rows = [segment_row("category", "hit", 100, 50),
                segment_row("category", "skipped", 100, 0)]
        found = {f.value: f for f in disparity.analyse(rows, expenses)}
        assert "skipped" in found, "a group the change entirely passes over is a finding"
        assert found["skipped"].kind == "passed_over"

    def test_it_is_told_apart_from_the_opposite_finding(self, expenses):
        """Both carry ratio 0.0 and they are opposite findings: one segment
        moving while the rest does not, and one not moving while the rest does."""
        rows = [segment_row("category", "hit", 100, 50),
                segment_row("category", "skipped", 100, 0)]
        found = {f.value: f for f in disparity.analyse(rows, expenses)}
        assert found["hit"].ratio == found["skipped"].ratio == 0.0
        assert found["hit"].kind == "concentrated"
        assert found["skipped"].kind == "passed_over"

    def test_only_the_exposure_finding_can_fail_a_run(self, expenses):
        rows = [segment_row("category", "hit", 100, 50),
                segment_row("category", "skipped", 100, 0)]
        gated = {f.value for f in disparity.gated(disparity.analyse(rows, expenses))}
        assert gated == {"hit"}, \
            "a distribution question must not stop a run; an exposure one may"

    def test_the_reading_does_not_call_an_unmoved_segment_mixed(self, expenses):
        rows = [segment_row("category", "hit", 100, 50),
                segment_row("category", "skipped", 100, 0)]
        text = disparity.describe(disparity.analyse(rows, expenses), expenses)
        assert "nothing here moved" in text
        assert "no case in this segment changed outcome" in text


class TestStaleSegmentRowsDoNotSurvive:
    """``case_segments`` was written with INSERT OR REPLACE and never cleared,
    so a re-run that saw fewer cases - or a domain that dropped a segment field -
    left rows the blast radius went on counting forever."""

    def test_a_rerun_replaces_rather_than_accumulates(self, fresh_db):
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES ('c1','d','s1','2025-01-01T00:00:00',"
                      "'{}','approve','')")
        rows = [{"case_id": "c1", "field": "category", "value": "travel"},
                {"case_id": "c1", "field": "dropped", "value": "x"}]
        store.save_replay("run-1", "d", "v2", "actual", 1, [], 0, {}, case_segments=rows)
        store.save_replay("run-1", "d", "v2", "actual", 1, [], 0, {},
                          case_segments=rows[:1])
        assert [r["field"] for r in store.query("SELECT field FROM case_segments")] == \
               ["category"]
        assert {r["field"] for r in store.segment_breakdown("d", "v2")} == {"category"}


class TestEveryCaseInTheWindow:
    """``load_cases`` defaulted to 10,000, which is the truncation the precedent
    gate goes out of its way to avoid, one order of magnitude further out."""

    def test_no_limit_means_no_limit(self, seeded):
        every = store.load_cases("expenses", until=datetime(2026, 9, 1))
        counted = store.query(
            "SELECT COUNT(*) n FROM cases WHERE domain='expenses'")[0]["n"]
        assert len(every) == counted

    def test_a_limit_is_still_honoured_when_asked_for(self, seeded):
        assert len(store.load_cases("expenses", until=datetime(2026, 9, 1), limit=5)) == 5


class TestTheImpactBandBelowOne:
    """``int(band)`` made any band under 1 zero, and every case in the domain
    then shared the signature "0+" - so the finest-grained setting a careful
    person would reach for silently made every pair of rulings a conflict."""

    def test_a_fractional_band_still_separates_cases(self):
        domain = load_domain("expenses").model_copy(deep=True)
        domain.conflicts.impact_band = 0.5
        low = domain.conflict_signature({"category": "meals", "amount_gbp": 1.2})
        high = domain.conflict_signature({"category": "meals", "amount_gbp": 9.9})
        assert low != high, "a 0.5 band must not put every claim in one bucket"

    def test_a_whole_band_is_unchanged(self):
        domain = load_domain("expenses")
        assert dict(domain.conflict_signature(
            {"category": "meals", "amount_gbp": 104}))["amount_gbp"] == "0+"
        assert dict(domain.conflict_signature(
            {"category": "meals", "amount_gbp": 260}))["amount_gbp"] == "250+"


class TestTheForecastIsScored:
    """Cost was an estimate with no way to be wrong. The vendor's own counts
    now sit beside it, and the gap is the part worth reading."""

    def test_nothing_measured_is_not_a_forecast_that_came_in_exactly_right(self):
        assert cost.reconcile(cost.estimate(1000, 10, "anthropic:claude-sonnet-5")) == \
            {"measured": False,
             "hint": "no usage reported; the ledger is the estimate alone. Usage is "
                     "collected by ptm.metered and is only available with PTM_OFFLINE=0."}

    def test_the_gap_is_reported_both_ways(self):
        ledger = {**cost.estimate(40_000, 100, "anthropic:claude-sonnet-5"),
                  **cost.from_usage([{"requests": 1, "input_tokens": 120,
                                      "output_tokens": 90}] * 100,
                                    "anthropic:claude-sonnet-5")}
        check = cost.reconcile(ledger, prompt_chars=40_000)
        assert check["measured"]
        assert check["actual_input_tokens"] == 12_000
        assert check["token_error"] == 0.2, "the estimate was 20% low on tokens"
        assert check["implied_chars_per_token"] == 3.33, \
            "the ratio that would have made the estimate right, which is the fix"

    def test_a_retry_is_a_second_billed_request(self):
        """An estimate built from 'one prompt, one request' misses the retry
        the vendor charged for."""
        priced = cost.from_usage(
            [{"requests": 2, "input_tokens": 100, "output_tokens": 50}],
            "anthropic:claude-sonnet-5")
        assert priced["actual_requests"] == 2 and priced["measured_calls"] == 1

    def test_the_ledger_keeps_both(self, fresh_db):
        store.save_replay("r", "d", "v2", "actual", 1, [], 0, {},
                          ledger={**cost.estimate(1000, 1, "anthropic:claude-sonnet-5"),
                                  **cost.from_usage([{"requests": 1, "input_tokens": 300,
                                                      "output_tokens": 100}],
                                                    "anthropic:claude-sonnet-5")})
        ledger = store.cost_ledger("d", "v2")
        assert ledger["input_tokens"] == 250 and ledger["actual_input_tokens"] == 300
