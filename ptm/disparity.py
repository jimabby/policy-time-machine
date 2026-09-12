"""Who the change lands on, asked as a question rather than as a table.

The blast radius panel already reports a flip rate per segment. In practice
nobody reads down it: twelve rows of percentages is exactly the shape of
information a room skims. The question underneath it - *is this change
concentrated on one group?* - is the first one a compliance or legal function
asks about a rule change, and for refunds, lending, admissions or moderation it
is the question that stops a proposal.

So this does three things a table does not:

**It pools the comparison.** A segment is compared against the rest of its own
field, not against the least-affected bucket. Comparing every value to the
minimum makes the smallest, noisiest group the protagonist of every finding.

**It refuses to cry wolf on small buckets.** A finding is only marked
significant when the two Wilson intervals are disjoint (:mod:`ptm.stats`), and
values below ``min_cases`` are not compared at all. Nine of twelve travel
cases moving is a 75% rate and almost no evidence; without this guard the panel
is noise and stops being read, which is worse than not having it.

**It keeps the direction attached.** A group whose cases mostly *loosen* is
being given something; a group whose cases mostly *tighten* is having something
taken away. Those are opposite findings and a flip rate cannot tell them apart.
A ratio below 1 is the third case worth seeing - a group the change largely
passes over, which for a loosening proposal means a benefit distributed
unevenly.

What this is not: evidence of discrimination. Segments differ in what they
contain, and a policy about one category will always move that category more.
Every finding here is a request to justify a concentration, and the
justification is often excellent. The gate exists so that the justification
happens *before* the rule ships rather than in the complaint that follows it.
"""

from __future__ import annotations

from . import stats
from .config import DomainConfig
from .models import DisparityFinding


def analyse(rows: list[dict], domain: DomainConfig) -> list[DisparityFinding]:
    """Findings from segment rows - :func:`ptm.diff.segment_stats` or the store's.

    Rows need ``field``, ``value``, ``cases``, ``flips`` and, for the direction,
    ``loosening``/``tightening`` with their impacts. Rows for fields the domain
    does not ask about are ignored rather than rejected, so this can be handed
    the whole blast radius.
    """
    policy = domain.disparity
    wanted = set(policy.fields or domain.segment_fields)
    by_field: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("field") in wanted:
            by_field.setdefault(row["field"], []).append(row)

    findings: list[DisparityFinding] = []
    for field, group in by_field.items():
        total_cases = sum(int(r.get("cases") or 0) for r in group)
        total_flips = sum(int(r.get("flips") or 0) for r in group)
        # One value is the whole field; there is nothing to compare it against,
        # and reporting a ratio of 1.0 would imply a comparison happened.
        if len(group) < 2 or not total_cases:
            continue
        for row in group:
            cases = int(row.get("cases") or 0)
            flips = int(row.get("flips") or 0)
            if cases < policy.min_cases:
                continue
            rest_cases = total_cases - cases
            rest_flips = total_flips - flips
            if rest_cases < policy.min_cases:
                continue
            rate = flips / cases
            rest_rate = (rest_flips / rest_cases) if rest_cases else 0.0
            ratio = (rate / rest_rate) if rest_rate else 0.0
            # Two findings, deliberately computed apart. A ratio of 0.0 is
            # ambiguous - it is "the rest of the field does not move" when this
            # segment does, and "this segment does not move" when the rest does -
            # so the test for each is written against the counts rather than
            # against the shared number. Reading it off the ratio alone is what
            # used to make a segment the change skips entirely, the strongest
            # pass-over there is, the one case that could never be reported.
            concentrated = (rest_flips == 0 and flips > 0) or (
                rest_rate > 0 and ratio >= policy.max_ratio)
            floor = (1 / policy.max_ratio) if policy.max_ratio else 0.0
            passed_over = rest_rate > 0 and ratio <= floor
            if not (concentrated or passed_over):
                continue
            lo, hi = stats.wilson_interval(flips, cases)
            loosening = int(row.get("loosening") or 0)
            tightening = int(row.get("tightening") or 0)
            findings.append(DisparityFinding(
                field=field,
                kind="concentrated" if concentrated else "passed_over",
                value=str(row.get("value", "")),
                cases=cases,
                flips=flips,
                flip_rate=round(rate, 4),
                flip_rate_lo=round(lo, 4),
                flip_rate_hi=round(hi, 4),
                rest_cases=rest_cases,
                rest_flips=rest_flips,
                rest_flip_rate=round(rest_rate, 4),
                ratio=round(ratio, 3),
                significant=stats.separated(flips, cases, rest_flips, rest_cases),
                direction=_direction(loosening, tightening),
                net_impact=round(float(row.get("impact_loosening") or 0)
                                 - float(row.get("impact_tightening") or 0), 2),
            ))
    # Significant first, then exposure before distribution, then by how far from
    # the rest of the field: a gated run should be able to print the top line and
    # have said the useful thing. An undefined ratio sorts as the most extreme
    # finding of its kind, which is what it is.
    findings.sort(key=lambda f: (not f.significant,
                                 f.kind != "concentrated",
                                 -abs((f.ratio or 99) - 1)))
    return findings


