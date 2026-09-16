"""The small refusals: things that were quietly doing the wrong thing.

None of these is about what the engine computes, which is why none of them was
caught by a feature test. They are all the same shape: a command that looked
like it had done what you asked, a table that grew forever, a header built out
of a filename, an evaluator that promised one exception type and raised another.
"""

from __future__ import annotations

import datetime
import pathlib

import pytest

from ptm import pit_check, proposal, safe_eval, seed, store
from ptm.models import Flip, Verdict

REPO = pathlib.Path(__file__).resolve().parents[1]
DAG_FILE = REPO / "dags" / "policy_time_machine.py"


class TestTheEvaluatorKeepsItsPromise:
    """``evaluate`` is documented as raising RuleError. It did not always."""

    def test_a_helper_refusing_its_argument_is_a_rule_error(self):
        """CPython caps integer-string conversion, so ``int('1' * 100000)``
        raises ValueError from inside a language this module claims to bound.
        ptm.judge swallowed it as 'does not match' while the lint, which only
        ever asks statically, reported the rule as sound."""
        with pytest.raises(safe_eval.RuleError) as exc:
            safe_eval.evaluate("int('1' * 100000)", {})
        assert "refuses" in str(exc.value)

    def test_a_rule_error_from_inside_is_not_re_wrapped(self):
        """The ceiling's own message has to survive, or the reason a rule was
        refused turns into 'a helper refused its argument'."""
        with pytest.raises(safe_eval.RuleError) as exc:
            safe_eval.evaluate("str(10 ** 60) * 100000", {})
        assert "past the ceiling" in str(exc.value)

    @pytest.mark.parametrize("expression", [
        "().__class__", "x[0]", "f'{x}'", "(lambda: 1)()", "[i for i in [1]]",
        "{1: 2}", "9 ** 9 ** 9", "((10 ** 64) ** 64) ** 64", "'x' * 10 ** 9",
        "__import__('os')", "open('x')", "max(range(10))",
    ])
    def test_the_language_is_still_the_language(self, expression):
        with pytest.raises(safe_eval.RuleError):
            safe_eval.evaluate(expression, {"x": 1})


class TestRetentionReachesTheTablesThatActuallyGrow:
    """``verdicts`` and ``flips`` are one row per case per run and every reader
    of both takes the newest row per case, so a superseded one is unreachable
    the moment the next run writes over it. They were the biggest tables here
    and nothing could drop them."""

    @pytest.fixture
    def replayed_four_times(self, fresh_db):
        for i in range(4):
            outcome = "deny" if i % 2 else "partial"
            flip = Flip(case_id="c1", decided_at=datetime.datetime(2024, 1, 1),
                        actual_outcome="approve", new_outcome=outcome, rationale="r",
                        confidence=0.9, policy_clause="1.1")
            store.save_replay(f"run{i}", "expenses", "v2", "actual", 1, [flip], 0.0,
                              {"c1": Verdict(outcome=outcome, rationale="r",
                                             confidence=0.9)})
        return fresh_db

    def test_superseded_rows_are_counted(self, replayed_four_times):
        preview = store.prune_preview(None, 0)
        assert preview["verdicts"] == 3 and preview["flips"] == 3

    def test_the_dry_run_counts_what_the_real_one_removes(self, replayed_four_times):
        """One definition, because a dry run that counts different rows from the
        one that deletes them is a promise about what is about to happen."""
        preview = store.prune_preview(None, 0)
        removed = store.prune(None, 0)
        assert {k: v for k, v in preview.items() if k != "cutoff"} == \
               {k: v for k, v in removed.items() if k != "cutoff"}

    def test_the_newest_verdict_and_flip_survive_at_any_age(self, replayed_four_times):
        store.prune(None, 0)
        assert [r["run_id"] for r in store.query("SELECT run_id FROM verdicts")] == ["run3"]
        assert [r["run_id"] for r in store.query("SELECT run_id FROM flips")] == ["run3"]

    def test_what_the_read_models_return_does_not_change(self, replayed_four_times):
        """The whole justification: pruning these is invisible because the rows
        were already unreachable."""
        before = store.latest_verdicts("expenses", "v2")
        store.prune(None, 0)
        assert store.latest_verdicts("expenses", "v2") == before

    def test_another_domain_is_untouched(self, replayed_four_times):
        removed = store.prune("refunds", 0)
        assert removed["verdicts"] == 0 and removed["flips"] == 0
        assert store.query("SELECT COUNT(*) n FROM verdicts")[0]["n"] == 4

    def test_precedents_are_still_never_pruned(self, replayed_four_times):
        """Age is not a reason to forget a human ruling."""
        assert "precedents" not in store.prune(None, 0)
        assert "precedent_history" not in store.prune(None, 0)


