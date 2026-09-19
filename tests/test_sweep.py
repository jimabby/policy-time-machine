"""The threshold sweep: what the number should be, not just which clause it is in.

Attribution stops at "clause 1.1 moves 48 decisions". These cover the step
after it - re-running the replay at each candidate value - and the ways a
sweep can quietly answer the wrong question: rewriting a number it was not
asked to touch, leaving the baseline drifting between points that are meant
to be comparable, or claiming a dial exists when no rule has one.
"""

from __future__ import annotations

import pytest

from ptm import sweep
from ptm.config import load_domain


class TestRetarget:
    def test_moves_the_number_the_field_is_compared_against(self):
        assert sweep.retarget("amount_gbp > 75", "amount_gbp", 100) == ("amount_gbp > 100", 1)

    def test_moves_it_from_either_side_of_the_operator(self):
        out, hits = sweep.retarget("75 < amount_gbp", "amount_gbp", 100)
        assert hits == 1 and "100" in out

    def test_leaves_other_fields_alone(self):
        """The whole point is moving one dial, not every number in the rule."""
        out, hits = sweep.retarget(
            "amount_gbp > 75 and days_notice < 7", "amount_gbp", 100)
        assert hits == 1
        assert "days_notice < 7" in out and "amount_gbp > 100" in out

    def test_leaves_string_comparisons_alone(self):
        out, hits = sweep.retarget("receipt == 'no'", "receipt", 5)
        assert hits == 0 and out == "receipt == 'no'"

    def test_does_not_rewrite_a_boolean_into_a_threshold(self):
        """True is an int in Python; treating it as a dial would be nonsense."""
        _, hits = sweep.retarget("flag == True", "flag", 3)
        assert hits == 0

    def test_reports_a_field_it_could_not_find(self):
        assert sweep.retarget("amount_gbp > 75", "absent", 1) == ("amount_gbp > 75", 0)


class TestThresholds:
    def test_finds_every_dial_in_the_shipped_domain(self, expenses):
        dials = sweep.thresholds(expenses, "v2")
        found = {(d["clause"], d["field"]) for d in dials}
        assert ("1.1", "amount_gbp") in found
        assert ("2.1", "amount_gbp") in found
        assert ("3.1", "days_notice") in found

    def test_records_where_each_dial_lives(self, expenses):
        """The same field appears in several clauses, and they do not have to
        move together - so a dial is (clause, field), not just field."""
        amounts = [d for d in sweep.thresholds(expenses, "v2")
                   if d["field"] == "amount_gbp"]
        assert len({d["clause"] for d in amounts}) > 1

    def test_a_version_with_no_rules_has_no_dials(self, expenses):
        bare = expenses.model_copy(deep=True)
        bare.offline_rules = {}
        assert sweep.thresholds(bare, "v2") == []


class TestVariant:
    def test_scoping_to_a_clause_leaves_the_others_where_they_were(self, expenses):
        patched, hits = sweep.variant(expenses, "v2", "amount_gbp", 500, clause="1.1")
        assert hits == 1
        rules = {r["clause"]: r["when"] for r in patched.offline_rules["v2"]}
        assert "500" in rules["1.1"]
        assert "1000" in rules["5.1"], "clause 5.1 was not asked to move"

    def test_leaves_the_loaded_domain_untouched(self, expenses):
        """load_domain is cached, so mutating in place would poison every
        later read of this domain in the process."""
        before = expenses.offline_rules["v2"][0]["when"]
        sweep.variant(expenses, "v2", "amount_gbp", 9999)
        assert expenses.offline_rules["v2"][0]["when"] == before


class TestSweep:
    def test_a_looser_threshold_changes_more_decisions(self, seeded):
        result = sweep.sweep("expenses", "v2", "amount_gbp", [25, 75, 250], clause="1.1")
        flips = [p["flips"] for p in result["points"]]
        assert flips == sorted(flips), f"expected monotonic growth, got {flips}"

    def test_the_current_setting_reproduces_the_headline(self, replayed):
        """75 is what the shipped policy says, so the sweep must agree with the
        replay at that point or it is measuring something else.

        Given the replay's own case set rather than whatever is in the database,
        so the comparison is against exactly the cases that produced the 147.
        """
        result = sweep.sweep("expenses", "v2", "amount_gbp", [75], clause="1.1",
                             cases=replayed["cases"])
        point = result["points"][0]
        assert point["is_current"] is True
        assert point["flips"] == len(replayed["flips"]) == 147

    def test_reports_the_value_in_force(self, seeded):
        assert sweep.sweep("expenses", "v2", "amount_gbp", [50],
                           clause="1.1")["current_value"] == 75

    def test_holds_the_baseline_still_across_points(self, seeded):
        """The policy in force does not change when a candidate dial moves, so
        the deviation count must not drift between points - if it did, the
        points would not be comparable with each other."""
        result = sweep.sweep("expenses", "v2", "amount_gbp", [50, 75, 100], clause="1.1")
        assert len({p["deviation_flips"] for p in result["points"]}) == 1

    def test_says_so_when_the_dial_does_not_exist(self, seeded):
        with pytest.raises(LookupError, match="compares"):
            sweep.sweep("expenses", "v2", "no_such_field", [1])

    def test_says_so_when_the_clause_has_no_such_dial(self, seeded):
        with pytest.raises(LookupError, match="available dials"):
            sweep.sweep("expenses", "v2", "days_notice", [3], clause="1.1")

    def test_refuses_without_history_rather_than_reporting_a_flat_line(self, fresh_db):
        with pytest.raises(LookupError, match="no expenses cases"):
            sweep.sweep("expenses", "v2", "amount_gbp", [75])

    def test_works_on_the_other_domain_too(self, seeded):
        """Nothing in the sweep knows what an amount is."""
        refunds = load_domain("refunds")
        dial = sweep.thresholds(refunds, "v2")[0]
        result = sweep.sweep("refunds", "v2", dial["field"], [dial["value"]],
                             clause=dial["clause"])
        assert result["points"][0]["is_current"] is True


