"""Two judges, the same policy, the same cases. Do they agree?

Every other check in this project compares the judge to itself.
:mod:`ptm.stability` asks whether it reproduces its own verdicts; flip
confirmation asks the same of one flip. Both are satisfied completely by a judge
that reads a clause wrongly and then reads it wrongly every single time - which
is not a rare failure but the *likely* one, because a systematic misreading is
exactly what a temperature-zero model does best.

:mod:`ptm.calibration` does compare the judge to something outside itself, and
it is the stronger check, but it can only speak about the handful of cases a
human has ruled on - by construction the contested ones. There is no ground
truth for the other five hundred and ninety.

A second model is not ground truth either, and this module does not pretend
otherwise. What it is, is *independent*: two judges that misread the same clause
in the same direction is a much smaller coincidence than one judge doing it
twice. So a case both judges decide the same way is evidence the policy decides
it; a case they split on is a case the policy does not settle, whatever
confidence either of them claimed. That second set is the useful output here,
because it is a list of sentences to go and rewrite - and it can be produced for
every case, not only for the ones somebody adjudicated.

What it cannot do is say which judge is right. Where they disagree this reports
both answers and stops; :mod:`ptm.calibration` is the only thing here that
scores a judge against an answer, and it needs a human to have supplied one.
"""

from __future__ import annotations

from collections import Counter

from . import stats
from .config import DomainConfig
from .models import CrossCheckReport, Verdict

__all__ = ["CrossCheckReport", "analyse", "describe"]


def analyse(primary: dict[str, Verdict], secondary: dict[str, Verdict],
            domain: DomainConfig, primary_label: str = "primary",
            secondary_label: str = "secondary",
            actual: dict[str, str] | None = None) -> CrossCheckReport:
    """Score two judges' verdicts on the cases they both saw.

    Only the overlap is compared. A case one judge never saw is a gap in the
    run, not a disagreement between the judges, and counting it either way would
    make the number depend on which tasks happened to succeed.

    ``actual`` maps a case to its recorded outcome, when the caller has it. It
    turns the split into something better than a count: a case the two judges
    split on where *one of them agrees with history* is a case the second judge
    would not have flipped, so the flip rate the pipeline reports rests on the
    first judge alone.
    """
    shared = sorted(set(primary) & set(secondary))
    agreed = 0
    disagreements: list[dict] = []
    clause_compared = clause_agreed = 0
    split_flips = 0
    for case_id in shared:
        a, b = primary[case_id], secondary[case_id]
        if a.policy_clause and b.policy_clause:
            clause_compared += 1
            clause_agreed += int(a.policy_clause == b.policy_clause)
        if a.outcome == b.outcome:
            agreed += 1
            continue
        was = (actual or {}).get(case_id, "")
        # A flip only one of them makes. The pipeline's headline number is built
        # from the primary judge's verdicts, so this is the part of it the
        # second judge would not have produced.
        contested_flip = bool(was) and (a.outcome != was) != (b.outcome != was)
        split_flips += int(contested_flip)
        disagreements.append({
            "case_id": case_id,
            "actual_outcome": was,
            primary_label: a.outcome,
            secondary_label: b.outcome,
            "primary_outcome": a.outcome,
            "secondary_outcome": b.outcome,
            "primary_clause": a.policy_clause,
            "secondary_clause": b.policy_clause,
            "primary_confidence": a.confidence,
            "secondary_confidence": b.confidence,
            "direction": domain.direction(b.outcome, a.outcome),
            "only_one_flips": contested_flip,
        })

    band = stats.rate(agreed, len(shared))
    # Which way the disagreement runs. Two judges that split at random is a
    # different problem from one judge that is reliably the more generous of the
    # two - the second is a prompt or a model difference somebody can act on.
    lean = Counter(d["direction"] for d in disagreements)
    # Confidence on the cases they split. If a judge claims high confidence
    # exactly where an independent judge disagrees, its confidence is not
    # measuring what the review routing assumes it measures.
    mean_split_confidence = (
        round(sum(d["primary_confidence"] for d in disagreements) / len(disagreements), 3)
        if disagreements else 0.0)
    mean_agreed_confidence = (
        round(sum(primary[c].confidence for c in shared
                  if primary[c].outcome == secondary[c].outcome) / agreed, 3)
        if agreed else 0.0)
    disagreements.sort(key=lambda d: (-d["primary_confidence"], d["case_id"]))
    return CrossCheckReport(
        domain=domain.name,
        primary=primary_label,
        secondary=secondary_label,
        compared=len(shared),
        agreed=agreed,
        agreement=band["rate"],
        agreement_lo=band["rate_lo"],
        agreement_hi=band["rate_hi"],
        clause_compared=clause_compared,
        clause_agreed=clause_agreed,
        clause_agreement=round(clause_agreed / clause_compared, 4) if clause_compared else 0.0,
        judged_only_by_primary=sorted(set(primary) - set(secondary)),
        judged_only_by_secondary=sorted(set(secondary) - set(primary)),
        lean=dict(sorted(lean.items(), key=lambda kv: -kv[1])),
        contested_flips=split_flips,
        mean_confidence_when_agreed=mean_agreed_confidence,
        mean_confidence_when_split=mean_split_confidence,
        disagreements=disagreements[:50],
    )


