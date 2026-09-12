"""Analytics that need no model: coverage, cohorts, and policy comparison.

These answer questions the flip list cannot:

    clause_coverage  - which rules of the proposed policy has history never
                       once exercised? Those are the ones you are shipping
                       untested, and no amount of replay will tell you about
                       them, because the whole point is that nothing hit them.

    cohort_impact    - who bears the change? "Costs GBP 12,014" is an
                       incomplete answer if it all lands on one grade.

    compare          - two candidate policies against the same history and the
                       same precedents.

Deliberately deterministic. The model's job is to explain; this is arithmetic,
and arithmetic should not be sampled.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import store
from .config import DomainConfig


# ------------------------------------------------------------------ coverage


def clause_coverage(domain: DomainConfig, version: str) -> dict:
    """Which clauses the replay actually exercised, and which it never reached.

    An unexercised clause is not necessarily wrong - it may cover a case that
    genuinely never arose - but it is unevidenced, and shipping it is a guess.
    """
    declared = domain.declared_clauses(version)
    used = {
        r["policy_clause"]: r["n"]
        for r in store.query(
            "SELECT policy_clause, COUNT(DISTINCT case_id) n FROM verdicts "
            "WHERE domain=? AND policy_version=? AND policy_clause <> '' "
            "GROUP BY policy_clause",
            (domain.name, version),
        )
    }
    total = store.query(
        "SELECT COUNT(DISTINCT case_id) n FROM verdicts WHERE domain=? AND policy_version=?",
        (domain.name, version),
    )[0]["n"]
    unruled = store.query(
        "SELECT COUNT(DISTINCT case_id) n FROM verdicts "
        "WHERE domain=? AND policy_version=? AND policy_clause = ''",
        (domain.name, version),
    )[0]["n"]

    clauses = [
        {
            "clause": c,
            "cases": used.get(c, 0),
            "share": round(used.get(c, 0) / total, 4) if total else 0.0,
            "exercised": c in used,
        }
        for c in declared
    ]
    # A clause the judge cited that the policy text does not define means the
    # two have drifted apart - worth surfacing rather than quietly dropping.
    undeclared = sorted(set(used) - set(declared))
    return {
        "version": version,
        "cases": total,
        "declared": len(declared),
        "exercised": sum(1 for c in clauses if c["exercised"]),
        "clauses": clauses,
        "unexercised": [c["clause"] for c in clauses if not c["exercised"]],
        "undeclared": [{"clause": c, "cases": used[c]} for c in undeclared],
        "decided_by_no_clause": unruled,
        "no_clause_share": round(unruled / total, 4) if total else 0.0,
    }


# ------------------------------------------------------------------- cohorts


def cohort_impact(domain: DomainConfig, version: str, field: str,
                  until: datetime | None = None) -> dict:
    """Break the change down by one attribute of the case's subject.

    Cases are loaded point-in-time, so a cohort is the value as it stood on the
    day - an employee promoted last year is counted in the grade they held when
    the claim was made, which is the only honest way to attribute the effect.
    """
    cases = store.load_cases(domain.name, until=until or datetime.now())
    flips = {r["case_id"]: r for r in store.flips_for_policy(domain.name, version)}
    judged = {
        r["case_id"]
        for r in store.query(
            "SELECT DISTINCT case_id FROM verdicts WHERE domain=? AND policy_version=?",
            (domain.name, version),
        )
    }

    buckets: dict[str, dict[str, Any]] = {}
    for c in cases:
        if c.case_id not in judged:
            continue  # never replayed under this policy; not evidence either way
        key = str(c.payload.get(field, "unknown"))
        b = buckets.setdefault(key, {"cohort": key, "cases": 0, "flips": 0,
                                     "loosening": 0, "tightening": 0, "net_impact": 0.0})
        b["cases"] += 1
        f = flips.get(c.case_id)
        if not f:
            continue
        b["flips"] += 1
        b[f["direction"]] = b.get(f["direction"], 0) + 1
        b["net_impact"] += f["impact"] if f["direction"] == "loosening" else -f["impact"]

    rows = sorted(buckets.values(), key=lambda b: -b["cases"])
    overall_cases = sum(b["cases"] for b in rows)
    overall_flips = sum(b["flips"] for b in rows)
    baseline = overall_flips / overall_cases if overall_cases else 0.0

    for b in rows:
        b["flip_rate"] = round(b["flips"] / b["cases"], 4) if b["cases"] else 0.0
        b["net_impact"] = round(b["net_impact"], 2)
        b["per_case_impact"] = round(b["net_impact"] / b["cases"], 2) if b["cases"] else 0.0
        # How far this cohort's flip rate sits from the population's. 1.0 is
        # proportionate; 2.0 means twice as likely to be affected.
        b["disproportion"] = round(b["flip_rate"] / baseline, 2) if baseline else 0.0

    return {
        "field": field,
        "baseline_flip_rate": round(baseline, 4),
        "cases": overall_cases,
        "cohorts": rows,
        "impact_unit": domain.impact_unit,
    }


def cohort_report(domain: DomainConfig, version: str) -> list[dict]:
    """Every cohort breakdown the domain declares."""
    return [cohort_impact(domain, version, f) for f in domain.cohort_fields]


def disproportionate(report: list[dict], threshold: float = 1.5,
                     min_cases: int = 20) -> list[dict]:
    """Cohorts the change lands on hardest, worth naming in a brief.

    ``min_cases`` keeps a cohort of three claims from reading as a finding.
    """
    out = []
    for breakdown in report:
        for c in breakdown["cohorts"]:
            if c["cases"] >= min_cases and c["disproportion"] >= threshold:
                out.append({"field": breakdown["field"], **c})
    return sorted(out, key=lambda c: -c["disproportion"])


# ----------------------------------------------------------------- comparison


def compare(domain: DomainConfig, left: str, right: str) -> dict:
    """Two candidate policies, against the same history and the same precedents."""
    precedents = store.load_precedents(domain.name)
    by_case = {p.case_id: p for p in precedents}

    def side(version: str) -> dict:
        s = store.policy_summary(domain.name, version)
        s["impact_unit"] = domain.impact_unit
        verdicts = {
            r["case_id"]: r["outcome"]
            for r in store.query(
                "SELECT case_id, outcome FROM verdicts WHERE domain=? AND policy_version=? "
                "GROUP BY case_id HAVING MAX(created_at)",
                (domain.name, version),
            )
        }
        checked = [cid for cid in by_case if cid in verdicts]
        violations = [cid for cid in checked if by_case[cid].correct_outcome != verdicts[cid]]
        coverage = clause_coverage(domain, version)
        return {
            "version": version,
            **s,
            "precedents_checked": len(checked),
            "violations": len(violations),
            "violating_cases": sorted(violations),
            "clauses_declared": coverage["declared"],
            "clauses_exercised": coverage["exercised"],
            "unexercised": coverage["unexercised"],
            "outcomes": verdicts,
        }

    a, b = side(left), side(right)
    shared = set(a["outcomes"]) & set(b["outcomes"])
    disagreements = sorted(c for c in shared if a["outcomes"][c] != b["outcomes"][c])

    # Strip the per-case maps out of the payload; they exist only to diff.
    for s in (a, b):
        s.pop("outcomes")

    return {
        "left": a,
        "right": b,
        "compared_cases": len(shared),
        "disagreements": len(disagreements),
        "disagreement_cases": disagreements[:50],
        "verdict": _which_is_safer(a, b),
        "precedents_on_file": len(precedents),
        # Precedents are established while reviewing one particular proposal, so
        # they carry its framing. Judging an older policy against them is not a
        # like-for-like test, and the reader should know that before concluding
        # the older policy is worse.
        "caveat": (
            "Precedents were established while reviewing a specific proposal, so they reflect the "
            "cases that proposal surfaced. A version that predates them is judged on questions it "
            "was never asked."
            if precedents else ""
        ),
        "impact_unit": domain.impact_unit,
    }


def _which_is_safer(a: dict, b: dict) -> str:
    """A one-line read on the comparison. Precedent first, then cost.

    "Fewer violations" is never allowed to read as an endorsement: a policy that
    reverses one human ruling still cannot ship, and saying which of two failing
    policies fails less would invite exactly that misreading.
    """
    if a["violations"] and b["violations"]:
        return (f"neither can ship as written - {a['version']} reverses {a['violations']} "
                f"established precedent(s) and {b['version']} reverses {b['violations']}")
    if a["violations"] or b["violations"]:
        bad = a if a["violations"] else b
        ok = b if a["violations"] else a
        return (f"{bad['version']} cannot ship: it reverses {bad['violations']} established "
                f"precedent(s). {ok['version']} reverses none")
    if a["net_impact"] != b["net_impact"]:
        cheaper = a if a["net_impact"] < b["net_impact"] else b
        return (f"neither reverses precedent; {cheaper['version']} costs "
                f"{abs(a['net_impact'] - b['net_impact']):,.0f} less")
    return "the two are equivalent on precedent and on cost"
