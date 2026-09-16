"""Confidence intervals, in one place so every band in this project means one thing.

Three different error bars get quoted around a flip rate here, and they measure
different things. Keeping the arithmetic in one module is what stops them being
read as interchangeable - or, worse, added together:

``ptm.stability``
    **Judge noise.** How often the same judge, asked the same question twice,
    answers differently. Offline it is identically zero.
``ptm.stats`` (this module)
    **Sampling error.** How much a rate measured on N cases would move had you
    measured a different N from the same history. It is a property of the
    sample size alone, so a perfectly consistent judge does not shrink it.
``ptm.calibration``
    **Judge accuracy.** How often the judge agrees with the human who ruled on
    the same case. The other two can both be perfect while this one is poor.

The interval is Wilson's score interval rather than the textbook normal
approximation, because the rates here are routinely small - a 2% flip rate in
one month's 120 cases - and the normal interval's lower bound goes below zero
exactly there. A negative lower bound on a count is how a number gets dismissed
by the room rather than argued with.

**And one question that comes before all three.** A band says how precise a
measurement turned out to be, once it has been paid for. :func:`sample_size` and
:func:`detectable_difference` ask it the other way round - *how many cases must
I replay to tell 20% from 24%?* - which is the question a backfill should be
sized by and the one nobody can put to an interval they have already bought. Two
hundred cases cannot separate those two rates at any confidence worth quoting,
and discovering that from overlapping bands afterwards costs a backfill.
"""

from __future__ import annotations

import math