def _direction(loosening: int, tightening: int) -> str:
    """Which way a segment's flips run, as a word rather than two counts."""
    if not loosening and not tightening:
        # Distinct from "mixed". A segment with nothing moving either way is the
        # pass-over finding, and calling that "mixed" describes the opposite.
        return "unmoved"
    if loosening and tightening:
        bigger, smaller = max(loosening, tightening), min(loosening, tightening)
        if smaller / bigger > 0.34:  # a third or more the other way is genuinely mixed
            return "mixed"
    if loosening > tightening:
        return "loosening"
    if tightening > loosening:
        return "tightening"
    return "mixed"


def gated(findings: list[DisparityFinding]) -> list[DisparityFinding]:
    """The findings a gate may act on: concentration, and actual evidence for it.

    A group the change *passes over* is worth a panel and not worth failing a
    run for - it is a distribution question, not an exposure one - and an
    unsupported ratio is a small-sample artefact by definition. Selected on
    ``kind`` rather than on the ratio, because 0.0 means one thing for each and
    a numeric test cannot tell which one it is looking at.
    """
    return [f for f in findings if f.significant and f.kind == "concentrated"]


def describe(findings: list[DisparityFinding], domain: DomainConfig) -> str:
    """Findings as the replay DAG and the selftest print them."""
    policy = domain.disparity
    if not policy.fields and not domain.segment_fields:
        return "disparity check skipped: this domain declares no segment_fields"
    if not findings:
        return (f"no segment moves more than {policy.max_ratio:g}x the rest of its field "
                f"(minimum {policy.min_cases} cases to compare)")
    unit = domain.impact_unit
    lines = [f"{len(findings)} segment(s) the change does not land on evenly:"]
    for f in findings:
        lines.append(
            f"  {f.field}={f.value:<16} {f.flips}/{f.cases} moved ({f.flip_rate:.1%}, "
            f"{f.flip_rate_lo:.1%}-{f.flip_rate_hi:.1%}) vs {f.rest_flips}/{f.rest_cases} "
            f"({f.rest_flip_rate:.1%}) - {_comparison(f)}")
        lines.append(
            f"      {_movement(f)}, net {unit} {f.net_impact:,.0f}"
            + ("" if f.significant else "  [not significant at this sample size]"))
    lines.append("a concentration is not a fault - it is a question. These are the "
                 "segments somebody should be able to explain before the rule ships.")
    return "\n".join(lines)


def _comparison(f: DisparityFinding) -> str:
    """How this segment stands against the rest of its field, in words.

    Words rather than the ratio, because the ratio is 0.0 in two opposite
    situations and a reader cannot tell which from the number.
    """
    if f.kind == "passed_over" and not f.flips:
        return f"nothing here moved, against {f.rest_flip_rate:.1%} across the rest of {f.field}"
    if not f.ratio:
        return f"the rest of {f.field} does not move at all"
    if f.kind == "passed_over":
        return f"{f.ratio:g}x the rest of {f.field} - the change largely passes it over"
    return f"{f.ratio:g}x the rest of {f.field}"


def _movement(f: DisparityFinding) -> str:
    if f.direction == "unmoved":
        return "no case in this segment changed outcome"
    return "both directions" if f.direction == "mixed" else f"mostly {f.direction}"
