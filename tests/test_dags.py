"""The DAG module: it must import cleanly and build what it claims to build.

Airflow is a heavy dependency, so the import-dependent checks skip when it is
absent and the structural ones run either way. CI installs Airflow, which is
what backs the README's claim of zero import errors.
"""

from __future__ import annotations

import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DAG_FILE = REPO / "dags" / "policy_time_machine.py"

has_airflow = True
try:  # pragma: no cover - depends on the environment
    import airflow  # noqa: F401
except ImportError:  # pragma: no cover
    has_airflow = False

needs_airflow = pytest.mark.skipif(not has_airflow, reason="Airflow is not installed")


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

    def test_every_task_belongs_to_a_domain_tagged_dag(self, dagbag):
        for dag_id, dag in dagbag.dags.items():
            assert "policy-time-machine" in dag.tags
            assert dag.tasks, f"{dag_id} has no tasks"