#: Two-sided 95% normal quantile. Named because a demo audience asks for "95%"
#: and a risk function occasionally asks for 99% (2.5758).
Z95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion, clamped to [0, 1].

    ``trials`` of zero returns ``(0.0, 1.0)``: nothing has been measured, and
    the honest interval for an unmeasured proportion is the whole range rather
    than a confident zero.
    """
    n = max(int(trials), 0)
    if n == 0:
        return 0.0, 1.0
    k = min(max(int(successes), 0), n)
    z2 = z * z
    denominator = n + z2
    centre = (k + z2 / 2) / denominator
    spread = z / denominator * math.sqrt(k * (n - k) / n + z2 / 4)
    return max(0.0, centre - spread), min(1.0, centre + spread)


def rate(successes: int, trials: int, z: float = Z95) -> dict:
    """A proportion that carries its own band.

    Returned as a dict rather than a tuple so it can be splatted into a summary
    and survive a JSON round trip into the dashboard without anyone having to
    remember which element was the lower bound.
    """
    lo, hi = wilson_interval(successes, trials, z)
    point = (successes / trials) if trials else 0.0
    return {
        "rate": round(point, 4),
        "rate_lo": round(lo, 4),
        "rate_hi": round(hi, 4),
        "n": int(trials),
        "k": int(successes),
    }


def separated(a_successes: int, a_trials: int,
              b_successes: int, b_trials: int, z: float = Z95) -> bool:
    """Whether two measured rates are far enough apart to be worth reporting.

    Non-overlapping Wilson intervals. Deliberately the conservative test: it is
    stricter than a two-proportion z-test, so anything it flags is flagged for
    a reason a sceptical reader can check by eye from the two bands printed
    beside it. Used by :mod:`ptm.disparity`, where the cost of crying wolf on a
    nine-case bucket is that nobody reads the panel again.
    """
    a_lo, a_hi = wilson_interval(a_successes, a_trials, z)
    b_lo, b_hi = wilson_interval(b_successes, b_trials, z)
    return a_lo > b_hi or b_lo > a_hi


#: One-sided normal quantile for 80% power - the conventional floor, and what
#: turns "how many cases" from an opinion into arithmetic. Power is the other
#: half of a sample size and the half that gets left out: a measurement sized on
#: confidence alone answers "how often will I cry wolf" and says nothing about
#: "how often will I miss a real change", which for a policy replay is the
#: expensive direction to be wrong in.
Z_POWER80 = 0.8416212335729143

#: 90% power, for a decision nobody gets to re-run.
Z_POWER90 = 1.2815515655446004


def sample_size(baseline: float, target: float, z: float = Z95,
                power: float = Z_POWER80) -> int:
    """Cases per arm needed to tell ``baseline`` apart from ``target``.

    The two-proportion form, because that is the shape of the real question: a
    replay measures one rate over N cases and a later replay measures another
    over N cases, and *did the edit help* is whether those two are separable.
    Comparing a measured rate against a number treated as exact would give an
    answer about half this size and would be wrong by precisely the variance of
    the thing it pretended to know.

    Returns cases **per arm**. Two policy versions replayed over the same
    history pay it once, because the same cases serve both arms - which is one
    of the reasons this project replays history rather than sampling it.

    ``baseline == target`` returns 0 rather than dividing by zero: there is no
    sample size that distinguishes a rate from itself.
    """
    lo, hi = float(baseline), float(target)
    delta = abs(hi - lo)
    if delta <= 0:
        return 0
    pooled = (lo + hi) / 2
    numerator = (z * math.sqrt(2 * pooled * (1 - pooled))
                 + power * math.sqrt(lo * (1 - lo) + hi * (1 - hi)))
    return int(math.ceil(numerator * numerator / (delta * delta)))


def detectable_difference(baseline: float, n: int, z: float = Z95,
                          power: float = Z_POWER80) -> float:
    """The smallest move from ``baseline`` that ``n`` cases per arm could find.

    The inverse of :func:`sample_size`, and the more useful direction here: the
    case count is rarely a choice - it is however much history exists - so the
    question is what that history can and cannot settle.

    Solved by bisection rather than by rearranging, because the pooled variance
    term contains the answer. Forty halvings of a bounded interval is
    microseconds and lands far inside the precision anyone should quote.
    """
    if n <= 0:
        return 1.0
    headroom = 1.0 - float(baseline)
    # A baseline with no room above it can still move down, and a difference of
    # zero would be the wrong answer for a rate of 100%.
    sign = 1.0 if headroom > 0 else -1.0
    lo, hi = 0.0, headroom if headroom > 0 else float(baseline)
    if hi <= 0:
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        needed = sample_size(baseline, baseline + sign * mid, z, power)
        if needed and needed <= n:
            hi = mid
        else:
            lo = mid
    return round(hi, 5)


def power_report(baseline: float, n: int, target: float | None = None,
                 z: float = Z95, power: float = Z_POWER80) -> dict:
    """What a replay of ``n`` cases can and cannot settle about ``baseline``.

    ``target`` is an optional rate somebody actually cares about reaching - "we
    need this under 20%" - and it turns the report from a statement about
    precision into a statement about whether this history is enough to check the
    claim at all. A comparison that no amount of judging can settle is worth
    knowing about before the judging, not after.
    """
    mde = detectable_difference(baseline, n, z, power)
    out = {
        "cases": int(n),
        "baseline_rate": round(float(baseline), 4),
        "power": round(1 - _tail(power), 2),
        "detectable_difference": mde,
        "detectable_rate_lo": round(max(0.0, baseline - mde), 4),
        "detectable_rate_hi": round(min(1.0, baseline + mde), 4),
    }
    if target is not None:
        needed = sample_size(baseline, target, z, power)
        out.update({
            "target_rate": round(float(target), 4),
            "cases_needed": needed,
            "sufficient": bool(needed and needed <= int(n)),
            "shortfall": max(0, needed - int(n)),
        })
    return out


def _tail(quantile: float) -> float:
    """The one-sided normal tail beyond a quantile, so power reads as 0.8 not 0.84."""
    return 0.5 * math.erfc(quantile / math.sqrt(2))


def describe_power(report: dict, digits: int = 1) -> str:
    """The power report as the CLI and the export bundle print it."""
    lines = [
        f"{report['cases']:,} case(s) can detect a move of "
        f"{report['detectable_difference']:.{digits}%} or more from "
        f"{report['baseline_rate']:.{digits}%} "
        f"(95% confidence, {report['power']:.0%} power)",
        f"  anything between {report['detectable_rate_lo']:.{digits}%} and "
        f"{report['detectable_rate_hi']:.{digits}%} is inside this sample's noise and "
        f"must not be reported as a change",
    ]
    if "target_rate" in report:
        if report["sufficient"]:
            lines.append(f"  telling {report['baseline_rate']:.{digits}%} from "
                         f"{report['target_rate']:.{digits}%} needs "
                         f"{report['cases_needed']:,}; this history has enough")
        else:
            lines.append(f"  telling {report['baseline_rate']:.{digits}%} from "
                         f"{report['target_rate']:.{digits}%} needs "
                         f"{report['cases_needed']:,} case(s), "
                         f"{report['shortfall']:,} more than exist. That comparison "
                         f"cannot be settled on this history however much is spent "
                         f"judging it.")
    return "\n".join(lines)


def describe_rate(band: dict, digits: int = 1) -> str:
    """``24.5% (21.1-28.3%)`` - the headline with its band attached."""
    return (f"{band['rate']:.{digits}%} "
            f"({band['rate_lo']:.{digits}%}-{band['rate_hi']:.{digits}%})")
