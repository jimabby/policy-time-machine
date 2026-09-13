"""Re-adjudicating a ruling that is no longer about the text it was made about.

:func:`ptm.diff.stale_precedents` has always been able to say which rulings
predate the clause now in front of them. The gate printed the warning on every
run and went on enforcing every one of them, and the README said
"re-adjudicating one of these is how it stops being a guess" - which nothing in
the project could do. The queue only ever held flips.

So the second half: a queue of stale rulings shaped exactly like the flip queue,
and an archive, because the one operation that overwrites a precedent must not
be the one operation that loses one.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ptm import diff, report, store
from ptm.config import load_domain
from ptm.judge import offline_verdict
from ptm.models import Precedent


def ruling(case_id: str, outcome: str = "deny", by: str = "finance.lead",
           version: str = "v1", clause: str = "1.1", note: str = "No receipt.",
           at: datetime | None = None) -> Precedent:
    return Precedent(case_id=case_id, domain="expenses", correct_outcome=outcome,
                     ruled_by=by, note=note, established_at=at or datetime(2025, 3, 4),
                     policy_version=version, judged_outcome="approve",
                     judged_clause=clause)


@pytest.fixture
def judged(fresh_db):
    """A replayed domain with one ruling made under v1 against a clause v2 rewrote.

    Seeded into the isolated database rather than leaning on the session one:
    these tests write precedents, and precedents are the one thing a re-seed
    deliberately does not clear.
    """
    from ptm.seed import seed_domain

    seed_domain("expenses", force=True)
    domain = load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1), limit=40)
    verdicts = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    store.save_replay("r1", "expenses", "v2", "actual", len(cases), [], 0.0, verdicts)
    store.save_precedent(ruling(cases[0].case_id))
    return {"domain": domain, "cases": cases, "verdicts": verdicts,
            "case_id": cases[0].case_id}


def queue(judged, **kwargs):
    domain, verdicts = judged["domain"], judged["verdicts"]
    precedents = store.load_precedents("expenses")
    stale = diff.stale_precedents(precedents, domain, "v2")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1),
                             case_ids=[r["case_id"] for r in stale])
    return diff.stale_review_items(stale, precedents, cases, verdicts, domain, **kwargs)


class TestTheQueue:
    def test_a_stale_ruling_reaches_it(self, judged):
        items = queue(judged)
        assert [i["case_id"] for i in items] == [judged["case_id"]]
        assert items[0]["readjudication"]["reason"] == "clause_changed"

    def test_an_item_is_shaped_exactly_like_a_flip(self, judged):
        """So the HITL fan-out, the rendering and the recording are one path."""
        from ptm.models import Flip

        item = dict(queue(judged)[0])
        item.pop("readjudication")
        assert Flip(**item).case_id == judged["case_id"]

    def test_it_carries_what_the_reviewer_needs_to_answer(self, judged):
        """They are not settling a case, they are deciding whether somebody
        else's answer survives a rewrite - which needs that answer, the reason
        given for it, and both versions of the sentence."""
        again = queue(judged)[0]["readjudication"]
        assert again["precedent_outcome"] == "deny"
        assert again["ruled_by"] == "finance.lead"
        assert again["note"] == "No receipt."
        assert again["was"] and again["now"] and again["was"] != again["now"]

    def test_a_case_the_policy_has_never_judged_is_skipped(self, judged):
        """Asking somebody to re-confirm against a policy nothing has applied
        is asking them to guess; the fix is to run the gate."""
        store.save_precedent(ruling(judged["cases"][1].case_id))
        judged["verdicts"].pop(judged["cases"][1].case_id)
        assert [i["case_id"] for i in queue(judged)] == [judged["case_id"]]

    def test_contradicted_rulings_come_first(self, judged):
        """A ruling the policy now agrees with is stale on paper only."""
        agreeing = next(c for c in judged["cases"][1:]
                        if judged["verdicts"][c.case_id].outcome == "approve")
        store.save_precedent(ruling(agreeing.case_id, outcome="approve"))
        items = queue(judged)
        assert items[0]["case_id"] == judged["case_id"], \
            "the one v2 contradicts is the one worth a human first"

    def test_the_queue_is_capped(self, judged):
        for case in judged["cases"][1:6]:
            store.save_precedent(ruling(case.case_id))
        assert len(queue(judged, limit=3)) == 3

    def test_nothing_stale_is_said_rather_than_left_blank(self, judged):
        assert "no ruling on file is stale" in diff.describe_readjudication([], "v2")


class TestTheArchive:
    def test_the_superseded_ruling_is_kept(self, judged):
        case_id = judged["case_id"]
        store.save_precedent(ruling(case_id, outcome="approve", by="ops.manager",
                                    version="v2", note="Clause 1.1 now exempts grade 3.",
                                    at=datetime(2026, 9, 13)))
        history = store.precedent_history("expenses", case_id)
        assert len(history) == 1
        assert history[0]["correct_outcome"] == "deny"
        assert history[0]["ruled_by"] == "finance.lead"
        assert history[0]["note"] == "No receipt.", \
            "the reason the first reviewer gave is the point of keeping it"

    def test_the_current_ruling_is_the_new_one(self, judged):
        case_id = judged["case_id"]
        store.save_precedent(ruling(case_id, outcome="approve", by="ops.manager",
                                    version="v2", at=datetime(2026, 9, 13)))
        current = {p.case_id: p for p in store.load_precedents("expenses")}[case_id]
        assert current.correct_outcome == "approve" and current.ruled_by == "ops.manager"

    def test_re_adjudicating_is_what_makes_a_ruling_fresh_again(self, judged):
        case_id = judged["case_id"]
        assert diff.stale_precedents(store.load_precedents("expenses"),
                                     judged["domain"], "v2")
        store.save_precedent(ruling(case_id, outcome="approve", by="ops.manager",
                                    version="v2", at=datetime(2026, 9, 13)))
        assert not diff.stale_precedents(store.load_precedents("expenses"),
                                         judged["domain"], "v2")

    def test_a_first_ruling_archives_nothing(self, judged):
        assert store.precedent_history("expenses", judged["cases"][9].case_id) == []

    def test_the_history_survives_a_re_seed(self, judged):
        """It is part of the one durable artefact, not an aggregate."""
        store.save_precedent(ruling(judged["case_id"], outcome="approve",
                                    at=datetime(2026, 9, 13)))
        store.clear_domain_results("expenses")
        assert store.precedent_history("expenses", judged["case_id"])
        assert "precedent_history" not in store.DERIVED_TABLES


class TestItIsVisible:
    def test_the_precedents_panel_says_a_ruling_was_revised(self, judged):
        case_id = judged["case_id"]
        store.save_precedent(ruling(case_id, outcome="approve", by="ops.manager",
                                    version="v2", at=datetime(2026, 9, 13)))
        rows = {r["case_id"]: r for r in report.precedents("expenses")}
        assert rows[case_id]["revisions"] == 1
        assert all(r["revisions"] == 0 for cid, r in rows.items() if cid != case_id)

    def test_the_history_read_model_says_which_answers_actually_changed(self, judged):
        store.save_precedent(ruling(judged["case_id"], outcome="approve",
                                    by="ops.manager", version="v2",
                                    at=datetime(2026, 9, 13)))
        store.save_precedent(ruling(judged["cases"][1].case_id))
        store.save_precedent(ruling(judged["cases"][1].case_id, by="ops.manager",
                                    version="v2", at=datetime(2026, 9, 13)))
        found = report.precedent_history("expenses")
        assert found["superseded"] == 2
        assert found["changed"] == 1, "one reviewer changed the answer"
        assert found["reconfirmed"] == 1, "the other confirmed it, which is a result too"
