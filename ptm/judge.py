"""Prompt construction, and an offline stand-in judge.

The real judge is the Common AI provider's ``LLMOperator``. The offline judge
exists so the whole project runs, end to end, with no API key and no network -
useful for CI, for rehearsing the demo, and for the moment the conference wifi
gives up.

Its conditions are computed by :mod:`ptm.safe_eval` rather than by ``eval``.
That is not paranoia about the shipped YAML: ``propose_<domain>`` has a model
write ``offline_rules`` for a drafted policy, and every replay of that draft
then evaluates them on a worker.
"""

from __future__ import annotations

from . import safe_eval
from .config import DomainConfig
from .models import Case, Verdict

SYSTEM_PROMPT = (
    "You are a policy adjudicator. You apply written policy to historical cases "
    "exactly as written, without sympathy, precedent or hindsight. You are shown "
    "each case as it was recorded on the day it was decided; you must not reason "
    "about anything that happened after that date. When the policy does not "
    "settle a case, you say so with low confidence rather than inventing a rule."
)

PROMPT = """You are adjudicating a historical {label} case under a proposed policy.

# The policy (version {version})
{policy}

# The case, as it was known on {decided_at}
{case}

# Your task
Decide the correct outcome under the policy above. The allowed outcomes are
exactly: {outcomes}.

Rules you must follow:
- Judge ONLY on the information in the case above. You are deliberately being
  shown the record as it stood on the day the case was decided. Do not
  speculate about anything that happened afterwards.
- Cite the specific clause that decides the case.
- If the policy genuinely does not settle this case, say so by returning a low
  confidence rather than by inventing a rule.
{extra}"""


def build_prompt(case: Case, domain: DomainConfig, version: str) -> str:
    return PROMPT.format(
        label=domain.label,
        version=version,
        policy=domain.policy_text(version),
        decided_at=case.decided_at.date().isoformat(),
        case=domain.render_case(case.payload),
        outcomes=", ".join(domain.outcomes),
        extra=domain.judge_instructions,
    )


#: Re-exported from :mod:`ptm.safe_eval`, which owns both the helper list and
#: the evaluator that may call them. One definition, because a lint that warns
#: about shadowing a helper the evaluator does not actually have is worse than
#: no warning at all.
SAFE_BUILTINS = safe_eval.SAFE_BUILTINS
SAFE_NAMES = safe_eval.SAFE_NAMES


#: The rationale a defaulted verdict carries. Named rather than inlined because
#: :mod:`ptm.rules` has to tell "no rule matched" apart from "a rule chose the
#: most generous outcome", and the two are otherwise identical on the wire.
NO_RULE_RATIONALE = "No rule matched; default outcome."


def offline_verdict(case: Case, domain: DomainConfig, version: str) -> Verdict:
    """Deterministic rule evaluation, used when PTM_OFFLINE=1.

    Rules come from the domain YAML's ``offline_rules`` block; the first
    matching rule wins, otherwise the domain's first (most generous) outcome.

    Conditions are computed by :mod:`ptm.safe_eval`, which walks the parsed
    expression rather than calling ``eval``. That matters because these rules
    are no longer only ever hand-written: :mod:`ptm.proposal` has a model write
    them for a drafted policy, and they are then evaluated on a worker like any
    other version's.
    """
    rules = domain.offline_rules.get(version, [])
    scope = case_scope(case)
    for rule in rules:
        try:
            matched = safe_eval.evaluate(rule["when"], scope)
        except safe_eval.RuleError:
            # A rule the evaluator refuses, or one reading a field this case
            # lacks. Both mean "does not match"; ptm.lint and ptm.rules.validate
            # are where a refusal is reported rather than swallowed.
            continue
        except Exception:  # arithmetic on a field of the wrong type, etc.
            continue
        if matched:
            return Verdict(
                outcome=domain.validate_outcome(rule["outcome"]),
                rationale=rule.get("because", "Matched offline rule."),
                confidence=float(rule.get("confidence", 0.9)),
                policy_clause=str(rule.get("clause", "")),
            )
    return Verdict(outcome=domain.validate_outcome(domain.outcomes[0]),
                   rationale=NO_RULE_RATIONALE, confidence=0.6)


def case_scope(case: Case) -> dict:
    """The names a rule sees when this case is judged.

    Public, and shared with :func:`ptm.lint.probe`, because a probe that
    evaluated rules against a scope built any other way would be answering a
    question about its own arithmetic. The coercion below is part of the
    contract - a payload carrying ``"500"`` is a rule's ``500`` - so a probe
    that skipped it would report working rules as broken and, worse, miss the
    quoted-threshold mistake it exists to catch.
    """
    return {k: _coerce(v) for k, v in case.payload.items()}


def _coerce(v):
    if isinstance(v, str):
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return v
    return v
