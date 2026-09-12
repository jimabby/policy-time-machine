"""Diffing verdicts against history, and against established precedent.

Four questions, in the order a policy owner actually asks them:

1. What changes?            :func:`flips`
2. Which clause changed it? :func:`attribute` / :func:`clause_attribution`
3. Who does it hit?         :func:`segment_stats`
4. Does it reverse a ruling a human already made? :func:`precedent_violations`

And one the gate has to ask about its own oracle before trusting it: is a ruling
still about the sentence it was made about? :func:`stale_precedents`.
"""

from __future__ import annotations

from collections import defaultdict

from . import stats
from .config import DomainConfig
from .models import Case, Flip, Precedent, PrecedentConflict, Verdict


#: Attribution buckets that are not a clause of the candidate policy.
DEVIATION = "(reviewer deviated from policy)"
UNEXPLAINED = "(no clause applies)"


def attribute(candidate: Verdict, baseline: Verdict | None) -> str:
    """Why this case moved: the clause responsible, in one label.

    Attribution is not simply "the clause the candidate policy cited", because
    the most common way a rule change moves a decision is by a restriction
    *ceasing* to fire. Nothing is cited in that case - the claim is simply
    permitted - so reading only the candidate verdict leaves the majority of
    changes unexplained and blames the rest on whatever happened to match.

    With a baseline verdict there are three honest answers:

    ``clause 2.1``
        The candidate policy cites a clause, and that clause decides the case.
    ``clause 1.1 relaxed``
        The policy in force cited a clause that the candidate no longer applies.
        This is the change, described from the side that actually changed.
    ``(reviewer deviated from policy)``
        Both policies agree on this case, so the recorded outcome was never what
        the rulebook said. This is not a consequence of the proposal at all, and
        counting it as one overstates the impact of the change.

    The deviation test comes first, and the order matters. If both policies reach
    the same outcome then the proposal changed nothing here, whatever clause the
    candidate happens to cite on its way to the same answer - so crediting that
    clause would blame the new policy for a decision the old one would also have
    overturned.
    """
    if baseline is not None and baseline.outcome == candidate.outcome:
        return DEVIATION
    if candidate.policy_clause:
        return f"clause {candidate.policy_clause}"
    if baseline is not None and baseline.policy_clause:
        return f"clause {baseline.policy_clause} relaxed"
    return UNEXPLAINED


def flips(cases: list[Case], verdicts: dict[str, Verdict], domain: DomainConfig,
          baseline: dict[str, Verdict] | None = None) -> list[Flip]:
    """Cases where the proposed policy disagrees with what actually happened.

    ``baseline`` is the same mapping judged under the policy currently in force.
    It is optional - without it the diff is still correct, it just cannot say
    which clause relaxed, nor which outcomes were never policy-compliant in the
    first place. See :func:`attribute`.
    """
    out: list[Flip] = []
    for case in cases:
        v = verdicts.get(case.case_id)
        if v is None or v.outcome == case.actual_outcome:
            continue
        base = (baseline or {}).get(case.case_id)
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
                # Captured from the hydrated payload, so a segment on a
                # point-in-time fact records the value as of the decision date.
                segments=domain.segments_of(case.payload),
                baseline_outcome=base.outcome if base else "",
                attribution=attribute(v, base),
            )
        )
    return out


