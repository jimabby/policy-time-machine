"""The gate on the one number here that scores the judge against an answer.

:func:`ptm.calibration.score` produced these figures from the day it was
written and nothing acted on any of them - the same gap :class:`RulesPolicy`
was added to close for the offline rules, and a worse one, because every other
check in this project is satisfied completely by a judge that misreads a clause
the same way every time.

These cover what the gate must do, and - more of them - what it must refuse to
do. A gate that fires on a project which has adjudicated three cases gets turned
off on day one, and a gate that passes because the measurement is switched off
teaches people to trust a number that means nothing.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import calibration, report, store
from ptm.config import CalibrationPolicy, load_domain
from ptm.models import Precedent, Verdict


def domain_with(**policy):
    config = load_domain("expenses").model_copy(deep=True)
    config.calibration = CalibrationPolicy(**policy)
    return config


def from_pairs(pairs: list[tuple[bool, float]], **policy):
    """A report over explicit (agreed, confidence) cases.

    Confidence is set independently of correctness on purpose. Tying the two
    together - a high number whenever the judge happens to be right - builds a
    fixture in which the review threshold always separates perfectly, so the one
    check that matters most here could never be exercised.
    """
    config = domain_with(**policy)
    precedents, verdicts = [], {}
    for i, (agrees, confidence) in enumerate(pairs):
        case_id = f"c{i}"
        precedents.append(Precedent(
            case_id=case_id, domain="expenses", correct_outcome="approve",
            ruled_by="finance.lead", established_at=datetime(2026, 1, 1)))
        verdicts[case_id] = Verdict(
            outcome="approve" if agrees else "deny", rationale="because",
            confidence=confidence, policy_clause="1.1")
    return config, calibration.score(config, "v2", verdicts, precedents)


def scored(right: int, wrong: int, confidence: float = 0.9,
           wrong_confidence: float | None = None, **policy):
    """A report over ``right`` agreements and ``wrong`` disagreements."""
    return from_pairs(
        [(True, confidence) for _ in range(right)]
        + [(False, confidence if wrong_confidence is None else wrong_confidence)
           for _ in range(wrong)],
        **policy)


class TestItFires:
    def test_on_a_judge_that_disagrees_too_often(self):
        config, report_ = scored(6, 14, min_accuracy=0.7, min_judged=10)
        problems = calibration.gate(report_, config)
        assert len(problems) == 1
        assert "30.0%" in problems[0] and "70.0%" in problems[0]

    def test_on_a_judge_that_is_right_but_too_sure_of_itself(self):
        """Accuracy can be fine while confidence is meaningless, and nothing
        else in the pipeline would ever mention it."""
        config, report_ = scored(15, 5, confidence=1.0, wrong_confidence=1.0,
                                 max_overconfidence=0.1, min_judged=10)
        problems = calibration.gate(report_, config)
        assert len(problems) == 1
        assert "overconfident" in problems[0]

    def test_on_a_review_threshold_that_sorts_nothing(self):
        """below_confidence routes human attention. If it does not predict
        correctness, the queue - and every precedent from it - was picked at
        random, which is the finding worth failing a run over."""
        # Half right on each side of the 75% line: the threshold is sorting the
        # queue, and what it sorts it into is two identical piles.
        config, report_ = from_pairs(
            [(True, 0.9)] * 5 + [(False, 0.9)] * 5
            + [(True, 0.5)] * 5 + [(False, 0.5)] * 5,
            require_threshold_separation=True, min_judged=10)
        assert report_.above_threshold["accuracy"] == report_.below_threshold["accuracy"]
        problems = calibration.gate(report_, config)
        assert any("does not separate" in p for p in problems)

    def test_it_names_more_than_one_problem_at_once(self):
        """Confidence spread either side of the 75% review threshold, so the
        separation check is measuring rather than abstaining."""
        config, report_ = from_pairs(
            [(True, 0.95)] * 2 + [(False, 0.95)] * 8
            + [(True, 0.5)] * 2 + [(False, 0.5)] * 8,
            min_accuracy=0.8, max_overconfidence=0.1,
            require_threshold_separation=True, min_judged=10)
        assert report_.below_threshold["n"] and report_.above_threshold["n"]
        assert len(calibration.gate(report_, config)) == 3


class TestItStaysQuiet:
    def test_when_nothing_has_been_adjudicated(self):
        """No evidence is not 0% accuracy. Failing here would make the gate fire
        loudest on a project that has not run yet."""
        config = domain_with(min_accuracy=0.9, min_judged=1)
        empty = calibration.score(config, "v2", {}, [])
        assert calibration.gate(empty, config) == []

    def test_when_too_few_rulings_have_been_scored(self):
        """Accuracy on three contested cases has a band running most of the way
        from 0 to 1. Gating on it fails runs for the sample size."""
        config, report_ = scored(0, 3, min_accuracy=0.9, min_judged=10)
        assert report_.accuracy == 0.0
        assert calibration.gate(report_, config) == []

    def test_the_moment_there_are_enough(self):
        config, report_ = scored(0, 10, min_accuracy=0.9, min_judged=10)
        assert calibration.gate(report_, config)

    def test_on_a_domain_that_has_set_no_thresholds(self):
        """Every setting defaults to off. A project that has never measured this
        must not start failing the first time it does."""
        config, report_ = scored(0, 20)
        assert config.calibration.min_accuracy == 0.0
        assert calibration.gate(report_, config) == []

    def test_when_the_threshold_has_nothing_on_one_side_of_it(self):
        """A judge confident on every ruling on file leaves nothing below the
        line. threshold_separates is False there because it is unmeasured, and
        failing on that is failing for a sample that has not arrived yet."""
        config, report_ = scored(6, 6, confidence=0.99, wrong_confidence=0.99,
                                 require_threshold_separation=True, min_judged=10)
        assert report_.below_threshold["n"] == 0
        assert report_.threshold_separates is False
        assert not [p for p in calibration.gate(report_, config) if "separate" in p]

    def test_an_underconfident_judge_is_not_a_finding(self):
        """The ceiling is on overconfidence. A judge that claims 60% and is
        right 90% of the time is wasting review budget, not endangering it."""
        config, report_ = scored(19, 1, confidence=0.6, max_overconfidence=0.1,
                                 min_judged=10)
        assert report_.overconfidence < 0
        assert not [p for p in calibration.gate(report_, config) if "overconfident" in p]


@pytest.fixture
def ruled(replayed):
    """A replay on file plus a human ruling on one of its cases.

    ``report.calibration`` scores stored verdicts against stored precedents, so
    both halves have to exist before there is anything for a gate to act on.
    """
    store.save_precedent(Precedent(
        case_id="exp-0001", domain="expenses", correct_outcome="approve",
        ruled_by="finance.lead", established_at=datetime(2026, 1, 1)))
    return replayed


class TestTheReportServesIt:
    def test_offline_it_is_reported_inert_and_not_gated(self, ruled):
        """The verdicts came from offline_rules, written from the same policy
        the reviewer was shown, so the figure describes the fixture."""
        result = report.calibration("expenses", "v2")
        assert result["measured"] is True
        assert result["inert"] is True
        assert result["problems"] == []

    def test_it_carries_the_gate_setting_and_the_floor(self, ruled):
        result = report.calibration("expenses", "v2")
        assert result["gate"] in {"warn", "fail"}
        assert result["min_judged"] >= 1

    def test_a_real_judge_is_gated(self, ruled):
        """The only thing holding the gate off offline is which judge answered,
        so a run stamped with a model must be scored for real.

        The session database is shared, so every run's label is put back exactly
        as it was - ``judge_model`` is what ptm.rules reads to decide whether its
        own figure is inert, and a test that left it blank would make that one
        silently pass for the wrong reason.
        """
        where = "WHERE domain='expenses' AND policy_version='v2'"
        before = [(r["run_id"], r["judge_model"]) for r in
                  store.query(f"SELECT run_id, judge_model FROM runs {where}")]
        with store.conn() as c:
            c.execute(f"UPDATE runs SET judge_model='anthropic:claude-sonnet-5' {where}")
        try:
            result = report.calibration("expenses", "v2")
            assert result["inert"] is False
            assert result["judged_by"] == ["anthropic:claude-sonnet-5"]
        finally:
            with store.conn() as c:
                c.executemany("UPDATE runs SET judge_model=? WHERE run_id=?",
                              [(model, run_id) for run_id, model in before])


class TestTheShippedDomains:
    @pytest.mark.parametrize("name", ["expenses", "refunds"])
    def test_declare_a_policy_that_warns_rather_than_fails(self, name):
        """Offline the measurement is inert, so 'fail' would be a gate nobody
        could satisfy or trust. It is raised once a real judge has been scored."""
        policy = load_domain(name).calibration
        assert policy.gate == "warn"
        assert 0 < policy.min_accuracy <= 1.0
        assert policy.min_judged >= 5
