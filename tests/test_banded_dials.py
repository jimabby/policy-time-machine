"""A dial that states a band rather than a threshold, and what refuses to sweep it.

:func:`ptm.sweep.retarget` moves *every* literal a field is compared against.
That is right for the one-sided threshold a clause normally states and silently
destructive for a two-sided one: ``40 < amount <= 100`` comes back as
``60 < amount <= 60``, a condition no case can satisfy. The rewrite reports two
hits, nothing raises, and the sweep draws a confident curve for a rule that
fires on nothing at any point of it.

That is the worst failure this module can have - not a refusal but a wrong
answer about the number a policy owner is choosing - so these cover the three
places that now have to notice: the sweep itself, the grid, and the lint that
tells somebody before they ask for either.
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
