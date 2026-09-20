"""Read models behind the Diff Explorer's API.

These are plain functions returning plain dicts. The FastAPI plugin is only
routing and status codes on top of them, which means the whole API surface is
testable without installing a web framework - and usable from a script or a
notebook without starting Airflow.

Unknown domains and versions raise :class:`LookupError`; the plugin turns that
into a 404. Nothing here writes.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import sys
from datetime import datetime

from . import calibration as calibration_engine
from . import cli, cost, diff, provenance, stats, store
from . import crosscheck as crosscheck_engine
from . import disparity as disparity_engine
from . import preflight as preflight_engine
from . import rules as rules_engine
from . import sweep as sweep_engine
from .config import JUDGE_MODEL, DomainConfig, available_domains, load_domain
from .models import Flip, Verdict


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
                    "conflict_key": d.conflicts.key,
                    # Versions a model drafted, listed separately so no caller
                    # can present one as a policy somebody approved.
                    "draft_versions": sorted(d.draft_versions)})
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
        """SELECT f.direction, COUNT(*) n, SUM(f.impact) impact FROM current_flips f
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM current_flips
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

    # A rate measured on this many cases, with the band that comes from the
    # sample size alone. It is not the judge's noise floor and the two must not
    # be added together - ptm/stats.py says which is which.
    band = stats.rate(sum(row["n"] for row in dirs), verdicts["n"] or 0)
    return {
        "runs": runs["runs"] or 0,
        "cases": verdicts["n"] or 0,
        "flips": sum(row["n"] for row in dirs),
        "flip_rate": band["rate"],
        "flip_rate_lo": band["rate_lo"],
        "flip_rate_hi": band["rate_hi"],
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
        "cross_check": store.latest_cross_check(domain, version),
        "flip_confirmation": confirmation_summary(domain, version),
        "is_draft": version in config.draft_versions,
    }


def confirmation_summary(domain: str, version: str) -> dict:
    """How much of the flip set has been re-judged, and how much of it held.

    Reported even when nothing has been measured, because "not measured" is the
    honest answer and an absent field reads like a clean bill of health.
    """
    measured = store.flip_stability(domain, version)
    if not measured:
        return {"measured": 0, "stable": 0, "unstable": 0,
                "hint": f"run judge_stability_{domain} with target=flips to put an error "
                        f"bar on individual flips before a human rules on them",
                "hint_key": "hint.flip_confirmation", "hint_args": {"domain": domain}}
    unstable = [r for r in measured.values() if not r["stable"]]
    return {
        "measured": len(measured),
        "stable": len(measured) - len(unstable),
        "unstable": len(unstable),
        "unstable_cases": sorted(r["case_id"] for r in unstable),
    }


def _flip_models(domain: str, version: str) -> list[Flip]:
    """The deduplicated flip rows as :class:`~ptm.models.Flip` objects."""
    out = []
    for r in store.flips_for_policy(domain, version):
        out.append(Flip(
            case_id=r["case_id"], decided_at=datetime.fromisoformat(r["decided_at"]),
            actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
            rationale=r["rationale"], confidence=r["confidence"],
            policy_clause=r["policy_clause"] or "", impact=r["impact"],
            payload=json.loads(r["payload"]), direction=r["direction"],
            segments=json.loads(r["segments"] or "{}"),
            attribution=r["attribution"] or "",
            baseline_outcome=r["baseline_outcome"] or "",
            stability=r.get("stability") or "",
        ))
    return out


def deviations(domain: str, version: str, limit: int = 200) -> dict:
    """Recorded outcomes that disagree with the policy already in force.

    Separated from the proposal's own impact because they are a different
    finding with a different owner: the proposal did not cause these, and the
    rulebook already in force would not have produced them either - somebody
    departed from it. Folding them into "the impact of v2" overstates v2;
    dropping them loses a real and quantified problem.
    """
    config = _checked(domain, version)
    rows = [f for f in _flip_models(domain, version) if f.attribution == diff.DEVIATION]
    rows.sort(key=lambda f: -f.impact)
    return {
        "domain": domain,
        "version": version,
        "in_force": config.in_force,
        "impact_unit": config.impact_unit,
        "count": len(rows),
        "total_impact": round(sum(f.impact for f in rows), 2),
        "cases": [f.model_dump(mode="json", exclude={"payload"}) for f in rows[:limit]],
    }


def precedent_check(domain: str, version: str) -> dict:
    """Which precedents this policy reverses - and which the status quo already does.

    Read from stored verdicts rather than by re-judging, so it costs nothing.
    A precedent with no verdict on file is reported as *unchecked* rather than
    silently passing, which is the exact failure the gate exists to prevent.
    """
    config = _checked(domain, version)
    precedents = store.load_precedents(domain)
    if not precedents:
        return {"precedents": 0, "checked": 0, "unchecked": [], "violations": [],
                "in_force": config.in_force, "in_force_violations": [], "introduced": [],
                "hint": "no precedents yet; run the adjudication DAG",
                "hint_key": "hint.no_precedents"}

    wanted = {p.case_id for p in precedents}

    def under(version_name: str):
        stored = store.latest_verdicts(domain, version_name)
        verdicts = {
            case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                             confidence=row["confidence"],
                             policy_clause=row["policy_clause"] or "")
            for case_id, row in stored.items() if case_id in wanted
        }
        missing = sorted(wanted - set(verdicts))
        return diff.precedent_violations(verdicts, precedents), missing

    found, unchecked = under(version)
    in_force_found = found if config.in_force == version else under(config.in_force)[0]
    pre_existing = {v["case_id"] for v in in_force_found}
    # Whether the oracle is still about this policy. Reported next to the
    # violations rather than in a panel of its own, because the number that
    # matters is how many of *these* reversals rest on a ruling that has not
    # been re-confirmed since the clause it was about changed.
    stale = diff.stale_precedents(precedents, config, version)
    stale_ids = {r["case_id"] for r in stale}
    return {
        "precedents": len(precedents),
        "checked": len(precedents) - len(unchecked),
        "unchecked": unchecked,
        "violations": [{**v, "stale": v["case_id"] in stale_ids} for v in found],
        "in_force": config.in_force,
        "in_force_violations": in_force_found,
        # The honest headline: what this proposal is responsible for breaking.
        "introduced": [v for v in found if v["case_id"] not in pre_existing],
        "stale": stale,
        "stale_summary": diff.describe_stale(stale, version),
    }


