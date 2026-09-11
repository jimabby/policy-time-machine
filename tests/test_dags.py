"""The DAG module: it must import cleanly and build what it claims to build.

Airflow is a heavy dependency, so the import-dependent checks skip when it is
absent and the structural ones run either way. CI installs Airflow, which is
what backs the README's claim of zero import errors.
"""

from __future__ import annotations

import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DAG_FILE = REPO / "dags" / "policy_time_machine.py"

#: Why Airflow could not be imported, if it could not. Kept rather than
#: discarded: a bare "not installed" skip is what let the CI job whose entire
#: purpose is running these tests report success without ever running one.
airflow_import_error: str | None = None
try:  # pragma: no cover - depends on the environment
    import airflow  # noqa: F401
except Exception as exc:  # pragma: no cover - any failure means we cannot parse
    airflow_import_error = f"{type(exc).__name__}: {exc}"

has_airflow = airflow_import_error is None

# Locally, Airflow is a heavy optional dependency and skipping is right. In the
# job that installs it on purpose, a skip is a false pass - so CI sets this and
# the skip becomes a failure that says why.
if not has_airflow and os.environ.get("PTM_REQUIRE_AIRFLOW") == "1":  # pragma: no cover
    raise RuntimeError(
        "PTM_REQUIRE_AIRFLOW=1 but Airflow could not be imported, so the DAG-parse "
        f"tests would have silently skipped. Import failed with: {airflow_import_error}"
    )

needs_airflow = pytest.mark.skipif(
    not has_airflow, reason=f"Airflow could not be imported ({airflow_import_error})")


class TestStatic:
    """Runs with or without Airflow installed."""

    def test_the_module_compiles(self):
        import py_compile

        py_compile.compile(str(DAG_FILE), doraise=True)

    def test_no_domain_knowledge_leaks_into_the_dag_layer(self):
        """The engine's central claim: swapping the YAML swaps the application.

        A domain word hard-coded here would mean the pipeline only looks generic.
        """
        source = DAG_FILE.read_text(encoding="utf-8").lower()
        for word in ("expense", "refund", "receipt", "gbp", "grade", "tier", "claim"):
            assert word not in source, f"{word!r} is domain knowledge, it belongs in the YAML"

    def test_prompts_are_not_carried_in_the_case_payload(self):
        """Every prompt embeds the whole policy text. Putting one in the dict that
        fans out to each judge task duplicates the policy per case, through XCom,
        for a value the offline judge never reads."""
        source = DAG_FILE.read_text(encoding="utf-8")
        assert '"prompt": build_prompt' not in source
        assert '"prompt_chars"' in source

    def test_human_answers_are_never_zipped_against_a_short_list(self):
        """``record`` runs on all_done so a failed review does not strand the
        others - but a short response list zipped positionally against the full
        flip list would file one reviewer's ruling against somebody else's
        case. Precedent cannot be recomputed, so it has to refuse."""
        source = DAG_FILE.read_text(encoding="utf-8")
        assert "if len(responses) != len(flips):" in source
        assert "attribute a ruling to the wrong case" in source

    def test_the_gate_loads_precedent_cases_by_id(self):
        """Loading everything and filtering is subject to the default limit, so
        past that many cases the gate would check a subset and still pass."""
        source = DAG_FILE.read_text(encoding="utf-8")
        assert "case_ids=ids" in source
        assert "if c.case_id in ids" not in source, "the filtered load is the truncating one"

    def test_per_case_segments_are_persisted(self):
        """Summing the per-run aggregates counts a case once per run that saw
        it, and manual runs overlap backfills on purpose."""
        assert "case_segments=diff.case_segment_rows" in DAG_FILE.read_text(encoding="utf-8")


@needs_airflow
class TestParses:
    @pytest.fixture(scope="class")
    def dagbag(self):
        from airflow.models.dagbag import DagBag

        bag = DagBag(dag_folder=str(REPO / "dags"), include_examples=False)
        assert not bag.import_errors, bag.import_errors
        return bag

    def test_four_dags_per_domain(self, dagbag, seeded):
        from ptm.config import available_domains

        for name in available_domains():
            for prefix in ("replay", "adjudicate", "precedent_gate", "judge_stability"):
                assert f"{prefix}_{name}" in dagbag.dags

    def test_replay_is_schedulable_and_backfillable(self, dagbag):
        dag = dagbag.dags["replay_expenses"]
        assert dag.schedule_interval == "@monthly"
        assert {"policy_version", "max_cases", "baseline_version"} <= set(dag.params)

    def test_adjudication_wakes_on_the_flips_asset(self, dagbag):
        dag = dagbag.dags["adjudicate_expenses"]
        assert "ptm://expenses/flips" in str(dag.timetable.summary)

    def test_the_gate_wakes_on_the_precedents_asset(self, dagbag):
        dag = dagbag.dags["precedent_gate_expenses"]
        assert "ptm://expenses/precedents" in str(dag.timetable.summary)

    def test_stability_is_manual_only(self, dagbag):
        """It costs real money per run, so it must not fire on a schedule."""
        assert dagbag.dags["judge_stability_expenses"].schedule_interval is None

    def test_stability_requires_at_least_two_samples(self, dagbag):
        params = dagbag.dags["judge_stability_expenses"].params
        assert params["samples_per_case"].schema.get("minimum") == 2

    def test_stability_can_target_the_recorded_flips(self, dagbag):
        """The aggregate noise floor and per-flip confirmation are two
        questions sharing one fan-out."""
        params = dagbag.dags["judge_stability_expenses"].params
        assert params["target"].schema.get("enum") == ["sample", "flips"]

    def test_the_gate_judges_the_policy_in_force_as_well(self, dagbag):
        """So a reversal the status quo already makes is not reported as the
        proposal's doing."""
        dag = dagbag.dags["precedent_gate_expenses"]
        assert "baseline_version" in dag.params
        assert any("baseline" in t.task_id for t in dag.tasks), \
            [t.task_id for t in dag.tasks]

    def test_adjudication_reports_what_it_held_back(self, dagbag):
        """A flip silently dropped from the queue looks exactly like a flip
        that never happened."""
        dag = dagbag.dags["adjudicate_expenses"]
        assert "unconfirmed" in {t.task_id for t in dag.tasks}

    def test_every_task_belongs_to_a_domain_tagged_dag(self, dagbag):
        for dag_id, dag in dagbag.dags.items():
            assert "policy-time-machine" in dag.tags
            assert dag.tasks, f"{dag_id} has no tasks"
