"""The point-in-time load is the project's central claim; these pin it down."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import insert_case, insert_fact
from ptm.models import Flip, Precedent, Verdict


def test_hydrates_only_facts_known_at_decision_time(db):
    insert_fact(db, "emp-1", "grade", "2", "2024-01-01T00:00:00")
    insert_fact(db, "emp-1", "grade", "3", "2025-07-01T00:00:00")
    insert_case(db, "before", "2025-06-01T10:00:00", {"amount_gbp": 100}, "deny", "emp-1")
    insert_case(db, "after", "2025-08-01T10:00:00", {"amount_gbp": 100}, "deny", "emp-1")

    by_id = {c.case_id: c for c in db.load_cases("expenses", until=datetime(2026, 1, 1))}
    # The promotion must not reach backwards: this is the whole trap.
    assert by_id["before"].payload["grade"] == "2"
    assert by_id["after"].payload["grade"] == "3"


def test_ignores_facts_recorded_after_every_case(db):
    insert_fact(db, "emp-1", "grade", "1", "2026-01-01T00:00:00")
    insert_case(db, "c1", "2025-06-01T10:00:00", {"amount_gbp": 100}, "deny", "emp-1")
    (c,) = db.load_cases("expenses", until=datetime(2026, 1, 1))
    assert "grade" not in c.payload


def test_interval_is_half_open(db):
    for i, when in enumerate(["2025-05-31T23:59:59", "2025-06-01T00:00:00",
                              "2025-06-30T23:59:59", "2025-07-01T00:00:00"]):
        insert_case(db, f"c{i}", when, {}, "deny")
    got = {c.case_id for c in db.load_cases(
        "expenses", since=datetime(2025, 6, 1), until=datetime(2025, 7, 1))}
    assert got == {"c1", "c2"}, "[since, until) - since inclusive, until exclusive"


def test_offset_aware_bounds_do_not_drop_boundary_cases(db):
    """Regression: the DAG's interval bounds are offset-aware and compared as text.

    Serialised, they carry a "+00:00" suffix, so a case decided exactly on the
    boundary sorted *below* the bound and vanished from its own window.
    """
    insert_case(db, "midnight", "2025-06-01T00:00:00", {}, "deny")
    got = db.load_cases("expenses",
                        since=datetime(2025, 6, 1, tzinfo=timezone.utc),
                        until=datetime(2025, 7, 1, tzinfo=timezone.utc))
    assert [c.case_id for c in got] == ["midnight"]


def test_aware_bounds_are_converted_not_truncated(db):
    """An offset that shifts the date must move the window, not be dropped."""
    plus2 = timezone(timedelta(hours=2))
    insert_case(db, "late", "2025-05-31T23:00:00", {}, "deny")
    # 2025-06-01T00:30+02:00 is 2025-05-31T22:30 UTC, so the 23:00 UTC case is after it.
    got = db.load_cases("expenses", since=datetime(2025, 6, 1, 0, 30, tzinfo=plus2),
                        until=datetime(2025, 7, 1, tzinfo=timezone.utc))
    assert [c.case_id for c in got] == ["late"]


def test_ts_normalises_to_naive_utc():
    aware = datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc)
    assert db_ts(aware) == "2025-06-01T09:00:00"
    assert db_ts(datetime(2025, 6, 1, 9, 0)) == "2025-06-01T09:00:00"
    assert db_ts("2025-06-01T09:00:00+00:00") == "2025-06-01T09:00:00"


def db_ts(v):
    from ptm.store import ts
    return ts(v)


def test_limit_keeps_earliest_by_default(db):
    for i in range(10):
        insert_case(db, f"c{i}", f"2025-0{i % 9 + 1}-01T10:00:00", {}, "deny")
    got = db.load_cases("expenses", until=datetime(2026, 1, 1), limit=3)
    assert len(got) == 3
    assert got[0].decided_at < got[-1].decided_at


def test_spread_samples_across_the_whole_window(db):
    """Regression: a capped manual run replayed only the oldest cases."""
    for i in range(24):
        insert_case(db, f"c{i:02d}", f"2025-01-01T10:00:00" if i == 0
                    else f"2025-{i % 12 + 1:02d}-{i % 27 + 1:02d}T10:00:00", {}, "deny")
    got = db.load_cases("expenses", until=datetime(2026, 1, 1), limit=4, spread=True)
    assert len(got) == 4
    every = db.load_cases("expenses", until=datetime(2026, 1, 1))
    # The sample must reach the end of the period, not stop in the first month.
    assert got[-1].decided_at >= every[-1].decided_at - (every[-1].decided_at - every[0].decided_at) / 4


def test_spread_is_a_noop_below_the_limit(db):
    for i in range(3):
        insert_case(db, f"c{i}", f"2025-0{i + 1}-01T10:00:00", {}, "deny")
    assert len(db.load_cases("expenses", until=datetime(2026, 1, 1), limit=10, spread=True)) == 3


def test_flips_round_trip_with_their_clause(db):
    insert_case(db, "c1", "2025-06-01T10:00:00", {"amount_gbp": 90}, "approve")
    flip = Flip(case_id="c1", decided_at=datetime(2025, 6, 1), actual_outcome="approve",
                new_outcome="deny", rationale="over threshold", confidence=0.7,
                policy_clause="1.1", impact=90.0, direction="tightening")
    db.save_flips("run-1", "expenses", "v2", [flip])
    (row,) = db.flips_for_policy("expenses", "v2")
    assert row["policy_clause"] == "1.1"
    assert row["direction"] == "tightening"
    assert row["reviewed"] == 0

    db.mark_reviewed(["c1"])
    assert db.flips_for_policy("expenses", "v2")[0]["reviewed"] == 1


def test_precedents_round_trip(db):
    db.save_precedent(Precedent(case_id="c1", domain="expenses", correct_outcome="approve",
                                ruled_by="finance.lead", note="long-standing employee",
                                established_at=datetime(2026, 1, 1)))
    (p,) = db.load_precedents("expenses")
    assert (p.correct_outcome, p.ruled_by, p.note) == ("approve", "finance.lead", "long-standing employee")


def test_precedent_is_unique_per_domain_and_case(db):
    for outcome in ("approve", "deny"):
        db.save_precedent(Precedent(case_id="c1", domain="expenses", correct_outcome=outcome,
                                    ruled_by="x", established_at=datetime(2026, 1, 1)))
    got = db.load_precedents("expenses")
    assert len(got) == 1 and got[0].correct_outcome == "deny", "a re-ruling replaces, not duplicates"


def test_insights_round_trip_and_replace(db):
    db.save_insight("expenses", "v2", "brief", {"headline": "first"}, "offline", "r1")
    db.save_insight("expenses", "v2", "brief", {"headline": "second"}, "offline", "r2")
    db.save_insight("expenses", "v2", "themes", {"themes": []}, "offline", "r2")
    got = db.load_insights("expenses", "v2")
    assert got["brief"]["headline"] == "second", "newer analysis replaces older for a kind"
    assert set(got) == {"brief", "themes"}
    assert got["brief"]["generated_by"] == "offline"
    assert db.load_insights("expenses", "v1") == {}


def test_init_db_adds_policy_clause_to_an_older_flips_table(db, monkeypatch):
    """CREATE TABLE IF NOT EXISTS will not add a column; init_db must migrate."""
    with db.conn() as c:
        c.execute("DROP TABLE flips")
        c.execute("CREATE TABLE flips (run_id TEXT, domain TEXT, policy_version TEXT, "
                  "case_id TEXT, actual_outcome TEXT, new_outcome TEXT, direction TEXT, "
                  "impact REAL, confidence REAL, rationale TEXT, reviewed INTEGER DEFAULT 0)")
    db.init_db()
    with db.conn() as c:
        assert "policy_clause" in {r["name"] for r in c.execute("PRAGMA table_info(flips)")}


def test_verdicts_are_keyed_by_run_and_case(db):
    v = Verdict(outcome="deny", rationale="r", confidence=0.9, policy_clause="1.1")
    db.save_verdicts("run-1", "expenses", "v2", {"c1": v})
    db.save_verdicts("run-1", "expenses", "v2", {"c1": v.model_copy(update={"outcome": "approve"})})
    rows = db.query("SELECT outcome FROM verdicts WHERE run_id='run-1'")
    assert [r["outcome"] for r in rows] == ["approve"], "a re-run overwrites its own verdicts"


def _flip(case_id, impact=100.0, direction="loosening", clause="1.1"):
    return Flip(case_id=case_id, decided_at=datetime(2025, 6, 1), actual_outcome="deny",
                new_outcome="approve", rationale="r", confidence=0.9,
                policy_clause=clause, impact=impact, direction=direction)


def _overlapping_runs(db):
    """A backfill month, then a manual run that re-judges the same case."""
    insert_case(db, "c1", "2025-06-01T10:00:00", {"amount_gbp": 100}, "deny")
    insert_case(db, "c2", "2025-06-02T10:00:00", {"amount_gbp": 50}, "deny")
    db.record_run("backfill", "expenses", "v2", "actual", 2, 2, 150.0)
    db.save_flips("backfill", "expenses", "v2", [_flip("c1"), _flip("c2", impact=50.0)])
    db.save_verdicts("backfill", "expenses", "v2",
                     {"c1": Verdict(outcome="approve", rationale="r", confidence=0.9),
                      "c2": Verdict(outcome="approve", rationale="r", confidence=0.9)})
    # A later manual run covers the same ground again.
    db.record_run("manual", "expenses", "v2", "actual", 2, 1, 100.0)
    db.save_flips("manual", "expenses", "v2", [_flip("c1")])
    db.save_verdicts("manual", "expenses", "v2",
                     {"c1": Verdict(outcome="approve", rationale="r", confidence=0.9)})


def test_overlapping_runs_do_not_double_count_a_case(db):
    """Regression: a manual run re-judging a backfilled case counted it twice.

    It inflated the themes' case counts past the number of flips that existed
    and put the same case_id in a theme's examples more than once.
    """
    _overlapping_runs(db)
    rows = db.flips_for_policy("expenses", "v2")
    assert [r["case_id"] for r in rows] == ["c1", "c2"], "one row per case, by impact"


def test_the_most_recent_run_wins_for_a_re_judged_case(db):
    """The latest run reflects the policy text as it now stands."""
    insert_case(db, "c1", "2025-06-01T10:00:00", {"amount_gbp": 100}, "deny")
    db.record_run("old", "expenses", "v2", "actual", 1, 1, 100.0)
    db.save_flips("old", "expenses", "v2", [_flip("c1", clause="1.1")])
    db.record_run("new", "expenses", "v2", "actual", 1, 1, 100.0)
    db.save_flips("new", "expenses", "v2", [_flip("c1", clause="9.9")])
    (row,) = db.flips_for_policy("expenses", "v2")
    assert row["policy_clause"] == "9.9"


def test_policy_summary_counts_cases_not_run_totals(db):
    """Regression: summing runs.cases_replayed double-counted overlapping runs."""
    _overlapping_runs(db)
    s = db.policy_summary("expenses", "v2")
    assert s["runs"] == 2
    assert s["cases_replayed"] == 2, "two distinct cases, not the 4 the run rows sum to"
    assert s["flips"] == 2, "two distinct flips, not 3"
    assert s["flip_rate"] == 1.0
    assert s["net_impact"] == 150.0, "c1 counted once, not twice"


def test_policy_summary_of_an_unreplayed_policy_is_empty(db):
    s = db.policy_summary("expenses", "v9")
    assert s["cases_replayed"] == 0 and s["flips"] == 0 and s["flip_rate"] == 0.0