class TestDiscardCanOnlyDiscardADraft:
    def test_a_path_that_climbs_out_of_the_drafts_folder_is_refused(self, seeded):
        """``adopt`` refused anything that was not a draft so nobody could
        promote a policy over a policy. This built a path out of the argument
        and unlinked whatever was there."""
        with pytest.raises(LookupError, match="not a draft version name"):
            proposal.discard("expenses", "../../policies/expenses/v1")

    @pytest.mark.parametrize("bad", ["../v1", "a/b", "a\\b", "", "  ", ".", ".."])
    def test_nothing_that_is_not_one_path_segment(self, bad, seeded):
        with pytest.raises(LookupError):
            proposal.discard("expenses", bad)

    def test_the_policy_it_would_have_deleted_is_still_there(self, seeded):
        from ptm.config import INCLUDE_DIR

        try:
            proposal.discard("expenses", "../../policies/expenses/v1")
        except LookupError:
            pass
        assert (INCLUDE_DIR / "policies" / "expenses" / "v1.md").exists()

    def test_the_cli_reports_it_rather_than_raising(self, seeded, capsys):
        assert proposal.main(["expenses", "--discard", "../../policies/expenses/v1"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("ERROR") and "Traceback" not in err

    def test_a_real_draft_name_is_still_accepted(self, seeded):
        """The refusal is about shape, not about existence: a name that could be
        a draft returns an empty list when there is no such draft."""
        assert proposal.discard("expenses", "v2-draft99") == []


class TestHelpDoesNoWork:
    """``python -m ptm.seed --help`` seeded every domain. With ``--force`` it
    would have cleared every derived table for them too."""

    def test_seed_help_seeds_nothing(self, fresh_db, capsys):
        assert seed.main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out
        assert store.query("SELECT COUNT(*) n FROM cases")[0]["n"] == 0

    def test_seed_help_wins_over_force(self, fresh_db, capsys):
        assert seed.main(["--force", "--help"]) == 0
        assert store.query("SELECT COUNT(*) n FROM cases")[0]["n"] == 0

    def test_seed_still_seeds_without_it(self, fresh_db, capsys):
        assert seed.main(["expenses"]) == 0
        assert store.query("SELECT COUNT(*) n FROM cases")[0]["n"] > 0

    def test_seed_refuses_a_domain_it_has_no_fixture_for(self, fresh_db, capsys):
        assert seed.main(["nosuchdomain"]) == 2
        assert "no synthetic fixture" in capsys.readouterr().err

    def test_seed_refuses_an_option_it_does_not_have(self, fresh_db, capsys):
        assert seed.main(["--reset"]) == 2
        assert "unknown option" in capsys.readouterr().err

    def test_pit_check_help_runs_no_replay(self, seeded, capsys):
        assert pit_check.main(["--help"]) == 0
        out = capsys.readouterr().out
        assert "usage" in out and "naive replay" not in out

    def test_pit_check_can_be_pointed_at_the_other_domain(self, seeded, capsys):
        """It took the arguments all along; the CLI could not reach them."""
        assert pit_check.main(["refunds", "v2"]) == 0
        assert "naive replay" in capsys.readouterr().out

    def test_pit_check_refuses_an_unknown_domain_without_a_traceback(self, seeded, capsys):
        assert pit_check.main(["nosuchdomain"]) == 2
        err = capsys.readouterr().err
        assert err.startswith("ERROR") and "Traceback" not in err

    def test_pit_check_refuses_an_unknown_version(self, seeded, capsys):
        assert pit_check.main(["expenses", "v99"]) == 2
        assert "unknown policy version" in capsys.readouterr().err


class TestTheDagsThatCouldRunTwiceAtOnce:
    """Everything here writes to one SQLite file, and two of the five DAGs had
    no cap at all - including the one an asset re-fires on every adjudication."""

    def test_every_dag_declares_max_active_runs(self):
        source = DAG_FILE.read_text(encoding="utf-8")
        blocks = source.split("@dag(")[1:]
        missing = [block[:block.index(")")].split("dag_id=")[1].splitlines()[0]
                   for block in blocks if "max_active_runs" not in block[:block.index("def ")]]
        assert not missing, f"DAGs with no concurrency cap: {missing}"


class TestTheReviewerIsNamedAndTheClockIsNot:
    """``ruled_by`` is what makes a precedent a fact about a person."""

    def test_the_payload_key_is_the_one_airflow_sends(self):
        """It read ``user_id``; HITLOperator returns ``responded_by_user``, a
        HITLUser of id and name. Every precedent recorded by a real run was
        therefore attributed to 'unknown'."""
        source = DAG_FILE.read_text(encoding="utf-8")
        assert 'resp.get("user_id")' not in source
        assert '"responded_by_user"' in source

    def test_a_timed_out_review_does_not_become_precedent(self):
        """Airflow answers an expired HITL task with ``defaults``, which here is
        the most generous outcome. Writing that into the one durable artefact
        because nobody looked would be the worst failure this pipeline has."""
        source = DAG_FILE.read_text(encoding="utf-8")
        record = source[source.index("def record(flips: list[dict], responses"):]
        record = record[:record.index("store.mark_reviewed")]
        assert '"timedout"' in record
        assert "expired.append" in record
        assert record.index("expired.append") < record.index("store.save_precedent")

    def test_the_queue_carries_its_rails(self):
        source = DAG_FILE.read_text(encoding="utf-8")
        review = source[source.index("HITLOperator.partial("):]
        review = review[:review.index(".expand(")]
        for rail in ("assigned_users=", "response_timeout=", "notifiers="):
            assert rail in review

    def test_the_domain_can_set_them(self, expenses):
        assert expenses.review.assigned_users == []
        assert expenses.review.response_timeout_hours == 0.0
        assert expenses.review.notifiers == []

    def test_a_negative_timeout_is_refused_at_load(self):
        from ptm.config import ReviewPolicy

        with pytest.raises(ValueError, match="response_timeout_hours"):
            ReviewPolicy(response_timeout_hours=-1)