def select_for_review(flips_: list[Flip], domain: DomainConfig) -> list[Flip]:
    """Pick the handful of flips actually worth a human's time.

    Humans are the scarcest resource in this system, so spend them on the
    cases where the judge was unsure, the money is large, or the change makes
    the organisation more permissive than it chose to be.

    Two things then narrow that list further, and both exist because the
    obvious ranking - straight by impact - spends the budget badly:

    **Unconfirmed flips are dropped.** A flip the judge will not reproduce when
    asked again is the model changing its mind, not the policy moving. Turning
    one into permanent precedent writes noise into the only durable artefact
    this system has. Flips are only excluded once something has actually
    measured them (see :mod:`ptm.stability`); an unmeasured flip is not assumed
    guilty.

    **Deviations get a reserved minority of the slots, not the top of the
    list.** A deviation is a case both policies decide the same way, so the
    proposal did not cause it - it is a finding about the reviewers. They are
    reliably the largest flips by money, so ranking on impact alone lets them
    take most of the queue and the resulting precedents then fail the gate for
    the candidate, which had nothing to do with them. They still deserve a
    couple of slots, because the ruling settles a case the policy *already* in
    force gets wrong.
    """
    r = domain.review
    candidates = [
        f for f in flips_
        if f.confidence < r.below_confidence
        or (r.above_impact and f.impact >= r.above_impact)
        or f.direction in r.always_review_directions
    ]
    if r.exclude_unstable:
        candidates = [f for f in candidates if f.stability != "unstable"]
    candidates.sort(key=lambda f: (-f.impact, f.confidence))

    driven = [f for f in candidates if f.attribution != DEVIATION]
    deviating = [f for f in candidates if f.attribution == DEVIATION]
    reserved = min(max(r.max_deviation_reviews, 0), len(deviating))

    chosen = driven[: max(r.max_reviews - reserved, 0)]
    chosen += deviating[: min(reserved, max(r.max_reviews - len(chosen), 0))]
    # Deviations ran short of their reserve; hand the slot back rather than
    # waste it. The cap is a ceiling on deviations, never a floor, so the
    # backfill only ever comes from flips the proposal actually caused.
    if len(chosen) < r.max_reviews:
        taken = {f.case_id for f in chosen}
        chosen += [f for f in driven if f.case_id not in taken][: r.max_reviews - len(chosen)]
    chosen.sort(key=lambda f: (-f.impact, f.confidence))
    return chosen


def deviations(flips_: list[Flip]) -> list[Flip]:
    """Flips both policies agree on: a reviewer-consistency finding.

    These are not the proposal's doing and are reported separately from it, but
    they are not noise either - each one is a decision the rulebook already in
    force would have made differently.
    """
    return sorted((f for f in flips_ if f.attribution == DEVIATION),
                  key=lambda f: -f.impact)


def case_segment_rows(cases: list[Case], domain: DomainConfig) -> list[dict]:
    """One row per (case, segment field), for :func:`ptm.store.save_replay`.

    Recorded per case rather than pre-aggregated so overlapping runs replace
    each other instead of both counting. Values come from the hydrated payload,
    so a segment on a point-in-time fact is captured as of the decision date.
    """
    if not domain.segment_fields:
        return []
    return [
        {"case_id": case.case_id, "field": field, "value": value}
        for case in cases
        for field, value in domain.segments_of(case.payload).items()
    ]


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


def stale_precedents(precedents: list[Precedent], domain: DomainConfig,
                     version: str) -> list[dict]:
    """Rulings made about a clause that has since been rewritten.

    Precedent is permanent, which is the point and also the risk. A reviewer
    ruled on one case, under one candidate policy, against one clause of it. If
    that clause now reads differently, the ruling is still a fact about the case
    but it is no longer a fact about *this* policy's treatment of it - and the
    gate, which knows only the outcome, will hold every future version to it
    exactly as hard as to a ruling made this morning.

    This is a warning and deliberately not a gate. Nothing here says the ruling
    is wrong: a reviewer who said "deny" may well say "deny" again about the
    rewritten clause. It says the ruling has not been re-confirmed since the
    text it was about changed, which is a thing a person should decide rather
    than a thing a run should fail on.

    Precedents recorded before the circumstances were captured carry no
    ``policy_version`` and are reported as unknown rather than as fresh -
    silence about them would read as a clean bill of health.
    """
    out: list[dict] = []
    for p in precedents:
        if not p.policy_version:
            out.append({
                "case_id": p.case_id, "ruled_by": p.ruled_by,
                "established_at": p.established_at.date().isoformat(),
                "reason": "unknown", "ruled_under": "", "clause": p.judged_clause,
                "detail": "recorded before the ruling's circumstances were captured, so "
                          "there is no way to tell what policy text it was about",
            })
            continue
        if p.policy_version == version:
            continue
        if p.policy_version not in domain.policies:
            out.append({
                "case_id": p.case_id, "ruled_by": p.ruled_by,
                "established_at": p.established_at.date().isoformat(),
                "reason": "version_gone", "ruled_under": p.policy_version,
                "clause": p.judged_clause,
                "detail": f"ruled under policy {p.policy_version}, which no longer exists, "
                          f"so what the reviewer was shown cannot be recovered",
            })
            continue
        if not p.judged_clause:
            continue
        was = domain.clause_text(p.policy_version, p.judged_clause)
        now = domain.clause_text(version, p.judged_clause)
        if was and now and was != now:
            out.append({
                "case_id": p.case_id, "ruled_by": p.ruled_by,
                "established_at": p.established_at.date().isoformat(),
                "reason": "clause_changed", "ruled_under": p.policy_version,
                "clause": p.judged_clause, "was": was, "now": now,
                "detail": f"ruled against clause {p.judged_clause} of policy "
                          f"{p.policy_version}; that clause reads differently in {version}",
            })
    out.sort(key=lambda r: (r["reason"], r["case_id"]))
    return out


