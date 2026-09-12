"""The claims the README makes, asserted.

These run the real seed and the real engine, so they are the slowest tests
here - and the ones that would catch a regression in the demo itself.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import ai, diff, store
from ptm.config import load_domain
from ptm.judge import offline_verdict
from ptm.models import Precedent
from ptm.seed import seed_expenses, seed_refunds


@pytest.fixture
def replayed(db):
    """Seed and replay the expenses domain under v2, as the DAGs would."""
    seed_expenses()
    domain = load_domain("expenses")
    cases = db.load_cases("expenses", until=datetime(2026, 10, 1))
    verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    flips = diff.flips(cases, verdicts, domain)
    db.save_verdicts("run-1", "expenses", "v2", verdicts)
    db.save_flips("run-1", "expenses", "v2", flips)
    return domain, cases, verdicts, flips


def test_the_seed_is_deterministic(db):
    first = seed_expenses()
    ids_a = [c.case_id for c in db.load_cases("expenses", until=datetime(2026, 10, 1))]
    second = seed_expenses()
    ids_b = [c.case_id for c in db.load_cases("expenses", until=datetime(2026, 10, 1))]
    assert first == second == {"cases": 600, "employees": 20, "facts": 28}
    assert ids_a == ids_b, "the demo must produce the same numbers every time"


def test_reseeding_does_not_accumulate_subject_facts(db):
    """Regression: a re-seed left every previous run's promotion dates behind.

    subject_facts is keyed by (subject, key, known_from), so replacing rather
    than clearing let old promotions pile up. The point-in-time replay then read
    grades the current fixture never generated, and the demo's headline numbers
    drifted every time anyone re-seeded.
    """
    seed_expenses()
    once = db.query("SELECT COUNT(*) n FROM subject_facts")[0]["n"]
    seed_expenses()
    seed_expenses()
    assert db.query("SELECT COUNT(*) n FROM subject_facts")[0]["n"] == once == 28


def test_reseeding_is_idempotent_for_the_whole_replay(db):
    """The headline numbers must not move between re-seeds."""
    domain = load_domain("expenses")

    def replay():
        cases = db.load_cases("expenses", until=datetime(2026, 10, 1))
        verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
        return diff.summarise(diff.flips(cases, verdicts, domain), len(cases), domain)

    seed_expenses()
    first = replay()
    seed_expenses()
    assert replay() == first


def test_seeding_both_domains_does_not_cross_contaminate_facts(db):
    """Re-seeding one domain must not disturb the other's point-in-time facts."""
    expenses_facts = seed_expenses()["facts"]
    refunds_facts = seed_refunds()["facts"]
    seed_expenses()

    def count(key):
        return db.query("SELECT COUNT(*) n FROM subject_facts WHERE key=?", (key,))[0]["n"]

    assert {r["key"] for r in db.query("SELECT DISTINCT key FROM subject_facts")} == {"grade", "tier"}
    assert count("grade") == expenses_facts
    assert count("tier") == refunds_facts, "the refunds facts survived an expenses re-seed intact"


def test_grade_is_never_stored_on_the_case_record(db):
    """Grade is a point-in-time fact. Storing it on the case would defeat the point."""
    seed_expenses()
    import json
    for row in db.query("SELECT payload FROM cases WHERE domain='expenses'"):
        assert "grade" not in json.loads(row["payload"])


def test_grade_is_hydrated_back_on_load(db):
    seed_expenses()
    cases = db.load_cases("expenses", until=datetime(2026, 10, 1))
    assert all("grade" in c.payload for c in cases)


def test_replay_changes_a_substantial_minority_of_outcomes(replayed):
    domain, cases, _, flips = replayed
    s = diff.summarise(flips, len(cases), domain)
    assert s["cases_replayed"] == 600
    # Pinned loosely: the seed is deterministic, but a rule tweak should not
    # need this test edited unless it changes the demo's character.
    assert 100 < s["flips"] < 250, f"expected a substantial minority, got {s['flips']}"
    assert s["loosening"] > s["tightening"], "v2 is a loosening policy overall"
    assert s["net_impact"] > 0, "v2 costs money"


def test_history_is_not_a_pure_rule_replay(db):
    """Reviewers deviated ~8%, so v1 must not reproduce history exactly.

    Without that noise the whole exercise is a rule diff and proves nothing.
    """
    seed_expenses()
    domain = load_domain("expenses")
    cases = db.load_cases("expenses", until=datetime(2026, 10, 1))
    disagreements = sum(1 for c in cases
                        if offline_verdict(c, domain, "v1").outcome != c.actual_outcome)
    assert 0.03 < disagreements / len(cases) < 0.15


