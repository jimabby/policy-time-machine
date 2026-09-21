"""Is the judge *right*? Scored against the humans who ruled.

Everything else in this project measures the judge against itself.
:mod:`ptm.stability` asks whether it reproduces its own verdicts; flip
confirmation asks the same question of one flip. Both can come back perfect
for a judge that is reliably, consistently wrong.

The ground truth was there the whole time. Every precedent is a case a human
looked at and settled, and every gate run stores what the judge said about
those same cases. Scoring one against the other costs nothing and answers the
question the whole pipeline rests on.

Two things make it worth more than a single accuracy number:

**The confidence field is load-bearing and has never been checked.**
``review.below_confidence`` routes cases to humans on the judge's own claim
about how sure it is. If a verdict claiming 0.9 is right no more often than one
claiming 0.6, that routing is sorting cases at random and the review budget -
the scarcest thing here - is being spent by a number that means nothing.

**Accuracy here is a floor, not an estimate.** Precedents are the *contested*
flips: low confidence, large money, or a loosening the organisation did not
choose. Nobody adjudicates the easy ones. The judge's accuracy over all cases
is higher than this, and anyone quoting this figure as overall accuracy is
quoting it wrong.
"""

from __future__ import annotations

import functools
import json
import sys
from collections import Counter

from . import cli, stats
from .config import DomainConfig, load_domain
from .models import CalibrationBucket, CalibrationReport, Precedent, Verdict

#: Confidence bands, ``lo <= c < hi`` with the last one closed at 1.0. Chosen to
#: straddle the shipped ``below_confidence`` of 0.75 rather than to be pretty,
#: because the point of the panel is whether that threshold separates anything.
BANDS: list[tuple[float, float]] = [(0.0, 0.6), (0.6, 0.75), (0.75, 0.9), (0.9, 1.01)]


def score(domain: DomainConfig, version: str, verdicts: dict[str, Verdict],
          precedents: list[Precedent]) -> CalibrationReport:
    """Score stored verdicts against human rulings on the same cases.

    ``verdicts`` is the latest verdict per case under ``version`` - normally
    whatever the precedent gate already stored, so this costs nothing to run.
    A precedent with no verdict on file goes to ``unjudged``: counting it as
    agreement would be the exact failure the gate exists to prevent, and
    counting it as disagreement would punish the judge for not having run.
    """
    threshold = domain.review.below_confidence
    scored: list[tuple[Precedent, Verdict, bool]] = []
    unjudged: list[str] = []
    for p in precedents:
        v = verdicts.get(p.case_id)
        if v is None:
            unjudged.append(p.case_id)
            continue
        scored.append((p, v, v.outcome == p.correct_outcome))

    judged = len(scored)
    agreed = sum(1 for _, _, ok in scored if ok)
    accuracy = agreed / judged if judged else 0.0
    lo, hi = stats.wilson_interval(agreed, judged)
    mean_confidence = (sum(v.confidence for _, v, _ in scored) / judged) if judged else 0.0

    buckets: list[CalibrationBucket] = []
    weighted_gap = 0.0
    for band_lo, band_hi in BANDS:
        group = [(v, ok) for _, v, ok in scored if band_lo <= v.confidence < band_hi]
        if not group:
            continue
        n = len(group)
        hits = sum(1 for _, ok in group if ok)
        band_accuracy = hits / n
        band_confidence = sum(v.confidence for v, _ in group) / n
        buckets.append(CalibrationBucket(
            lo=band_lo, hi=min(band_hi, 1.0), n=n, agreed=hits,
            accuracy=round(band_accuracy, 4),
            mean_confidence=round(band_confidence, 4),
            gap=round(band_confidence - band_accuracy, 4),
        ))
        weighted_gap += (n / judged) * abs(band_confidence - band_accuracy)

    # Where the judge goes wrong, not just how often. An error concentrated in
    # one outcome is a prompt problem with a fix; error spread evenly across
    # every pair is a judge that does not understand the policy.
    wrong = Counter((p.correct_outcome, v.outcome) for p, v, ok in scored if not ok)
    confusion = [
        {"ruled": ruled, "judged": said, "n": n,
         "case_ids": sorted(p.case_id for p, v, ok in scored
                            if not ok and p.correct_outcome == ruled and v.outcome == said)}
        for (ruled, said), n in wrong.most_common()
    ]

    below = [ok for _, v, ok in scored if v.confidence < threshold]
    above = [ok for _, v, ok in scored if v.confidence >= threshold]
    return CalibrationReport(
        domain=domain.name,
        policy_version=version,
        judged=judged,
        agreed=agreed,
        accuracy=round(accuracy, 4),
        accuracy_lo=round(lo, 4),
        accuracy_hi=round(hi, 4),
        mean_confidence=round(mean_confidence, 4),
        overconfidence=round(mean_confidence - accuracy, 4),
        expected_calibration_error=round(weighted_gap, 4),
        buckets=buckets,
        confusion=confusion,
        review_threshold=threshold,
        below_threshold=_side(below),
        above_threshold=_side(above),
        # Does the threshold the review policy routes on actually sort the
        # cases? Two disjoint intervals, the right way round. Anything weaker
        # and a four-case difference would be reported as a working rule.
        threshold_separates=bool(
            below and above
            and sum(above) / len(above) > sum(below) / len(below)
            and stats.separated(sum(above), len(above), sum(below), len(below))
        ),
        unjudged=unjudged,
    )