def describe_stale(stale: list[dict], version: str) -> str:
    """The staleness warning as the gate and the dashboard print it."""
    if not stale:
        return f"every ruling on file was made about policy {version}'s text as it stands"
    lines = [f"{len(stale)} ruling(s) may no longer be about the policy they were made "
             f"about, and are still enforced as though they were:"]
    for row in stale:
        lines.append(f"  {row['case_id']} ({row['ruled_by']}, {row['established_at']}): "
                     f"{row['detail']}")
    lines.append("  re-adjudicating one of these is how it stops being a guess; nothing "
                 "here fails a run, because whether a ruling still holds is a person's call.")
    return "\n".join(lines)


def precedent_conflicts(rows: list[dict], domain: DomainConfig) -> list[PrecedentConflict]:
    """Human rulings that contradict each other rather than the policy.

    The gate in :mod:`ptm.diff` protects precedent from the policy. This
    protects precedent from itself: if two reviewers gave materially identical
    cases different answers, the regression suite is now unsatisfiable and every
    future policy is guaranteed to "fail" one of them. Detection is a warning,
    not a gate - the fix is a conversation between two humans, not a code change.

    ``rows`` are :func:`ptm.store.precedents_with_payload` records.
    """
    if not domain.conflicts.key:
        return []
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        sig = domain.conflict_signature(row["payload"])
        if sig is not None:
            buckets[sig].append(row)

    conflicts: list[PrecedentConflict] = []
    for sig, group in buckets.items():
        by_outcome: dict[str, list[str]] = defaultdict(list)
        for row in group:
            by_outcome[row["correct_outcome"]].append(row["case_id"])
        if len(by_outcome) < 2:
            continue
        conflicts.append(
            PrecedentConflict(
                signature=[list(pair) for pair in sig],
                outcomes={k: sorted(v) for k, v in by_outcome.items()},
                case_ids=sorted(row["case_id"] for row in group),
                ruled_by=sorted({row["ruled_by"] for row in group}),
                # The reason each reviewer gave, carried through. A conflict is
                # resolved by two people talking, and this is the only thing in
                # the record that tells them what they are disagreeing about -
                # without it the panel can say they disagree and nothing more.
                notes=[{"case_id": row["case_id"], "ruled_by": row["ruled_by"],
                        "outcome": row["correct_outcome"], "note": row.get("note") or ""}
                       for row in sorted(group, key=lambda r: r["case_id"])],
            )
        )
    # Widest disagreement first: a three-way split is more urgent than a pair.
    conflicts.sort(key=lambda c: (-len(c.outcomes), -len(c.case_ids)))
    return conflicts


def clause_attribution(flips_: list[Flip], domain: DomainConfig) -> list[dict]:
    """Which clause produces which share of the change.

    Turns "147 decisions change" into "clause 1.1 accounts for 96 of them" -
    the difference between knowing a policy is expensive and knowing which
    sentence of it to rewrite. Buckets come from :func:`attribute`, so one of
    them may turn out to be the reviewers rather than the policy.
    """
    buckets: dict[str, list[Flip]] = defaultdict(list)
    for f in flips_:
        # Fall back to the raw clause for flips recorded before attribution
        # existed, so an old row still lands somewhere meaningful.
        fallback = f"clause {f.policy_clause}" if f.policy_clause else UNEXPLAINED
        buckets[f.attribution or fallback].append(f)

    total = len(flips_)
    out = []
    for clause, group in buckets.items():
        loosening = [f for f in group if f.direction == "loosening"]
        tightening = [f for f in group if f.direction == "tightening"]
        out.append({
            "clause": clause,
            # A deviation is not caused by the proposal, so any caller reporting
            # "the impact of this change" has to be able to exclude it.
            "policy_driven": clause != DEVIATION,
            "flips": len(group),
            "share": round(len(group) / total, 4) if total else 0.0,
            "loosening": len(loosening),
            "tightening": len(tightening),
            "impact_loosening": round(sum(f.impact for f in loosening), 2),
            "impact_tightening": round(sum(f.impact for f in tightening), 2),
            "net_impact": round(sum(f.impact for f in loosening) - sum(f.impact for f in tightening), 2),
            "mean_confidence": round(sum(f.confidence for f in group) / len(group), 3),
            "impact_unit": domain.impact_unit,
        })
    out.sort(key=lambda r: (-r["flips"], r["clause"]))
    return out


