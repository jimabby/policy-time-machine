"""The AI layer: what the replay *means*, not just what it counted.

The judge (``ptm.judge``) answers one case at a time. This module answers the
questions a decision-maker actually asks once 147 outcomes have moved:

    brief      - what does this policy change do, in prose, with the numbers?
    themes     - which *kinds* of case moved? 147 rows is not an answer.
    amendment  - the gate failed; what is the smallest edit to the policy text
                 that stops it reversing a human ruling?

Each one is a pydantic ``output_type`` handed to the Common AI provider's
``LLMOperator``, so the result is typed rather than parsed out of prose, plus a
prompt builder and a deterministic offline stand-in. The offline versions are
not "AI" and do not pretend to be - they are computed from the same aggregates
the prompt would have shown the model, so PTM_OFFLINE=1 still exercises the
whole pipeline (storage, DAG wiring, the dashboard panel) with no API key.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .config import DomainConfig
from .models import Flip

# --------------------------------------------------------------------- models


class PolicyBrief(BaseModel):
    """An executive summary of a replay. The thing you forward to the board."""

    headline: str = Field(
        description="One sentence, under 120 characters, stating what this policy change actually does. No preamble."
    )
    verdict: str = Field(
        description="Exactly one of: 'ship', 'ship_with_caveats', 'do_not_ship'. Your recommendation on the evidence."
    )
    summary: str = Field(
        description="Three to five sentences explaining the change's effect, citing the specific numbers you were given."
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Two to four concrete risks, each one sentence. Say what could go wrong, not that caution is advisable.",
    )
    watch_items: list[str] = Field(
        default_factory=list,
        description="Two to three things to measure after shipping to catch this going wrong, each one sentence.",
    )
    blind_spots: list[str] = Field(
        default_factory=list,
        description=(
            "One sentence each for any rule no historical case exercised, and for any group of "
            "people the change lands on disproportionately. Empty only if there are none."
        ),
    )


class Theme(BaseModel):
    """A cluster of flips that moved for the same underlying reason."""

    name: str = Field(description="A short noun phrase naming the pattern, e.g. 'Small receiptless claims'.")
    case_count: int = Field(ge=0, description="How many of the flips shown belong to this theme.")
    clause: str = Field(default="", description="The policy clause driving this theme, e.g. '1.1'. Empty if several.")
    direction: str = Field(
        default="lateral", description="'loosening', 'tightening' or 'mixed' - the net direction of this theme."
    )
    explanation: str = Field(description="One or two sentences on why this group of cases moved.")
    example_case_ids: list[str] = Field(default_factory=list, description="Up to three case IDs from this theme.")


class FlipThemes(BaseModel):
    """Structured clustering of a replay's flips."""

    themes: list[Theme] = Field(default_factory=list, description="Three to six themes, largest first.")
    unexplained: int = Field(default=0, ge=0, description="Flips that fit no coherent theme.")


class AmendmentEdit(BaseModel):
    """One surgical change to the policy text."""

    clause: str = Field(description="The clause to change, e.g. '1.1'. Use 'new' for a clause to be added.")
    current_text: str = Field(default="", description="The clause as written today. Empty for a new clause.")
    proposed_text: str = Field(description="The clause as it should read. Self-contained policy prose.")
    reason: str = Field(description="One sentence: which precedent this reconciles and how.")


class Amendment(BaseModel):
    """A minimal patch to a policy that reverses no human ruling."""

    feasible: bool = Field(
        description="False if the precedents genuinely conflict with each other and no single policy satisfies them all."
    )
    rationale: str = Field(description="Two or three sentences on the approach taken, or why it is impossible.")
    edits: list[AmendmentEdit] = Field(default_factory=list, description="The smallest set of edits that clears the gate.")
    residual_risk: str = Field(
        default="", description="One sentence on what these edits change for cases that were NOT under review."
    )


