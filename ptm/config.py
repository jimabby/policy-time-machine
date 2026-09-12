"""Domain configuration loading.

A "domain" is the only thing that changes between an expense policy, an
insurance claims policy and a content moderation policy. Swapping the YAML
file swaps the entire application.
"""

from __future__ import annotations

import math
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
    #: Cap on how many of ``max_reviews`` may go to flips the proposal did not
    #: cause. A deviation - both policies agree, the recorded outcome did not -
    #: is a finding about your reviewers, not about the rule being proposed, and
    #: left uncapped the biggest of them crowd out the cases the proposal is
    #: actually responsible for. They are still worth a few slots, because the
    #: ruling settles a case the *current* policy already gets wrong.
    max_deviation_reviews: int = 2
    #: Whether to keep flips the judge would not reproduce out of the human
    #: queue. Requires a confirmation pass (``judge_stability`` with
    #: ``target=flips``); with no measurement on file nothing is excluded.
    exclude_unstable: bool = True


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


class DisparityPolicy(BaseModel):
    """When a change landing unevenly is worth stopping for.

    Blast radius reports the rates; this decides which of them somebody has to
    account for. See :mod:`ptm.disparity` for why the comparison is pooled and
    why small buckets are excluded rather than reported quietly.
    """

    #: Segment fields to check. Empty means every field in ``segment_fields``.
    fields: list[str] = Field(default_factory=list)
    #: How many times the rest of its field a segment may move before it is a
    #: finding. Also read the other way: a segment moving less than
    #: ``1 / max_ratio`` is a group the change largely passes over.
    max_ratio: float = 2.0
    #: Below this, a segment is not compared at all. Nine of twelve cases is a
    #: 75% rate and almost no evidence; publishing it as a finding is how a
    #: panel stops being read.
    min_cases: int = 30
    #: ``warn`` prints and carries on. ``fail`` stops the replay when a
    #: concentration is both large and statistically supported - for a domain
    #: where shipping the rule first and explaining afterwards is not an option.
    gate: str = "warn"


class RulesPolicy(BaseModel):
    """How far the offline rules may drift from the judge before it is a failure.

    :func:`ptm.rules.agreement` has always produced this number and nothing has
    ever acted on it, so a rule set that quietly stopped implementing the policy
    left the sweep confidently wrong and the panel green. These are the two
    thresholds that make it load-bearing.

    Both default to 0, which enforces nothing: a project that has never measured
    agreement against a real judge must not start failing runs the first time it
    does. Set them once you have a figure to hold the rules to.
    """

    #: Minimum share of cases where the rules reach the judge's outcome.
    min_outcome_agreement: float = 0.0
    #: Minimum share where they reach it *citing the same clause*. The weaker
    #: looking number and the one the sweep actually rests on: a rule that gets
    #: the right answer from the wrong clause makes the attribution panel, and
    #: therefore every threshold curve drawn from it, describe the wrong
    #: sentence.
    min_clause_agreement: float = 0.0
    #: ``warn`` reports and carries on. ``fail`` refuses to serve a sweep whose
    #: rules no longer implement the policy it claims to be sweeping.
    gate: str = "warn"


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
    #: Whether a change landing unevenly across segments is worth stopping for.
    disparity: DisparityPolicy = Field(default_factory=DisparityPolicy)
    #: How far the offline rules may drift from the judge before the sweep built
    #: on them stops being served. See :class:`RulesPolicy`.
    rules: RulesPolicy = Field(default_factory=RulesPolicy)
    #: Fixtures for PTM_OFFLINE=1 only; the real judge never reads these.
    offline_rules: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    #: Versions that were drafted by :mod:`ptm.proposal` rather than written by
    #: a person. Merged in from ``include/drafts/<domain>/`` at load time, and
    #: kept as a separate list so nothing can present a machine's draft as an
    #: approved policy - the dashboard and the lint both say which is which.
    draft_versions: list[str] = Field(default_factory=list)

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
        # Explicit encoding, not the platform default: policies are UTF-8 and
        # contain typographic punctuation. Read as cp1252 on a Windows checkout
        # they load without error and come back mojibake, which then reaches the
        # judge's prompt and any draft written from them.
        return (INCLUDE_DIR / self.policies[version]).read_text(encoding="utf-8")

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
                    # Arithmetic in floats, formatted with :g. Rounding the band
                    # to an int first meant any band below 1 became 0, and every
                    # case in the domain then shared the signature "0+" - so a
                    # fine-grained band, the setting a careful person would
                    # reach for, silently made every pair of rulings a conflict.
                    floor = math.floor(float(value or 0) / band) * band
                    value = f"{floor:g}+"
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

    def clause_text(self, version: str, clause: str) -> str:
        """One clause's text, joined across its continuation lines.

        Lives here beside :meth:`clauses` because two callers need the same
        answer for different reasons - :mod:`ptm.proposal` rewrites a threshold
        in it, and :func:`ptm.diff.stale_precedents` asks whether it has changed
        since a human ruled on it - and two readers of a policy that disagree
        about where a clause ends is exactly the drift this project is about.
        """
        match = re.search(rf"^\s*{re.escape(clause)}\s+(.*?)(?=^\s*\d+\.\d+\s|^#|\Z)",
                          self.policy_text(version), re.MULTILINE | re.DOTALL)
        return " ".join(match.group(1).split()) if match else ""

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


#: Where :mod:`ptm.proposal` writes drafted policy versions. Deliberately not
#: ``policies/``: a draft a model wrote must never sit in the same folder as the
#: text a person approved, and keeping them apart makes discarding every draft
#: one delete rather than an audit.
DRAFTS_DIR = "drafts"


def merge_drafts(config: DomainConfig) -> DomainConfig:
    """Register drafted versions found on disk as policy versions of this domain.

    A draft is ``include/drafts/<domain>/<version>.md``, optionally beside a
    ``<version>.rules.yaml`` holding the offline rules that let the
    deterministic judge evaluate it. Merging them at load time is what makes a
    drafted policy immediately replayable, gateable and sweepable by every DAG
    and every endpoint, with no new code path - and without the proposer having
    to rewrite the hand-maintained domain YAML, a file that is mostly comments
    explaining decisions a person made.

    A draft never shadows a declared version. If the YAML names it, the YAML
    wins: that file is the one somebody is accountable for.
    """
    folder = INCLUDE_DIR / DRAFTS_DIR / config.name
    if not folder.is_dir():
        return config
    for path in sorted(folder.glob("*.md")):
        version = path.stem
        if version in config.policies:
            continue
        config.policies[version] = f"{DRAFTS_DIR}/{config.name}/{path.name}"
        config.draft_versions.append(version)
        rules = path.with_suffix(".rules.yaml")
        if rules.exists():
            config.offline_rules[version] = yaml.safe_load(rules.read_text(encoding="utf-8")) or []
    return config


@lru_cache(maxsize=None)
def load_domain(name: str) -> DomainConfig:
    path = INCLUDE_DIR / "domains" / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
        raise FileNotFoundError(f"no domain config {name!r} at {path}; available: {available}")
    return merge_drafts(DomainConfig(**yaml.safe_load(path.read_text(encoding="utf-8"))))


def available_domains() -> list[str]:
    return sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
