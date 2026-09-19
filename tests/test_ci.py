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
        assert {"engine", "dags", "docker", "dashboard"} <= set(workflow["jobs"])

    def test_no_step_runs_ptm_without_its_environment(self, workflow):
        """A step missing it does not fall back to the repo - it fails with an
        empty domain list, or worse, silently reads a different fixture."""
        offenders = {}
        for job_name, step in steps(workflow):
            body = step.get("run") or ""
            if "python -m ptm." not in body and "-m ptm." not in body:
                continue
            # A step that runs the engine *inside the container* takes its
            # environment from the image and the compose file, not from the
            # runner. Setting PTM_INCLUDE_DIR on a `docker compose exec` step
            # would point at a path on the host that the container cannot see -
            # so demanding one here would be demanding the wrong thing, loudly.
            # The container's own environment is asserted separately, below.
            if "docker compose exec" in body:
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

    def test_the_demo_stack_is_built_and_torn_down(self, workflow):
        """`docker compose up --build` is the first command in the README and
        was the one thing no job ran, so a broken Dockerfile or compose file
        shipped unnoticed. Asserted down to the teardown: a job that leaves the
        stack up wedges the port for whatever the runner does next.
        """
        body = " ".join(step.get("run") or ""
                        for step in workflow["jobs"]["docker"]["steps"])
        assert "docker compose up --build" in body
        assert "--wait" in body, (
            "without --wait the job races the healthcheck and asserts against an "
            "Airflow that has not finished migrating")
        assert "docker compose down" in body
        assert any(step.get("if") == "always()"
                   for step in workflow["jobs"]["docker"]["steps"]), \
            "the teardown has to run even when an assertion above it failed"

    def test_the_container_supplies_what_the_exec_steps_rely_on(self):
        """The other half of the exemption above.

        The docker job runs the engine through `docker compose exec` and does
        not pass it an environment, which is only correct as long as the image
        and the compose file still set one. If either stops, those steps read
        the container's default paths and the job goes green having checked a
        fixture that is not there - the exact failure this file was written
        after, one layer further in.
        """
        compose = yaml.safe_load(
            (REPO / "docker-compose.yaml").read_text(encoding="utf-8"))
        declared = compose["services"]["airflow"]["environment"]
        assert ENGINE_ENV <= set(declared)
        assert "PTM_DB" in declared

        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        for name in (*ENGINE_ENV, "PTM_DB"):
            assert name in dockerfile, (
                f"{name} is not set in the image, so anything running in a "
                f"container started from it without compose reads the wrong path")

    def test_the_coverage_floor_is_enforced_somewhere(self, workflow):
        """The floor lives in pyproject.toml, so the only thing CI has to get
        right is asking for coverage at all. A suite run without it reports
        success having measured nothing, which is the shape of failure this
        whole file exists to catch."""
        body = " ".join(step.get("run") or "" for _, step in steps(workflow))
        assert "--cov=ptm" in body

    def test_the_jobs_that_can_skip_themselves_are_forced_not_to(self, workflow):
        """The DAG, plugin and browser tests skip when their dependency is
        missing. A job whose whole point is that dependency has to fail instead,
        or it reports success having run nothing."""
        required = set()
        for _, step in steps(workflow):
            required |= {k for k in (step.get("env") or {}) if k.startswith("PTM_REQUIRE_")}
        assert {"PTM_REQUIRE_AIRFLOW", "PTM_REQUIRE_PLUGIN", "PTM_REQUIRE_BROWSER"} <= required
