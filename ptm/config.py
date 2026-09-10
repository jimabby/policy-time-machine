"""Domain configuration loading.

A "domain" is the only thing that changes between an expense policy, an
insurance claims policy and a content moderation policy. Swapping the YAML
file swaps the entire application.
"""

from __future__ import annotations

import os
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


class ReviewPolicy(BaseModel):
    """Which flips are worth a human's attention.

    The whole point is that humans review tens of cases, not thousands.
    """

    max_reviews: int = 8
    below_confidence: float = 0.75
    above_impact: float = 0.0
    always_review_directions: list[str] = Field(default_factory=lambda: ["loosening"])


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
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
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
