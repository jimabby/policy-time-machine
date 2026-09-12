"""Diffing verdicts against history, and against established precedent."""

from __future__ import annotations

from itertools import zip_longest
from typing import Iterator

from .config import DomainConfig
from .models import Case, Flip, Precedent, Verdict


def flips(cases: list[Case], verdicts: dict[str, Verdict], domain: DomainConfig) -> list[Flip]:
    """Cases where the proposed policy disagrees with what actually happened."""
    out: list[Flip] = []
    for case in cases:
        v = verdicts.get(case.case_id)
        if v is None or v.outcome == case.actual_outcome:
            continue
        out.append(
            Flip(
                case_id=case.case_id,
                decided_at=case.decided_at,
                actual_outcome=case.actual_outcome,
                new_outcome=v.outcome,
                rationale=v.rationale,
                confidence=v.confidence,
                policy_clause=v.policy_clause,
                impact=domain.impact_of(case.payload),
                payload=case.payload,
                direction=domain.direction(case.actual_outcome, v.outcome),
            )
        )
    return out


def select_for_review(flips_: list[Flip], domain: DomainConfig) -> list[Flip]:
    """Pick the handful of flips actually worth a human's time.

    Humans are the scarcest resource in this system, so spend them on the
    cases where the judge was unsure, the money is large, or the change makes
    the organisation more permissive than it chose to be.
    """
    r = domain.review
    unsure, costly, directional = [], [], []
    for f in flips_:
        # A case can qualify on more than one ground; bucket it by the strongest.
        if f.confidence < r.below_confidence:
            unsure.append(f)
        elif r.above_impact and f.impact >= r.above_impact:
            costly.append(f)
        elif f.direction in r.always_review_directions:
            directional.append(f)

    # Sorting the whole candidate pool by impact alone would let eight large
    # claims crowd out every ambiguous one, and an ambiguous case is the more
    # useful precedent: it settles the drafting, not just the invoice. So take
    # from each ground in turn, ambiguity first.
    for bucket in (unsure, costly, directional):
        bucket.sort(key=lambda f: (-f.impact, f.confidence))

    picked: list[Flip] = []
    # A manual replay covers all of history in one run, so the same case can
    # appear under several run_ids. The reviewer should see it once.
    seen: set[str] = set()
    for f in _round_robin(unsure, costly, directional):
        if f.case_id in seen:
            continue
        seen.add(f.case_id)
        picked.append(f)
        if len(picked) == r.max_reviews:
            break
    return picked


def _round_robin(*buckets: list[Flip]) -> Iterator[Flip]:
    """Draw from each bucket in turn, so no one ground monopolises the quota."""
    for group in zip_longest(*buckets):
        for f in group:
            if f is not None:
                yield f


def precedent_violations(verdicts: dict[str, Verdict], precedents: list[Precedent]) -> list[dict]:
    """Where a policy version contradicts a ruling a human already made.

    This is the regression suite. A policy change that trips this has quietly
    reversed a decision somebody was accountable for.
    """
    by_case = {p.case_id: p for p in precedents}
    violations = []
    for case_id, v in verdicts.items():
        p = by_case.get(case_id)
        if p and p.correct_outcome != v.outcome:
            violations.append(
                {
                    "case_id": case_id,
                    "established_outcome": p.correct_outcome,
                    "proposed_outcome": v.outcome,
                    "ruled_by": p.ruled_by,
                    "established_at": p.established_at.date().isoformat(),
                    "note": p.note,
                    "proposed_rationale": v.rationale,
                }
            )
    return violations


def summarise(flips_: list[Flip], total: int, domain: DomainConfig) -> dict:
    loosening = [f for f in flips_ if f.direction == "loosening"]
    tightening = [f for f in flips_ if f.direction == "tightening"]
    return {
        "cases_replayed": total,
        "flips": len(flips_),
        "flip_rate": round(len(flips_) / total, 4) if total else 0.0,
        "loosening": len(loosening),
        "tightening": len(tightening),
        "impact_unit": domain.impact_unit,
        "impact_loosening": round(sum(f.impact for f in loosening), 2),
        "impact_tightening": round(sum(f.impact for f in tightening), 2),
        "net_impact": round(sum(f.impact for f in loosening) - sum(f.impact for f in tightening), 2),
    }