def thresholds(domain: str, version: str) -> list[dict]:
    """The numeric dials a sweep can move in this version's offline rules."""
    config = _checked(domain, version)
    return sweep_engine.thresholds(config, version)


def _rules_check(domain: str, version: str) -> dict:
    """Whether the rules a sweep is about to be computed from still implement the policy.

    Attached to every sweep result rather than left on its own panel. The sweep
    is arithmetic over the offline rules, so it is worth exactly what the rules
    are worth - and a curve that arrives with no statement about that is a curve
    somebody will read as being about the policy.
    """
    config = _checked(domain, version)
    try:
        result = rule_agreement(domain, version)
    except LookupError:
        return {"measured": False}
    problems = rules_engine.gate(result, config)
    return {
        "measured": bool(result.get("compared")),
        "inert": bool(result.get("inert")),
        "agreement": result.get("rate"),
        "clause_agreement": result.get("clause_agreement"),
        "compared": result.get("compared", 0),
        "gate": config.rules.gate,
        "problems": problems,
        "hint": result.get("hint", ""),
        # Forwarded, not restated: the sweep panel shows the rules panel's hint,
        # and a key that stops travelling with it silently drops back to English.
        "hint_key": result.get("hint_key", ""),
        "hint_args": result.get("hint_args", {}),
    }


def sweep(domain: str, version: str, field: str, values: list[float] | str,
          clause: str = "") -> dict:
    """Re-run the replay at each candidate threshold. See :mod:`ptm.sweep`.

    ``values`` may be a list or the raw comma-separated string a query string
    carries. Parsing it here rather than in the plugin is what keeps the plugin
    to routing alone, and therefore keeps this endpoint covered by a test suite
    that does not install FastAPI.

    Refuses outright when the rules have drifted past the domain's
    ``rules.gate`` of ``fail``. A sweep is arithmetic over the offline rules, so
    serving one built on rules that no longer agree with the judge hands back a
    curve about the fixture wearing the policy's name.
    """
    config = _checked(domain, version)
    if isinstance(values, str):
        try:
            values = sweep_engine.parse_values(values)
        except ValueError as exc:
            raise LookupError(f"values must be comma-separated numbers: {exc}") from exc
    if not values:
        raise LookupError("a sweep needs at least one value")
    if len(values) > 40:
        raise LookupError(f"{len(values)} values is more than one sweep will run; cap is 40")
    check = _rules_check(domain, version)
    if check["problems"] and config.rules.gate == "fail":
        raise LookupError("; ".join(check["problems"]))
    return {**sweep_engine.sweep(domain, version, field, values, clause=clause),
            "rules_check": check}


#: Ceiling on a grid. |A| x |B| full replays is cheap per point and not cheap
#: at 400 of them, and an endpoint anyone can call is not the place to find that
#: out. Two single sweeps are how a range gets narrowed to something this size.
MAX_GRID_POINTS = 64


def joint_sweep(domain: str, version: str, first_field: str, first_values: list | str,
                second_field: str, second_values: list | str,
                first_clause: str = "", second_clause: str = "") -> dict:
    """Two dials at once. See :func:`ptm.sweep.joint` for why a grid, not two curves."""
    _checked(domain, version)
    axes = []
    for field, raw, clause in ((first_field, first_values, first_clause),
                               (second_field, second_values, second_clause)):
        values = raw
        if isinstance(values, str):
            try:
                values = sweep_engine.parse_values(values)
            except ValueError as exc:
                raise LookupError(
                    f"values for {field} must be comma-separated numbers: {exc}") from exc
        if not values:
            raise LookupError(f"the {field} axis needs at least one value")
        axes.append({"field": field, "values": values, "clause": clause})
    points = len(axes[0]["values"]) * len(axes[1]["values"])
    if points > MAX_GRID_POINTS:
        raise LookupError(
            f"{points} grid points is more than one request will run; the cap is "
            f"{MAX_GRID_POINTS}. Narrow each axis with a single sweep first.")
    check = _rules_check(domain, version)
    if check["problems"] and load_domain(domain).rules.gate == "fail":
        raise LookupError("; ".join(check["problems"]))
    return {**sweep_engine.joint(domain, version, axes[0], axes[1]),
            "rules_check": check}


FLIP_COLUMNS = ["case_id", "decided_at", "actual_outcome", "new_outcome",
                "baseline_outcome", "direction", "attribution", "policy_clause",
                "impact", "confidence", "stability", "reviewed", "precedent",
                "rationale", "actual_rationale"]


def flips_csv(domain: str, version: str, limit: int = 5000) -> str:
    """The flip set as CSV, for the spreadsheet the decision gets argued in."""
    config = _checked(domain, version)
    rows = flips(domain, version, limit=limit)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=[*FLIP_COLUMNS, *config.segment_fields],
                            extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for r in rows:
        writer.writerow({**r, **{f: r["segments"].get(f, "") for f in config.segment_fields}})
    return buffer.getvalue()


