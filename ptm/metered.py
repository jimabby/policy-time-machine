"""An ``LLMOperator`` that also reports what the call actually cost.

:mod:`ptm.cost` is honest about being an estimate: it counts the characters in
the prompts this project built and divides by four. That is the right precision
for *"can I afford this backfill"* and the wrong precision for anything else,
and until now there was nothing to check it against - so the constant four was
a number nobody could be wrong about.

The provider already has the real figures. ``LLMOperator.execute`` calls
``agent.run_sync`` and hands the result to ``log_run_summary``, which reads
``result.usage`` - requests, input tokens, output tokens, as the vendor counted
them. It then returns ``result.output`` alone, so those numbers reach the task
log and nothing else.

This is the smallest wrapper that keeps them. It does **not** re-implement
``execute``: the approval path, the output-serialisation rules and the
deserialisation-walker handling all belong to the provider and are exactly the
kind of thing a copy drifts away from. Instead it wraps the *hook*, so the
operator builds and runs its agent exactly as it always did and the wrapper
sees the result on the way past.

The measured numbers never replace the estimate. Both are recorded, because the
gap between them is the point: it prices a future backfill better than either
does alone, and it is the only thing that can tell you the four in
:data:`ptm.cost.CHARS_PER_TOKEN` has been wrong for your policies all along.
"""

from __future__ import annotations

import inspect
from typing import Any

#: XCom key the per-call usage lands under. Separate from the return value, so
#: the verdict a downstream task zips positionally stays exactly what it was -
#: a wrapper object in its place would have every consumer unwrap it.
USAGE_KEY = "ptm_usage"


def _usage_dict(result: Any) -> dict:
    """``result.usage`` as plain JSON, tolerating a provider that renames it.

    Defensive on purpose: this runs inside a paid judge task, and an attribute
    that moved in a provider upgrade must cost the *measurement*, never the
    verdict the run is actually for.
    """
    usage = getattr(result, "usage", None)
    if callable(usage):  # some pydantic-ai versions expose it as a method
        try:
            usage = usage()
        except Exception:
            return {}
    if usage is None:
        return {}
    out = {}
    for field in ("requests", "input_tokens", "output_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if isinstance(value, (int, float)):
            out[field] = int(value)
    return out


def metered_operator():
    """Build the operator class. Imported lazily so ``ptm`` stays Airflow-free.

    Every other module here runs with no Airflow installed - that is what lets
    the whole engine be tested and demoed without it - so the import lives
    inside the call rather than at module scope.
    """
    from airflow.providers.common.ai.operators.llm import LLMOperator

    def _underlying_hook_builder():
        """The provider's own ``llm_hook`` body, whatever descriptor wraps it.

        The hook has to be built by the provider's code and then wrapped, which
        means reaching past the descriptor to the function underneath. That used
        to be spelled ``LLMOperator.llm_hook.func(self)`` - correct for the
        ``cached_property`` the provider uses today and an ``AttributeError`` on
        every judge task the day it becomes a plain ``property``, which is a
        refactor no provider would consider breaking.

        ``requirements.txt`` pins a floor and no ceiling, deliberately, so the
        pin cannot be what protects this. Both descriptor shapes are handled -
        ``cached_property`` exposes ``.func`` and ``property`` exposes ``.fget`` -
        and anything else returns None, which :meth:`llm_hook` below turns into
        an unmetered run rather than a failed one.

        ``getattr_static`` rather than ``getattr``: fetching ``llm_hook`` off the
        class normally would invoke the descriptor rather than hand it over.
        """
        descriptor = inspect.getattr_static(LLMOperator, "llm_hook", None)
        return getattr(descriptor, "func", None) or getattr(descriptor, "fget", None)

    class _Recorder:
        """Stands in for the hook, and watches the agent it hands back."""

        def __init__(self, hook, sink: list):
            self._hook = hook
            self._sink = sink

        def __getattr__(self, name):  # everything else is the real hook's
            return getattr(self._hook, name)

        def create_agent(self, *args, **kwargs):
            agent = self._hook.create_agent(*args, **kwargs)
            sink = self._sink
            run_sync = agent.run_sync

            def recording_run_sync(*a, **kw):
                result = run_sync(*a, **kw)
                sink.append(_usage_dict(result))
                return result

            agent.run_sync = recording_run_sync
            return agent

    class MeteredLLMOperator(LLMOperator):
        """``LLMOperator``, plus the vendor's own token counts on a second XCom key.

        The return value is unchanged - downstream tasks zip verdicts to cases
        positionally and must keep seeing a verdict. Usage is pushed under
        :data:`USAGE_KEY`, which a mapped fan-out collects with a single
        ``xcom_pull(task_ids=..., key=USAGE_KEY)``.
        """

        @property
        def llm_hook(self):
            # Shadows the provider's cached_property. Wrapping the hook rather
            # than copying execute() is what keeps this correct across a
            # provider upgrade: the operator still builds and runs its own
            # agent, and this only watches the result go past.
            if getattr(self, "_ptm_hook", None) is None:
                self._ptm_usage: list[dict] = []
                build = _underlying_hook_builder()
                if build is None:
                    # The measurement is the expendable half. A provider that has
                    # reshaped llm_hook must cost the token counts and nothing
                    # else, so fall back to the inherited hook and judge exactly
                    # as an unmetered LLMOperator would. Said out loud, because a
                    # ledger quietly missing its actual_* columns reads as a run
                    # nobody metered rather than as one that could not be.
                    self.log.warning(
                        "ptm: cannot reach the provider's llm_hook to meter it, so this "
                        "task reports no token usage. The verdicts are unaffected; the "
                        "cost ledger keeps its estimate and no actual_* figures.")
                    self._ptm_hook = super().llm_hook
                else:
                    self._ptm_hook = _Recorder(build(self), self._ptm_usage)
            return self._ptm_hook

        def execute(self, context):
            self._ptm_hook = None
            output = super().execute(context)
            measured = [row for row in getattr(self, "_ptm_usage", []) if row]
            if measured:
                total = {
                    field: sum(int(row.get(field) or 0) for row in measured)
                    for field in ("requests", "input_tokens", "output_tokens", "total_tokens")
                }
                # requests is what the vendor charged for, which is not always
                # one: a retry or a tool round trip is a second billed call, and
                # an estimate built from "one prompt, one request" misses it.
                total["requests"] = total["requests"] or len(measured)
                context["ti"].xcom_push(key=USAGE_KEY, value=total)
                self.log.info("ptm: measured %s in / %s out tokens over %s request(s)",
                              total["input_tokens"], total["output_tokens"], total["requests"])
            return output

    return MeteredLLMOperator