# -------------------------------------------------------------------- prompts

BRIEF_SYSTEM = (
    "You are a policy analyst briefing an executive who will decide whether to adopt a rule change. "
    "You have been given the result of replaying every real decision the organisation made over two "
    "years under the proposed rule, point-in-time correct. Be concrete and numerate: cite the figures "
    "you were given rather than gesturing at them. Your reader is deciding, not browsing, so lead with "
    "the answer. Never invent a number that is not in the data below."
)

THEMES_SYSTEM = (
    "You are analysing which kinds of case a policy change moves. You will be shown individual changed "
    "decisions. Group them by the underlying mechanism that moved them - the clause and the fact pattern "
    "- not by superficial similarity. A good theme is one a policy author could act on. Only use the "
    "case IDs you are shown."
)

AMENDMENT_SYSTEM = (
    "You are a policy drafter. A proposed policy has been found to reverse rulings that accountable "
    "humans already made on specific cases; those rulings are binding precedent. Propose the smallest "
    "possible edit to the policy text that stops it reversing them, while preserving the change's "
    "original intent. Prefer narrowing an existing clause over adding a new one. If the precedents "
    "contradict each other so that no policy can satisfy them all, say so instead of inventing a fudge."
)


def build_brief_prompt(summary: dict, top_flips: list[Flip], domain: DomainConfig, version: str,
                       coverage: dict | None = None, cohorts: list[dict] | None = None) -> str:
    """Ground the brief in the aggregates, the largest movements, and the blind spots.

    ``coverage`` and ``cohorts`` are what the flip list cannot show: rules no
    case ever exercised, and who actually bears the change. Both are computed
    arithmetic (see ptm.analysis), handed to the model to explain rather than
    to derive.
    """
    unit = domain.impact_unit
    lines = [
        f"# Proposed change to the {domain.label} policy (version {version})",
        "",
        "## What the replay found",
        f"- Decisions replayed: {summary.get('cases_replayed', 0):,}",
        f"- Outcomes that change: {summary.get('flips', 0):,} ({summary.get('flip_rate', 0):.1%})",
        f"- More generous than history: {summary.get('loosening', 0):,} cases, {unit} {summary.get('impact_loosening', 0):,.0f}",
        f"- More strict than history: {summary.get('tightening', 0):,} cases, {unit} {summary.get('impact_tightening', 0):,.0f}",
        f"- Net effect: {unit} {summary.get('net_impact', 0):,.0f}",
        "",
        "## The policy text being proposed",
        domain.policy_text(version),
        "",
        f"## The {len(top_flips)} largest individual changes",
    ]
    for f in top_flips:
        lines.append(
            f"- {f.case_id} ({f.decided_at.date().isoformat()}): was '{f.actual_outcome}', "
            f"becomes '{f.new_outcome}' [{f.direction}, {unit} {f.impact:,.0f}, "
            f"judge confidence {f.confidence:.0%}, clause {f.policy_clause or 'n/a'}] - {f.rationale}"
        )
    if coverage:
        lines += ["", "## Which rules history actually exercised"]
        for c in coverage.get("clauses", []):
            lines.append(f"- Clause {c['clause']}: decided {c['cases']} case(s)"
                         + ("" if c["exercised"] else "  <- NEVER exercised by any historical case"))
        if coverage.get("decided_by_no_clause"):
            lines.append(
                f"- {coverage['decided_by_no_clause']} case(s) "
                f"({coverage.get('no_clause_share', 0):.1%}) were settled by no clause at all - the "
                f"policy does not reach them and they take the default outcome."
            )

    if cohorts:
        lines += ["", "## Who bears the change"]
        for breakdown in cohorts:
            lines.append(f"By {breakdown['field']} (population flip rate "
                         f"{breakdown.get('baseline_flip_rate', 0):.1%}):")
            for c in breakdown.get("cohorts", []):
                lines.append(
                    f"  - {breakdown['field']}={c['cohort']}: {c['cases']} case(s), "
                    f"{c['flip_rate']:.1%} changed ({c['disproportion']}x the population rate), "
                    f"net {unit} {c['net_impact']:,.0f}"
                )

    lines += [
        "",
        "## Your task",
        "Write the brief. A positive net figure means the change costs the organisation money; a",
        "negative one means it saves money. Judge confidence below 75% means the policy text did not",
        "clearly settle that case - clusters of those are a drafting problem, not a cost problem.",
        "A clause no case exercised is unevidenced, not necessarily wrong - say which it is. A cohort",
        "well above the population flip rate belongs in blind_spots even when the total looks modest.",
    ]
    return "\n".join(lines)


