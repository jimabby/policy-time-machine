"""Read models behind the Diff Explorer's API.

These are plain functions returning plain dicts. The FastAPI plugin is only
routing and status codes on top of them, which means the whole API surface is
testable without installing a web framework - and usable from a script or a
notebook without starting Airflow.

Unknown domains and versions raise :class:`LookupError`; the plugin turns that
into a 404. Nothing here writes.
"""

from __future__ import annotations

import json

from . import cost, diff, store
from .config import JUDGE_MODEL, DomainConfig, available_domains, load_domain


def _domain(name: str) -> DomainConfig:
    try:
        return load_domain(name)
    except FileNotFoundError as exc:
        raise LookupError(f"Unknown domain: {name}") from exc


def _checked(name: str, version: str) -> DomainConfig:
    config = _domain(name)
    if version not in config.policies:
        raise LookupError(f"Unknown policy version: {version}")
    return config


def domains() -> list[dict]:
    out = []
    for name in available_domains():
        d = load_domain(name)
        out.append({"name": name, "label": d.label, "outcomes": d.outcomes,
                    "policies": sorted(d.policies), "impact_unit": d.impact_unit,
                    "in_force": d.in_force, "segment_fields": d.segment_fields,
                    "conflict_key": d.conflicts.key})
    return out


def summary(domain: str, version: str) -> dict:
    config = _checked(domain, version)
    runs = store.query(
        "SELECT COUNT(*) runs FROM runs WHERE domain=? AND policy_version=?",
        (domain, version))[0]
    verdicts = store.query(
        """SELECT COUNT(*) n FROM (
             SELECT case_id, MAX(rowid) FROM verdicts
             WHERE domain=? AND policy_version=? GROUP BY case_id
           )""", (domain, version))[0]
    dirs = store.query(
        """SELECT f.direction, COUNT(*) n, SUM(f.impact) impact FROM flips f
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM flips
                 WHERE domain=? AND policy_version=? GROUP BY case_id) latest
             ON latest.latest_rowid=f.rowid
           GROUP BY f.direction""", (domain, version))
    precedent_count = store.query(
        "SELECT COUNT(*) n FROM precedents WHERE domain=?", (domain,))[0]["n"]
    net_impact = sum(
        (row["impact"] or 0) if row["direction"] == "loosening" else -(row["impact"] or 0)
        for row in dirs if row["direction"] in {"loosening", "tightening"})

    # Separate what the proposal causes from what it merely reveals: a flip both
    # policies agree on was a reviewer departing from the rulebook they already
    # had, and charging it to the proposal overstates the proposal.
    clauses = store.clause_breakdown(domain, version)
    policy_driven = sum(r["flips"] for r in clauses if r["policy_driven"])
    driven_net = sum((r["impact_loosening"] or 0) - (r["impact_tightening"] or 0)
                     for r in clauses if r["policy_driven"])
    baselines = store.query(
        """SELECT DISTINCT baseline_version FROM runs
           WHERE domain=? AND policy_version=? AND baseline_version <> ''""",
        (domain, version))

    return {
        "runs": runs["runs"] or 0,
        "cases": verdicts["n"] or 0,
        "flips": sum(row["n"] for row in dirs),
        "net_impact": round(net_impact, 2),
        "by_direction": dirs,
        "precedents": precedent_count,
        "impact_unit": config.impact_unit,
        "policy_driven_flips": policy_driven,
        "deviation_flips": sum(r["flips"] for r in clauses if not r["policy_driven"]),
        "policy_driven_net_impact": round(driven_net, 2),
        "baseline_versions": sorted(b["baseline_version"] for b in baselines),
        "precedent_conflicts": len(conflicts(domain)),
        "stability": store.latest_stability(domain, version),
    }


