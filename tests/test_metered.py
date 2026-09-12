"""Keeping the token counts the provider only logs.

``LLMOperator.execute`` calls ``agent.run_sync``, hands the result to
``log_run_summary`` - which reads ``result.usage`` - and then returns
``result.output`` alone. The real figures reach the task log and nothing else.

The operator subclass needs Airflow, so it is exercised in the DAG-parse job.
What is tested here is the part that is fragile and does not: reading usage off
a result object whose shape belongs to pydantic-ai, not to this project.
"""

from __future__ import annotations

import pytest

from ptm import cost
from ptm.metered import USAGE_KEY, _usage_dict


class Usage:
    def __init__(self, **fields):
        for key, value in fields.items():
            setattr(self, key, value)


class Result:
    def __init__(self, usage):
        self.usage = usage


class TestReadingUsageOffTheResult:
    def test_the_attribute_form(self):
        """What apache-airflow-providers-common-ai 0.8.0 reads."""
        result = Result(Usage(requests=1, input_tokens=900, output_tokens=120,
                              total_tokens=1020))
        assert _usage_dict(result) == {"requests": 1, "input_tokens": 900,
                                       "output_tokens": 120, "total_tokens": 1020}

    def test_the_callable_form(self):
        """Some pydantic-ai versions expose usage as a method instead. This runs
        inside a paid judge task, so the shape moving must cost the measurement
        and never the verdict."""
        usage = Usage(requests=2, input_tokens=10, output_tokens=5, total_tokens=15)
        assert _usage_dict(Result(lambda: usage))["requests"] == 2

    @pytest.mark.parametrize("result", [Result(None), Result(Usage()), object()])
    def test_anything_else_is_no_measurement_rather_than_an_error(self, result):
        assert _usage_dict(result) == {}

    def test_a_field_that_is_not_a_number_is_dropped(self):
        """A vendor reporting None for a count must not become a zero that
        reads as 'this call was free'."""
        assert "input_tokens" not in _usage_dict(
            Result(Usage(requests=1, input_tokens=None, output_tokens=5)))

    def test_the_xcom_key_is_not_the_return_value(self):
        """Downstream zips verdicts to cases positionally. Usage has to travel
        on its own key or every consumer has to unwrap a verdict."""
        assert USAGE_KEY and USAGE_KEY != "return_value"


class TestTheWrapping:
    """The operator needs Airflow, but the *mechanism* does not.

    It wraps the hook rather than re-implementing ``execute`` - the approval
    path, the output-serialisation rules and the deserialisation-walker handling
    belong to the provider and are exactly what a copy drifts away from. What
    that costs is a dependency on a shape: a ``cached_property`` called
    ``llm_hook``, a ``create_agent`` that returns something with ``run_sync``.
    This stands that shape up and proves the wrapper still lets it through.
    """

    @pytest.fixture
    def operator_class(self):
        import pathlib
        from functools import cached_property

        class FakeAgent:
            def run_sync(self, prompt, usage_limits=None):
                class Response:
                    output = {"outcome": "approve"}
                    usage = Usage(requests=1, input_tokens=900, output_tokens=120,
                                  total_tokens=1020)
                return Response()

        class FakeHook:
            def create_agent(self, output_type=None, instructions=None, **kwargs):
                return FakeAgent()

            def anything_else(self):
                return "passthrough"

        class FakeLLMOperator:
            @cached_property
            def llm_hook(self):
                return FakeHook()

            def execute(self, context):
                agent = self.llm_hook.create_agent(output_type=dict, instructions="x")
                return agent.run_sync("prompt").output

        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "ptm" / "metered.py").read_text(encoding="utf-8")
        source = source.replace(
            "from airflow.providers.common.ai.operators.llm import LLMOperator", "")
        namespace = {"LLMOperator": FakeLLMOperator}
        exec(compile(source, "ptm/metered.py", "exec"), namespace)  # noqa: S102
        return namespace["metered_operator"]()

    def _run(self, operator_class):
        import types

        pushed = {}

        class TI:
            def xcom_push(self, key, value):
                pushed[key] = value

        operator = operator_class.__new__(operator_class)
        operator._ptm_hook = None
        operator.log = types.SimpleNamespace(info=lambda *a, **k: None)
        return operator, operator.execute({"ti": TI()}), pushed

    def test_the_return_value_is_untouched(self, operator_class):
        """Downstream zips this against a case. A wrapper object in its place
        would make every consumer unwrap it."""
        _, output, _ = self._run(operator_class)
        assert output == {"outcome": "approve"}

    def test_the_usage_lands_on_its_own_key(self, operator_class):
        _, _, pushed = self._run(operator_class)
        assert pushed[USAGE_KEY] == {"requests": 1, "input_tokens": 900,
                                     "output_tokens": 120, "total_tokens": 1020}

    def test_the_rest_of_the_hook_still_works(self, operator_class):
        """It stands in for the hook, so everything it does not care about has
        to reach the real one untouched."""
        operator, _, _ = self._run(operator_class)
        assert operator.llm_hook.anything_else() == "passthrough"


class TestPricingWhatWasMeasured:
    def test_measured_calls_and_billed_requests_are_different_numbers(self):
        """One prompt can be two billed requests. An estimate built from
        'one prompt, one request' misses the retry."""
        priced = cost.from_usage(
            [{"requests": 3, "input_tokens": 100, "output_tokens": 50}],
            "anthropic:claude-sonnet-5")
        assert priced["actual_requests"] == 3
        assert priced["measured_calls"] == 1

    def test_it_prices_with_the_same_table_as_the_estimate(self):
        """Otherwise the gap between them would be a pricing difference rather
        than a token-counting one, and the whole comparison would mean nothing."""
        model = "anthropic:claude-sonnet-5"
        in_price, out_price = cost.price_for(model)
        priced = cost.from_usage(
            [{"requests": 1, "input_tokens": 1_000_000, "output_tokens": 0}], model)
        assert priced["actual_cost_usd"] == round(in_price, 4)
        assert out_price  # the output half is exercised by the reconcile tests

    def test_nothing_measured_prices_to_nothing(self):
        priced = cost.from_usage([], "anthropic:claude-sonnet-5")
        assert priced["actual_cost_usd"] == 0.0 and priced["actual_requests"] == 0