def build_themes_prompt(flips_: list[Flip], domain: DomainConfig, version: str) -> str:
    unit = domain.impact_unit
    lines = [
        f"# Changed {domain.label} decisions under policy {version}",
        "",
        f"{len(flips_)} decisions changed. Each line is one case.",
        "",
    ]
    for f in flips_:
        lines.append(
            f"- {f.case_id}: '{f.actual_outcome}' -> '{f.new_outcome}' [{f.direction}, "
            f"{unit} {f.impact:,.0f}, conf {f.confidence:.0%}, clause {f.policy_clause or 'n/a'}] {f.rationale}"
        )
    lines += [
        "",
        "## The policy that produced these",
        domain.policy_text(version),
        "",
        "## Your task",
        "Group these cases into three to six themes, largest first. Every theme must name the mechanism",
        "that moved its cases. Count honestly: case_count values should roughly account for the cases",
        f"shown, with anything genuinely miscellaneous counted in 'unexplained'. Valid outcomes are: "
        f"{', '.join(domain.outcomes)}.",
    ]
    return "\n".join(lines)


def build_amendment_prompt(violations: list[dict], domain: DomainConfig, version: str) -> str:
    lines = [
        f"# Policy {version} for {domain.label} reverses {len(violations)} human ruling(s)",
        "",
        "## The policy text as proposed",
        domain.policy_text(version),
        "",
        "## The precedents it reverses",
    ]
    for v in violations:
        lines.append(
            f"### Case {v['case_id']}\n"
            f"- {v['ruled_by']} ruled '{v['established_outcome']}' on {v['established_at']}\n"
            f"- Policy {version} gives '{v['proposed_outcome']}'\n"
            f"- The policy's reasoning: {v.get('proposed_rationale', 'n/a')}\n"
            f"- The human's note: {v.get('note') or 'none given'}"
        )
    lines += [
        "",
        "## Your task",
        f"Propose the minimal amendment. Allowed outcomes in this domain are: {', '.join(domain.outcomes)}.",
        "Quote current_text verbatim from the policy above when editing an existing clause. Do not",
        "propose abandoning the change wholesale - the change has a purpose; narrow it.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------ offline fallback


def offline_brief(summary: dict, top_flips: list[Flip], domain: DomainConfig, version: str,
                  coverage: dict | None = None, cohorts: list[dict] | None = None) -> PolicyBrief:
    """Deterministic stand-in for PTM_OFFLINE=1.

    Computed from the same aggregates the prompt would show the model. It is
    not a model and does not read the policy prose - it exists so that the
    storage, the DAG task and the dashboard panel are all exercised with no key.
    """
    unit = domain.impact_unit
    n, total = summary.get("flips", 0), summary.get("cases_replayed", 0)
    rate = summary.get("flip_rate", 0.0)
    net = summary.get("net_impact", 0.0)
    loose, tight = summary.get("loosening", 0), summary.get("tightening", 0)
    unsure = [f for f in top_flips if f.confidence < domain.review.below_confidence]

    direction = "costs" if net > 0 else "saves"
    verdict = "ship"
    if tight:
        verdict = "ship_with_caveats"
    if rate > 0.2 or unsure:
        verdict = "do_not_ship"

    risks = []
    if tight:
        risks.append(
            f"{tight} decision(s) become stricter, reversing outcomes {unit} "
            f"{summary.get('impact_tightening', 0):,.0f} in value that were granted at the time."
        )
    if rate > 0.15:
        risks.append(f"A {rate:.1%} flip rate is large enough that the change is a rewrite in effect, not a tweak.")
    if unsure:
        clauses = sorted({f.policy_clause for f in unsure if f.policy_clause})
        risks.append(
            f"{len(unsure)} of the {len(top_flips)} largest changes were decided at low confidence"
            + (f", concentrated in clause {', '.join(clauses)}" if clauses else "")
            + " - the text is ambiguous there, not merely generous."
        )
    if net > 0:
        risks.append(f"Net {unit} {abs(net):,.0f} of additional cost is unbudgeted unless this was planned for.")

    blind_spots = []
    if coverage:
        never = coverage.get("unexercised") or []
        if never:
            blind_spots.append(
                f"Clause{'s' if len(never) > 1 else ''} {', '.join(never)} "
                f"{'were' if len(never) > 1 else 'was'} never exercised by any case in two years of "
                f"history: shipping {'them' if len(never) > 1 else 'it'} is a guess, not a measurement."
            )
        if coverage.get("no_clause_share", 0) > 0.25:
            blind_spots.append(
                f"{coverage['decided_by_no_clause']} case(s) "
                f"({coverage['no_clause_share']:.1%}) are settled by no clause at all and fall through "
                f"to '{domain.outcomes[0]}' - the policy does not reach most of its own population."
            )
    for c in (cohorts or []):
        for row in c.get("cohorts", []):
            if row["cases"] >= 20 and row["disproportion"] >= 1.5:
                blind_spots.append(
                    f"{c['field']}={row['cohort']} absorbs {row['flip_rate']:.1%} of its cases changing, "
                    f"{row['disproportion']}x the population rate, for net {unit} {row['net_impact']:,.0f}."
                )

    return PolicyBrief(
        headline=f"Policy {version} moves {n:,} of {total:,} {domain.label} decisions and {direction} {unit} {abs(net):,.0f}.",
        verdict=verdict,
        summary=(
            f"Replaying {total:,} historical decisions under policy {version} changes {n:,} of them ({rate:.1%}). "
            f"{loose} become more generous ({unit} {summary.get('impact_loosening', 0):,.0f}) and {tight} become "
            f"stricter ({unit} {summary.get('impact_tightening', 0):,.0f}), for a net {direction[:-1] or 'effect'} of "
            f"{unit} {abs(net):,.0f}. "
            + (
                f"Of the {len(top_flips)} largest changes, {len(unsure)} were decided below "
                f"{domain.review.below_confidence:.0%} confidence and are the ones to read first: the policy "
                f"text did not clearly settle them, so those outcomes are a drafting artefact rather than a "
                f"decision."
                if unsure
                else "The judge settled every large change at high confidence, so the figures reflect the policy as written."
            )
        ),
        risks=risks[:4] or ["No material risk detected in the replayed population."],
        watch_items=[
            f"Actual {domain.impact_unit or 'impact'} against the {unit} {abs(net):,.0f} projected here, monthly.",
            f"The {loose} loosened case types, for volume growth once the looser rule is known.",
            "Any case reaching a human that the policy should have settled outright.",
        ],
        blind_spots=blind_spots[:4],
    )


def offline_themes(flips_: list[Flip], domain: DomainConfig) -> FlipThemes:
    """Group flips by the mechanism that moved them - the axis the model is asked for.

    A clause is the best available proxy for "mechanism". Where the judge cited
    none, the outcome transition is the next best: a block of cases going from
    'deny' to 'approve' with no clause attached means no rule in the new policy
    reaches them at all, which is a finding rather than a long tail.
    """
    buckets: dict[tuple[str, str], list[Flip]] = {}
    for f in flips_:
        key = (f.policy_clause, "") if f.policy_clause else ("", f"{f.actual_outcome}->{f.new_outcome}")
        buckets.setdefault(key, []).append(f)

    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    # Anything past the sixth group is a genuine tail, not a pattern to name.
    unexplained = sum(len(group) for _, group in ranked[6:])

    themes: list[Theme] = []
    for (clause, transition), group in ranked[:6]:
        dirs = {f.direction for f in group}
        biggest = sorted(group, key=lambda f: -f.impact)
        total = sum(f.impact for f in group)
        if clause:
            moves = sorted({f"'{f.actual_outcome}' to '{f.new_outcome}'" for f in group})
            name = f"Clause {clause}"
            why = (f"{len(group)} case(s) moved on clause {clause}, worth {domain.impact_unit} "
                   f"{total:,.0f}. The shifts seen here are {', '.join(moves[:3])}.")
        else:
            was, becomes = transition.split("->")
            name = f"'{was}' now '{becomes}', unruled"
            why = (f"{len(group)} case(s) worth {domain.impact_unit} {total:,.0f} changed from "
                   f"'{was}' to '{becomes}' with no clause deciding them: no rule in the proposed "
                   f"policy reaches this fact pattern, so they fall through to the default outcome.")
        themes.append(
            Theme(
                name=name,
                case_count=len(group),
                clause=clause,
                direction=dirs.pop() if len(dirs) == 1 else "mixed",
                explanation=why,
                example_case_ids=[f.case_id for f in biggest[:3]],
            )
        )
    return FlipThemes(themes=themes, unexplained=unexplained)


def offline_amendment(violations: list[dict], domain: DomainConfig, version: str) -> Amendment:
    """Name the conflict precisely without pretending to draft prose.

    Drafting policy language is exactly the part a deterministic fallback
    should not fake, so this reports the shape of the conflict and defers.
    """
    if not violations:
        return Amendment(feasible=True, rationale="No precedent is reversed; no amendment is needed.")

    # Two humans ruling differently on the same outcome pair is a genuine conflict.
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for v in violations:
        by_pair.setdefault((v["proposed_outcome"], v["established_outcome"]), []).append(v)

    edits = [
        AmendmentEdit(
            clause="unknown",
            current_text="",
            proposed_text=(
                f"Carve out the fact pattern of case {v['case_id']} so that policy {version} yields "
                f"'{v['established_outcome']}' rather than '{v['proposed_outcome']}'."
            ),
            reason=f"{v['ruled_by']} ruled '{v['established_outcome']}' on {v['established_at']}; the policy must not reverse that.",
        )
        for v in violations
    ]
    return Amendment(
        feasible=True,
        rationale=(
            f"Policy {version} reverses {len(violations)} ruling(s) across {len(by_pair)} distinct outcome "
            f"pair(s). Offline mode reports the conflicts rather than drafting policy language, which is the "
            f"part that requires the model: set PTM_OFFLINE=0 for a drafted amendment."
        ),
        edits=edits,
        residual_risk=(
            "Any carve-out narrow enough to satisfy these precedents will also narrow the change for "
            "cases that were never reviewed; the next replay is what measures that."
        ),
    )


def as_model(raw: Any, model: type[BaseModel]) -> BaseModel:
    """Coerce whatever XCom handed back into ``model``.

    LLMOperator pushes the pydantic model itself, offline tasks push a dict, and
    a serialising XCom backend can hand either one back as a JSON string -
    ``model_validate`` rejects that last case, so it is handled explicitly.
    """
    if isinstance(raw, model):
        return raw
    if isinstance(raw, dict):
        return model(**raw)
    if isinstance(raw, (str, bytes, bytearray)):
        return model.model_validate_json(raw)
    return model.model_validate(raw)
