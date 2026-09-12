"""The precedent record: the reviewer's reasoning, and whether a ruling still applies.

Precedent is the only durable output here, so it is the only thing that cannot
be recomputed when it turns out to be missing something. Two things it used to
be missing: the reviewer's reason, which the HITL task read out of a parameter
nothing declared, and the circumstances - which policy they were shown - without
which a ruling made about a rewritten clause is enforced as though it were made
this morning.
"""

from __future__ import annotations

from datetime import datetime

from ptm import diff, store
from ptm.models import Precedent


def ruling(case_id="c1", **kwargs) -> Precedent:
    base = dict(case_id=case_id, domain="expenses", correct_outcome="deny",
                ruled_by="finance.lead", established_at=datetime(2025, 3, 1))
    return Precedent(**{**base, **kwargs})


class TestTheReasonIsKept:
    def test_a_note_survives_the_round_trip(self, fresh_db):
        store.save_precedent(ruling(note="Too strict for a claim of this size."))
        [back] = store.load_precedents("expenses")
        assert back.note == "Too strict for a claim of this size."

    def test_the_hitl_task_declares_the_parameter_it_reads(self):
        """`record` has always read params_input['note']. Nothing declared it,
        so every precedent on file carried an empty note - including the ones
        the drafter is shown under the heading 'their note'."""
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "dags" / "policy_time_machine.py").read_text(encoding="utf-8")
        review = source[source.index("HITLOperator.partial("):]
        review = review[:review.index(".expand(")]
        assert '"note"' in review, \
            "the reviewer cannot record a reason the operator never asks for"

    def test_conflicting_rulings_carry_each_reviewer_s_reason(self, fresh_db, expenses):
        """A conflict is settled by two people talking. The panel has to show
        them what they are disagreeing about, not only that they disagree."""
        rows = [
            {"case_id": "a", "correct_outcome": "deny", "ruled_by": "alice",
             "note": "Outside the cap.", "payload": {"category": "meals", "receipt": "no",
                                                     "director_approval": "no",
                                                     "amount_gbp": 100}},
            {"case_id": "b", "correct_outcome": "approve", "ruled_by": "bob",
             "note": "Client was present.", "payload": {"category": "meals",
                                                        "receipt": "no",
                                                        "director_approval": "no",
                                                        "amount_gbp": 110}},
        ]
        [conflict] = diff.precedent_conflicts(rows, expenses)
        assert {n["ruled_by"] for n in conflict.notes} == {"alice", "bob"}
        assert {n["note"] for n in conflict.notes} == {"Outside the cap.",
                                                       "Client was present."}


class TestWhetherARulingStillApplies:
    def test_a_ruling_about_a_rewritten_clause_is_flagged(self, expenses):
        """Clause 1.1 moved from GBP 25 to GBP 75 between v1 and v2. A ruling
        made against v1's 1.1 was about a sentence v2 no longer contains."""
        stale = diff.stale_precedents(
            [ruling(policy_version="v1", judged_clause="1.1")], expenses, "v2")
        assert [r["reason"] for r in stale] == ["clause_changed"]
        assert stale[0]["was"] != stale[0]["now"]

    def test_a_ruling_made_about_this_very_version_is_not_flagged(self, expenses):
        assert not diff.stale_precedents(
            [ruling(policy_version="v2", judged_clause="1.1")], expenses, "v2")

    def test_an_unchanged_clause_is_not_flagged(self, expenses):
        """Only a clause whose *text* moved is stale. A new version that leaves
        a sentence alone leaves every ruling about it standing.

        The shipped fixture rewords all six clauses between v1 and v2 - which is
        what makes it a good demo and a useless test case for this - so the
        no-change branch is built here: a second version name pointed at the
        same markdown.
        """
        renamed = expenses.model_copy(
            update={"policies": {**expenses.policies, "v1-copy": expenses.policies["v1"]}})
        assert renamed.clause_text("v1", "1.1") == renamed.clause_text("v1-copy", "1.1")
        assert not diff.stale_precedents(
            [ruling(policy_version="v1", judged_clause="1.1")], renamed, "v1-copy")

    def test_a_ruling_with_no_circumstances_is_unknown_not_fresh(self, expenses):
        """Silence about a precedent recorded before this was captured would
        read as a clean bill of health."""
        [row] = diff.stale_precedents([ruling()], expenses, "v2")
        assert row["reason"] == "unknown"

    def test_a_ruling_under_a_version_that_no_longer_exists_is_flagged(self, expenses):
        [row] = diff.stale_precedents(
            [ruling(policy_version="v99", judged_clause="1.1")], expenses, "v2")
        assert row["reason"] == "version_gone"

    def test_it_is_a_warning_and_says_so(self, expenses):
        text = diff.describe_stale(
            diff.stale_precedents([ruling(policy_version="v1", judged_clause="1.1")],
                                  expenses, "v2"), "v2")
        assert "nothing here fails a run" in text, \
            "whether a ruling still holds is a person's call, not a run's"

    def test_the_circumstances_survive_the_round_trip(self, fresh_db):
        store.save_precedent(ruling(policy_version="v2", judged_outcome="approve",
                                    judged_clause="6.1"))
        [back] = store.load_precedents("expenses")
        assert (back.policy_version, back.judged_outcome, back.judged_clause) == \
               ("v2", "approve", "6.1")