def export_bundle(domain: str, version: str, limit: int = 5000) -> dict:
    """Everything the Diff Explorer shows, in one downloadable object.

    A policy decision is argued about away from the dashboard, so the numbers
    have to be able to leave it - together, and with the caveats attached
    rather than stripped off by whoever pastes them into a slide.
    """
    config = _checked(domain, version)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "domain": domain,
        "label": config.label,
        "policy_version": version,
        "in_force": config.in_force,
        "impact_unit": config.impact_unit,
        "summary": summary(domain, version),
        "clauses": clauses(domain, version),
        "segments": segments(domain, version),
        "deviations": deviations(domain, version),
        "precedent_check": precedent_check(domain, version),
        "conflicts": conflicts(domain),
        "precedents": precedents(domain),
        "cost": cost_report(domain, version),
        "stability": stability(domain, version),
        "cross_check": cross_check(domain, version),
        "calibration": calibration(domain, version),
        "disparity": disparity(domain, version),
        "preflight": preflight(domain, version),
        "rule_agreement": rule_agreement(domain, version),
        "power": power(domain, version),
        "coverage": provenance.coverage(domain, version),
        "flips": flips(domain, version, limit=limit),
        "caveats": [
            "Impact is the value of the cases whose outcome changes, not a cash-flow "
            "forecast.",
            "Cost figures marked estimated_* are measured from prompt size at a fixed "
            "4 characters per token. actual_* are the vendor's own counts, and only "
            "exist for runs judged by a real model; 'reconciliation' scores one against "
            "the other.",
            "Flips attributed to " + diff.DEVIATION + " are cases the policy already in "
            "force decided differently too, and are not caused by this proposal.",
            "A flip rate quoted without the judge stability figure is quoted without an "
            "error bar.",
            "Judge self-consistency is satisfied by a judge that misreads a clause the "
            "same way every time. 'cross_check' is the only figure here that asks a "
            "second, independent judge - and where two judges split, the disagreement is "
            "the finding, not either answer.",
            "flip_rate_lo/hi is sampling error - how much this rate could move on a "
            "different sample of the same size. It is a different band from the judge's "
            "noise floor and the two do not add.",
            "Judge accuracy is measured against precedents, which are the contested "
            "flips. It is a floor on the judge's accuracy over all cases, not an "
            "estimate of it.",
            "A segment carrying more of the change than the rest of its field is a "
            "question to answer, not a finding of unfairness.",
            "'power' is what this many cases could have detected. A difference smaller "
            "than it is not a finding, whichever direction it points in.",
        ],
    }


def flips(domain: str, version: str, limit: int = 200,
          offset: int = 0, search: str = "") -> list[dict]:
    _checked(domain, version)
    rows = store.query(
        """SELECT f.case_id, f.actual_outcome, f.new_outcome, f.direction, f.impact,
                  f.confidence, f.rationale, f.reviewed, f.policy_clause, f.attribution,
                  f.baseline_outcome, f.segments, f.stability, c.decided_at, c.payload,
                  c.actual_rationale,
                  (SELECT correct_outcome FROM precedents p
                    WHERE p.case_id = f.case_id AND p.domain = f.domain) AS precedent
           FROM current_flips f JOIN cases c ON c.case_id = f.case_id
           JOIN (SELECT case_id, MAX(rowid) latest_rowid FROM current_flips
                 WHERE domain=? AND policy_version=? GROUP BY case_id) latest
             ON latest.latest_rowid=f.rowid
           WHERE instr(lower(f.case_id), lower(?)) > 0
           ORDER BY f.impact DESC, f.case_id LIMIT ? OFFSET ?""",
        (domain, version, search, limit, offset))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
        r["segments"] = json.loads(r["segments"] or "{}")
    return rows


def flip_page(domain: str, version: str, limit: int = 200,
              offset: int = 0, search: str = "") -> dict:
    _checked(domain, version)
    counts = store.query("""SELECT COUNT(DISTINCT case_id) total,
        COUNT(DISTINCT CASE WHEN instr(lower(case_id),lower(?))>0 THEN case_id END) matched
        FROM current_flips WHERE domain=? AND policy_version=?""", (search, domain, version))[0]
    return {**counts, "offset": offset, "limit": limit,
            "items": flips(domain, version, limit, offset, search)}


def coverage(domain: str, version: str) -> dict:
    _checked(domain, version)
    return provenance.coverage(domain, version)


def snapshots(domain: str, version: str) -> list[dict]:
    _domain(domain)
    return provenance.snapshots(domain, version, include_inputs=True)


