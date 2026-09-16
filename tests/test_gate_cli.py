"""The regression suite, reachable from a shell.

``precedent_gate_<domain>`` is what the README calls the point of this project,
and it was the one measurement here with no ``python -m ptm.*`` entry point - so
the lint, the preflight, the sweep and the calibration score could all run in CI
and the gate could not. :mod:`ptm.gate` is that DAG's ``enforce`` task reading
stored verdicts instead of paying to judge them again.

The behaviour worth pinning is the third exit code. A precedent nothing has
judged is not a pass, and a shell that only checks for zero has to be able to
tell "nothing is reversed" from "nothing was checked".
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import gate, store
from ptm.models import Precedent


def ruling(case_id: str, outcome: str, version: str = "v2") -> Precedent:
    return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                     ruled_by="finance.lead", note="Ruled in a test.",
                     established_at=datetime(2026, 1, 1), policy_version=version)


@pytest.fixture
def judged(fresh_db):
    """A replay of the shipped fixture, with verdicts on file for both versions."""
    from ptm.config import load_domain
    from ptm.judge import offline_verdict
    from ptm.seed import seed_domain

    seed_domain("expenses")
    domain = load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
    for version in ("v1", "v2"):
        store.save_verdicts(f"pytest__{version}", "expenses", version,
                            {c.case_id: offline_verdict(c, domain, version) for c in cases})
    return {"cases": cases, "domain": domain}


def a_case_where(judged, version: str, outcome: str) -> str:
    """A case id this version's stored verdict gives ``outcome`` for."""
    stored = store.latest_verdicts("expenses", version)
    for case_id, row in sorted(stored.items()):
        if row["outcome"] == outcome:
            return case_id
    raise AssertionError(f"no case judged {outcome!r} under {version}")


class TestItAnswersTheSameQuestionTheDagDoes:
    def test_a_ruling_the_policy_agrees_with_passes(self, judged, capsys):
        case_id = a_case_where(judged, "v2", "deny")
        store.save_precedent(ruling(case_id, "deny"))
        assert gate.main(["expenses", "v2"]) == 0
        assert "GATE PASSES" in capsys.readouterr().out

    def test_a_ruling_the_policy_reverses_fails(self, judged, capsys):
        case_id = a_case_where(judged, "v2", "deny")
        store.save_precedent(ruling(case_id, "approve"))
        assert gate.main(["expenses", "v2"]) == gate.FAILED
        assert "GATE FAILS" in capsys.readouterr().err

    def test_it_separates_what_the_proposal_introduced(self, judged, capsys):
        """The whole reason the gate judges the policy in force too: a reversal
        the status quo already makes is not this proposal's doing."""
        case_id = a_case_where(judged, "v2", "deny")
        store.save_precedent(ruling(case_id, "approve"))
        result = gate.check("expenses", "v2")
        if store.latest_verdicts("expenses", "v1")[case_id]["outcome"] == "deny":
            assert result["introduced"] == []
            assert case_id in result["in_force_violations"]
            assert gate.main(["expenses", "v2", "--introduced-only"]) == 0
            assert "None is this proposal's doing" in capsys.readouterr().out


class TestItRefusesRatherThanReportingAPassItDidNotCheck:
    def test_a_precedent_with_no_verdict_is_exit_two(self, judged, capsys):
        store.save_precedent(ruling("exp-0001", "approve"))
        with store.conn() as connection:
            connection.execute(
                "DELETE FROM verdicts WHERE domain='expenses' AND case_id='exp-0001'")
        assert gate.main(["expenses", "v2"]) == gate.CANNOT_RUN
        assert "never been judged" in capsys.readouterr().err

    def test_that_is_a_different_code_from_a_failure(self):
        """A shell checking for zero must be able to tell them apart, and a
        gate that could not would report 'not run' as 'passed'."""
        assert gate.CANNOT_RUN != gate.FAILED != 0

    def test_offline_judge_settles_them_and_says_whose_answer_it_is(self, judged, capsys):
        store.save_precedent(ruling("exp-0001", "approve"))
        with store.conn() as connection:
            connection.execute(
                "DELETE FROM verdicts WHERE domain='expenses' AND case_id='exp-0001'")
        gate.main(["expenses", "v2", "--offline-judge", "--introduced-only"])
        assert "the fixture agreeing with itself" in capsys.readouterr().out

    def test_an_unknown_version_cannot_run(self, judged, capsys):
        assert gate.main(["expenses", "v99"]) == gate.CANNOT_RUN
        assert "unknown policy version" in capsys.readouterr().err

    def test_an_unknown_baseline_cannot_run(self, judged, capsys):
        assert gate.main(["expenses", "v2", "--baseline", "v99"]) == gate.CANNOT_RUN
        assert "unknown baseline" in capsys.readouterr().err

    def test_baseline_needs_a_value(self, judged, capsys):
        assert gate.main(["expenses", "v2", "--baseline"]) == gate.CANNOT_RUN
        assert "needs a version" in capsys.readouterr().err

    def test_no_precedents_at_all_passes_and_says_it_means_nothing(self, judged, capsys):
        assert gate.main(["expenses", "v2"]) == 0
        assert "no regression suite to run" in capsys.readouterr().out


class TestItReportsWhatTheDagReports:
    def test_a_stale_ruling_is_named_beside_the_reversal_it_causes(self, judged):
        case_id = a_case_where(judged, "v2", "deny")
        stale = ruling(case_id, "approve", version="v1")
        stale.judged_clause = "1.1"
        store.save_precedent(stale)
        result = gate.check("expenses", "v2")
        assert result["stale"]
        assert any(row["stale"] for row in result["violations"])

    def test_precedent_conflicts_travel_with_the_answer(self, judged):
        """A self-contradictory precedent set makes some reversal unavoidable,
        and a gate that failed without saying so is a mystery."""
        result = gate.check("expenses", "v2")
        assert "conflicts" in result

    def test_no_baseline_leaves_every_reversal_unattributed(self, judged):
        case_id = a_case_where(judged, "v2", "deny")
        store.save_precedent(ruling(case_id, "approve"))
        result = gate.check("expenses", "v2", baseline_version="")
        assert result["baseline_version"] == ""
        assert result["in_force_violations"] == []
        # With nothing to compare against, everything reads as introduced -
        # which is why the DAG judges the in-force policy too.
        assert result["introduced"] == [case_id]

    def test_a_ruling_whose_case_is_gone_is_reported_apart(self, judged):
        store.save_precedent(ruling("exp-not-a-case", "approve"))
        result = gate.check("expenses", "v2")
        assert "exp-not-a-case" in result["cases_missing"]
