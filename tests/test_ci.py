"""The workflow, checked the way everything else here is.

Several of this project's guarantees exist only as a CI step - the DAGs parsing
under a real Airflow, the plugin's routes, the dashboard rendering in a browser.
A step that is quietly misconfigured does not announce itself: it either passes
for the wrong reason or fails for one unrelated to the change that triggered it.

The specific failure this was written after: a new step was inserted between an
existing step's `run:` and its `env:`, so the environment silently moved to the
new step and the old one ran against the container's default paths. The fix was
one line; noticing took a red build.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

#: The one that is load-bearing for every engine step. Without it the engine
#: resolves /opt/airflow/include, which exists in the container and nowhere
#: else. PTM_OFFLINE is deliberately not required: it defaults to "1", so a step
#: that omits it still runs offline, and demanding it here would turn a real
#: check into one people silence.
ENGINE_ENV = {"PTM_INCLUDE_DIR"}


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.exists():  # pragma: no cover
        pytest.skip("no workflow file")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def steps(workflow: dict) -> list[tuple[str, dict]]:
    return [(job_name, step)
            for job_name, job in workflow["jobs"].items()
            for step in job.get("steps", [])]


class TestEveryStepThatRunsTheEngineIsPointedAtIt:
    def test_the_workflow_parses_and_has_the_jobs_it_claims(self, workflow):
        assert {"engine", "dags", "dashboard"} <= set(workflow["jobs"])

    def test_no_step_runs_ptm_without_its_environment(self, workflow):
        """A step missing it does not fall back to the repo - it fails with an
        empty domain list, or worse, silently reads a different fixture."""
        offenders = {}
        for job_name, step in steps(workflow):
            body = step.get("run") or ""
            if "python -m ptm." not in body and "-m ptm." not in body:
                continue
            missing = ENGINE_ENV - set(step.get("env") or {})
            if missing:
                offenders[f"{job_name}: {step.get('name', body.splitlines()[0])}"] = \
                    sorted(missing)
        assert not offenders, (
            f"these steps run the engine without telling it where the fixtures are: "
            f"{offenders}")

    def test_every_step_that_runs_pytest_outside_the_engine_job_carries_its_guard(
            self, workflow):
        """The DAG, plugin and browser suites skip themselves when their
        dependency is absent, and each is forced not to by a ``PTM_REQUIRE_*``
        variable. That only works if the step has an env block at all.

        Matched on *running* pytest, not on the word: ``pip install ... pytest``
        is an install line and has no business carrying an env block.
        """
        offenders = []
        for job_name, step in steps(workflow):
            body = step.get("run") or ""
            runs_pytest = any(line.strip().startswith(("pytest", "python -m pytest"))
                              for line in body.splitlines())
            if not runs_pytest or job_name == "engine":
                continue
            env = step.get("env") or {}
            if not any(k.startswith("PTM_REQUIRE_") for k in env):
                offenders.append(f"{job_name}: {step.get('name', 'unnamed')}")
        assert not offenders, (
            f"these run a suite that can skip itself, with nothing forcing it not to: "
            f"{offenders}")

    def test_the_jobs_that_can_skip_themselves_are_forced_not_to(self, workflow):
        """The DAG, plugin and browser tests skip when their dependency is
        missing. A job whose whole point is that dependency has to fail instead,
        or it reports success having run nothing."""
        required = set()
        for _, step in steps(workflow):
            required |= {k for k in (step.get("env") or {}) if k.startswith("PTM_REQUIRE_")}
        assert {"PTM_REQUIRE_AIRFLOW", "PTM_REQUIRE_PLUGIN", "PTM_REQUIRE_BROWSER"} <= required