def review_case(domain: str, version: str, case_id: str) -> dict:
    """The evidence a reviewer needs, with archived inputs when available."""
    import re

    config = _checked(domain, version)
    cases = store.load_cases(domain, until=datetime.max, case_ids=[case_id])
    if not cases:
        raise LookupError(f"unknown case {case_id!r}")
    case = cases[0].model_dump(mode="json")
    pointers = store.query("SELECT run_id FROM replay_cases WHERE domain=? AND policy_version=? AND case_id=?",
                           (domain, version, case_id))
    run = pointers[0]["run_id"] if pointers else None
    archived = store.query("SELECT snapshot FROM replay_snapshots WHERE run_id=?", (run,)) if run else []
    snap = json.loads(archived[0]["snapshot"]) if archived else None
    if snap:
        case = next((c for c in snap["inputs"] if c["case_id"] == case_id), case)
    candidate = store.latest_verdicts(domain, version).get(case_id)
    baseline_version = snap["baseline_version"] if snap else config.in_force
    baseline = store.latest_verdicts(domain, baseline_version).get(case_id)
    if run:
        for suffix, target in (("", "candidate"), (store.BASELINE_RUN_SUFFIX, "baseline")):
            rows = store.query("SELECT * FROM verdicts WHERE run_id=? AND domain=? AND case_id=?",
                               (run + suffix, domain, case_id))
            if rows:
                if target == "candidate":
                    candidate = rows[0]
                else:
                    baseline = rows[0]
    if snap:
        candidate = snap.get("verdicts", {}).get(case_id, candidate)
        baseline = snap.get("baseline_verdicts", {}).get(case_id, baseline)

    def policy_side(verdict, policy_version, archived_policy):
        text = archived_policy["policy_text"] if archived_policy else (
            config.policy_text(policy_version) if policy_version in config.policies else "")
        clause = (verdict or {}).get("policy_clause", "")
        match = re.search(rf"^\s*{re.escape(clause)}\s+(.*?)(?=^\s*\d+\.\d+\s|^#|\Z)",
                          text, re.MULTILINE | re.DOTALL) if clause else None
        return {"version": policy_version, "verdict": verdict, "policy_text": text,
                "clause": clause, "clause_text": match.group(1).strip() if match else ""}

    cross = store.latest_cross_check(domain, version)
    disagreements = (cross or {}).get("report", {}).get("disagreements", [])
    return {"case": case, "run_id": run, "archived_inputs": bool(snap),
            "candidate": policy_side(candidate, version, snap["policy"] if snap else None),
            "baseline": policy_side(baseline, baseline_version, snap.get("baseline") if snap else None),
            "rulings": [p.model_dump(mode="json") for p in store.load_precedents(domain) if p.case_id == case_id],
            "history": store.precedent_history(domain, case_id),
            "stability": store.flip_stability(domain, version).get(case_id),
            "disagreement": next((d for d in disagreements if d["case_id"] == case_id), None),
            "cross_check_at": (cross or {}).get("created_at"),
            "review_dag": f"adjudicate_{domain}",
            "note": "Record rulings in the Airflow human review task. Secondary-model evidence "
                    "is from its separately dated measurement; absence of a recorded disagreement "
                    "does not establish agreement."}


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
    # What the cache has already saved, and what it would save on a re-run. The
    # forecast above is the cost of judging every case; the cache is the reason
    # the *second* measurement of an edited policy need not cost that again.
    cached = store.cache_stats(domain, version)
    cached["estimated_saved_usd"] = cost.estimate(
        int(cached["hit_prompt_chars"] or 0), int(cached["hits"] or 0),
        JUDGE_MODEL)["estimated_cost_usd"] if cached["hits"] else 0.0
    ledger = store.cost_ledger(domain, version)
    # Score the forecast against what was actually billed, where anything
    # measured it. Reported as a check rather than folded into the ledger: the
    # estimate is what a reader compares the next backfill against, so replacing
    # it would destroy the only evidence of how good it is.
    check = cost.reconcile(
        {"estimated_input_tokens": ledger["input_tokens"],
         "estimated_cost_usd": ledger["cost_usd"],
         "actual_input_tokens": ledger["actual_input_tokens"],
         "actual_cost_usd": ledger["actual_cost_usd"]},
        prompt_chars=int(ledger["input_tokens"] * cost.CHARS_PER_TOKEN))
    return {
        "ledger": ledger,
        "forecast": {**forecast, "cases": n_cases,
                     "with_baseline_pass_usd": round(forecast["estimated_cost_usd"] * 2, 4)},
        "reconciliation": check,
        "cache": cached,
        "judge_model": JUDGE_MODEL,
    }


def power(domain: str, version: str, target: float | None = None) -> dict:
    """How big a change this much history could actually detect. :mod:`ptm.stats`.

    The question that comes *before* a backfill and that nothing here could
    answer. Every other band in this project is retrospective - it says how
    precise a measurement turned out to be once it had been paid for - and the
    decision somebody is actually making is whether to pay for it at all. A
    replay of one month's 120 cases cannot tell a 24% flip rate from a 20% one
    at any confidence worth quoting, and finding that out from two overlapping
    bands afterwards costs a backfill.

    ``target`` is a rate somebody cares about reaching - "we need this under
    20%" - and turns the answer from a statement about precision into a
    statement about whether this history can check the claim at all.

    Costs nothing: it reads the case count and the measured rate off the
    aggregates and does arithmetic.
    """
    config = _checked(domain, version)
    head = summary(domain, version)
    cases = head["cases"]
    report = stats.power_report(head["flip_rate"], cases, target)
    return {
        "domain": domain,
        "version": version,
        "impact_unit": config.impact_unit,
        "measured": bool(cases),
        **report,
        "summary": stats.describe_power(report) if cases else "",
        "hint": "" if cases else
                f"nothing has been replayed under {domain}/{version}, so there is no rate "
                f"to size a sample against. Replay it first.",
        "hint_key": "" if cases else "hint.power_unmeasured",
        "hint_args": {"domain": domain, "version": version},
        "caveat": "this is sampling error and nothing else. A judge that contradicts "
                  "itself, or that disagrees with the humans, moves the answer further "
                  "than any of this - and those two bands do not add to this one.",
        "caveat_key": "caveat.power",
    }


def stability(domain: str, version: str) -> dict:
    """The error bar on this policy's flip rate."""
    _checked(domain, version)
    latest = store.latest_stability(domain, version)
    if not latest:
        return {"measured": False,
                "hint": f"run the judge_stability_{domain} DAG to put an error bar "
                        f"on this policy's flip rate",
                "hint_key": "hint.stability", "hint_args": {"domain": domain}}
    rows = store.query(
        """SELECT case_id, outcome, COUNT(*) n FROM judge_samples
           WHERE run_id=? GROUP BY case_id, outcome ORDER BY case_id""",
        (latest["run_id"],))
    by_case: dict[str, dict] = {}
    for row in rows:
        by_case.setdefault(row["case_id"], {})[row["outcome"]] = row["n"]
    return {"measured": True, **latest,
            "disagreeing_cases": {k: v for k, v in by_case.items() if len(v) > 1}}


def cross_check(domain: str, version: str) -> dict:
    """A second judge's answers on the same policy. See :mod:`ptm.crosscheck`.

    Returns a hint rather than a report when nothing has run, because an absent
    panel reads as a check that passed. The cases the two judges split on are
    the output worth reading - they are the sentences of the policy that do not
    settle a case, found without spending a human on any of them.
    """
    _checked(domain, version)
    latest = store.latest_cross_check(domain, version)
    if not latest:
        return {"measured": False,
                "hint": f"run judge_stability_{domain} with compare_model set to a second "
                        f"model to find out how much of this flip rate one judge is "
                        f"responsible for",
                "hint_key": "hint.crosscheck", "hint_args": {"domain": domain}}
    report_body = latest["report"]
    return {
        "measured": True,
        "run_id": latest["run_id"],
        "created_at": latest["created_at"],
        "report": report_body,
        "summary": crosscheck_engine.describe(
            crosscheck_engine.CrossCheckReport(**report_body)),
        "caveat": "a second judge is independent, not correct. A case they split on is "
                  "evidence the policy does not settle it - it is not evidence about "
                  "which model was right.",
        "caveat_key": "caveat.crosscheck",
    }


