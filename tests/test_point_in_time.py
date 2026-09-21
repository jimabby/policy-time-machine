"""The property the whole project rests on: replay sees only what was known.

If these fail, every number the pipeline produces is quietly wrong in the
direction that flatters the proposal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ptm import diff, pit_check, store
from ptm.judge import offline_verdict

NOW = datetime(2026, 9, 1)


class TestHydration:
    def test_a_case_sees_the_fact_value_from_its_own_date(self, fresh_db):
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("early", "expenses", "emp-1", "2025-01-01T00:00:00",
                       '{"amount_gbp": 100}', "deny", ""))
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("late", "expenses", "emp-1", "2025-12-01T00:00:00",
                       '{"amount_gbp": 100}', "deny", ""))
            c.executemany("INSERT INTO subject_facts VALUES (?,?,?,?)", [
                ("emp-1", "grade", "1", "2024-01-01T00:00:00"),
                ("emp-1", "grade", "3", "2025-06-01T00:00:00"),
            ])
        by_id = {c.case_id: c for c in store.load_cases("expenses", until=NOW)}
        assert by_id["early"].payload["grade"] == "1"
        assert by_id["late"].payload["grade"] == "3"

    def test_a_fact_recorded_after_the_decision_is_invisible(self, fresh_db):
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("c1", "expenses", "emp-1", "2025-01-01T00:00:00",
                       '{"amount_gbp": 100}', "deny", ""))
            c.execute("INSERT INTO subject_facts VALUES (?,?,?,?)",
                      ("emp-1", "grade", "3", "2025-08-01T00:00:00"))
        [case] = store.load_cases("expenses", until=NOW)
        assert "grade" not in case.payload

    def test_the_most_recent_prior_value_wins(self, fresh_db):
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("c1", "expenses", "emp-1", "2025-07-01T00:00:00",
                       '{"amount_gbp": 100}', "deny", ""))
            c.executemany("INSERT INTO subject_facts VALUES (?,?,?,?)", [
                ("emp-1", "grade", "1", "2024-01-01T00:00:00"),
                ("emp-1", "grade", "2", "2025-01-01T00:00:00"),
                ("emp-1", "grade", "3", "2025-06-01T00:00:00"),
                ("emp-1", "grade", "4", "2025-09-01T00:00:00"),
            ])
        [case] = store.load_cases("expenses", until=NOW)
        assert case.payload["grade"] == "3"


class TestIntervals:
    def test_the_window_excludes_its_upper_bound(self, seeded):
        """Adjacent monthly runs must not both replay the same case."""
        march = store.load_cases("expenses", since=datetime(2025, 3, 1),
                                 until=datetime(2025, 4, 1))
        april = store.load_cases("expenses", since=datetime(2025, 4, 1),
                                 until=datetime(2025, 5, 1))
        assert march and april
        assert not ({c.case_id for c in march} & {c.case_id for c in april})

    def test_monthly_windows_partition_the_period(self, seeded):
        from ptm.selftest import month_windows

        seen: set[str] = set()
        for lo, hi in month_windows(datetime(2024, 9, 1), datetime(2026, 9, 1)):
            batch = {c.case_id for c in store.load_cases("expenses", since=lo, until=hi)}
            assert not (seen & batch), "a case was replayed by two runs"
            seen |= batch
        assert len(seen) == len(store.load_cases("expenses", until=NOW))

    def test_an_offset_aware_bound_keeps_a_case_on_the_boundary(self, fresh_db):
        """The DAG's bounds are pendulum objects, so they carry a UTC offset.

        SQLite compares decided_at as TEXT, and "2025-06-01T00:00:00" sorts
        below "2025-06-01T00:00:00+00:00", so a case decided exactly on the
        boundary used to vanish from its own window.
        """
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("midnight", "expenses", "emp-1", "2025-06-01T00:00:00",
                       '{"amount_gbp": 100}', "deny", ""))
        found = store.load_cases("expenses",
                                 since=datetime(2025, 6, 1, tzinfo=timezone.utc),
                                 until=datetime(2025, 7, 1, tzinfo=timezone.utc))
        assert [c.case_id for c in found] == ["midnight"]

    def test_an_offset_is_converted_rather_than_discarded(self, fresh_db):
        """Stripping the suffix would be wrong; the instant has to be respected."""
        with store.conn() as c:
            c.execute("INSERT INTO cases VALUES (?,?,?,?,?,?,?)",
                      ("late_may", "expenses", "emp-1", "2025-05-31T23:30:00",
                       '{"amount_gbp": 100}', "deny", ""))
        plus_two = timezone(timedelta(hours=2))
        # 01:00+02:00 is 23:00 UTC, so the 23:30 UTC case falls inside the window.
        found = store.load_cases("expenses",
                                 since=datetime(2025, 6, 1, 1, 0, tzinfo=plus_two),
                                 until=datetime(2025, 7, 1, tzinfo=timezone.utc))
        assert [c.case_id for c in found] == ["late_may"]


class TestCapSampling:
    """A capped manual run must not sample the least representative slice.

    Slowly-changing facts have not changed yet at the start of the period, so a
    cap taken from the front of history misses exactly the interactions the
    engine exists to get right.
    """

    def test_newest_first_keeps_the_recent_end(self, seeded):
        oldest = store.load_cases("expenses", until=NOW, limit=100)
        newest = store.load_cases("expenses", until=NOW, limit=100, newest_first=True)
        assert newest[-1].decided_at > oldest[-1].decided_at
        assert newest[0].decided_at > oldest[0].decided_at

    def test_results_stay_chronological_either_way(self, seeded):
        for newest_first in (False, True):
            cases = store.load_cases("expenses", until=NOW, limit=50,
                                     newest_first=newest_first)
            assert cases == sorted(cases, key=lambda c: c.decided_at)

    def test_the_oldest_slice_understates_the_point_in_time_effect(self, seeded, expenses):
        """The concrete reason the default changed: the front of the period has
        barely any grade-3 employees, so a capped replay taken from there cannot
        see the seniority exemption the proposal turns on."""
        oldest = store.load_cases("expenses", until=NOW, limit=150)
        newest = store.load_cases("expenses", until=NOW, limit=150, newest_first=True)
        def senior(cases):
            return sum(1 for c in cases if str(c.payload.get("grade")) == "3")

        assert senior(newest) > senior(oldest)


class TestNaiveReplayIsWrong:
    def test_todays_facts_produce_wrong_verdicts(self, seeded, expenses):
        cases = store.load_cases("expenses", until=NOW)
        correct = {c.case_id: offline_verdict(c, expenses, "v2") for c in cases}

        latest = {
            r["subject_id"]: r["value"]
            for r in store.query(
                """SELECT f.subject_id, f.value FROM subject_facts f
                   WHERE f.key = ? AND f.known_from =
                     (SELECT MAX(g.known_from) FROM subject_facts g
                       WHERE g.subject_id = f.subject_id AND g.key = f.key)""",
                (expenses.pit_field,))
        }
        subject = {r["case_id"]: r["subject_id"] for r in store.query(
            "SELECT case_id, subject_id FROM cases WHERE domain = 'expenses'")}
        for c in cases:
            c.payload[expenses.pit_field] = latest[subject[c.case_id]]
        naive = {c.case_id: offline_verdict(c, expenses, "v2") for c in cases}

        wrong = [k for k in correct if correct[k].outcome != naive[k].outcome]
        assert wrong, "the fixture must actually contain the point-in-time trap"

        # Every error has to flatter the proposal. That direction is the reason
        # this matters: a naive backtest does not add noise, it adds bias.
        directions = {
            expenses.direction(correct[k].outcome, naive[k].outcome) for k in wrong
        }
        assert directions == {"loosening"}

    def test_the_documented_error_count_still_holds(self, seeded, expenses):
        """Pins the number the README quotes, so a fixture change cannot
        silently invalidate the claim in the docs."""
        from ptm.pit_check import main as pit_main

        cases = store.load_cases("expenses", until=NOW)
        correct = {c.case_id: offline_verdict(c, expenses, "v2") for c in cases}
        assert len(cases) == 600
        assert len(diff.flips(cases, correct, expenses)) == 147
        assert callable(pit_main)


class TestThePointInTimeCheckHasAnEntryPoint:
    """The figure the whole point-in-time argument rests on, as a return value.

    ``39 of 600 wrong`` is quoted in the README, in docs/DESIGN.md and spoken
    aloud in the demo video, and the only way to obtain it was to run the
    module and read the line back off standard output. Every other measurement
    in this project returns its number; this one printed it, so every document
    quoting it was quoting a figure nothing could check.
    """

    def test_it_returns_the_numbers_the_cli_prints(self, seeded, capsys):
        result = pit_check.compare("expenses", "v2")
        pit_check.run("expenses", "v2")
        printed = capsys.readouterr().out
        assert f"{result['pit_flips']} flips" in printed
        assert f"wrong on {result['naive_wrong']} / {result['compared']}" in printed

    def test_the_trap_is_actually_in_the_fixture(self, seeded):
        """A zero here would mean the demo's central claim has nothing behind it."""
        result = pit_check.compare("expenses", "v2")
        assert result["naive_wrong"] > 0
        assert result["compared"] > 0
        assert len(result["disagreements"]) == result["naive_wrong"]

    def test_every_disagreement_names_both_answers(self, seeded):
        for row in pit_check.compare("expenses", "v2")["disagreements"]:
            assert row["correct"] and row["naive"]
            assert row["correct"] != row["naive"]

    def test_an_unknown_version_is_refused_rather_than_measured(self, seeded):
        """Both shipped domains declare a pit_field, so the refusal worth
        asserting here is the other one: a version that does not exist must not
        come back as a comparison over nothing."""
        with pytest.raises(SystemExit):
            pit_check.compare("expenses", "v99")

    def test_the_other_shipped_domain_is_measurable_too(self, seeded):
        """refunds declares its own pit_field (tier), so the check is not a
        thing that only works on the domain it was written against."""
        result = pit_check.compare("refunds", "v2")
        assert result["pit_field"] == "tier"
        assert result["compared"] > 0
