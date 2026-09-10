"""Domain-agnostic data model for the Policy Time Machine.

Nothing in this module knows what an expense, a refund or a claim is. A
"case" is an opaque JSON payload plus the outcome a human actually reached.
The domain lives entirely in include/domains/*.yaml.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Case(BaseModel):
    """One real historical decision."""

    case_id: str
    domain: str
    decided_at: datetime
    payload: dict[str, Any]
    actual_outcome: str
    actual_rationale: str = ""


class Verdict(BaseModel):
    """What the judge thinks should have happened under a given policy.

    This is the ``response_model`` handed to the Common AI provider, so the
    field descriptions are load-bearing - they are what the model sees.
    """

    outcome: str = Field(description="The verdict. Must be exactly one of the allowed outcomes for this domain.")
    rationale: str = Field(description="Two or three sentences citing the specific clause of the policy that decides this case.")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0 if the policy is genuinely ambiguous for this case, 1.0 if the policy decides it outright.")
    policy_clause: str = Field(default="", description="The identifier of the clause relied on, e.g. '3.2'. Empty if no single clause applies.")


class Flip(BaseModel):
    """A case where the proposed policy disagrees with history."""

    case_id: str
    decided_at: datetime
    actual_outcome: str
    new_outcome: str
    rationale: str
    confidence: float
    policy_clause: str
    impact: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["loosening", "tightening", "lateral"] = "lateral"
    #: Segment values as of the decision date, e.g. ``{"category": "travel",
    #: "grade": "2"}``. Captured here because point-in-time facts are not part
    #: of the case record and must never be re-derived from today's data.
    segments: dict[str, str] = Field(default_factory=dict)
    #: What the policy *currently in force* gives for this case, when a baseline
    #: pass ran. Empty string means no baseline was judged.
    baseline_outcome: str = ""
    #: Why this case moved, in one bucket. See :func:`ptm.diff.attribute`.
    attribution: str = ""


class StabilityReport(BaseModel):
    """How much of a measured flip rate is the judge being inconsistent.

    A deterministic judge scores zero here. A real model does not, and a flip
    rate quoted without this number is quoted without an error bar.
    """

    cases_sampled: int
    samples_per_case: int
    #: Cases where repeated judging of the *same* case disagreed with itself.
    unstable_cases: int
    #: unstable_cases / cases_sampled - the share of the flip rate that is noise.
    disagreement_rate: float
    #: Per-case detail, worst first, so an unstable case can be inspected.
    unstable: list[dict[str, Any]] = Field(default_factory=list)


class PrecedentConflict(BaseModel):
    """Two human rulings that cannot both be right.

    Precedent is the only durable output of this system, so it is also the only
    thing that can quietly rot. Two reviewers answering materially identical
    cases differently makes the regression suite self-contradictory, and nothing
    else in the pipeline would notice.
    """

    signature: list[tuple[str, str]]
    outcomes: dict[str, list[str]]
    case_ids: list[str]
    ruled_by: list[str]


class Precedent(BaseModel):
    """A human ruling, kept forever.

    This is the only durable output of the whole system. Everything else is
    recomputable; this is not.
    """

    case_id: str
    domain: str
    correct_outcome: str
    ruled_by: str
    note: str = ""
    established_at: datetime
    established_by_run: str = ""