def conflicts(domain: str) -> list[dict]:
    """Human rulings that contradict each other rather than the policy."""
    config = _domain(domain)
    return [c.model_dump(mode="json")
            for c in diff.precedent_conflicts(store.precedents_with_payload(domain), config)]


def precedents(domain: str) -> list[dict]:
    _domain(domain)
    rows = store.query(
        "SELECT * FROM precedents WHERE domain=? ORDER BY established_at DESC", (domain,))
    # A ruling that has been re-adjudicated reads exactly like one made first
    # time, and they are not the same thing: the second was made about a clause
    # that had changed under the first. Re-adjudication is the only thing that
    # replaces a precedent, so the count is the whole disclosure.
    revisions = store.revision_counts(domain)
    for row in rows:
        row["revisions"] = revisions.get(row["case_id"], 0)
    return rows


def precedent_history(domain: str) -> dict:
    """Rulings that a later ruling replaced. See :func:`ptm.store.save_precedent`.

    Precedent is the only durable artefact here, so the one operation that
    overwrites one - re-adjudicating a ruling made about a clause that has since
    been rewritten - has to leave what it replaced readable. A reviewer who
    changed their predecessor's answer, and the reason each of them gave, is the
    most interesting record this system holds.
    """
    _domain(domain)
    rows = store.precedent_history(domain)
    current = {r["case_id"]: r for r in store.query(
        "SELECT * FROM precedents WHERE domain=?", (domain,))}
    for row in rows:
        now = current.get(row["case_id"], {})
        row["current_outcome"] = now.get("correct_outcome", "")
        row["current_ruled_by"] = now.get("ruled_by", "")
        row["changed"] = bool(now) and now.get("correct_outcome") != row["correct_outcome"]
    return {
        "domain": domain,
        "superseded": len(rows),
        "changed": sum(1 for r in rows if r["changed"]),
        "reconfirmed": sum(1 for r in rows if not r["changed"]),
        "rulings": rows,
        "caveat": "a superseded ruling is not a mistake. It was made about the policy "
                  "text as it then read, and is kept because whether it still holds is a "
                  "judgement somebody made rather than a fact the gate can recompute.",
        "caveat_key": "caveat.superseded",
    }


def calibration(domain: str, version: str) -> dict:
    """Is the judge right? Scored against the humans who ruled. See :mod:`ptm.calibration`.

    Costs nothing: it scores verdicts already on file, which for the precedent
    cases is whatever the gate last stored. Returns a hint rather than a report
    when there is nothing to score, because a calibration panel showing 100%
    from zero cases is the most misleading thing this API could return.
    """
    config = _checked(domain, version)
    precedents = store.load_precedents(domain)
    if not precedents:
        return {"measured": False, "judged": 0,
                "hint": f"no human rulings on file for {domain}; run the adjudication "
                        f"DAG, then the gate, and the judge can be scored against them",
                "hint_key": "hint.calibration_none", "hint_args": {"domain": domain}}
    stored = store.latest_verdicts(domain, version)
    verdicts = {
        case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                         confidence=row["confidence"], policy_clause=row["policy_clause"] or "")
        for case_id, row in stored.items()
    }
    result = calibration_engine.score(config, version, verdicts, precedents)
    if not result.judged:
        return {"measured": False, "judged": 0, "report": result.model_dump(mode="json"),
                "hint": f"{len(precedents)} precedent(s) on file but none judged under "
                        f"{version}; run precedent_gate_{domain} to score the judge",
                "hint_key": "hint.calibration_unjudged",
                "hint_args": {"n": len(precedents), "version": version, "domain": domain}}
    # Which judge produced the verdicts being scored. Offline they came from
    # offline_rules, written from the same policy the reviewer was shown, so
    # the figure describes the fixture rather than a judge - exactly the caveat
    # rule_agreement carries, and for the same reason. The gate is held off
    # while it holds; a gate that passes because the check is switched off is
    # worse than no gate.
    models = sorted({
        row["judge_model"] for row in store.query(
            "SELECT DISTINCT judge_model FROM runs WHERE domain=? AND policy_version=?",
            (domain, version)) if row["judge_model"]})
    inert = all(m in {"", "offline"} for m in models)
    problems = [] if inert else calibration_engine.gate(result, config)
    return {
        "measured": True,
        "judged": result.judged,
        "report": result.model_dump(mode="json"),
        "summary": calibration_engine.describe(result),
        "judged_by": models,
        "inert": inert,
        # The gate's answer travels with the measurement, the way the rules
        # check travels with a sweep. A panel that shows a number and leaves the
        # threshold it is judged against somewhere else is a panel nobody reads
        # as a verdict.
        "gate": config.calibration.gate,
        "problems": problems,
        "min_judged": config.calibration.min_judged,
        "caveat": "precedents are the contested flips - the cases nobody could settle "
                  "by reading the rule. This is a floor on the judge's accuracy, not an "
                  "estimate of it.",
        "caveat_key": "caveat.calibration",
    }


