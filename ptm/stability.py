"""How much of a measured flip rate is the judge disagreeing with itself.

The headline number this project produces is *"147 of 600 outcomes change"*. The
obvious objection is: **is that the policy, or is that the model?** A language
model asked the same question twice does not always answer the same way, and a
flip rate quoted without that number is quoted without an error bar.

So measure it. Take a sample of cases, judge each one several times under the
*same* policy, and count how often the judge contradicts itself. Every
disagreement found here is a flip that might not be real.

    disagreement_rate = cases where repeated judging disagreed / cases sampled

The offline judge is deterministic and scores exactly zero, which is worth
stating rather than hiding: offline, this measures nothing, and the number only
becomes meaningful with ``PTM_OFFLINE=0``. That honesty is the point - a
stability check that cannot fail is not a check.
"""

from __future__ import annotations

import random
from collections import Counter

from .models import Case, StabilityReport


def sample_cases(cases: list[Case], n: int, seed: int = 7) -> list[Case]:
    """A deterministic, chronologically spread subset of cases.

    Deterministic so two stability runs are comparable, and spread across the
    period rather than drawn from one end, because a judge's consistency can
    itself vary with the kind of case - and the kinds of case are not evenly
    distributed through history.
    """
    if n <= 0 or n >= len(cases):
        return list(cases)
    rng = random.Random(seed)
    ordered = sorted(cases, key=lambda c: c.decided_at)
    # Take one case from each of n equal slices of the period, so the sample
    # cannot collapse onto a single month.
    step = len(ordered) / n
    picked = []
    for i in range(n):
        lo, hi = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
        picked.append(ordered[rng.randrange(lo, min(hi, len(ordered)))])
    return picked


def analyse(samples: list[dict], samples_per_case: int) -> StabilityReport:
    """Turn repeated verdicts into a disagreement rate.

    ``samples`` are dicts of ``case_id``, ``outcome`` and ``confidence`` - one
    per (case, repeat). Cases with a single sample are counted as sampled but can
    never be unstable, which keeps the rate honest when a judge task failed.
    """
    by_case: dict[str, list[dict]] = {}
    for s in samples:
        by_case.setdefault(s["case_id"], []).append(s)

    unstable = []
    for case_id, group in by_case.items():
        outcomes = Counter(s["outcome"] for s in group)
        if len(outcomes) < 2:
            continue
        modal, modal_n = outcomes.most_common(1)[0]
        confidences = [s["confidence"] for s in group]
        unstable.append({
            "case_id": case_id,
            "outcomes": dict(outcomes),
            "modal_outcome": modal,
            # 1.0 means every repeat agreed; 0.5 means a coin flip between two.
            "agreement": round(modal_n / len(group), 3),
            "samples": len(group),
            "confidence_spread": round(max(confidences) - min(confidences), 3),
            "mean_confidence": round(sum(confidences) / len(confidences), 3),
        })
    # Least agreement first: the case the judge is most confused by, first.
    unstable.sort(key=lambda r: (r["agreement"], -r["confidence_spread"]))

    sampled = len(by_case)
    return StabilityReport(
        cases_sampled=sampled,
        samples_per_case=samples_per_case,
        unstable_cases=len(unstable),
        disagreement_rate=round(len(unstable) / sampled, 4) if sampled else 0.0,
        unstable=unstable,
    )


def describe(report: StabilityReport, flip_rate: float | None = None) -> str:
    """A one-paragraph reading of the result, for logs and the DAG's return."""
    if report.cases_sampled == 0:
        return "no cases sampled; nothing to say about judge stability."
    if report.samples_per_case < 2:
        return (f"{report.cases_sampled} cases sampled once each - at least 2 samples "
                f"per case are needed to detect disagreement.")
    lines = [
        f"judged {report.cases_sampled} cases {report.samples_per_case}x each under the same policy",
        f"  {report.unstable_cases} disagreed with themselves "
        f"({report.disagreement_rate:.1%} disagreement rate)",
    ]
    if report.unstable_cases == 0:
        lines.append("  the judge is self-consistent on this sample; the measured flip "
                     "rate is the policy, not the model.")
    elif flip_rate:
        share = report.disagreement_rate / flip_rate if flip_rate else 0.0
        lines.append(f"  against a {flip_rate:.1%} flip rate, up to {share:.0%} of the "
                     f"measured change could be judge noise rather than policy.")
    return "\n".join(lines)
