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
from datetime import datetime

from . import calibration as calibration_engine
from . import crosscheck as crosscheck_engine
from . import cost, diff, disparity as disparity_engine, preflight as preflight_engine
from . import rules as rules_engine
from . import stats, store
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
                        f"bar on individual flips before a human rules on them"}
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
                "hint": "no precedents yet; run the adjudication DAG"}

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
        ],
    }


def flips(domain: str, version: str, limit: int = 200) -> list[dict]:
    _checked(domain, version)
    rows = store.query(
        """SELECT f.case_id, f.actual_outcome, f.new_outcome, f.direction, f.impact,
                  f.confidence, f.rationale, f.reviewed, f.policy_clause, f.attribution,
                  f.baseline_outcome, f.segments, f.stability, c.decided_at, c.payload,
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
                        f"responsible for"}
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
    }


def conflicts(domain: str) -> list[dict]:
    """Human rulings that contradict each other rather than the policy."""
    config = _domain(domain)
    return [c.model_dump(mode="json")
            for c in diff.precedent_conflicts(store.precedents_with_payload(domain), config)]


def precedents(domain: str) -> list[dict]:
    _domain(domain)
    return store.query(
        "SELECT * FROM precedents WHERE domain=? ORDER BY established_at DESC", (domain,))


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
                        f"DAG, then the gate, and the judge can be scored against them"}
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
                        f"{version}; run precedent_gate_{domain} to score the judge"}
    return {
        "measured": True,
        "judged": result.judged,
        "report": result.model_dump(mode="json"),
        "summary": calibration_engine.describe(result),
        "caveat": "precedents are the contested flips - the cases nobody could settle "
                  "by reading the rule. This is a floor on the judge's accuracy, not an "
                  "estimate of it.",
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
                        f"what the judge said"}
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


def drafts(domain: str) -> list[dict]:
    """Amendments drafted by :mod:`ptm.proposal`, newest first, with their provenance."""
    config = _domain(domain)
    rows = store.drafts(domain)
    on_disk = set(config.draft_versions)
    for row in rows:
        # A draft whose files were discarded leaves its provenance behind. Say
        # so rather than linking to a policy version that no longer resolves.
        row["available"] = row["version"] in on_disk
    return rows
