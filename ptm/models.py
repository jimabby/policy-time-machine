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