class TestParseValues:
    def test_reads_a_comma_separated_list(self):
        assert sweep.parse_values("25, 50,75") == [25, 50, 75]

    def test_keeps_fractions_as_fractions(self):
        assert sweep.parse_values("0.5,1") == [0.5, 1]

    def test_rejects_something_that_is_not_a_number(self):
        with pytest.raises(ValueError):
            sweep.parse_values("25,abc")


class TestCli:
    def test_listing_the_dials_needs_no_database(self, capsys):
        assert sweep.main(["expenses", "v2"]) == 0
        assert "clause 1.1" in capsys.readouterr().out

    def test_running_a_sweep_prints_a_table(self, seeded, capsys):
        assert sweep.main(["expenses", "v2", "1.1", "amount_gbp", "50,75"]) == 0
        out = capsys.readouterr().out
        assert "<- current" in out and "policy-driven" in out

    def test_usage_when_called_with_too_little(self, capsys):
        assert sweep.main([]) == 2
        assert "usage:" in capsys.readouterr().out


class TestADialThatDecidesNothing:
    """A flat curve is not an insensitive threshold, it is a threshold that
    decides nothing - and the two look identical in a column of numbers.

    The shipped fixture has one. expenses/v2 clause 6.1 exempts grade 3+ from
    the receipt requirement, and it is only ever reached by cases clause 1.1 has
    already declined to decide; the outcome it gives, ``approve``, is the one
    they fall through to anyway. So the exemption changes which clause is
    *cited* and nothing else, and sweeping its threshold draws four identical
    columns while the interaction panel reports the two dials "independent" -
    true, and read as the opposite of the finding to act on.
    """

    def test_the_shipped_inert_dial_is_reported_as_inert(self, seeded):
        result = sweep.sweep("expenses", "v2", "grade", [2, 3, 4, 6], clause="6.1")
        assert len({point["flips"] for point in result["points"]}) == 1
        assert result["inert"]["measured"] is True
        assert result["inert"]["inert"] is True
        assert "changes which clause is cited" in result["inert"]["note"]

    def test_a_dial_that_moves_decisions_is_not(self, seeded):
        result = sweep.sweep("expenses", "v2", "amount_gbp", [25, 75, 250], clause="1.1")
        assert result["inert"]["measured"] is True
        assert result["inert"]["inert"] is False

    def test_one_setting_measures_nothing_rather_than_reporting_inert(self, seeded):
        """A single point cannot say whether a dial moves anything, and saying
        it does not would be a finding invented from one measurement."""
        result = sweep.sweep("expenses", "v2", "amount_gbp", [75], clause="1.1")
        assert result["inert"]["measured"] is False
        assert "at least two settings" in result["inert"]["hint"]

    def test_it_compares_which_cases_moved_not_how_many(self, seeded, monkeypatch):
        """Two settings can reach the same flip count through different cases.
        Counting would call that 'no effect', which is the same confident wrong
        answer the collapsing check exists to prevent."""
        import ptm.sweep as module

        seen = []
        original = module._signature
        monkeypatch.setattr(module, "_signature",
                            lambda found: seen.append(found) or original(found))
        module.sweep("expenses", "v2", "amount_gbp", [50, 75], clause="1.1")
        assert seen, "the sweep did not take a per-setting signature"

    def test_the_cli_says_so_after_the_table(self, seeded, capsys):
        assert sweep.main(["expenses", "v2", "6.1", "grade", "2,3,4,6"]) == 0
        out = capsys.readouterr().out
        assert "WARNING" in out
        assert out.index("WARNING") > out.index("flips")


