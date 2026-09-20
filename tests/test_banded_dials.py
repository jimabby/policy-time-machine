"""A dial that states a band rather than a threshold, and what refuses to sweep it.

:func:`ptm.sweep.retarget` moves *every* literal a field is compared against.
That is right for the one-sided threshold a clause normally states and silently
destructive for a two-sided one: ``40 < amount <= 100`` comes back as
``60 < amount <= 60``, a condition no case can satisfy. The rewrite reports two
hits, nothing raises, and the sweep draws a confident curve for a rule that
fires on nothing at any point of it.

That is the worst failure this module can have - not a refusal but a wrong
answer about the number a policy owner is choosing - so these cover the three
places that have to notice: the sweep itself, the grid, and the lint that
tells somebody before they ask for either.

**And then the way out.** Refusing was right and it was not enough: "reimbursed
between GBP 40 and GBP 100" is an ordinary sentence for a policy to contain, and
a tool whose headline question is *so what should the number actually be* cannot
answer "not that one". A band is two thresholds written in one sentence, so
``--edge lower`` and ``--edge upper`` move one of them and leave the other where
the policy put it. The rest of these cover that: which end is which under both
spellings, that the other end does not move, that the end in force is the one
marked current, and the one new way it can go wrong - a floor pushed past its
own ceiling, which is a legal rule that matches nothing and would otherwise
arrive as a flat stretch of curve.
"""

from __future__ import annotations

import pytest

from ptm import lint, proposal, report, sweep
from ptm.config import load_domain


def banded(version: str = "v2", when: str = "amount_gbp > 40 and amount_gbp <= 100"):
    """The shipped domain with one clause restated as a band."""
    domain = load_domain("expenses").model_copy(deep=True)
    domain.offline_rules[version] = [
        {"when": when, "outcome": "partial", "clause": "2.1",
         "because": "Reimbursed between GBP 40 and GBP 100."},
        {"when": "amount_gbp > 1000 and director_approval == 'no'", "outcome": "deny",
         "clause": "5.1", "because": "Still needs director approval."},
    ]
    return domain


class TestDetection:
    def test_both_spellings_of_a_band_are_the_same_finding(self):
        """``40 < x <= 100`` is one Compare node and ``x > 40 and x <= 100`` is
        two. They mean the same thing to a reader, so they have to here."""
        chained = sweep.compared_values("40 < amount_gbp <= 100")
        anded = sweep.compared_values("amount_gbp > 40 and amount_gbp <= 100")
        assert chained["amount_gbp"] == anded["amount_gbp"] == [40, 100]

    def test_a_plain_threshold_is_not_a_band(self):
        assert sweep.collapsing(load_domain("expenses"), "v2") == []

    def test_one_literal_repeated_is_not_a_band(self):
        """The test is on *distinct* literals. A rule that states 75 twice is
        redundant, not two-sided: rewriting both to 100 leaves a rule that still
        means exactly what it meant, so refusing to sweep it would be wrong."""
        domain = banded(when="amount_gbp > 75 and (amount_gbp > 75 or grade < 3)")
        assert sweep.collapsing(domain, "v2") == []
        assert sweep.retarget(domain.offline_rules["v2"][0]["when"],
                              "amount_gbp", 100)[0] == \
            "amount_gbp > 100 and (amount_gbp > 100 or grade < 3)"

    def test_it_names_the_clause_the_rule_and_both_ends(self):
        found = sweep.collapsing(banded(), "v2")
        assert len(found) == 1
        assert found[0]["clause"] == "2.1"
        assert found[0]["field"] == "amount_gbp"
        assert found[0]["values"] == [40, 100]

    def test_narrowing_to_another_dial_finds_nothing(self):
        """The sweep only has to refuse the dial somebody actually asked for."""
        assert sweep.collapsing(banded(), "v2", "amount_gbp", clause="5.1") == []
        assert sweep.collapsing(banded(), "v2", "director_approval") == []


class TestTheRewriteIsStillDestructive:
    def test_retarget_collapses_a_band_as_it_always_did(self):
        """The primitive is unchanged on purpose - the detection is what is new.
        If this ever stops collapsing, the checks above are guarding nothing."""
        out, hits = sweep.retarget("amount_gbp > 40 and amount_gbp <= 100",
                                   "amount_gbp", 60)
        assert hits == 2
        assert out == "amount_gbp > 60 and amount_gbp <= 60"


