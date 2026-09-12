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
    #: Whether re-judging this exact case reproduced the same verdict:
    #: ``"stable"``, ``"unstable"``, or ``""`` when it was never re-judged.
    #: An unstable flip is the judge changing its mind, not the policy moving,
    #: so it must not be handed to a human as though it were settled.
    stability: str = ""


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


class FlipConfirmation(BaseModel):
    """Whether one recorded flip survives being judged again.

    :class:`StabilityReport` puts an error bar on a whole replay. This puts one
    on a single flip, which is what the human queue actually needs: a flip the
    judge will not reproduce is not a policy change and must not become
    precedent.
    """

    case_id: str
    samples: int
    outcomes: dict[str, int]
    modal_outcome: str
    #: Share of samples that agreed with the modal outcome. 1.0 is unanimous.
    agreement: float
    #: True when every sample agreed *and* agreed with the recorded flip.
    stable: bool
    #: The outcome the replay recorded, for the case where re-judging is
    #: self-consistent but lands somewhere else entirely.
    recorded_outcome: str = ""


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


class CalibrationBucket(BaseModel):
    """How often the judge was right when it claimed a given confidence.

    One row of :class:`CalibrationReport`. ``gap`` is the number that matters:
    positive means the judge was more confident than it deserved to be, which
    is the direction that quietly breaks the review routing - an overconfident
    wrong verdict never reaches the human who would have caught it.
    """

    lo: float
    hi: float
    n: int
    agreed: int
    accuracy: float
    mean_confidence: float
    #: mean_confidence - accuracy. Positive is overconfident.
    gap: float


class CalibrationReport(BaseModel):
    """Whether the judge is *right*, measured against humans who ruled.

    :class:`StabilityReport` measures whether the judge is *consistent*, which
    is a different and weaker property: a judge can reproduce itself perfectly
    and be reliably wrong. The ground truth here is the precedent set - cases a
    human looked at and settled - so this is the only place in the project where
    the judge is scored against an answer rather than against itself.

    It carries a sampling bias that has to be quoted alongside it: precedents
    are, by construction, the *contested* flips. Nobody adjudicates the easy
    ones. Accuracy measured here is therefore a floor on the judge's accuracy
    over all cases, not an estimate of it.
    """

    domain: str
    policy_version: str
    #: Precedents that had a stored verdict to score. A precedent with no
    #: verdict on file is listed in ``unjudged`` rather than counted as agreed.
    judged: int
    agreed: int
    accuracy: float
    accuracy_lo: float
    accuracy_hi: float
    mean_confidence: float
    #: mean_confidence - accuracy over every scored case.
    overconfidence: float
    #: Weighted mean absolute gap across buckets - the usual expected
    #: calibration error, reported so two judges can be compared on one number.
    expected_calibration_error: float
    buckets: list[CalibrationBucket] = Field(default_factory=list)
    #: What the judge said against what the human ruled, for the cases it got
    #: wrong. Error concentrated in one outcome is a fixable prompt problem.
    confusion: list[dict[str, Any]] = Field(default_factory=list)
    #: Accuracy either side of the domain's ``below_confidence`` review
    #: threshold. If the two are not separated, that threshold is not selecting
    #: the cases a human should see, whatever else it is doing.
    review_threshold: float = 0.0
    below_threshold: dict[str, Any] = Field(default_factory=dict)
    above_threshold: dict[str, Any] = Field(default_factory=dict)
    threshold_separates: bool = False
    unjudged: list[str] = Field(default_factory=list)