def _side(flags: list[bool]) -> dict:
    n = len(flags)
    hits = sum(flags)
    lo, hi = stats.wilson_interval(hits, n)
    return {"n": n, "agreed": hits, "accuracy": round(hits / n, 4) if n else 0.0,
            "accuracy_lo": round(lo, 4), "accuracy_hi": round(hi, 4)}


def _lean(gap: float) -> str:
    """Which way a confidence gap runs, in words. Zero is neither way.

    ``'over' if gap > 0 else 'under'`` read a gap of exactly zero - a judge
    whose stated confidence matches how often it is right, which is the thing
    this module is asking for - as "underconfident by 0%". It is the one result
    worth printing plainly, and it was the one reported as a fault.

    The threshold is the *displayed* precision rather than the stored one, and
    that is the whole subtlety: at four decimal places a gap of 0.0001 is not
    zero, so it took the other branch and printed "overconfident by 0%" - the
    same sentence, with a direction attached to a magnitude the reader is being
    shown as nothing. A number that rounds away must not leave a word behind.
    """
    if f"{abs(gap):.0%}" == "0%":
        return "as confident as it is right"
    return f"{'over' if gap > 0 else 'under'}confident by {abs(gap):.0%}"


def describe(report: CalibrationReport) -> str:
    """The report as the CLI and the DAG log print it."""
    if not report.judged:
        return ("no precedent has a stored verdict under policy "
                f"{report.policy_version}; nothing to score the judge against")
    band = stats.describe_rate({"rate": report.accuracy, "rate_lo": report.accuracy_lo,
                                "rate_hi": report.accuracy_hi})
    lines = [
        f"judge vs {report.judged} human ruling(s) under policy {report.policy_version}",
        f"  agreed on {report.agreed} of {report.judged}  -  {band}",
        f"  mean confidence {report.mean_confidence:.0%}, {_lean(report.overconfidence)} "
        f"(ECE {report.expected_calibration_error:.0%})",
    ]
    for b in report.buckets:
        lines.append(f"    claimed {b.lo:.0%}-{b.hi:.0%}: right {b.agreed}/{b.n} "
                     f"({b.accuracy:.0%}), said {b.mean_confidence:.0%} "
                     f"-> {_lean(b.gap)}")
    if report.confusion:
        lines.append("  where it goes wrong:")
        for row in report.confusion[:5]:
            lines.append(f"    human ruled '{row['ruled']}', judge said "
                         f"'{row['judged']}'  x{row['n']}")
    below, above = report.below_threshold, report.above_threshold
    if below.get("n") and above.get("n"):
        lines.append(
            f"  the review threshold ({report.review_threshold:.0%}) "
            + ("separates: " if report.threshold_separates else "does NOT separate: ")
            + f"below it {below['accuracy']:.0%} right ({below['n']} cases), "
              f"above it {above['accuracy']:.0%} right ({above['n']} cases)")
        if not report.threshold_separates:
            lines.append("    a confidence that does not predict correctness is routing "
                         "the review budget at random")
    if report.unjudged:
        lines.append(f"  {len(report.unjudged)} precedent(s) had no verdict on file and "
                     f"were not scored: {report.unjudged[:5]}")
    lines.append("  precedents are the contested flips, so this is a floor on the "
                 "judge's accuracy, not an estimate of it")
    return "\n".join(lines)