class TestSweepRefuses:
    def test_it_refuses_rather_than_drawing_the_curve(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError, match="more than one number"):
            sweep.sweep("expenses", "v2", "amount_gbp", [50, 60, 70], clause="2.1")

    def test_the_message_says_what_to_do_about_it(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError) as exc:
            sweep.sweep("expenses", "v2", "amount_gbp", [50], clause="2.1")
        assert "Split the band across two rules" in str(exc.value)

    def test_a_grid_refuses_on_either_axis(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError, match="more than one number"):
            sweep.joint("expenses", "v2",
                        {"field": "director_approval", "values": [0, 1], "clause": ""},
                        {"field": "amount_gbp", "values": [50, 60], "clause": "2.1"})

    def test_a_sound_dial_in_the_same_version_still_sweeps(self, monkeypatch, seeded):
        """The refusal is about one dial, not about the version. A policy with
        one band in it must not lose every other curve."""
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        result = sweep.sweep("expenses", "v2", "amount_gbp", [800, 1200], clause="5.1")
        assert len(result["points"]) == 2


class TestItIsListedRatherThanHidden:
    def test_thresholds_marks_the_dial_instead_of_dropping_it(self):
        """A field that vanished from the menu reads as a policy with no such
        threshold, which is a different and equally wrong thing to believe."""
        dials = sweep.thresholds(banded(), "v2")
        band = [d for d in dials if d["clause"] == "2.1"]
        assert band and band[0]["collapses"] is True
        assert band[0]["values"] == [40, 100]

    def test_a_sound_dial_is_marked_sweepable(self):
        dials = sweep.thresholds(load_domain("expenses"), "v2")
        assert dials and not any(d["collapses"] for d in dials)

    def test_the_api_carries_the_flag_the_dashboard_filters_on(self, seeded):
        assert all("collapses" in d for d in report.thresholds("expenses", "v2"))


class TestTheLintSaysSoFirst:
    def test_it_warns_before_anybody_asks_for_the_curve(self, monkeypatch):
        monkeypatch.setattr(lint, "load_domain", lambda name: banded())
        problems = lint.check_domain("expenses")
        banded_warnings = [p for p in problems if "band, not a threshold" in p.message]
        assert len(banded_warnings) == 1
        assert banded_warnings[0].level == "WARN", "a band is a legal rule, not an error"

    def test_the_shipped_domains_have_none(self):
        for name in ("expenses", "refunds"):
            assert not [p for p in lint.check_domain(name)
                        if "band, not a threshold" in p.message]


class TestTheProposerSkipsIt:
    def test_a_banded_dial_is_never_searched(self, monkeypatch, seeded):
        """The proposer scores a setting as though somebody could adopt it.
        ptm.sweep.variant would hand it a rule that matches nothing."""
        domain = banded()
        found = proposal.evidence(domain, "v2")
        assert all(d["clause"] != "2.1" for d in found["dials"])
        assert any(d["clause"] == "5.1" for d in found["dials"])


class TestWhichEndIsWhich:
    """The classification the whole feature rests on. Get this backwards and
    ``--edge lower`` moves the ceiling, silently, on a curve somebody acts on."""

    @pytest.mark.parametrize("expression", [
        "amount_gbp > 40 and amount_gbp <= 100",
        "40 < amount_gbp <= 100",
        # The field on the other side of its own comparison. Ordinary to write
        # and the case a check built on one spelling gets exactly wrong.
        "40 < amount_gbp and 100 >= amount_gbp",
        "amount_gbp <= 100 and amount_gbp > 40",
    ])
    def test_every_spelling_of_one_band_agrees(self, expression):
        found = sweep.bounds_in(expression, "amount_gbp")
        assert [b["value"] for b in found["lower"]] == [40]
        assert [b["value"] for b in found["upper"]] == [100]
        assert found[sweep.LOOSE] == []

    def test_strictness_is_carried_with_the_bound(self):
        """``40 < x`` and ``40 <= x`` are different rules, and the difference is
        the whole of whether ``40 <= x <= 40`` admits anything."""
        assert sweep.bounds_in("amount_gbp > 40", "amount_gbp")["lower"] == \
            [{"value": 40, "closed": False}]
        assert sweep.bounds_in("amount_gbp >= 40", "amount_gbp")["lower"] == \
            [{"value": 40, "closed": True}]

    def test_a_number_that_sits_on_neither_end_is_kept_not_dropped(self):
        """``5 < 10 < amount`` compares amount against 10; the 5 is a bound on
        nothing. Dropping it would let --edge move a rule this cannot read."""
        found = sweep.bounds_in("5 < 10 < amount_gbp", "amount_gbp")
        assert [b["value"] for b in found["lower"]] == [10]
        assert [b["value"] for b in found[sweep.LOOSE]] == [5]

    def test_an_equality_is_both_ends_at_once_and_therefore_neither(self):
        found = sweep.bounds_in("grade == 3", "grade")
        assert [b["value"] for b in found["point"]] == [3]
        assert found["lower"] == found["upper"] == []

    def test_a_relation_between_two_fields_has_no_dial_on_it(self):
        assert sweep.bounds_in("amount_gbp > other_total", "amount_gbp")["lower"] == []