def flips(domain: str, version: str, limit: int = 200) -> list[dict]:
    _checked(domain, version)
    rows = store.query(
        """SELECT f.case_id, f.actual_outcome, f.new_outcome, f.direction, f.impact,
                  f.confidence, f.rationale, f.reviewed, f.policy_clause, f.attribution,
                  f.baseline_outcome, f.segments, c.decided_at, c.payload,
                  c.actual_rationale,
                  (SELECT correct_outcome FROM precedents p
                    WHERE p.case_id = f.case_id AND p.domain = f.domain) AS precedent
           FROM flips f JOIN cases c ON c.case_id = f.case_id
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM flips
                 WHERE domain=? AND policy_version=? GROUP BY case_id) latest
             ON latest.latest_rowid=f.rowid
           ORDER BY f.impact DESC LIMIT ?""",
        (domain, version, limit))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
        r["segments"] = json.loads(r["segments"] or "{}")
    return rows


def clauses(domain: str, version: str) -> list[dict]:
    """Which clause is responsible for which share of the change."""
    _checked(domain, version)
    return store.clause_breakdown(domain, version)


def segments(domain: str, version: str) -> list[dict]:
    """Blast radius: who the change lands on, with denominators."""
    _checked(domain, version)
    rows = store.segment_breakdown(domain, version)
    for r in rows:
        r["flip_rate"] = round((r["flips"] or 0) / r["cases"], 4) if r["cases"] else 0.0
        r["net_impact"] = round((r["impact_loosening"] or 0) - (r["impact_tightening"] or 0), 2)
    return rows


def compare(domain: str, left: str, right: str, limit: int = 200) -> dict:
    """Two candidate policies side by side: did the edit to clause 1.1 help?"""
    config = _checked(domain, left)
    _checked(domain, right)
    result = store.compare_versions(domain, left, right)
    result["differences"] = result["differences"][:limit]
    result["impact_unit"] = config.impact_unit
    return result


def cost_report(domain: str, version: str) -> dict:
    """What judging has cost, and what a full replay would cost.

    The ledger is what happened. The forecast is what a complete point-in-time
    replay of every case on file would cost against the configured model - the
    number worth seeing *before* launching a backfill. Both are estimates
    measured from prompt size rather than read back from the vendor; ptm/cost.py
    says why, and how wrong that can be.
    """
    config = _checked(domain, version)
    n_cases = store.query("SELECT COUNT(*) n FROM cases WHERE domain=?", (domain,))[0]["n"]
    sample = store.query("SELECT payload FROM cases WHERE domain=? LIMIT 50", (domain,))
    case_chars = 0
    if sample:
        case_chars = sum(
            len(config.render_case(json.loads(r["payload"]))) for r in sample) // len(sample)
    forecast = cost.estimate_backfill(n_cases, len(config.policy_text(version)),
                                      case_chars, JUDGE_MODEL)
    return {
        "ledger": store.cost_ledger(domain, version),
        "forecast": {**forecast, "cases": n_cases,
                     "with_baseline_pass_usd": round(forecast["estimated_cost_usd"] * 2, 4)},
        "judge_model": JUDGE_MODEL,
    }


def stability(domain: str, version: str) -> dict:
    """The error bar on this policy's flip rate."""
    _checked(domain, version)
    latest = store.latest_stability(domain, version)
    if not latest:
        return {"measured": False,
                "hint": f"run the judge_stability_{domain} DAG to put an error bar "
                        f"on this policy's flip rate"}
    rows = store.query(
        """SELECT case_id, outcome, COUNT(*) n FROM judge_samples
           WHERE run_id=? GROUP BY case_id, outcome ORDER BY case_id""",
        (latest["run_id"],))
    by_case: dict[str, dict] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], {})[row["outcome"]] = row["n"]
    return {"measured": True, **latest,
            "disagreeing_cases": {k: v for k, v in by_case.items() if len(v) > 1}}


def conflicts(domain: str) -> list[dict]:
    """Human rulings that contradict each other rather than the policy."""
    config = _domain(domain)
    return [c.model_dump(mode="json")
            for c in diff.precedent_conflicts(store.precedents_with_payload(domain), config)]


def precedents(domain: str) -> list[dict]:
    _domain(domain)
    return store.query(
        "SELECT * FROM precedents WHERE domain=? ORDER BY established_at DESC", (domain,))