def gate(report: CalibrationReport, domain: DomainConfig) -> list[str]:
    """Where the judge is further from the humans than the domain allows.

    Returns the reasons, or an empty list. The same two exemptions
    :func:`ptm.rules.gate` carries, for the same reasons, plus one this check
    needs on its own:

    **Nothing scored.** No precedent judged under this version is no evidence,
    not 0% accuracy. Failing there would make the gate fire loudest on a project
    that has not adjudicated anything yet - which is every project on day one.

    **Too little scored.** Precedents are the contested flips and there are
    never many of them. Accuracy on three cases has a confidence band running
    most of the way from 0 to 1, and a gate that acts on it is failing runs for
    the sample size. ``min_judged`` is the floor, and falling below it is
    reported as unmeasured rather than as a pass.

    **An inert judge.** Offline the verdicts come from ``offline_rules``, which
    were written from the same policy the humans were shown. What the figure
    then measures is the fixture, and a gate that fires on it teaches people to
    edit the fixture. Callers pass ``inert`` through
    :func:`ptm.report.calibration`, which knows which judge answered.
    """
    policy = domain.calibration
    if not report.judged or report.judged < max(policy.min_judged, 1):
        return []
    problems: list[str] = []
    if policy.min_accuracy and report.accuracy < policy.min_accuracy:
        problems.append(
            f"the judge reaches the human's outcome on {report.accuracy:.1%} of "
            f"{report.judged} ruling(s), below the {policy.min_accuracy:.1%} this domain "
            f"requires. Every flip in the replay is one of this judge's verdicts, so a "
            f"flip rate measured now describes the judge as much as the policy.")
    if report.overconfidence > policy.max_overconfidence:
        problems.append(
            f"it claims {report.mean_confidence:.0%} confidence and is right "
            f"{report.accuracy:.0%} of the time - overconfident by "
            f"{report.overconfidence:.0%}, past the {policy.max_overconfidence:.0%} "
            f"allowed. Confidence is what routes the review budget, and an overconfident "
            f"wrong verdict is one that never reaches the human who would have caught it.")
    below, above = report.below_threshold, report.above_threshold
    # Both sides have to have cases before "does not separate" means anything.
    # A judge confident on every ruling on file leaves nothing below the line,
    # and ``threshold_separates`` is False there because it is *unmeasured* -
    # reporting that as a finding would fail a run for a sample that has not
    # arrived yet, which is the same mistake ``min_judged`` exists to avoid.
    if (policy.require_threshold_separation and not report.threshold_separates
            and below.get("n") and above.get("n")):
        problems.append(
            f"the review threshold of {report.review_threshold:.0%} does not separate: "
            f"{above.get('accuracy', 0):.0%} right above it ({above.get('n', 0)} case(s)) "
            f"against {below.get('accuracy', 0):.0%} below it ({below.get('n', 0)}). "
            f"review.below_confidence is selecting the cases a human sees, so on this "
            f"evidence that queue - and every precedent established from it - is being "
            f"chosen at random.")
    return problems


USAGE = """usage:
  python -m ptm.calibration [domain] [version] [--json]

Score the judge against the humans who ruled on the same cases, and gate on it.
The only measurement here that scores the judge against an answer rather than
against itself.

  domain    defaults to 'expenses'
  version   defaults to 'v2'
  --json    the whole score on stdout and the prose on stderr, with the gate's
            verdict and the exit code in it. This is a gate, and a gate that
            can only say pass or fail leaves a CI step reporting that something
            breached a threshold rather than which threshold and by how much.

Exits non-zero when the domain's calibration.gate is 'fail' and a threshold is
breached. Inert with PTM_OFFLINE=1, where the verdicts came from the offline
rules rather than from a judge - it says so and does not gate."""


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.calibration [domain] [version]``; non-zero on a failing gate."""
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    as_json = "--json" in args
    # Same contract as ptm.gate: stdout is the document and nothing else, so
    # the prose has one destination picked here rather than at each call.
    say = functools.partial(print, file=sys.stderr if as_json else sys.stdout)
    positional = [a for a in args if not a.startswith("-")]
    # Taking the flags out of the positional list means a misspelled one would
    # otherwise be dropped on the floor: `--jsonn` would run and print prose,
    # and the caller parsing stdout would see the table it asked not to get.
    # Every other entry point here refuses an option it does not know, and this
    # one used to get that for free by reading argv[0] as the domain.
    unknown = [a for a in args if a.startswith("-") and a != "--json"]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    from . import report as report_module

    def answer(result: dict, code: int, problems: list[str]) -> int:
        if as_json:
            # The domain and version are stated here rather than taken from the
            # read model, which does not carry them: the plugin knows what it
            # asked for and a file on somebody's disk does not.
            json.dump({"domain": domain_name, "version": version, **result,
                       "code": code, "passed": not problems,
                       "gate_problems": problems}, sys.stdout, indent=2, default=str)
            print()
        return code

    try:
        result = report_module.calibration(domain_name, version)
    except LookupError as exc:
        if as_json:
            json.dump({"error": str(exc), "domain": domain_name, "version": version,
                       "code": 2, "measured": False, "passed": False},
                      sys.stdout, indent=2)
            print()
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if not result.get("measured"):
        say(result.get("hint", "nothing to score the judge against"))
        return answer(result, 0, [])
    report = CalibrationReport(**result["report"])
    say(describe(report))
    if result.get("inert"):
        say("  scored against verdicts the offline rules produced, so this measures the "
            "fixture rather than a judge - the gate is held off until PTM_OFFLINE=0")
        return answer(result, 0, [])
    domain = load_domain(domain_name)
    problems = gate(report, domain)
    for problem in problems:
        print(f"GATE  {problem}", file=sys.stderr)
    code = 1 if problems and domain.calibration.gate == "fail" else 0
    return answer(result, code, problems)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