def disparity(domain: str, version: str) -> dict:
    """Whether the change lands on one group harder than the rest. :mod:`ptm.disparity`."""
    config = _checked(domain, version)
    rows = segments(domain, version)
    findings = disparity_engine.analyse(rows, config)
    gated = disparity_engine.gated(findings)
    return {
        "domain": domain,
        "version": version,
        "impact_unit": config.impact_unit,
        "fields": config.disparity.fields or config.segment_fields,
        "max_ratio": config.disparity.max_ratio,
        "min_cases": config.disparity.min_cases,
        "gate": config.disparity.gate,
        "findings": [f.model_dump(mode="json") for f in findings],
        "gating": [f.model_dump(mode="json") for f in gated],
        "summary": disparity_engine.describe(findings, config),
        "caveat": "a concentration is a question, not a verdict. Segments differ in "
                  "what they contain, and the explanation is often good - the point is "
                  "that somebody gives it before the rule ships.",
        "caveat_key": "caveat.disparity",
    }


def preflight(domain: str, version: str) -> dict:
    """Problems readable in the policy text itself, before a replay. :mod:`ptm.preflight`."""
    config = _checked(domain, version)
    findings = preflight_engine.structural(config, version)
    blocking = preflight_engine.blocking(findings)
    return {
        "domain": domain,
        "version": version,
        "findings": [f.model_dump(mode="json") for f in findings],
        "blocking": len(blocking),
        "summary": preflight_engine.describe(findings, version),
        "caveat": "structural only - this reads the policy's shape, not its meaning. "
                  "Contradictions and ambiguity need the model pass on the replay DAG.",
        "caveat_key": "caveat.preflight",
    }


def rule_agreement(domain: str, version: str) -> dict:
    """Do the offline rules actually implement the policy? :mod:`ptm.rules`.

    The number the threshold sweep rests on. The sweep is computed from the
    rules, so it is only worth what the rules are worth, and until this is
    measured nobody knows what that is.
    """
    config = _checked(domain, version)
    rules = config.offline_rules.get(version, [])
    stored = store.latest_verdicts(domain, version)
    models = sorted({
        row["judge_model"] for row in store.query(
            "SELECT DISTINCT judge_model FROM runs WHERE domain=? AND policy_version=?",
            (domain, version)) if row["judge_model"]})
    if not rules or not stored:
        return {"domain": domain, "policy_version": version, "compared": 0,
                "rules": len(rules), "judged_by": models,
                "inert": all(m in {"", "offline"} for m in models),
                "hint": f"{'no offline rules for ' + version if not rules else 'no verdicts on file'}"
                        f"; replay {version} first, then the rules can be scored against "
                        f"what the judge said",
                # Two different problems wearing one sentence. They send a reader
                # to different files, so they are different keys.
                "hint_key": "hint.rules_norules" if not rules else "hint.rules_noverdicts",
                "hint_args": {"version": version}}
    verdicts = {
        case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                         confidence=row["confidence"], policy_clause=row["policy_clause"] or "")
        for case_id, row in stored.items()
    }
    cases = store.load_cases(domain, until=datetime.now())
    result = rules_engine.agreement(config, version, rules, cases, verdicts)
    result["rules"] = len(rules)
    result["judged_by"] = models
    # Offline the verdicts being scored against were produced by these same
    # rules, so agreement is 1.0 by construction. Saying so is the whole
    # difference between a measurement and a decoration.
    result["inert"] = all(m in {"", "offline"} for m in models)
    result["summary"] = rules_engine.describe(result)
    return result


def history(domain: str) -> dict:
    """Every version of this policy that has been replayed, side by side.

    The question this answers is the one the whole loop is *for* and that no
    panel could answer: **did the edit help?** A single-version view says 147
    outcomes change; it cannot say whether that is better or worse than the 161
    the version before it changed, nor whether the draft written to fix it
    actually fixed it. Comparing meant opening two tabs and doing arithmetic.

    Each row carries the three numbers a version is judged on - what it moves,
    what of that it is *responsible* for, and how many human rulings it reverses
    - plus the sampling band on the rate, because two versions measured on
    different numbers of cases differ by sample size before they differ by
    policy. Reversals come from stored verdicts, so this costs nothing.
    """
    config = _domain(domain)
    totals = store.version_totals(domain)
    precedents = store.load_precedents(domain)
    rows = []
    for version in sorted(set(totals) | set(config.policies)):
        row = totals.get(version) or {
            "policy_version": version, "runs": 0, "cases": 0, "flips": 0,
            "loosening": 0, "tightening": 0, "impact_loosening": 0.0,
            "impact_tightening": 0.0, "policy_driven_flips": 0, "mean_confidence": 0.0,
            "judged_by": [], "first_run": "", "last_run": "",
            "estimated_cost_usd": 0.0, "actual_cost_usd": 0.0,
        }
        band = stats.rate(row["flips"], row["cases"])
        # Scored against the rulings on file, from verdicts already stored. A
        # version nothing has judged reports unchecked rather than zero: no
        # reversals and no evidence look identical in a column of integers.
        stored = store.latest_verdicts(domain, version)
        judged = {
            case_id: Verdict(outcome=r["outcome"], rationale=r["rationale"],
                             confidence=r["confidence"], policy_clause=r["policy_clause"] or "")
            for case_id, r in stored.items()
        }
        checked = [p for p in precedents if p.case_id in judged]
        rows.append({
            **row,
            "known": version in config.policies,
            "is_draft": version in config.draft_versions,
            "in_force": version == config.in_force,
            "flip_rate": band["rate"],
            "flip_rate_lo": band["rate_lo"],
            "flip_rate_hi": band["rate_hi"],
            "net_impact": round((row["impact_loosening"] or 0)
                                - (row["impact_tightening"] or 0), 2),
            "deviation_flips": row["flips"] - row["policy_driven_flips"],
            "precedents_checked": len(checked),
            "reverses": len(diff.precedent_violations(judged, precedents)),
            "estimated_cost_usd": round(row["estimated_cost_usd"] or 0, 4),
            "actual_cost_usd": round(row["actual_cost_usd"] or 0, 4),
        })
    rows.sort(key=lambda r: (r["last_run"] or "", r["policy_version"]))
    return {
        "domain": domain,
        "in_force": config.in_force,
        "impact_unit": config.impact_unit,
        "precedents": len(precedents),
        "versions": rows,
        "runs": store.runs_over_time(domain),
        "caveat": "two versions are only comparable over the same cases. A version "
                  "replayed on one month and one replayed on two years differ by sample "
                  "before they differ by policy, which is what the band is for - and a "
                  "version with no verdicts on file reverses no precedent because "
                  "nothing has asked it, not because it agrees.",
        "caveat_key": "caveat.history",
    }


