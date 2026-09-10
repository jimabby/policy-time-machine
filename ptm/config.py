"""Domain configuration loading.

A "domain" is the only thing that changes between an expense policy, an
insurance claims policy and a content moderation policy. Swapping the YAML
file swaps the entire application.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

INCLUDE_DIR = Path(os.environ.get("PTM_INCLUDE_DIR", "/opt/airflow/include"))
DB_PATH = Path(os.environ.get("PTM_DB", str(INCLUDE_DIR / "ptm.db")))
# The project is designed to be runnable at a booth with no credentials. The
# compose file and .env.example agree with this default; setting 0 opts into a
# real Common AI connection.
OFFLINE = os.environ.get("PTM_OFFLINE", "1") == "1"
LLM_CONN_ID = os.environ.get("PTM_LLM_CONN_ID", "pydanticai_default")
#: Model identifier used for the cost ledger. Mirrors the ``host`` half of the
#: pydantic-ai connection; it never selects the model, it only prices it.
JUDGE_MODEL = os.environ.get("PTM_JUDGE_MODEL", "anthropic:claude-sonnet-5")


class ReviewPolicy(BaseModel):
    """Which flips are worth a human's attention.

    The whole point is that humans review tens of cases, not thousands.
    """

    max_reviews: int = 8
    below_confidence: float = 0.75
    above_impact: float = 0.0
    always_review_directions: list[str] = Field(default_factory=lambda: ["loosening"])


class ConflictPolicy(BaseModel):
    """How to tell whether two human rulings contradict each other.

    Two precedents conflict when they agree on every field in ``key`` (with the
    impact field bucketed into ``impact_band``-wide bands) yet a human gave them
    different outcomes. Leaving ``key`` empty disables the check.
    """

    #: Payload fields that make two cases materially alike. Free-text fields
    #: and identifiers must stay out of this list or nothing ever matches.
    key: list[str] = Field(default_factory=list)
    #: Width of the band the impact field is rounded into before comparison, so
    #: a GBP 104 claim and a GBP 111 claim count as the same kind of case.
    impact_band: float = 100.0


class DomainConfig(BaseModel):
    name: str
    label: str
    #: Ordered most-generous to most-strict. The ordering is what lets us
    #: classify a flip as loosening or tightening without domain knowledge.
    outcomes: list[str]
    impact_field: str | None = None
    impact_unit: str = ""
    case_template: str
    #: The slowly-changing subject fact this domain depends on. Only used by
    #: ptm.pit_check to demonstrate what a naive replay gets wrong.
    pit_field: str | None = None
    judge_instructions: str = ""
    policies: dict[str, str]
    #: The policy actually in force today. Replays judge each case under *both*
    #: this and the candidate, which is what lets a change be attributed to the
    #: clause responsible - including a clause that stopped applying - and what
    #: separates "the policy changed" from "a reviewer deviated from the policy".
    in_force: str = "v1"
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    #: Payload dimensions to break the blast radius down by. These are the
    #: first question a policy owner asks after "how many": *who does this hit?*
    #: Point-in-time facts (``pit_field``) are legitimate segments - they are
    #: captured as of the decision date, not as of today.
    segment_fields: list[str] = Field(default_factory=list)
    conflicts: ConflictPolicy = Field(default_factory=ConflictPolicy)
    #: Fixtures for PTM_OFFLINE=1 only; the real judge never reads these.
    offline_rules: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    def validate_outcome(self, outcome: str) -> str:
        """Reject a judge response that is outside this domain's contract."""
        if outcome not in self.outcomes:
            raise ValueError(
                f"invalid outcome {outcome!r} for {self.name!r}; expected one of {self.outcomes}"
            )
        return outcome

    def policy_text(self, version: str) -> str:
        if version not in self.policies:
            raise KeyError(f"domain {self.name!r} has no policy version {version!r}; have {sorted(self.policies)}")
        return (INCLUDE_DIR / self.policies[version]).read_text()

    def render_case(self, payload: dict[str, Any]) -> str:
        """Render a case payload as text for the judge. Missing keys render empty."""
        return self.case_template.format_map(_Blank(payload))

    def impact_of(self, payload: dict[str, Any]) -> float:
        if not self.impact_field:
            return 0.0
        try:
            return float(payload.get(self.impact_field, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def segments_of(self, payload: dict[str, Any]) -> dict[str, str]:
        """The segment values for one case, as strings.

        Stringified deliberately: a segment is a label to group by, and
        ``grade: 3`` arriving as an int from one source and a str from another
        must not split into two buckets.
        """
        out: dict[str, str] = {}
        for field in self.segment_fields:
            value = payload.get(field)
            out[field] = "unknown" if value is None or value == "" else str(value)
        return out

    def conflict_signature(self, payload: dict[str, Any]) -> tuple | None:
        """A hashable description of "cases like this one", or None if disabled."""
        if not self.conflicts.key:
            return None
        band = self.conflicts.impact_band or 0
        sig: list[tuple[str, str]] = []
        for field in self.conflicts.key:
            value = payload.get(field)
            if field == self.impact_field and band > 0:
                try:
                    value = f"{int(float(value or 0) // band) * int(band)}+"
                except (TypeError, ValueError):
                    value = "unknown"
            sig.append((field, "unknown" if value is None or value == "" else str(value)))
        return tuple(sig)

    def clauses(self, version: str) -> list[str]:
        """Clause identifiers declared by a policy version's markdown.

        Used by the lint to catch an offline fixture citing a clause the policy
        does not contain, which is how an offline demo silently stops
        implementing the policy it claims to.
        """
        pattern = re.compile(r"^\s*(\d+\.\d+)\s", re.MULTILINE)
        return sorted(set(pattern.findall(self.policy_text(version))))

    def direction(self, old: str, new: str) -> str:
        """Loosening = the new policy is more generous than history was."""
        try:
            o, n = self.outcomes.index(old), self.outcomes.index(new)
        except ValueError:
            return "lateral"
        if n < o:
            return "loosening"
        if n > o:
            return "tightening"
        return "lateral"


class _Blank(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


@lru_cache(maxsize=None)
def load_domain(name: str) -> DomainConfig:
    path = INCLUDE_DIR / "domains" / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
        raise FileNotFoundError(f"no domain config {name!r} at {path}; available: {available}")
    return DomainConfig(**yaml.safe_load(path.read_text()))


def available_domains() -> list[str]:
    return sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