def describe(report: CrossCheckReport, flips: int | None = None) -> str:
    """The report as the DAG log and the CLI print it."""
    if not report.compared:
        return (f"no cases judged by both {report.primary} and {report.secondary}; "
                f"nothing to cross-check")
    band = stats.describe_rate({"rate": report.agreement, "rate_lo": report.agreement_lo,
                                "rate_hi": report.agreement_hi})
    lines = [
        f"{report.primary} vs {report.secondary} on {report.compared} case(s) "
        f"under the same policy",
        f"  same outcome on {report.agreed}  -  {band}",
        f"  same clause cited on {report.clause_agreed} of {report.clause_compared} "
        f"({report.clause_agreement:.1%})",
    ]
    if report.compared != report.agreed:
        split = report.compared - report.agreed
        lines.append(f"  {split} case(s) the two judges split on. These are cases the "
                     f"policy does not settle - the disagreement is the finding, and "
                     f"neither answer is the right one to write down.")
        if report.lean:
            lines.append("    which way: " + ", ".join(
                f"{n} {'both ways' if k == 'lateral' else k}"
                for k, n in report.lean.items())
                + f"  (direction is {report.primary} relative to {report.secondary})")
        if report.contested_flips:
            detail = (f" of the {flips} this policy reports" if flips else "")
            lines.append(f"    {report.contested_flips} of them are flips only "
                         f"{report.primary} makes{detail} - that much of the headline "
                         f"rests on one judge")
        if report.mean_confidence_when_split >= report.mean_confidence_when_agreed:
            lines.append(
                f"    {report.primary} claimed {report.mean_confidence_when_split:.0%} "
                f"confidence where it was contradicted against "
                f"{report.mean_confidence_when_agreed:.0%} where it was confirmed - "
                f"confidence that does not fall on contested cases is not measuring "
                f"what the review routing spends it on")
        for d in report.disagreements[:5]:
            lines.append(f"    {d['case_id']}: {report.primary} '{d['primary_outcome']}' "
                         f"(clause {d['primary_clause'] or '-'}, "
                         f"conf {d['primary_confidence']:.0%}), "
                         f"{report.secondary} '{d['secondary_outcome']}' "
                         f"(clause {d['secondary_clause'] or '-'})")
    else:
        lines.append("  the two judges never disagreed, so nothing here suggests the "
                     "policy is ambiguous on this sample.")
    if report.judged_only_by_primary or report.judged_only_by_secondary:
        lines.append(f"  {len(report.judged_only_by_primary)} case(s) only "
                     f"{report.primary} saw and {len(report.judged_only_by_secondary)} "
                     f"only {report.secondary} saw; those are excluded rather than counted")
    lines.append("  a second judge is independent, not correct. Where they disagree this "
                 "says so and stops - only ptm.calibration scores a judge against an answer.")
    return "\n".join(lines)