def drafts(domain: str) -> list[dict]:
    """Amendments drafted by :mod:`ptm.proposal`, newest first, with their provenance."""
    config = _domain(domain)
    rows = store.drafts(domain)
    on_disk = set(config.draft_versions)
    for row in rows:
        # A draft whose files were discarded leaves its provenance behind. Say
        # so rather than linking to a policy version that no longer resolves.
        row["available"] = row["version"] in on_disk
        # Adopted and discarded both leave include/drafts/ empty, and they are
        # the opposite decision - so the reason the files are gone is reported
        # rather than left to be inferred from their absence.
        row["adopted"] = bool(row.get("adopted_as"))
        row["state"] = ("adopted" if row["adopted"]
                        else "available" if row["available"] else "discarded")
    return rows


# ------------------------------------------------------------------- the CLI

def describe_comparison(result: dict, limit: int = 20) -> str:
    """Two versions side by side, as the CLI prints them.

    The question is *did the edit help?*, so the answer leads with the cases
    that moved between the two rather than with either version's totals: a
    reader who wanted the totals has ``--history``, and a count of differences
    with no case ids under it is the shape of answer that sends somebody back
    to the dashboard.
    """
    left, right = result["left"], result["right"]
    lines = [f"{left} against {right}, over the {result['compared']} case(s) both "
             f"versions have judged"]
    if not result["compared"]:
        # Nothing judged under both is not agreement, and a bare "0 differ"
        # reads exactly like perfect agreement. Same refusal the rule gate and
        # the calibration gate make about a measurement never taken.
        lines.append(f"  neither version has verdicts for any case the other has judged "
                     f"({result['judged_left']} under {left}, {result['judged_right']} "
                     f"under {right}). Replay both before comparing them.")
        return "\n".join(lines)
    lines.append(f"  agree on {result['agree']}, differ on {result['differ']} "
                 f"({result['differ'] / result['compared']:.1%})")
    if not result["differ"]:
        lines.append(f"  {left} and {right} reach the same outcome for every case both "
                     f"have seen; whatever the edit changed, it did not change an answer "
                     f"on this history")
        return "\n".join(lines)
    lines.append(f"  {'case':<12}{'recorded':>12}{'  ' + left:>14}{'  ' + right:>14}")
    for row in result["differences"][:limit]:
        lines.append(f"  {row['case_id']:<12}{row['actual_outcome'] or '-':>12}"
                     f"  {row['left_outcome']:>12}  {row['right_outcome']:>12}"
                     f"   clause {row['left_clause'] or '-'} -> "
                     f"{row['right_clause'] or '-'}")
    if result["differ"] > limit:
        lines.append(f"  ... and {result['differ'] - limit} more; -o writes the whole set")
    lines.append("  a difference is not an improvement. Which of the two is right about "
                 "a case is what the precedent gate answers, and only for the cases a "
                 "human has ruled on.")
    return "\n".join(lines)


def describe_history(result: dict, digits: int = 1) -> str:
    """Every replayed version in one table, as the CLI prints it.

    Three numbers per version because a version is judged on three different
    things and quoting one of them is how a policy gets adopted for the wrong
    reason: what it moves, how much of that it is *responsible* for, and how
    many human rulings it reverses.
    """
    unit = result["impact_unit"]
    lines = [f"every version of {result['domain']} that has been replayed, oldest run "
             f"first ({result['precedents']} ruling(s) on file)"]
    # 22 for the rate, because the band it carries is "24.5% (21.2%-28.1%)" -
    # nineteen characters at a one-decimal precision and more at two. The first
    # width tried was 18, which does not truncate, it simply stops padding, so
    # the flip count and the rate ran together into one unreadable number.
    lines.append(f"  {'version':<14}{'cases':>7}{'flips':>7}{'rate':>22}"
                 f"{'caused':>8}{'reverses':>10}{'net ' + unit:>14}")
    for row in result["versions"]:
        if not row["runs"]:
            # Listed rather than dropped: a version nothing has replayed is a
            # real state and an absent row reads as a version that does not
            # exist. "never the candidate" rather than "never replayed",
            # because the policy in force is judged on every case as the
            # *baseline* of somebody else's run - it has verdicts on file and
            # no run of its own, and calling that unreplayed would contradict
            # the comparison two commands away that reads those very verdicts.
            mark = " (draft)" if row["is_draft"] else ""
            lines.append(f"  {row['policy_version'] + mark:<14}{'-':>7}{'-':>7}"
                         f"{'never the candidate':>22}{'-':>8}{'-':>10}{'-':>14}")
            continue
        band = (f"{row['flip_rate']:.{digits}%} "
                f"({row['flip_rate_lo']:.{digits}%}-{row['flip_rate_hi']:.{digits}%})")
        reverses = (f"{row['reverses']}" if row["precedents_checked"]
                    else "unchecked")
        mark = ("*" if row["in_force"] else " ") + ("d" if row["is_draft"] else " ")
        lines.append(f"  {row['policy_version']:<12}{mark}{row['cases']:>7}"
                     f"{row['flips']:>7}{band:>22}{row['policy_driven_flips']:>8}"
                     f"{reverses:>10}{row['net_impact']:>14,.0f}")
    lines.append("  (* = in force, d = a draft nobody has approved. 'caused' excludes "
                 "changes the policy in force already makes; 'unchecked' means no "
                 "verdict on file, which is not the same as reversing nothing. A "
                 "version that is never the candidate can still have been judged as "
                 "another run's baseline - use --compare to see those verdicts.)")
    lines.append(f"  {result['caveat']}")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.report <domain> <version> [--csv] [-o FILE]
        Everything the Diff Explorer shows, as one JSON bundle with the
        caveats attached - or --csv for the flip set alone.

  python -m ptm.report <domain> <version> --power [--target RATE]
        How big a change this much history could actually detect, and how
        many cases it would take to settle a comparison it cannot.

  python -m ptm.report <domain> --compare <left> <right> [--json] [-o FILE]
        Did the edit help? The two versions case by case, over the cases both
        of them have judged.

  python -m ptm.report <domain> --history [--json] [-o FILE]
        Every version ever replayed, side by side: what each moves, what it is
        responsible for, and how many human rulings it reverses.

  python -m ptm.report --list
        The domains and versions this database knows about.