def test_a_naive_replay_gets_cases_wrong_and_always_flatters_the_proposal(db):
    """The README's headline claim: point-in-time is not a technicality."""
    seed_expenses()
    domain = load_domain("expenses")
    cases = db.load_cases("expenses", until=datetime(2026, 10, 1))
    correct = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}

    latest = {r["subject_id"]: r["value"] for r in db.query(
        """SELECT f.subject_id, f.value FROM subject_facts f WHERE f.key='grade'
           AND f.known_from = (SELECT MAX(g.known_from) FROM subject_facts g
                               WHERE g.subject_id=f.subject_id AND g.key=f.key)""")}
    subject = {r["case_id"]: r["subject_id"] for r in
               db.query("SELECT case_id, subject_id FROM cases WHERE domain='expenses'")}
    for c in cases:
        c.payload["grade"] = latest[subject[c.case_id]]
    naive = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}

    wrong = [k for k in correct if correct[k].outcome != naive[k].outcome]
    assert wrong, "the trap must actually fire, or the fixture has stopped demonstrating it"
    # Every error comes from applying a later promotion to an earlier claim, so
    # every one of them wrongly approves. That is why the bias is not random.
    assert all(domain.direction(correct[k].outcome, naive[k].outcome) == "loosening"
               for k in wrong), "a naive replay should only ever flatter the proposal"


def test_the_gate_catches_a_policy_that_reverses_a_ruling(replayed, db):
    domain, _, _, flips = replayed
    contested = diff.select_for_review(flips, domain)
    assert contested, "something must reach a human"

    # The reviewer sides with history on the tightenings, as in the demo.
    for f in contested:
        db.save_precedent(Precedent(
            case_id=f.case_id, domain="expenses",
            correct_outcome=f.actual_outcome if f.direction == "tightening" else f.new_outcome,
            ruled_by="finance.lead", note="test", established_at=datetime(2026, 1, 1)))

    precedents = db.load_precedents("expenses")
    ids = {p.case_id for p in precedents}
    cases = [c for c in db.load_cases("expenses", until=datetime(2026, 10, 1)) if c.case_id in ids]
    verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    violations = diff.precedent_violations(verdicts, precedents)

    assert violations, "v2 reverses the tightenings the reviewer overruled"
    assert all(v["ruled_by"] == "finance.lead" for v in violations)


def test_the_gate_passes_when_precedent_agrees_with_the_policy(replayed, db):
    domain, _, _, flips = replayed
    contested = diff.select_for_review(flips, domain)
    # A reviewer who rubber-stamps the policy creates no conflict with it.
    for f in contested:
        db.save_precedent(Precedent(case_id=f.case_id, domain="expenses",
                                    correct_outcome=f.new_outcome, ruled_by="x",
                                    established_at=datetime(2026, 1, 1)))
    precedents = db.load_precedents("expenses")
    ids = {p.case_id for p in precedents}
    cases = [c for c in db.load_cases("expenses", until=datetime(2026, 10, 1)) if c.case_id in ids]
    verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    assert diff.precedent_violations(verdicts, precedents) == []


def test_review_stays_within_a_humans_budget(replayed):
    domain, _, _, flips = replayed
    assert len(diff.select_for_review(flips, domain)) <= domain.review.max_reviews
    assert len(flips) > 10 * domain.review.max_reviews, "the funnel must actually be narrowing"


def test_analysis_accounts_for_nearly_every_flip(replayed):
    domain, _, _, flips = replayed
    themes = ai.offline_themes(flips, domain)
    named = sum(t.case_count for t in themes.themes)
    assert named + themes.unexplained == len(flips), "every flip is counted once"
    assert named / len(flips) > 0.9, "the themes should explain the bulk of the change"


def test_the_brief_is_grounded_in_the_actual_numbers(replayed):
    domain, cases, _, flips = replayed
    s = diff.summarise(flips, len(cases), domain)
    b = ai.offline_brief(s, sorted(flips, key=lambda f: -f.impact)[:25], domain, "v2")
    assert str(s["flips"]) in b.headline
    assert "600" in b.summary
    assert b.verdict == "do_not_ship", "a 24% flip rate is not a tweak"


def test_insights_survive_a_round_trip_through_the_store(replayed, db):
    domain, cases, _, flips = replayed
    s = diff.summarise(flips, len(cases), domain)
    brief = ai.offline_brief(s, flips[:5], domain, "v2")
    db.save_insight("expenses", "v2", "brief", brief.model_dump(), "offline", "run-1")
    loaded = db.load_insights("expenses", "v2")["brief"]
    assert loaded["headline"] == brief.headline
    assert loaded["risks"] == brief.risks


def test_the_engine_carries_no_expenses_knowledge(db):
    """The second domain is the proof: same engine, different everything."""
    seed_refunds()
    domain = load_domain("refunds")
    cases = db.load_cases("refunds", until=datetime(2026, 10, 1))
    assert len(cases) == 400
    assert all("tier" in c.payload for c in cases), "the refunds point-in-time fact"

    verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    flips = diff.flips(cases, verdicts, domain)
    s = diff.summarise(flips, len(cases), domain)
    assert flips, "v2 must change something here too"
    assert s["impact_unit"] == "GBP"
    assert all(f.new_outcome in domain.outcomes for f in flips)

    themes = ai.offline_themes(flips, domain)
    assert themes.themes, "the analysis layer is domain-agnostic too"


def test_the_two_domains_do_not_leak_into_each_other(db):
    seed_expenses()
    seed_refunds()
    exp = db.load_cases("expenses", until=datetime(2026, 10, 1))
    ref = db.load_cases("refunds", until=datetime(2026, 10, 1))
    assert {c.domain for c in exp} == {"expenses"}
    assert {c.domain for c in ref} == {"refunds"}
    assert not {c.case_id for c in exp} & {c.case_id for c in ref}
