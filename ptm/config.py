"""Domain configuration loading.

A "domain" is the only thing that changes between an expense policy, an
insurance claims policy and a content moderation policy. Swapping the YAML
file swaps the entire application.
"""

from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

INCLUDE_DIR = Path(os.environ.get("PTM_INCLUDE_DIR", "/opt/airflow/include"))
DB_PATH = Path(os.environ.get("PTM_DB", str(INCLUDE_DIR / "ptm.db")))
OFFLINE = os.environ.get("PTM_OFFLINE", "0") == "1"
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
    #: Payload/fact keys worth breaking the impact down by - "who bears this
    #: change". Declared per domain because only the domain knows which of its
    #: fields describe a person rather than a transaction.
    cohort_fields: list[str] = Field(default_factory=list)
    policies: dict[str, str]
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    #: Fixtures for PTM_OFFLINE=1 only; the real judge never reads these.
    offline_rules: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    def policy_text(self, version: str) -> str:
        if version in self.policies:
            return (INCLUDE_DIR / self.policies[version]).read_text()
        candidate = self._candidate(version)
        if candidate:
            return candidate["text"]
        raise KeyError(f"domain {self.name!r} has no policy version {version!r}; "
                       f"have {sorted(self.all_versions())}")

    def rules_for(self, version: str) -> list[dict[str, Any]]:
        """Offline fixture rules for a version, hand-written or drafted."""
        if version in self.offline_rules:
            return self.offline_rules[version]
        candidate = self._candidate(version)
        return candidate["offline_rules"] if candidate else []

    def all_versions(self) -> list[str]:
        """Hand-written versions plus any candidate drafted into the registry."""
        return sorted(set(self.policies) | {c["version"] for c in self._candidates()})

    def _candidate(self, version: str) -> dict[str, Any] | None:
        return _registry(lambda: _store().load_policy_version(self.name, version))

    def _candidates(self) -> list[dict[str, Any]]:
        return _registry(lambda: _store().candidate_versions(self.name)) or []

    def declared_clauses(self, version: str) -> list[str]:
        """Every clause number the policy text defines, in document order.

        Clauses are what the judge cites, so this is the denominator for "which
        rules has history actually exercised?". Matches a leading ``N.N`` at the
        start of a line, which is how the policies in include/ are written.
        """
        seen: list[str] = []
        for line in self.policy_text(version).splitlines():
            m = re.match(r"\s*(\d+\.\d+)\s", line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
        return seen

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


def _store():
    # Imported late: ptm.store reads DB_PATH from this module, so importing it
    # at the top would be circular.
    from . import store
    return store


def _registry(read):
    """Read the candidate registry, tolerating its absence.

    A domain is fully usable before any database exists - the engine runs from
    YAML alone - so "no table yet" means "no candidates", not a failure.
    """
    try:
        return read()
    except sqlite3.Error:
        return None


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
