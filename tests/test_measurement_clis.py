"""The measurements that could only be reached by triggering a DAG.

:mod:`ptm.gate` opens by making the argument these cover: everything in this
project can be run from a shell with no Airflow and no key *precisely* so it
works in CI and on a laptop, and a measurement reachable only by starting a
scheduler is a measurement nobody takes. The gate fixed that for itself and
left three behind - the judge's noise floor, the second opinion, and who the
change lands on - plus the two read models that answer the question the whole
loop is for, *did the edit help?*, which lived only on a FastAPI route behind
an Airflow login.

What is worth pinning here is not that the numbers come out. It is that each
one still refuses the thing its module exists to refuse: an inert measurement
does not gate, a figure nobody has taken is not reported as zero, and a
comparison with nothing in common is not reported as agreement.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from ptm import crosscheck, disparity, report, stability, store
from ptm.config import load_domain
from ptm.judge import offline_verdict
from ptm.models import Precedent


@pytest.fixture
def replayed_db(fresh_db):
    """An isolated database carrying one full replay of the shipped fixture.

    The session fixture would do for the reading tests and not for the writing
    ones: :func:`ptm.stability.run` stores a stability run and tags flip rows,
    and a test that did that to the shared database would change what every
    later test sees.
    """
    from ptm import cost, diff
    from ptm.seed import seed_domain

    seed_domain("expenses")
    domain = load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
    candidate = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    baseline = {c.case_id: offline_verdict(c, domain, "v1") for c in cases}
    flips = diff.flips(cases, candidate, domain, baseline=baseline)
    store.save_replay("pytest__clis", "expenses", "v2", "actual", len(cases), flips,
                      diff.summarise(flips, len(cases), domain)["net_impact"], candidate,
                      segments=diff.segment_stats(cases, flips, domain),
                      case_segments=diff.case_segment_rows(cases, domain),
                      ledger=cost.zero(), baseline_version="v1",
                      baseline_verdicts=baseline)
    return {"domain": domain, "cases": cases, "flips": flips}


# --------------------------------------------------------------- judge noise

class TestStability:
    def test_it_takes_the_measurement_rather_than_reading_one(self, replayed_db):
        """Unlike the gate, this one has to judge - so from a shell it judges
        offline and stores the result the way the DAG does."""
        result = stability.run("expenses", "v2", cases=10, repeats=3)
        assert result["report"]["cases_sampled"] == 10
        assert result["report"]["samples_per_case"] == 3
        assert store.latest_stability("expenses", "v2")["cases_sampled"] == 10

    def test_offline_it_says_the_instrument_is_switched_off(self, replayed_db, capsys):
        """A 0% disagreement rate from a deterministic judge is not a clean
        bill of health, and this project never prints one as though it were."""
        assert stability.main(["expenses", "v2", "--cases", "6"]) == 0
        out = capsys.readouterr().out
        assert "0.0% disagreement rate" in out
        assert "instrument is switched off" in out

    def test_it_will_not_gate_on_a_measurement_it_calls_meaningless(self, replayed_db):
        """The same refusal ptm.rules and ptm.calibration make. A ceiling of 0
        would fail every run if the inert check were allowed to fire."""
        assert stability.main(
            ["expenses", "v2", "--cases", "6", "--max-disagreement", "0"]) == 0

    def test_confirming_flips_tags_the_rows_the_queue_reads(self, replayed_db):
        """The half that is not inert offline: the confirmation pass is what
        keeps a flip the judge will not reproduce out of the human queue, and
        the tag has to land on the flip rows for select_for_review to see it."""
        result = stability.run("expenses", "v2", target="flips", cases=5)
        assert result["flips_confirmed"] == 5
        tagged = store.flip_stability("expenses", "v2")
        assert len(tagged) == 5
        assert all(row["stable"] for row in tagged.values()), (
            "a deterministic judge reproduces everything, which is the point of "
            "the caveat rather than a reason to skip the pass")

    def test_a_case_judged_once_cannot_disagree_with_itself(self, replayed_db, capsys):
        """Refused rather than clamped: 0% from one sample is a fact about the
        command, not about the judge."""
        assert stability.main(["expenses", "v2", "--samples", "1"]) == 2
        assert "at least 2" in capsys.readouterr().err

    def test_show_reads_instead_of_running(self, replayed_db, capsys):
        stability.run("expenses", "v2", cases=4, repeats=2)
        assert stability.main(["expenses", "v2", "--show"]) == 0
        assert "4 case(s) disagreed" in capsys.readouterr().out.replace("0 of ", "")

    def test_show_says_nothing_measured_rather_than_zero(self, fresh_db, capsys):
        from ptm.seed import seed_domain

        seed_domain("expenses")
        assert stability.main(["expenses", "v2", "--show"]) == 0
        assert "nothing measured" in capsys.readouterr().out

    def test_confirming_with_no_replay_on_file_refuses(self, fresh_db, capsys):
        from ptm.seed import seed_domain

        seed_domain("expenses")
        assert stability.main(["expenses", "v2", "--target", "flips"]) == 2
        assert "no recorded flips" in capsys.readouterr().err

    def test_the_json_form_carries_the_caveat_with_the_number(self, replayed_db, capsys):
        assert stability.main(["expenses", "v2", "--cases", "4", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["inert"] is True and body["note"]
        assert body["report"]["cases_sampled"] == 4


# ------------------------------------------------------------ a second judge

class TestCrossCheck:
    def test_nothing_measured_is_not_agreement(self, replayed_db, capsys):
        """The one number this module must never invent. An empty panel reads
        as a check that passed, so it says what to run instead."""
        assert crosscheck.main(["expenses", "v2"]) == 0
        out = capsys.readouterr().out
        assert "no cross-check on file" in out
        assert "compare_model" in out

    def test_it_exits_zero_with_nothing_on_file(self, replayed_db):
        """Nothing measured is not a failure - the same stance the gate takes
        about a domain nobody has adjudicated yet."""
        assert crosscheck.main(["expenses", "v2"]) == 0

    def test_it_reads_the_stored_report(self, replayed_db, capsys):
        from ptm import crosscheck as engine

        domain = load_domain("expenses")
        primary = {"exp-0001": offline_verdict(
            store.load_cases("expenses", until=datetime(2026, 9, 1),
                             case_ids=["exp-0001"])[0], domain, "v2")}
        secondary = dict(primary)
        stored = engine.analyse(primary, secondary, domain,
                                primary_label="a", secondary_label="b")
        store.save_cross_check("pytest__cross", "expenses", "v2",
                               stored.model_dump(mode="json"))

        assert crosscheck.main(["expenses", "v2"]) == 0
        out = capsys.readouterr().out
        assert "a second judge is independent, not correct" in out

    def test_an_unknown_domain_is_an_error(self, replayed_db, capsys):
        assert crosscheck.main(["nosuchdomain"]) == 2
        assert "Unknown domain" in capsys.readouterr().err


# ------------------------------------------------------- who it lands on

class TestDisparity:
    def test_it_finds_the_concentration_the_fixture_actually_has(self, replayed_db,
                                                                 capsys):
        assert disparity.main(["expenses", "v2"]) == 0
        out = capsys.readouterr().out
        assert "category=meals" in out
        assert "a concentration is not a fault - it is a question" in out

    def test_it_is_not_inert_offline(self, replayed_db):
        """Unlike stability and the cross-check, this reads the blast radius the
        replay stored - so from a shell it is a real gate rather than a
        rehearsal of one."""
        result = report.disparity("expenses", "v2")
        assert result["gating"], "the shipped fixture concentrates on one category"

    def test_the_domain_chooses_whether_it_stops_a_run(self, replayed_db, capsys):
        assert disparity.main(["expenses", "v2"]) == 0
        assert "reported and not enforced" in capsys.readouterr().err

    def test_the_gate_can_be_overridden_for_one_run(self, replayed_db, capsys):
        assert disparity.main(["expenses", "v2", "--gate", "fail"]) == 1
        assert "GATE FAILS" in capsys.readouterr().err

    def test_an_unsupported_gate_mode_is_refused(self, replayed_db, capsys):
        assert disparity.main(["expenses", "v2", "--gate", "maybe"]) == 2
        assert "'warn' or 'fail'" in capsys.readouterr().err

    def test_the_json_form_keeps_the_caveat(self, replayed_db, capsys):
        assert disparity.main(["expenses", "v2", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["caveat"] and body["findings"]
        assert body["gate"] in {"warn", "fail"}


# -------------------------------------------------------- did the edit help?

class TestCompareAndHistory:
    def test_compare_puts_the_cases_under_the_count(self, replayed_db, capsys):
        """A count of differences with no case ids under it sends the reader
        back to the dashboard, which is what this exists to avoid."""
        assert report.main(["expenses", "--compare", "v1", "v2"]) == 0
        out = capsys.readouterr().out
        assert "v1 against v2" in out
        assert "exp-" in out, "the differing cases are named"

    def test_compare_refuses_a_version_against_itself(self, replayed_db, capsys):
        """It would agree on everything and say nothing, which reads as a
        finding rather than as a mistake."""
        assert report.main(["expenses", "--compare", "v2", "v2"]) == 2
        assert "two different versions" in capsys.readouterr().err

    def test_compare_needs_two_versions(self, replayed_db, capsys):
        assert report.main(["expenses", "--compare", "v1"]) == 2
        assert "two versions" in capsys.readouterr().err

    def test_nothing_in_common_is_not_reported_as_agreement(self, fresh_db, capsys):
        from ptm.seed import seed_domain

        seed_domain("expenses")
        assert report.main(["expenses", "--compare", "v1", "v2"]) == 0
        out = capsys.readouterr().out
        assert "Replay both before comparing them" in out
        assert "differ on 0" not in out, (
            "a version pair nothing has judged must not read as perfect agreement")

    def test_history_lists_every_version_including_the_unreplayed(self, replayed_db,
                                                                  capsys):
        assert report.main(["expenses", "--history"]) == 0
        out = capsys.readouterr().out
        assert "v2" in out and "v1" in out
        assert "never the candidate" in out, (
            "the policy in force has verdicts on file as somebody else's baseline")

    def test_the_history_columns_do_not_collide(self, replayed_db, capsys):
        """The flip count and the rate ran together at the first width tried,
        because a too-narrow format spec does not truncate, it stops padding."""
        report.main(["expenses", "--history"])
        line = next(ln for ln in capsys.readouterr().out.splitlines()
                    if ln.strip().startswith("v2"))
        assert "147   24.5%" in line or "147  24.5%" in line, line

    def test_both_have_a_json_form(self, replayed_db, capsys):
        assert report.main(["expenses", "--history", "--json"]) == 0
        history = json.loads(capsys.readouterr().out)
        assert history["versions"] and history["caveat"]

        assert report.main(["expenses", "--compare", "v1", "v2", "--json"]) == 0
        compared = json.loads(capsys.readouterr().out)
        assert compared["compared"] and compared["differences"]

    def test_an_unknown_domain_is_an_error_not_a_traceback(self, replayed_db, capsys):
        assert report.main(["nosuchdomain", "--history"]) == 2
        assert "Unknown domain" in capsys.readouterr().err

    def test_they_need_a_domain(self, replayed_db, capsys):
        assert report.main(["--history"]) == 2
        assert "name a domain" in capsys.readouterr().err


# ------------------------------------------------------------- small repairs

class TestCalibrationWording:
    def test_a_perfectly_calibrated_judge_is_not_called_underconfident(self):
        """``'over' if gap > 0 else 'under'`` read a gap of exactly zero - the
        thing the module is asking for - as "underconfident by 0%"."""
        from ptm.calibration import _lean

        assert _lean(0.0) == "as confident as it is right"
        assert "overconfident by 14%" == _lean(0.14)
        assert "underconfident by 14%" == _lean(-0.14)

    def test_a_gap_that_rounds_away_does_not_leave_a_word_behind(self):
        """The residual case, and the reason the test is worth having.

        Keying off the stored value rather than the printed one still produced
        "overconfident by 0%" for a gap of 0.0001 - a direction attached to a
        magnitude the reader is being shown as nothing. The threshold has to be
        the precision the sentence is formatted at.
        """
        from ptm.calibration import _lean

        # 0.005 is in this list rather than the next one because Python rounds
        # half to even, so it formats as 0% too. Asking the formatter instead of
        # picking a threshold by hand is what makes the word and the number
        # agree at every boundary, including the ones nobody would guess.
        for tiny in (0.0001, -0.0001, 0.004, -0.004, 0.005, -0.005):
            assert _lean(tiny) == "as confident as it is right", tiny
        assert _lean(0.006) == "overconfident by 1%", "and one that does not round away"

    def test_the_report_still_names_the_direction_when_there_is_one(self, replayed_db):
        from ptm import calibration
        from ptm.models import Verdict

        domain = load_domain("expenses")
        store.save_precedent(Precedent(
            case_id="exp-0001", domain="expenses", correct_outcome="deny",
            ruled_by="finance.lead", established_at=datetime(2026, 1, 1),
            policy_version="v2"))
        scored = calibration.score(
            domain, "v2",
            {"exp-0001": Verdict(outcome="deny", rationale="", confidence=1.0)},
            store.load_precedents("expenses"))
        assert "overconfident by 0%" not in calibration.describe(scored)


class TestTheArgumentHandlingOfTheNewEntryPoints:
    """The paths that were the bug everywhere else in this project.

    ``ptm.sweep`` shipped four of these - a traceback for an unknown domain, a
    traceback for a quoted threshold, a traceback for a malformed axis, and an
    exit 0 for a version that did not exist - precisely because its happy paths
    were tested and its refusals were not. Three new entry points is three new
    chances to do it again, so every refusal each one can make is exercised
    here: the exit code is 2, the message names what was wrong, and nothing
    raises.
    """

    @pytest.mark.parametrize("argv", [
        ["expenses", "v2", "--cases"],
        ["expenses", "v2", "--samples"],
        ["expenses", "v2", "--seed"],
        ["expenses", "v2", "--max-disagreement"],
        ["expenses", "v2", "--target"],
    ])
    def test_stability_refuses_a_flag_with_no_value(self, replayed_db, capsys, argv):
        assert stability.main(argv) == 2
        assert "needs a value" in capsys.readouterr().err

    @pytest.mark.parametrize("argv,expected", [
        (["expenses", "v2", "--target", "bogus"], "'sample' or 'flips'"),
        (["expenses", "v2", "--cases", "abc"], "whole numbers"),
        (["expenses", "v2", "--samples", "two"], "whole numbers"),
        (["expenses", "v2", "--seed", "x"], "whole numbers"),
        (["expenses", "v2", "--max-disagreement", "5"], "between 0 and 1"),
        # A leading minus is a sign, not another flag - `--seed -1` is a seed.
        # Reading it as a missing value sent the reader looking for the wrong
        # mistake, and a rate of -1 is refused on its own terms here.
        (["expenses", "v2", "--max-disagreement", "-1"], "between 0 and 1"),
        (["expenses", "v2", "--seed", "-1", "--cases", "abc"], "whole numbers"),
        (["expenses", "v2", "--nosuchflag"], "--nosuchflag"),
        (["nosuchdomain", "v2"], "no domain config"),
        (["expenses", "v99"], "unknown policy version"),
    ])
    def test_stability_refuses_with_a_reason(self, replayed_db, capsys, argv, expected):
        assert stability.main(argv) == 2
        assert expected in capsys.readouterr().err

    def test_stability_show_reports_an_unknown_domain(self, replayed_db, capsys):
        assert stability.main(["nosuchdomain", "v2", "--show"]) == 2
        assert "Unknown domain" in capsys.readouterr().err

    def test_stability_show_has_a_json_form(self, replayed_db, capsys):
        stability.run("expenses", "v2", cases=3, repeats=2)
        assert stability.main(["expenses", "v2", "--show", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["measured"] is True and body["cases_sampled"] == 3

    def test_stability_gates_when_the_judge_is_not_the_offline_one(self, replayed_db,
                                                                   monkeypatch, capsys):
        """The branch the offline judge can never reach, and the reason the
        ceiling exists: a judge this noisy is one whose flip rates cannot be
        acted on, and the run has to say so with its exit code."""
        monkeypatch.setattr(stability, "OFFLINE", False)
        monkeypatch.setattr(stability, "run", lambda *a, **k: {
            "report": {"disagreement_rate": 0.4}, "inert": False, "summary": "noisy",
            "confirmation_summary": "", "note": ""})
        assert stability.main(["expenses", "v2", "--max-disagreement", "0.1"]) == 1
        assert "GATE FAILS" in capsys.readouterr().err

    def test_stability_holds_the_gate_off_while_the_figure_is_inert(self, replayed_db,
                                                                    monkeypatch, capsys):
        """Kept for the day the offline judge stops being deterministic. A
        measurement this module has just called meaningless must not fail a
        run - the same stance ptm.rules and ptm.calibration take."""
        monkeypatch.setattr(stability, "run", lambda *a, **k: {
            "report": {"disagreement_rate": 0.4}, "inert": True, "summary": "noisy",
            "confirmation_summary": "", "note": stability.INERT_NOTE})
        assert stability.main(["expenses", "v2", "--max-disagreement", "0.1"]) == 0
        assert "held off while the measurement is inert" in capsys.readouterr().err

    @pytest.mark.parametrize("argv,expected", [
        (["expenses", "v2", "--gate"], "'warn' or 'fail'"),
        (["expenses", "v2", "--gate", "maybe"], "'warn' or 'fail'"),
        (["expenses", "v2", "--nosuchflag"], "--nosuchflag"),
        (["nosuchdomain"], "Unknown domain"),
        (["expenses", "v99"], "Unknown policy version"),
    ])
    def test_disparity_refuses_with_a_reason(self, replayed_db, capsys, argv, expected):
        assert disparity.main(argv) == 2
        assert expected in capsys.readouterr().err

    @pytest.mark.parametrize("argv,expected", [
        (["expenses", "v2", "--nosuchflag"], "--nosuchflag"),
        (["nosuchdomain"], "Unknown domain"),
        (["expenses", "v99"], "Unknown policy version"),
    ])
    def test_crosscheck_refuses_with_a_reason(self, replayed_db, capsys, argv, expected):
        assert crosscheck.main(argv) == 2
        assert expected in capsys.readouterr().err

    def test_every_one_of_them_defaults_to_the_shipped_domain(self, replayed_db):
        """Called bare, they answer about expenses/v2 like every sibling does."""
        assert stability.main(["--cases", "3", "--samples", "2"]) == 0
        assert crosscheck.main([]) == 0
        assert disparity.main([]) == 0
