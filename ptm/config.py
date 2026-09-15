"""Domain configuration loading.

A "domain" is the only thing that changes between an expense policy, an
insurance claims policy and a content moderation policy. Swapping the YAML
file swaps the entire application.
"""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

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

    @field_validator("max_ratio")
    @classmethod
    def _ratio_must_exceed_one(cls, value: float) -> float:
        """A ratio below 1 inverts the two findings into each other.

        :mod:`ptm.disparity` reads this number twice: a segment moving more than
        ``max_ratio`` times the rest of its field is ``concentrated``, and one
        moving less than ``1 / max_ratio`` is ``passed_over``. Those are opposite
        findings and the arithmetic only keeps them apart while the multiplier is
        above one. At 0.5 the floor becomes 2.0, the two tests overlap, and a
        segment moving at exactly the same rate as the rest of its field
        satisfies both - so it is reported as a concentration, gated on, and
        capable of failing a replay for being perfectly average.

        Rejected at load rather than clamped. Somebody who wrote 0.5 meant
        something by it, and the value they meant is almost certainly 2.0 with
        the comparison the other way round; silently substituting a number they
        did not ask for is how a gate ends up measuring something nobody chose.
        """
        if value <= 1.0:
            raise ValueError(
                f"disparity.max_ratio must be greater than 1, got {value}. It is how many "
                f"times the rest of its field a segment may move before it is a finding, "
                f"and the same number read as 1/{value} is what makes a segment the change "
                f"passes over. At or below 1 those two tests overlap and a segment moving "
                f"exactly like the rest of its field is reported as a concentration.")
        return value


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


class CalibrationPolicy(BaseModel):
    """How wrong the judge may be about the cases humans settled before it is a failure.

    :func:`ptm.calibration.score` has produced these numbers since it was
    written and nothing has ever acted on them - the same gap
    :class:`RulesPolicy` exists to close for the offline rules, and a worse one,
    because this is the only measurement in the project that scores the judge
    against an answer rather than against itself.

    Three settings, because there are three separate ways for it to be bad news
    and they have different owners:

    ``min_accuracy``
        The judge disagrees with the humans too often. The flip set is a list of
        verdicts, so this is the floor under everything downstream of it.
    ``max_overconfidence``
        It is right about as often as before, but claims to be more certain than
        it is. Nothing else notices this at all.
    ``require_threshold_separation``
        ``review.below_confidence`` routes the scarcest resource here - human
        attention - on the judge's own claim about how sure it is. If verdicts
        above that line are no more often right than the ones below it, the
        review budget is being spent by a number that means nothing, and every
        precedent established from that queue was chosen at random.

    All three default to off, for the reason :class:`RulesPolicy` gives: a
    project that has never measured this must not start failing runs the first
    time it does. And like the rules gate, it stays silent on a measurement that
    is inert or absent - see :func:`ptm.calibration.gate`.
    """

    #: Minimum share of human rulings the judge must reach the same outcome on.
    min_accuracy: float = 0.0
    #: Ceiling on ``mean_confidence - accuracy``. Positive is overconfident,
    #: which is the direction that quietly breaks the review routing: an
    #: overconfident wrong verdict never reaches the human who would catch it.
    max_overconfidence: float = 1.0
    #: Whether to require that ``review.below_confidence`` actually sorts the
    #: cases - measurably, by disjoint Wilson intervals, not by a few points.
    require_threshold_separation: bool = False
    #: Below this many scored rulings nothing is checked. Accuracy on three
    #: contested cases is a number with a band from 0.1 to 0.8, and gating on
    #: it fails runs for the sample size rather than for the judge.
    min_judged: int = 10
    #: ``warn`` reports and carries on. ``fail`` stops the gate run.
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
    #: How wrong the judge may be about the cases humans have already settled.
    #: See :class:`CalibrationPolicy`.
    calibration: CalibrationPolicy = Field(default_factory=CalibrationPolicy)
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
        return clauses_in(self.policy_text(version))

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


def clauses_in(text: str) -> list[str]:
    """Clause identifiers declared by a policy markdown, sorted.

    Takes the text rather than a version because one caller does not have a
    version yet: :mod:`ptm.proposal` drafts an amendment, has a model write the
    offline rules for it, and validates those rules *before* the draft is
    written to disk and becomes a version anything can resolve. Reading the
    clause list off the drafted markdown is the only way to validate rules
    against the policy they actually implement rather than against the one they
    were derived from.
    """
    return sorted(set(re.findall(r"^\s*(\d+\.\d+)\s", text, re.MULTILINE)))


class _Blank(dict):
    def __missing__(self, key: str) -> str:
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


#: Loaded domains, each beside a fingerprint of the files it was built from.
_LOADED: dict[str, tuple[tuple, DomainConfig]] = {}


def _fingerprint(name: str) -> tuple:
    """What a domain's config was built from, cheap enough to check on every call.

    The cache this backs used to be an unconditional :func:`functools.lru_cache`,
    which is right for a task that runs once and exits and wrong for the two
    processes that do not. ``propose_<domain>`` writes a draft on a worker;
    :func:`merge_drafts` picks drafts up at load time; and the API server hosting
    the plugin loaded the domain when it started. Nothing invalidated it there,
    so a freshly drafted policy was missing from ``/api/domains``, reported by
    ``/api/drafts`` as ``available: false`` - which the dashboard renders as
    **"files gone"** - and 404'd from every version-scoped endpoint, until
    somebody restarted the webserver. The DAG side already knew and called
    :func:`ptm.proposal.reload_domain`; the read side had no equivalent and no
    reason to think it needed one.

    A stat of the YAML and a listing of the drafts folder is microseconds, next
    to a request that goes on to read hundreds of cases out of SQLite, so the
    check is simply made every time rather than put behind a TTL nobody could
    tune. ``cache_clear()`` is still honoured for callers that know they have
    just written something and would rather not depend on clock resolution.
    """
    stamp: list = []
    try:
        info = (INCLUDE_DIR / "domains" / f"{name}.yaml").stat()
        stamp.append((info.st_mtime_ns, info.st_size))
    except OSError:
        stamp.append(())
    folder = INCLUDE_DIR / DRAFTS_DIR / name
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            try:
                info = path.stat()
            except OSError:  # removed between the listing and the stat
                continue
            stamp.append((path.name, info.st_mtime_ns, info.st_size))
    return tuple(stamp)


def load_domain(name: str) -> DomainConfig:
    """The domain config, re-read whenever the files behind it have changed."""
    stamp = _fingerprint(name)
    cached = _LOADED.get(name)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    path = INCLUDE_DIR / "domains" / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
        raise FileNotFoundError(f"no domain config {name!r} at {path}; available: {available}")
    config = merge_drafts(DomainConfig(**yaml.safe_load(path.read_text(encoding="utf-8"))))
    _LOADED[name] = (stamp, config)
    return config


def _clear_loaded() -> None:
    """Drop every loaded domain. Named ``load_domain.cache_clear`` for callers."""
    _LOADED.clear()


#: Kept as an attribute of the function so every existing caller - the proposer,
#: the DAGs, the tests - goes on working against the lru_cache-shaped API.
load_domain.cache_clear = _clear_loaded


def available_domains() -> list[str]:
    return sorted(p.stem for p in (INCLUDE_DIR / "domains").glob("*.yaml"))