def segment_stats(cases: list[Case], flips_: list[Flip], domain: DomainConfig) -> list[dict]:
    """Blast radius: per segment value, how many cases and how many moved.

    ``cases`` supplies the denominator. Nine flips out of twelve travel claims
    is a different proposal from nine out of four hundred, and a count without
    its denominator invites exactly that mistake.
    """
    if not domain.segment_fields:
        return []
    counts: dict[tuple[str, str], dict] = {}

    def bucket(field: str, value: str) -> dict:
        return counts.setdefault((field, value), {
            "field": field, "value": value, "cases": 0, "flips": 0,
            "loosening": 0, "tightening": 0,
            "impact_loosening": 0.0, "impact_tightening": 0.0,
        })

    for case in cases:
        for field, value in domain.segments_of(case.payload).items():
            bucket(field, value)["cases"] += 1
    for f in flips_:
        # Fall back to re-deriving from the payload for flips built before
        # segments were captured, so an old row still lands in a bucket.
        segments = f.segments or domain.segments_of(f.payload)
        for field, value in segments.items():
            row = bucket(field, value)
            row["flips"] += 1
            if f.direction in ("loosening", "tightening"):
                row[f.direction] += 1
                row[f"impact_{f.direction}"] += f.impact

    out = []
    for row in counts.values():
        row["impact_loosening"] = round(row["impact_loosening"], 2)
        row["impact_tightening"] = round(row["impact_tightening"], 2)
        row["flip_rate"] = round(row["flips"] / row["cases"], 4) if row["cases"] else 0.0
        out.append(row)
    out.sort(key=lambda r: (r["field"], -r["flips"], r["value"]))
    return out


def summarise(flips_: list[Flip], total: int, domain: DomainConfig) -> dict:
    loosening = [f for f in flips_ if f.direction == "loosening"]
    tightening = [f for f in flips_ if f.direction == "tightening"]
    by_clause = clause_attribution(flips_, domain)
    policy_driven = [f for f in flips_ if f.attribution != DEVIATION]
    driven_loose = [f for f in policy_driven if f.direction == "loosening"]
    driven_tight = [f for f in policy_driven if f.direction == "tightening"]
    # The flip rate is a proportion measured on however many cases this run
    # saw, and a capped or monthly run sees few. Quoting it bare invites a
    # comparison between two months that differ by nothing but sample size, so
    # it carries its own sampling band - which is a different error bar from the
    # judge's noise floor and must not be confused with it. See ptm/stats.py.
    band = stats.rate(len(flips_), total)
    return {
        "cases_replayed": total,
        "flips": len(flips_),
        "flip_rate": band["rate"],
        "flip_rate_lo": band["rate_lo"],
        "flip_rate_hi": band["rate_hi"],
        "loosening": len(loosening),
        "tightening": len(tightening),
        "impact_unit": domain.impact_unit,
        "impact_loosening": round(sum(f.impact for f in loosening), 2),
        "impact_tightening": round(sum(f.impact for f in tightening), 2),
        "net_impact": round(sum(f.impact for f in loosening) - sum(f.impact for f in tightening), 2),
        "by_clause": by_clause,
        # The honest headline. Flips where both policies agree were never caused
        # by the proposal - they are reviewers having departed from the rulebook
        # they already had - and folding them into "the impact of v2" inflates it.
        "policy_driven_flips": len(policy_driven),
        "deviation_flips": len(flips_) - len(policy_driven),
        "policy_driven_net_impact": round(
            sum(f.impact for f in driven_loose) - sum(f.impact for f in driven_tight), 2),
    }