class TestMovingOneEnd:
    def test_the_lower_end_moves_and_the_upper_stays(self):
        out, hits = sweep.retarget("amount_gbp > 40 and amount_gbp <= 100",
                                   "amount_gbp", 60, "lower")
        assert (out, hits) == ("amount_gbp > 60 and amount_gbp <= 100", 1)

    def test_the_upper_end_moves_and_the_lower_stays(self):
        out, hits = sweep.retarget("40 < amount_gbp <= 100", "amount_gbp", 250, "upper")
        assert (out, hits) == ("40 < amount_gbp <= 250", 1)

    def test_naming_no_end_still_collapses_exactly_as_before(self):
        """The default is unchanged on purpose. Every caller that had it before
        ends were nameable must behave identically."""
        assert sweep.retarget("amount_gbp > 40 and amount_gbp <= 100",
                              "amount_gbp", 60) == \
            ("amount_gbp > 60 and amount_gbp <= 60", 2)

    def test_an_end_the_rule_does_not_state_moves_nothing(self):
        """A one-sided threshold has no ceiling, so asking for one is not an
        error - it is a rewrite with no hits, which the sweep reports by name."""
        assert sweep.retarget("amount_gbp > 75", "amount_gbp", 100, "upper") == \
            ("amount_gbp > 75", 0)


class TestAFloorPastItsCeiling:
    """The one new way this can go wrong, and it is not a rewrite artefact:
    ``150 < amount <= 100`` is what the number somebody typed actually means."""

    @pytest.mark.parametrize("expression,empty", [
        ("150 < amount_gbp <= 100", True),
        ("60 < amount_gbp <= 100", False),
        ("40 <= amount_gbp <= 40", False),   # admits exactly 40
        ("40 < amount_gbp <= 40", True),     # admits nothing
        ("amount_gbp > 150 and amount_gbp <= 100", True),
        ("amount_gbp > 75", False),
    ])
    def test_it_knows_when_nothing_can_satisfy_the_rule(self, expression, empty):
        assert sweep.empty_for(expression, "amount_gbp") is empty

    def test_it_declines_to_answer_about_a_shape_it_cannot_read(self):
        """Sound rather than complete, like ptm.safe_eval.implies. An `or` makes
        the question unanswerable here, and guessing would refuse a live rule."""
        assert sweep.empty_for("amount_gbp > 150 or amount_gbp < 100", "amount_gbp") is False

    def test_the_other_terms_of_the_rule_do_not_confuse_it(self):
        assert sweep.empty_for(
            "category == 'meals' and amount_gbp > 150 and amount_gbp <= 100",
            "amount_gbp") is True