Writes to stdout unless -o names a file, so it pipes into jq, an attachment,
or the spreadsheet the decision actually gets argued in. --compare and
--history print a table; add --json for the object behind it."""


def _flag(args: list[str], name: str) -> str | None:
    """The value after ``--name``, or None. Empty string if the flag ends the line."""
    if name not in args:
        return None
    index = args.index(name)
    value = args[index + 1] if index + 1 < len(args) else ""
    return "" if value.startswith("-") else value


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.report <domain> <version>`` - the bundle, without Airflow.

    Every other measurement in this project can be reached from a shell with no
    Airflow and no key. The one artefact built to *leave* the room could not:
    :func:`export_bundle` assembles the numbers with their caveats attached
    precisely so they cannot be pasted into a slide without them, and it existed
    only behind a FastAPI route behind an Airflow login. A decision gets argued
    about away from the dashboard, which is exactly when nobody can start the
    dashboard.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    if "--list" in args:
        for name in available_domains():
            config = load_domain(name)
            versions = ", ".join(
                v + (" (draft)" if v in config.draft_versions else "")
                + (" (in force)" if v == config.in_force else "")
                for v in sorted(config.policies))
            print(f"{name:<12} {versions}")
        return 0

    as_csv = "--csv" in args
    as_power = "--power" in args
    as_history = "--history" in args
    as_json = "--json" in args
    # --compare takes two versions rather than one, so it cannot go through
    # _flag; both are read off the positionals below, after the option scan has
    # had its say about anything dashed.
    comparing = "--compare" in args
    target: float | None = None
    if "--target" in args:
        index = args.index("--target")
        raw = args[index + 1] if index + 1 < len(args) else ""
        try:
            target = float(raw)
        except ValueError:
            print(f"ERROR --target needs a rate between 0 and 1, e.g. 0.20\n\n{USAGE}",
                  file=sys.stderr)
            return 2
        if not 0.0 <= target <= 1.0:
            print(f"ERROR --target must be between 0 and 1, got {target}", file=sys.stderr)
            return 2
        args.pop(index + 1)
    # Not `_flag(-o) or _flag(--out)`: _flag returns "" for a flag with nothing
    # after it, `or` collapses that to the second lookup and then to None, and
    # `python -m ptm.report expenses v2 -o` printed the whole bundle to the
    # terminal instead of saying the filename was missing. An empty string here
    # means the flag was given without a value, which is a mistake to report.
    out_path = _flag(args, "-o")
    if out_path is None:
        out_path = _flag(args, "--out")
    if out_path == "":
        print(f"ERROR -o needs a file to write to\n\n{USAGE}", file=sys.stderr)
        return 2
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in {"-o", "--out"}:
            skip = True
            continue
        # --target's value is popped above, so only the flag itself is left.
        if arg in {"--csv", "--power", "--target", "--history", "--json", "--compare"}:
            continue
        if arg.startswith("-"):
            print(f"ERROR unknown option {arg!r}\n\n{USAGE}", file=sys.stderr)
            return 2
        positional.append(arg)

    # Two modes take a domain and no version, because they are about the domain:
    # --history compares every version it has, and --compare names its own two.
    if as_history or comparing:
        if not positional:
            print(f"ERROR name a domain; have {available_domains()}\n\n{USAGE}",
                  file=sys.stderr)
            return 2
        domain = positional[0]
        store.init_db()
        try:
            if comparing:
                if len(positional) < 3:
                    print(f"ERROR --compare needs two versions to compare\n\n{USAGE}",
                          file=sys.stderr)
                    return 2
                left, right = positional[1], positional[2]
                if left == right:
                    # Not an error the read model would catch: comparing a
                    # version with itself agrees on everything and says nothing,
                    # which reads as a finding rather than as a mistake.
                    print(f"ERROR --compare needs two different versions; {left!r} "
                          f"agrees with itself on every case by construction",
                          file=sys.stderr)
                    return 2
                result = compare(domain, left, right, limit=5000)
                body = (json.dumps(result, indent=2, default=str) if as_json
                        else describe_comparison(result))
            else:
                result = history(domain)
                body = (json.dumps(result, indent=2, default=str) if as_json
                        else describe_history(result))
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        return _emit(body, out_path)

    if len(positional) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    domain, version = positional[0], positional[1]

    store.init_db()
    try:
        if as_power:
            result = power(domain, version, target)
            body = result["summary"] or result["hint"]
            if result["measured"]:
                body += "\n  " + result["caveat"]
        elif as_csv:
            body = flips_csv(domain, version)
        else:
            body = json.dumps(export_bundle(domain, version), indent=2, default=str)
    except LookupError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    return _emit(body, out_path)


def _emit(body: str, out_path: str | None) -> int:
    """Write the result where the caller asked for it. One copy, two callers."""
    if out_path:
        # Explicit encoding, for the same reason policy_text reads one: the
        # bundle carries policy prose and reviewer notes, and a cp1252 default
        # would refuse the file rather than write it wrongly.
        pathlib.Path(out_path).write_text(body, encoding="utf-8")
        print(f"wrote {out_path} ({len(body):,} bytes)", file=sys.stderr)
    else:
        print(body)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
