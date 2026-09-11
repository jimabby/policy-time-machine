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


def describe_rate(band: dict, digits: int = 1) -> str:
    """``24.5% (21.1-28.3%)`` - the headline with its band attached."""
    return (f"{band['rate']:.{digits}%} "
            f"({band['rate_lo']:.{digits}%}-{band['rate_hi']:.{digits}%})")
