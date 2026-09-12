"""Build the DAG module against stubbed Airflow, in both judge modes.

This is NOT an Airflow parse - it cannot tell you that ``LLMOperator`` accepts
these keyword arguments, and only `make up` can. What it does catch is the
class of mistake that is otherwise invisible here: a name that only exists in
one branch, a task defined but never wired, a helper renamed in one place.
The ``PTM_OFFLINE=0`` path has no other coverage at all.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FakeArg:
    """Stands in for an XComArg / mapped task output."""

    def __init__(self, name):
        self.name = name

    @property
    def output(self):
        return self

    def __rshift__(self, other):
        RECORDED["deps"].append((self.name, getattr(other, "name", other)))
        return other

    def __getitem__(self, i):
        return self


RECORDED = {"dags": [], "tasks": [], "operators": [], "deps": []}


def _install_airflow_stubs():
    RECORDED.update(dags=[], tasks=[], operators=[], deps=[])

    def dag(**kwargs):
        def wrap(fn):
            def run():
                RECORDED["dags"].append(kwargs)
                return fn()
            return run
        return wrap

    def task(_fn=None, **_kw):
        def wrap(fn):
            def call(*a, **k):
                RECORDED["tasks"].append(fn.__name__)
                return FakeArg(fn.__name__)
            call.expand = lambda **k: FakeArg(fn.__name__)
            return call
        return wrap(_fn) if callable(_fn) else wrap

    class FakeOperator:
        @classmethod
        def partial(cls, **kwargs):
            RECORDED["operators"].append({"cls": cls.__name__, **kwargs})
            holder = types.SimpleNamespace()
            holder.expand = lambda **k: FakeArg(kwargs.get("task_id", "op"))
            return holder

    sdk = types.ModuleType("airflow.sdk")
    sdk.Asset = lambda uri: {"uri": uri}
    sdk.Param = lambda default=None, **kw: default
    sdk.dag = dag
    sdk.task = task

    exc = types.ModuleType("airflow.exceptions")
    exc.AirflowFailException = type("AirflowFailException", (Exception,), {})

    airflow = types.ModuleType("airflow")
    airflow.sdk, airflow.exceptions = sdk, exc

    mods = {"airflow": airflow, "airflow.sdk": sdk, "airflow.exceptions": exc}
    for path, attr in [
        ("airflow.providers.common.ai.operators.llm", "LLMOperator"),
        ("airflow.providers.standard.operators.hitl", "HITLOperator"),
    ]:
        m = types.ModuleType(path)
        setattr(m, attr, type(attr, (FakeOperator,), {}))
        mods[path] = m
        # Parent packages must exist for the import machinery.
        parts = path.split(".")
        for i in range(1, len(parts)):
            mods.setdefault(".".join(parts[:i]), types.ModuleType(".".join(parts[:i])))

    usage = types.ModuleType("pydantic_ai.usage")
    usage.UsageLimits = lambda **kw: kw
    mods["pydantic_ai"] = types.ModuleType("pydantic_ai")
    mods["pydantic_ai.usage"] = usage

    pendulum = types.ModuleType("pendulum")
    from datetime import datetime, timezone
    pendulum.datetime = lambda *a, **kw: datetime(*a, tzinfo=timezone.utc)
    pendulum.now = lambda tz=None: datetime.now(timezone.utc)
    pendulum.parse = datetime.fromisoformat
    mods["pendulum"] = pendulum

    for name, mod in mods.items():
        sys.modules[name] = mod


def build_dags(offline: bool):
    _install_airflow_stubs()
    import ptm.config
    monkey = ptm.config.OFFLINE
    ptm.config.OFFLINE = offline
    try:
        sys.modules.pop("dags.policy_time_machine", None)
        sys.modules.pop("policy_time_machine", None)
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "dags"))
        # import_module runs the module body; a reload would run it a second
        # time and double every recorded DAG.
        importlib.import_module("policy_time_machine")
        return dict(RECORDED)
    finally:
        ptm.config.OFFLINE = monkey
        for name in list(sys.modules):
            if name.startswith(("airflow", "pydantic_ai", "pendulum")):
                del sys.modules[name]
        sys.modules.pop("policy_time_machine", None)


@pytest.mark.parametrize("offline", [True, False])
def test_the_module_builds_four_dags_per_domain(offline):
    rec = build_dags(offline)
    ids = [d["dag_id"] for d in rec["dags"]]
    for domain in ("expenses", "refunds"):
        assert f"replay_{domain}" in ids
        assert f"adjudicate_{domain}" in ids
        assert f"precedent_gate_{domain}" in ids
        assert f"amend_{domain}" in ids
    assert len(ids) == len(set(ids)), "dag_ids must be unique"


@pytest.mark.parametrize("offline", [True, False])
def test_the_asset_chain_is_unbroken(offline):
    """Each stage must be woken by the one before it, with nothing polling."""
    rec = build_dags(offline)
    by_id = {d["dag_id"]: d for d in rec["dags"]}
    for domain in ("expenses", "refunds"):
        assert by_id[f"replay_{domain}"]["schedule"] == "@monthly"
        for dag_id, uri in [
            (f"adjudicate_{domain}", f"ptm://{domain}/flips"),
            (f"precedent_gate_{domain}", f"ptm://{domain}/precedents"),
            (f"amend_{domain}", f"ptm://{domain}/amendments"),
        ]:
            schedule = by_id[dag_id]["schedule"]
            assert isinstance(schedule, list) and schedule[0]["uri"] == uri, \
                f"{dag_id} is not triggered by {uri}"


@pytest.mark.parametrize("offline", [True, False])
def test_the_gate_registers_a_candidate_before_it_fails(offline):
    """A drafted fix must be materialised and published, not just logged.

    If record_amendment ran after enforce, the gate's failure would stop it and
    the amendment would never reach the DAG that tests it.
    """
    rec = build_dags(offline)
    assert ("record_amendment", "enforce") in rec["deps"]


@pytest.mark.parametrize("offline", [True, False])
def test_the_analysis_tasks_are_wired_in_both_modes(offline):
    rec = build_dags(offline)
    assert "save_analysis" in rec["tasks"], "the brief must be persisted"
    if offline:
        assert "analyse_offline" in rec["tasks"]
        assert "analysis_prompts" not in rec["tasks"], \
            "offline must not build prompts nothing consumes"
    else:
        assert "analysis_prompts" in rec["tasks"]
        assert {"brief", "themes"} <= {o["task_id"] for o in rec["operators"]}


def test_the_llm_path_caps_spend_on_every_model_call():
    """A runaway judge on a 600-case backfill is a real bill."""
    rec = build_dags(offline=False)
    for op in rec["operators"]:
        if op["cls"] == "LLMOperator":
            assert op.get("usage_limits"), f"{op['task_id']} has no usage_limits"
            assert op.get("output_type"), f"{op['task_id']} is not typed"


def test_the_judge_is_the_only_concurrency_capped_operator():
    rec = build_dags(offline=False)
    judge = [o for o in rec["operators"] if o.get("task_id") == "judge"]
    assert judge and judge[0].get("max_active_tis_per_dag") == 8, \
        "the fan-out judge must not melt the model endpoint"


def test_the_reviewer_can_record_why(offline=True):
    """Regression: the note was read back but never collected."""
    rec = build_dags(offline)
    hitl = [o for o in rec["operators"] if o["cls"] == "HITLOperator"]
    assert hitl, "adjudication must use HITL"
    for op in hitl:
        assert "note" in (op.get("params") or {}), "no way for the reviewer to say why"
        assert op["options"], "the reviewer picks the correct outcome, not yes/no"


@pytest.mark.parametrize("offline", [True, False])
def test_publish_waits_for_the_analysis(offline):
    rec = build_dags(offline)
    assert ("save_analysis", "publish") in rec["deps"]