class TestAGridAxisThatDecidesNothing:
    def test_the_inert_axis_is_named(self, seeded):
        result = sweep.joint(
            "expenses", "v2",
            {"field": "amount_gbp", "clause": "2.1", "values": [30, 45, 60, 75]},
            {"field": "grade", "clause": "6.1", "values": [2, 3, 4, 6]})
        assert result["first_inert"]["inert"] is False
        assert result["second_inert"]["inert"] is True
        # The interaction reading is still true and still not the point.
        assert result["interaction"]["independent"] is True

    def test_an_axis_of_one_setting_measures_nothing(self, seeded):
        result = sweep.joint(
            "expenses", "v2",
            {"field": "amount_gbp", "clause": "2.1", "values": [30, 60]},
            {"field": "grade", "clause": "6.1", "values": [3]})
        assert result["second_inert"]["measured"] is False

    def test_the_cli_warns_before_the_interaction_reading(self, seeded, capsys):
        assert sweep.main(["expenses", "v2", "--joint",
                           "2.1:amount_gbp=30,60", "6.1:grade=2,4"]) == 0
        out = capsys.readouterr().out
        assert "WARNING" in out and "interaction:" in out
        assert out.index("WARNING") < out.index("interaction:")


class TestTheCliRefusesRatherThanCrashes:
    """This was the last entry point still interpreting argv by its raw length.

    Every sibling reports a bad argument the same way - ``ERROR ...`` on stderr
    and exit 2, the code this project reserves for "could not be run" - because
    :mod:`ptm.cli` and :func:`ptm.selftest.cli_main` were written after a
    traceback came out of the one command whose job is to show the project
    running cleanly. ``ptm.sweep`` never got that pass: it raised
    ``FileNotFoundError`` at exit 1 for an unknown domain, ``ValueError`` for a
    quoted threshold, and - the one that was not even a crash - exited **0**
    for an unknown version, printing an empty dial list.
    """

    def test_an_unknown_domain_is_an_error_and_not_a_traceback(self, capsys):
        assert sweep.main(["nosuchdomain", "v2"]) == 2
        assert "no domain config" in capsys.readouterr().err

    def test_an_unknown_version_is_refused_rather_than_reported_empty(self, capsys):
        """The worst of the four, because it looked like an answer.

        ``numeric dials in expenses/v99:`` followed by nothing, exit 0, reads
        as a policy that states no thresholds. That is precisely the confident
        wrong answer :func:`ptm.sweep.thresholds` refuses to give about a band
        rule, given about a version that does not exist.
        """
        assert sweep.main(["expenses", "v99"]) == 2
        err = capsys.readouterr().err
        assert "unknown policy version 'v99'" in err and "v1" in err

    def test_an_unknown_version_is_refused_in_the_sweeping_form_too(self, capsys):
        assert sweep.main(["expenses", "v99", "amount_gbp", "25,50"]) == 2
        assert "unknown policy version" in capsys.readouterr().err, (
            "it used to blame the field for a version that was never there")

    def test_a_quoted_threshold_is_reported_not_raised(self, seeded, capsys):
        assert sweep.main(["expenses", "v2", "amount_gbp", "abc"]) == 2
        assert "comma-separated numbers" in capsys.readouterr().err

    def test_a_malformed_joint_axis_is_reported_not_raised(self, seeded, capsys):
        assert sweep.main(["expenses", "v2", "--joint",
                           "amount_gbp", "days_notice=3,7"]) == 2
        assert "needs the form" in capsys.readouterr().err

    def test_an_unknown_option_is_named(self, capsys):
        assert sweep.main(["expenses", "v2", "--nosuchflag"]) == 2
        assert "--nosuchflag" in capsys.readouterr().err

    def test_a_flag_no_longer_shifts_the_positionals(self, seeded, capsys):
        """Arguments were read by count, so a stray flag moved field onto values.

        ``--joint`` had to be filtered out of the axis list by hand for exactly
        this reason; every other flag simply corrupted the parse.
        """
        assert sweep.main(["expenses", "v2", "--json", "amount_gbp", "25,50"]) == 2
        assert "--json" in capsys.readouterr().err

    def test_an_empty_dial_list_now_says_what_it_means(self, capsys):
        """The sentence the unknown-version path used to produce by accident.

        A version with no numeric dial is a real state and worth printing; it
        just has to be the only thing that prints this.
        """
        assert sweep.main(["refunds", "v1"]) == 0
        out = capsys.readouterr().out
        assert "numeric dials in refunds/v1" in out

    def test_a_negative_threshold_is_a_value_and_not_a_flag(self, seeded, capsys):
        """A leading minus is a sign. Reading it as an option would refuse a
        sweep that used to work, which is the one thing a parse fix must not do.

        Thresholds are policy numbers and some domains have signed ones - a
        balance, a temperature, a margin. The old code took values positionally
        and so accepted these by accident; a flag scan written as
        ``startswith("-")`` would have turned that accident into a refusal.
        """
        assert sweep.main(["expenses", "v2", "amount_gbp", "-25,50,75"]) == 0
        assert "-25" in capsys.readouterr().out

    def test_the_forms_that_worked_still_work(self, seeded, capsys):
        """The fix is a parse, not a new interface."""
        assert sweep.main(["expenses", "v2"]) == 0
        assert sweep.main(["expenses", "v2", "amount_gbp", "50,75"]) == 0
        assert sweep.main(["expenses", "v2", "1.1", "amount_gbp", "50,75"]) == 0
        assert sweep.main(["expenses", "v2", "--joint",
                           "1.1:amount_gbp=50,75", "3.1:days_notice=3,7"]) == 0
        assert "<- current" in capsys.readouterr().out
