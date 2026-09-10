"""Prompt construction, and an offline stand-in judge.

The real judge is the Common AI provider's ``LLMOperator``. The offline judge
exists so the whole project runs, end to end, with no API key and no network -
useful for CI, for rehearsing the demo, and for the moment the conference wifi
gives up.
"""

from __future__ import annotations

from .config import DomainConfig
from .models import Case, Verdict

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


_SAFE = {"__builtins__": {}, "abs": abs, "len": len, "min": min, "max": max, "float": float, "int": int, "str": str}


def offline_verdict(case: Case, domain: DomainConfig, version: str) -> Verdict:
    """Deterministic rule evaluation, used when PTM_OFFLINE=1.

    Rules come from the domain YAML's ``offline_rules`` block; the first
    matching rule wins, otherwise the domain's first (most generous) outcome.
    Expressions are evaluated with no builtins - this is a local demo fixture,
    not a sandbox, so only ever point it at YAML you wrote yourself.
    """
    rules = domain.offline_rules.get(version, [])
    scope = dict(_SAFE)
    scope.update({k: _coerce(v) for k, v in case.payload.items()})
    for rule in rules:
        try:
            if eval(rule["when"], scope):  # noqa: S307 - local fixture, see docstring
                return Verdict(
                    outcome=domain.validate_outcome(rule["outcome"]),
                    rationale=rule.get("because", "Matched offline rule."),
                    confidence=float(rule.get("confidence", 0.9)),
                    policy_clause=str(rule.get("clause", "")),
                )
        except Exception:  # a rule referencing a field this case lacks simply does not match
            continue
    return Verdict(outcome=domain.validate_outcome(domain.outcomes[0]), rationale="No rule matched; default outcome.", confidence=0.6)


def _coerce(v):
    if isinstance(v, str):
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            return v
    return v