class DisparityFinding(BaseModel):
    """One segment the change lands on much harder than the rest of its field.

    Blast radius already reports a flip rate per segment. This asks the question
    a policy owner is answerable for and that a table of rates lets everyone
    skip: *is this change concentrated on one group?*

    Deliberately narrow about what it claims. A flip-rate difference is not
    discrimination - segments differ in what they contain, and a policy about
    one category will always move that category more. It is a prompt to justify
    a concentration, and the justification may be excellent.
    """

    field: str
    #: The segment value carrying the change, compared against the rest of its
    #: field pooled - never against the single least-affected bucket, which
    #: would make every finding a story about the smallest group.
    value: str
    cases: int
    flips: int
    flip_rate: float
    flip_rate_lo: float
    flip_rate_hi: float
    rest_cases: int
    rest_flips: int
    rest_flip_rate: float
    #: flip_rate / rest_flip_rate, or 0.0 when the rest of the field has no
    #: flips at all - a ratio of infinity is reported in words, not as a number.
    ratio: float
    #: Whether the two Wilson intervals are disjoint. A finding without this is
    #: a small-sample artefact, and is reported as one rather than gated on.
    significant: bool
    #: Which way the concentration runs for the affected group: mostly
    #: loosening, mostly tightening, or mixed.
    direction: str
    net_impact: float = 0.0


class PolicyFinding(BaseModel):
    """Something wrong with a policy *before* anyone pays to replay it.

    A clause that cannot be cited, a rule contradicting another, an outcome the
    domain does not have. Cheap to find by reading the policy; expensive to
    discover as six hundred verdicts attributed to ``(no clause applies)``.
    """

    clause: str = Field(default="", description="The clause this concerns, e.g. '3.1'. Empty if it is about the policy as a whole.")
    kind: str = Field(description="One of: unnumbered, duplicate, contradiction, ambiguous, unreachable, undefined_outcome, uncited.")
    severity: str = Field(default="warning", description="'error' if a replay would produce wrong or unattributable results, 'warning' otherwise.")
    detail: str = Field(description="One or two sentences saying what is wrong and what to change.")


class ClauseEdit(BaseModel):
    """One proposed edit to one clause of a policy."""

    clause: str = Field(description="The clause identifier being changed, e.g. '1.1'. For a new clause, the next free number in the right section.")
    current_text: str = Field(default="", description="The clause as it reads today, verbatim. Empty when proposing a new clause.")
    proposed_text: str = Field(description="The clause as it should read, in the same register as the rest of the policy. One or two sentences.")
    rationale: str = Field(description="Which piece of evidence motivates this edit - a precedent reversed, a sweep curve, an attribution share.")
    expected_effect: str = Field(default="", description="What this should do to the replay, in a sentence, so the next run can check it.")


class PolicyPatch(BaseModel):
    """A drafted amendment to a policy, with the evidence it came from.

    The one output in this project a model is allowed to *write* rather than
    judge - and the only reason that is safe is that it is checked by machinery
    that already exists. A patch is a proposal; the precedent gate and a replay
    decide whether it was a good one. See :mod:`ptm.proposal`.
    """

    summary: str = Field(description="One sentence: what this amendment changes, and why.")
    edits: list[ClauseEdit] = Field(default_factory=list, description="The clause-level edits - the smallest set that addresses the evidence.")
    expected_effect: str = Field(default="", description="What the drafter expects to happen to the flip count and to the precedent violations.")
    risks: str = Field(default="", description="What this could break that the evidence does not show. Say it plainly rather than reassuring.")


class DraftRule(BaseModel):
    """One ``offline_rules`` entry, as a model may propose it."""

    when: str = Field(description="A Python boolean expression over the case payload's fields only, e.g. \"amount > 75 and flag == 'no'\".")
    outcome: str = Field(description="The outcome this rule gives. Must be exactly one of the domain's allowed outcomes.")
    clause: str = Field(default="", description="The clause of the policy this rule implements, e.g. '1.1'.")
    because: str = Field(default="", description="One sentence, phrased as the rationale a verdict would carry.")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0, description="How decisively the clause settles a case this rule matches.")


class RuleSet(BaseModel):
    """Offline rules for one policy version, ordered - first match wins.

    ``offline_rules`` are maintained by hand next to, but separate from, the
    markdown policy, so they drift. :mod:`ptm.lint` catches a rule naming a
    field that does not exist; nothing catches a rule that is simply wrong about
    what the policy says. This is the type a model fills in from the policy
    text, and :func:`ptm.rules.agreement` is what stops it being trusted on its
    own word.
    """

    rules: list[DraftRule] = Field(default_factory=list, description="Most specific first. A case is decided by the first rule that matches.")
    notes: str = Field(default="", description="Anything in the policy that could not be expressed as a rule over the case fields.")
