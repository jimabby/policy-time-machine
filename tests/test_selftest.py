"""The whole loop, end to end, on an isolated database.

Also pins the figures the README quotes. A fixture or rule change that moves
them is fine - but it has to move the documentation too, and this is what
notices.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest

from ptm import diff, pit_check, selftest, store
from ptm.judge import offline_verdict

NOW = datetime(2026, 9, 1)


@pytest.fixture(scope="function")
def output(fresh_db, capsys):
    selftest.main("expenses", "v2")
    return capsys.readouterr().out


def number_before(text: str, phrase: str) -> int:
    match = re.search(rf"(\d[\d,]*)\s+{re.escape(phrase)}", text)
    assert match, f"{phrase!r} not found in:\n{text}"
    return int(match.group(1).replace(",", ""))


class TestWholeLoop:
    def test_replays_the_documented_history(self, output):
        assert number_before(output, "decisions under policy v2") == 600

    def test_finds_the_documented_number_of_changes(self, output):
        assert number_before(output, "outcomes change") == 147

    def test_routes_only_a_handful_to_a_human(self, output):
        """The economics of the whole system: humans see tens, not thousands."""
        assert number_before(output, "flips routed to a human") == 8

    def test_establishes_precedent(self, output):
        assert number_before(output, "precedents established") == 8

    def test_the_gate_catches_the_reversal(self, output):
        assert "GATE FAILS" in output
        assert number_before(output, "violation(s)") == 2

    def test_the_gate_separates_what_the_proposal_introduced(self, output):
        """A reversal the policy in force already makes is not v2's doing.

        The fixture's reviewer sides with history on the big tightenings, and
        every tightening in the human queue is a deviation - a case v1 decides
        the same way v2 does. So v2 introduces nothing here, and saying so is
        the difference between a gate that informs and one that just alarms.
        """
        assert "0 introduced by v2" in output
        assert "every reversal is one policy v1 already makes" in output

    def test_reserves_most_of_the_human_budget_for_the_proposal(self, output):
        """Deviations are the biggest flips by money and would otherwise take
        the whole queue, leaving the proposal itself unreviewed."""
        match = re.search(r"\((\d+) caused by v2, (\d+) pre-existing deviations\)", output)
        assert match, output
        caused, deviations = int(match.group(1)), int(match.group(2))
        assert caused + deviations == 8
        assert deviations <= 2, "the deviation budget is capped at max_deviation_reviews"
        assert caused >= 6

    def test_reports_the_deviations_it_did_not_queue(self, output):
        """Capping them in the queue must not mean hiding them."""
        assert "38 recorded outcomes disagree with policy v1" in output

    def test_attributes_every_change_to_something(self, output):
        from ptm.diff import UNEXPLAINED

        assert UNEXPLAINED not in output

    def test_separates_policy_effects_from_reviewer_deviation(self, output):
        assert re.search(r"\d+ of 147 changes are caused by policy v2", output)
        assert "not this proposal's doing" in output

    def test_reports_the_blast_radius(self, output):
        assert "blast radius" in output
        assert "by category" in output
        assert "by grade" in output

    def test_forecasts_what_a_real_judge_would_cost(self, output):
        assert re.search(r"USD [\d,.]+ \(estimated\)", output)

    def test_checks_precedent_against_itself(self, output):
        assert "precedent self-consistency" in output

    def test_measures_the_judge_noise_floor(self, output):
        assert "disagreement rate" in output

    def test_segment_totals_match_the_headline(self, output):
        """The blast radius denominators are the same 600 cases as the headline.

        Runs overlap by design, and the pre-aggregated per-run rows cannot be
        summed without counting a case once per run that saw it.
        """
        from ptm import report

        summary = report.summary("expenses", "v2")
        segments = report.segments("expenses", "v2")
        for field in {r["field"] for r in segments}:
            rows = [r for r in segments if r["field"] == field]
            assert sum(r["cases"] for r in rows) == summary["cases"], field
            assert sum(r["flips"] for r in rows) == summary["flips"], field

    def test_confirms_flips_before_a_human_rules_on_them(self, output):
        """An error bar on the whole replay does not say whether this flip is
        real, and precedent is permanent."""
        assert "re-judged 25 flips 3x each" in output
        assert "25 reproduced, 0 did not" in output

    def test_is_ascii_only(self, output):
        """Windows consoles use cp1252, where a stray en dash aborts the demo."""
        output.encode("ascii")


class TestReseeding:
    """The fixture must be reproducible, or every number in the demo drifts."""

    def test_reseeding_does_not_accumulate_subject_facts(self, fresh_db):
        """Regression: facts are keyed by (subject, key, known_from).

        Replacing rather than clearing them left every previous run's promotion
        dates in place, so the point-in-time replay read grades the fixture
        never generated and the headline numbers moved on each re-seed.
        """
        from ptm.seed import seed_expenses

        seed_expenses()
        once = store.query("SELECT COUNT(*) n FROM subject_facts")[0]["n"]
        seed_expenses()
        seed_expenses()
        assert store.query("SELECT COUNT(*) n FROM subject_facts")[0]["n"] == once

    def test_the_replay_is_identical_across_re_seeds(self, fresh_db):
        from ptm.config import load_domain
        from ptm.seed import seed_expenses

        domain = load_domain("expenses")

        def replay():
            cases = store.load_cases("expenses", until=NOW)
            verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
            return diff.summarise(diff.flips(cases, verdicts, domain), len(cases), domain)

        seed_expenses()
        first = replay()
        seed_expenses()
        assert replay() == first

    def test_re_seeding_one_domain_leaves_the_other_intact(self, fresh_db):
        from ptm.seed import seed_expenses, seed_refunds

        expenses_facts = seed_expenses()["facts"]
        refunds_facts = seed_refunds()["facts"]
        seed_expenses()

        def count(key):
            return store.query(
                "SELECT COUNT(*) n FROM subject_facts WHERE key=?", (key,))[0]["n"]

        assert count("grade") == expenses_facts
        assert count("tier") == refunds_facts


class TestOtherDomain:
    def test_the_engine_carries_no_domain_knowledge(self, fresh_db, capsys):
        """The closing move of the demo: same pipeline, different domain."""
        selftest.main("refunds", "v2")
        out = capsys.readouterr().out
        assert number_before(out, "decisions under policy v2") == 400
        assert "by tier" in out, "the refunds point-in-time fact"
        assert "GATE" in out

    def test_replaying_one_domain_leaves_the_other_intact(self, fresh_db, capsys):
        """save_replay clears prior rows by run id, which is safe in Airflow
        because run ids are per-DAG. A selftest sharing run ids across domains
        would silently delete the results it had just produced for the other."""
        from ptm import report

        selftest.main("expenses", "v2")
        selftest.main("refunds", "v2")
        capsys.readouterr()
        assert report.summary("expenses", "v2")["flips"] == 147
        assert report.summary("refunds", "v2")["flips"] == 85

    def test_an_unknown_domain_says_what_is_available(self, fresh_db):
        with pytest.raises(KeyError, match="no synthetic fixture"):
            selftest.main("nonexistent", "v2")


class TestPointInTimeCheck:
    def test_reports_the_documented_error_count(self, fresh_db, capsys):
        from ptm.seed import seed_expenses

        seed_expenses()
        pit_check.main("expenses", "v2")
        out = capsys.readouterr().out
        assert number_before(out, "flips") == 147
        assert re.search(r"wrong on 39 / 600 cases", out), out

    def test_refuses_without_data_rather_than_reporting_zero(self, fresh_db):
        with pytest.raises(SystemExit, match="no cases"):
            pit_check.main("expenses", "v2")

    def test_refuses_a_domain_with_no_point_in_time_fact(self, fresh_db, expenses):
        import ptm.config as config

        bare = expenses.model_copy(deep=True)
        bare.pit_field = None
        with pytest.MonkeyPatch.context() as m:
            m.setattr(config, "load_domain", lambda name: bare)
            m.setattr(pit_check, "load_domain", lambda name: bare)
            with pytest.raises(SystemExit, match="no pit_field"):
                pit_check.main("expenses", "v2")
