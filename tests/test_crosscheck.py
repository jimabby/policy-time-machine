"""A second judge, and what the disagreement is allowed to mean.

The failure this panel has to avoid is being read as a scoreboard. It cannot
say which model was right - nothing here can, except :mod:`ptm.calibration`,
and only where a human ruled. What it can say is that two independent judges
split on a case, which is evidence about the *policy*.
"""

from __future__ import annotations

from ptm import crosscheck
from ptm.models import Verdict


def v(outcome, clause="1.1", confidence=0.9):
    return Verdict(outcome=outcome, rationale="r", confidence=confidence,
                   policy_clause=clause)


class TestTheComparison:
    def test_only_cases_both_judges_saw_are_compared(self, expenses):
        """A case one judge never saw is a gap in the run, not a disagreement."""
        report = crosscheck.analyse(
            {"a": v("approve"), "b": v("deny"), "c": v("approve")},
            {"a": v("approve"), "b": v("deny")}, expenses)
        assert report.compared == 2 and report.agreed == 2
        assert report.judged_only_by_primary == ["c"]
        assert report.agreement == 1.0, \
            "the unseen case must not count as agreement or as disagreement"

    def test_agreement_carries_its_sampling_band(self, expenses):
        """Two judges agreeing on 4 of 4 is not the same claim as 400 of 400."""
        small = crosscheck.analyse({str(i): v("approve") for i in range(4)},
                                   {str(i): v("approve") for i in range(4)}, expenses)
        large = crosscheck.analyse({str(i): v("approve") for i in range(400)},
                                   {str(i): v("approve") for i in range(400)}, expenses)
        assert small.agreement == large.agreement == 1.0
        assert small.agreement_lo < large.agreement_lo

    def test_clause_agreement_is_the_stricter_question(self, expenses):
        """Same answer, different sentence - which is what attribution reports."""
        report = crosscheck.analyse(
            {"a": v("deny", "1.1")}, {"a": v("deny", "5.1")}, expenses)
        assert report.agreed == 1, "the outcome agrees"
        assert report.clause_agreement == 0.0, "the reason does not"


class TestWhatTheSplitMeans:
    def test_a_flip_only_one_judge_makes_is_counted(self, expenses):
        """The headline flip rate is built from the primary judge alone, and
        this is the part of it a second judge would not have produced."""
        report = crosscheck.analyse(
            {"a": v("deny"), "b": v("approve")},
            {"a": v("approve"), "b": v("approve")}, expenses,
            actual={"a": "approve", "b": "approve"})
        assert report.contested_flips == 1

    def test_a_disagreement_where_both_depart_from_history_is_not_a_contested_flip(
            self, expenses):
        """Both judges flip the case; they only differ on where to. The flip is
        not in doubt, so counting it would overstate what rests on one judge."""
        report = crosscheck.analyse(
            {"a": v("deny")}, {"a": v("partial")}, expenses, actual={"a": "approve"})
        assert report.compared - report.agreed == 1
        assert report.contested_flips == 0

    def test_the_lean_separates_a_model_difference_from_ambiguity(self, expenses):
        """One judge reliably the stricter of the two is something to act on.
        An even split is the policy being genuinely unclear."""
        one_way = crosscheck.analyse(
            {"a": v("deny"), "b": v("deny")},
            {"a": v("approve"), "b": v("approve")}, expenses)
        assert one_way.lean == {"tightening": 2}
        mixed = crosscheck.analyse(
            {"a": v("deny"), "b": v("approve")},
            {"a": v("approve"), "b": v("deny")}, expenses)
        assert set(mixed.lean) == {"tightening", "loosening"}

    def test_confidence_on_contested_cases_is_reported(self, expenses):
        """Confidence routes the review budget. A judge that is most confident
        exactly where an independent judge contradicts it is not measuring
        difficulty, and the panel has to be able to say so."""
        report = crosscheck.analyse(
            {"a": v("deny", confidence=0.99), "b": v("approve", confidence=0.5)},
            {"a": v("approve"), "b": v("approve")}, expenses)
        assert report.mean_confidence_when_split == 0.99
        assert report.mean_confidence_when_agreed == 0.5
        assert "not measuring what the review routing spends it on" in \
            crosscheck.describe(report)


class TestTheReading:
    def test_it_never_says_which_judge_was_right(self, expenses):
        text = crosscheck.describe(crosscheck.analyse(
            {"a": v("deny")}, {"a": v("approve")}, expenses, "first", "second"))
        assert "independent, not correct" in text
        for claim in ("wrong", "correct answer", "better model"):
            assert claim not in text.lower().replace("not correct", "")

    def test_perfect_agreement_is_not_reported_as_a_clean_policy(self, expenses):
        text = crosscheck.describe(crosscheck.analyse(
            {"a": v("approve")}, {"a": v("approve")}, expenses))
        assert "on this sample" in text, \
            "agreement on a sample is not a statement about the whole policy"

    def test_nothing_compared_says_so_rather_than_scoring_zero(self, expenses):
        report = crosscheck.analyse({}, {}, expenses)
        assert report.compared == 0
        assert "nothing to cross-check" in crosscheck.describe(report)