class TestTheSweepDrawsTheCurve:
    def test_one_end_sweeps_where_the_whole_rule_was_refused(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        result = sweep.sweep("expenses", "v2", "amount_gbp", [50, 60, 80],
                             clause="2.1", edge="lower")
        assert [p["value"] for p in result["points"]] == [50, 60, 80]
        assert result["edge"] == "lower"
        assert not result["emptied"]

    def test_the_end_in_force_is_the_one_marked_current(self, monkeypatch, seeded):
        """Not the first number in the rule. Sweeping the ceiling of
        ``40 < x <= 100`` and marking 40 as current is a caption under the wrong
        column, and the column is the thing a reader picks a number off."""
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        upper = sweep.sweep("expenses", "v2", "amount_gbp", [80, 100, 140],
                            clause="2.1", edge="upper")
        assert upper["current_value"] == 100
        assert [p["value"] for p in upper["points"] if p["is_current"]] == [100]
        lower = sweep.sweep("expenses", "v2", "amount_gbp", [20, 40, 60],
                            clause="2.1", edge="lower")
        assert lower["current_value"] == 40

    def test_a_setting_that_empties_the_clause_is_reported_not_plotted_quietly(
            self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        result = sweep.sweep("expenses", "v2", "amount_gbp", [60, 150],
                             clause="2.1", edge="lower")
        assert [p["value"] for p in result["points"] if p["empties_rule"]] == [150]
        assert [row["value"] for row in result["emptied"]] == [150]
        assert result["emptied"][0]["rules"][0]["clause"] == "2.1"
        said = sweep.describe_emptied(result["emptied"], "amount_gbp", "lower")
        assert "match no case at all" in said and "2.1" in said

    def test_an_end_nothing_states_is_refused_by_name(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError, match="upper bound"):
            sweep.sweep("expenses", "v2", "amount_gbp", [1200],
                        clause="5.1", edge="upper")

    def test_an_end_that_is_not_an_end_is_refused(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError, match="lower"):
            sweep.sweep("expenses", "v2", "amount_gbp", [50], clause="2.1", edge="sideways")

    def test_two_floors_in_one_rule_are_still_refused(self, monkeypatch, seeded):
        """--edge is not a way to sweep anything. It names one end; a rule that
        states that end twice has not got one threshold there either."""
        domain = banded(when="amount_gbp > 40 and amount_gbp > 55 and amount_gbp <= 100")
        monkeypatch.setattr(sweep, "load_domain", lambda name: domain)
        with pytest.raises(LookupError, match="more than one lower bound"):
            sweep.sweep("expenses", "v2", "amount_gbp", [50], clause="2.1", edge="lower")
        # ...while the end that *is* unambiguous in the very same rule sweeps.
        assert len(sweep.sweep("expenses", "v2", "amount_gbp", [90, 120],
                               clause="2.1", edge="upper")["points"]) == 2


class TestTheGridTakesEndsToo:
    def test_an_axis_can_name_one_end(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        result = sweep.joint(
            "expenses", "v2",
            {"field": "amount_gbp", "values": [50, 60], "clause": "2.1", "edge": "lower"},
            {"field": "amount_gbp", "values": [800, 1200], "clause": "5.1"})
        assert len(result["points"]) == 4

    def test_the_two_ends_of_one_band_are_two_different_dials(self, monkeypatch, seeded):
        """Same field, same clause, opposite ends. That is a grid worth running -
        it is the only thing that shows a band's width mattering - so the
        same-dial refusal must not swallow it."""
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        result = sweep.joint(
            "expenses", "v2",
            {"field": "amount_gbp", "values": [40, 60], "clause": "2.1", "edge": "lower"},
            {"field": "amount_gbp", "values": [100, 140], "clause": "2.1", "edge": "upper"})
        assert len(result["points"]) == 4

    def test_the_same_end_of_the_same_dial_is_still_refused(self, monkeypatch, seeded):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        with pytest.raises(LookupError, match="same dial"):
            sweep.joint(
                "expenses", "v2",
                {"field": "amount_gbp", "values": [40, 60], "clause": "2.1",
                 "edge": "lower"},
                {"field": "amount_gbp", "values": [50, 70], "clause": "2.1",
                 "edge": "lower"})


class TestTheMenuOffersTheRoute:
    def test_a_band_lists_both_of_its_ends(self):
        dials = sweep.thresholds(banded(), "v2")
        band = next(d for d in dials if d["clause"] == "2.1")
        assert band["collapses"] is True
        assert band["edges"] == ["lower", "upper"]

    def test_a_dial_no_end_can_be_told_on_offers_none(self):
        domain = banded(when="amount_gbp > 40 and amount_gbp > 55 and amount_gbp <= 100")
        band = next(d for d in sweep.thresholds(domain, "v2") if d["clause"] == "2.1")
        assert band["edges"] == ["upper"], "one end is still unambiguous"

    def test_the_api_carries_the_ends_to_the_dashboard(self, seeded):
        assert all("edges" in d for d in report.thresholds("expenses", "v2"))


class TestTheLintPointsAtIt:
    def test_the_warning_names_the_flag_that_works(self, monkeypatch):
        monkeypatch.setattr(lint, "load_domain", lambda name: banded())
        message = next(p.message for p in lint.check_domain("expenses")
                       if "band, not a threshold" in p.message)
        assert "--edge lower" in message and "--edge upper" in message


class TestTheFlagOnTheCommandLine:
    """``--edge`` takes a value, which is the thing that made it worth testing
    at this level: every other flag this module has is a bare switch, and the
    positional parser works by filtering flags out of argv. A value left behind
    by that filter does not error - it lands where the *field name* is read."""

    def test_it_sweeps_one_end_and_says_which(self, monkeypatch, seeded, capsys):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        assert sweep.main(["expenses", "v2", "2.1", "amount_gbp", "50,60,80",
                           "--edge", "lower"]) == 0
        out = capsys.readouterr().out
        assert "the lower end of clause 2.1 amount_gbp" in out
        assert "\n            50" in out and "\n            80" in out

    def test_the_joined_spelling_works_too(self, monkeypatch, seeded, capsys):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        assert sweep.main(["expenses", "v2", "2.1", "amount_gbp", "50,60",
                           "--edge=upper"]) == 0
        assert "the upper end of" in capsys.readouterr().out

    def test_its_value_is_not_read_as_a_field_name(self, monkeypatch, seeded, capsys):
        """The failure this test exists for: --edge placed before the
        positionals, its value swallowed into them, and 'lower' swept as a
        dial. It has to work from either side of the arguments."""
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        assert sweep.main(["expenses", "v2", "--edge", "lower",
                           "2.1", "amount_gbp", "50,60"]) == 0
        assert "the lower end of clause 2.1 amount_gbp" in capsys.readouterr().out

    def test_an_end_that_is_not_an_end_is_refused_by_name(self, capsys):
        assert sweep.main(["expenses", "v2", "2.1", "amount_gbp", "50",
                           "--edge", "sideways"]) == 2
        assert "lower or upper" in capsys.readouterr().err

    def test_the_flag_without_a_value_is_refused(self, capsys):
        assert sweep.main(["expenses", "v2", "2.1", "amount_gbp", "50", "--edge"]) == 2
        assert "--edge needs a value" in capsys.readouterr().err

    def test_a_grid_is_told_to_name_its_ends_on_the_axes(self, capsys):
        """One flag cannot say two things, and the two ends of one band is the
        grid this feature makes possible. Refused rather than applied to both."""
        assert sweep.main(["expenses", "v2", "--joint", "2.1:amount_gbp=40,60",
                           "1.1:amount_gbp=50,75", "--edge", "lower"]) == 2
        assert "names its ends on the axes" in capsys.readouterr().err

    def test_the_dial_menu_offers_the_route(self, monkeypatch, capsys):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        assert sweep.main(["expenses", "v2"]) == 0
        out = capsys.readouterr().out
        assert "a band, compared against [40, 100]" in out
        assert "--edge lower or --edge upper" in out

    def test_a_setting_that_switches_a_clause_off_is_marked_in_the_table(
            self, monkeypatch, seeded, capsys):
        monkeypatch.setattr(sweep, "load_domain", lambda name: banded())
        assert sweep.main(["expenses", "v2", "2.1", "amount_gbp", "60,150",
                           "--edge", "lower"]) == 0
        out = capsys.readouterr().out
        assert "[clause matches nothing at this setting]" in out
        assert "WARNING" in out and "match no case at all" in out

    def test_the_usage_text_documents_it(self, capsys):
        assert sweep.main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "--edge lower|upper" in out
        assert "2.1:amount@lower=30,45" in out

    def test_nothing_changes_for_a_sweep_that_names_no_end(self, seeded, capsys):
        """The shipped fixture has no band, so this is the whole existing
        surface proving it did not move."""
        assert sweep.main(["expenses", "v2", "1.1", "amount_gbp", "50,75,100"]) == 0
        out = capsys.readouterr().out
        assert "sweeping clause 1.1 amount_gbp" in out
        assert "end of" not in out
