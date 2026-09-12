"""The AI layer: prompt grounding, and the offline stand-ins."""

from __future__ import annotations

from datetime import datetime

from ptm import ai
from ptm.models import Flip


def flip(case_id="c1", impact=100.0, confidence=0.9, direction="loosening",
         clause="1.1", was="deny", becomes="approve"):
    return Flip(case_id=case_id, decided_at=datetime(2025, 6, 1), actual_outcome=was,
                new_outcome=becomes, rationale="because the threshold moved",
                confidence=confidence, policy_clause=clause, impact=impact, direction=direction)


SUMMARY = {"cases_replayed": 600, "flips": 147, "flip_rate": 0.245, "loosening": 135,
           "tightening": 12, "impact_loosening": 17994.0, "impact_tightening": 5980.0,
           "net_impact": 12014.0, "impact_unit": "GBP"}


# ------------------------------------------------------------------ prompts


def test_brief_prompt_states_every_aggregate_and_the_policy(expenses):
    p = ai.build_brief_prompt(SUMMARY, [flip()], expenses, "v2")
    for token in ["600", "147", "24.5%", "17,994", "5,980", "12,014", "GBP"]:
        assert token in p, f"the model must be shown {token}"
    assert "GBP 75" in p, "the policy text is inlined so the brief can cite clauses"
    assert "positive net figure means the change costs" in p, "sign convention must be explicit"


def test_brief_prompt_lists_the_individual_cases_it_was_given(expenses):
    p = ai.build_brief_prompt(SUMMARY, [flip("exp-0478", impact=1714.0)], expenses, "v2")
    assert "exp-0478" in p and "1,714" in p and "2025-06-01" in p


def test_themes_prompt_constrains_the_model_to_the_domains_outcomes(expenses):
    p = ai.build_themes_prompt([flip()], expenses, "v2")
    assert "approve, partial, deny" in p
    assert "c1" in p


def test_amendment_prompt_names_the_human_and_the_reversal(expenses):
    violations = [{"case_id": "exp-0478", "established_outcome": "approve",
                   "proposed_outcome": "deny", "ruled_by": "finance.lead",
                   "established_at": "2026-01-01", "note": "long-standing employee",
                   "proposed_rationale": "over the threshold"}]
    p = ai.build_amendment_prompt(violations, expenses, "v2")
    assert "finance.lead" in p and "exp-0478" in p
    assert "long-standing employee" in p, "the human's reasoning is the most useful input"
    assert "narrow it" in p, "the model must not be allowed to just abandon the change"


def test_amendment_prompt_handles_a_missing_note(expenses):
    p = ai.build_amendment_prompt(
        [{"case_id": "c1", "established_outcome": "approve", "proposed_outcome": "deny",
          "ruled_by": "x", "established_at": "2026-01-01"}], expenses, "v2")
    assert "none given" in p


# ------------------------------------------------------------ offline brief


def test_offline_brief_reports_cost_when_the_net_is_positive(expenses):
    b = ai.offline_brief(SUMMARY, [flip()], expenses, "v2")
    assert "costs" in b.headline and "12,014" in b.headline
    assert b.verdict in {"ship", "ship_with_caveats", "do_not_ship"}


def test_offline_brief_reports_saving_when_the_net_is_negative(expenses):
    b = ai.offline_brief({**SUMMARY, "net_impact": -4000.0, "tightening": 0}, [flip()], expenses, "v2")
    assert "saves" in b.headline


def test_a_large_flip_rate_blocks_the_recommendation(expenses):
    b = ai.offline_brief({**SUMMARY, "flip_rate": 0.45}, [], expenses, "v2")
    assert b.verdict == "do_not_ship"


def test_a_clean_small_change_is_shippable(expenses):
    quiet = {**SUMMARY, "flips": 3, "flip_rate": 0.005, "tightening": 0,
             "impact_tightening": 0.0, "net_impact": -50.0}
    b = ai.offline_brief(quiet, [flip(confidence=0.99)], expenses, "v2")
    assert b.verdict == "ship"
    assert b.risks, "even a clean brief says something in the risks column"


def test_ambiguous_flips_are_called_out_as_a_drafting_problem(expenses):
    b = ai.offline_brief(SUMMARY, [flip(confidence=0.3, clause="1.1")], expenses, "v2")
    assert any("confidence" in r for r in b.risks)
    assert "clause 1.1" in " ".join(b.risks)


def test_offline_brief_always_offers_watch_items(expenses):
    assert len(ai.offline_brief(SUMMARY, [], expenses, "v2").watch_items) >= 2


# ----------------------------------------------------------- offline themes


def test_themes_group_by_clause(expenses):
    flips_ = [flip(f"a{i}", clause="1.1") for i in range(5)] + \
             [flip(f"b{i}", clause="2.1") for i in range(3)]
    t = ai.offline_themes(flips_, expenses)
    assert [x.case_count for x in t.themes] == [5, 3], "largest theme first"
    assert t.themes[0].clause == "1.1"
    assert t.unexplained == 0


def test_clauseless_flips_group_by_outcome_transition(expenses):
    """No clause means no rule reached the case - a finding, not a long tail."""
    flips_ = [flip(f"c{i}", clause="", was="deny", becomes="approve") for i in range(4)]
    t = ai.offline_themes(flips_, expenses)
    assert t.unexplained == 0
    assert t.themes[0].case_count == 4
    assert "unruled" in t.themes[0].name
    assert "fall through to the default" in t.themes[0].explanation


def test_a_mixed_direction_theme_is_labelled_mixed(expenses):
    flips_ = [flip("a", clause="1.1", direction="loosening"),
              flip("b", clause="1.1", direction="tightening")]
    assert ai.offline_themes(flips_, expenses).themes[0].direction == "mixed"


def test_themes_are_capped_and_the_rest_counted_as_tail(expenses):
    flips_ = [flip(f"c{i}", clause=f"{i}.0") for i in range(9)]
    t = ai.offline_themes(flips_, expenses)
    assert len(t.themes) == 6
    assert t.unexplained == 3, "the three smallest groups become the tail"


def test_themes_cite_their_largest_examples(expenses):
    flips_ = [flip("small", clause="1.1", impact=1.0), flip("big", clause="1.1", impact=999.0)]
    assert ai.offline_themes(flips_, expenses).themes[0].example_case_ids[0] == "big"


def test_themes_of_nothing(expenses):
    t = ai.offline_themes([], expenses)
    assert t.themes == [] and t.unexplained == 0


# -------------------------------------------------------- offline amendment


def test_no_violations_needs_no_amendment(expenses):
    a = ai.offline_amendment([], expenses, "v2")
    assert a.feasible and not a.edits


def test_offline_amendment_defers_the_drafting_and_says_so(expenses):
    violations = [{"case_id": "c1", "established_outcome": "approve", "proposed_outcome": "deny",
                   "ruled_by": "finance.lead", "established_at": "2026-01-01"}]
    a = ai.offline_amendment(violations, expenses, "v2")
    assert len(a.edits) == 1
    assert "c1" in a.edits[0].proposed_text
    assert "PTM_OFFLINE=0" in a.rationale, "offline must not pretend to have drafted policy prose"
    assert a.residual_risk


# ------------------------------------------------------------- xcom coercion


def test_as_model_accepts_a_model_a_dict_or_json(expenses):
    b = ai.offline_brief(SUMMARY, [], expenses, "v2")
    assert ai.as_model(b, ai.PolicyBrief) is b
    assert ai.as_model(b.model_dump(), ai.PolicyBrief).headline == b.headline
    assert ai.as_model(b.model_dump_json(), ai.PolicyBrief).headline == b.headline
