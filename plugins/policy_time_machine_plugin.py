"""Airflow plugin: the Policy Diff Explorer.

Registers a FastAPI app (read-only API + a self-contained dashboard page) and
an external view so it appears as a tab in the Airflow UI. Airflow 3.1 also
supports `react_apps`, but those are still marked experimental, so the
dashboard is served as a single dependency-free page - see README for how to
switch to the React registration instead.

Note: FastAPI plugin endpoints are NOT covered by Airflow's own auth. These
routes are read-only and this is a demo, but do not expose them as-is.
"""

from __future__ import annotations

import json
from pathlib import Path

from airflow.plugins_manager import AirflowPlugin
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ptm import analysis, store
from ptm.config import available_domains, load_domain

app = FastAPI(title="Policy Time Machine")

#: The dashboard is read-only, but it is the first thing a new checkout opens,
#: and every query below needs the schema to exist. Creating it on import is
#: cheap and turns "no such table: runs" into an empty state.
store.init_db()


def _domain_or_404(name: str):
    try:
        return load_domain(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/domains")
def domains() -> list[dict]:
    out = []
    for name in available_domains():
        d = load_domain(name)
        candidates = store.candidate_versions(name)
        out.append({"name": name, "label": d.label, "outcomes": d.outcomes,
                    # Drafted candidates are selectable like any other version.
                    "policies": d.all_versions(),
                    "written": sorted(d.policies),
                    "candidates": [c["version"] for c in candidates],
                    "cohort_fields": d.cohort_fields,
                    "impact_unit": d.impact_unit})
    return out


@app.get("/api/coverage/{domain}/{version}")
def coverage(domain: str, version: str) -> dict:
    """Which rules of this policy history has actually exercised."""
    d = _domain_or_404(domain)
    try:
        return analysis.clause_coverage(d, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/cohorts/{domain}/{version}")
def cohorts(domain: str, version: str) -> dict:
    """Who bears the change, broken down by the domain's declared cohort fields."""
    d = _domain_or_404(domain)
    report = analysis.cohort_report(d, version)
    return {"breakdowns": report, "disproportionate": analysis.disproportionate(report)}


@app.get("/api/compare/{domain}/{left}/{right}")
def compare(domain: str, left: str, right: str) -> dict:
    """Two candidate policies against the same history and the same precedents."""
    d = _domain_or_404(domain)
    try:
        return analysis.compare(d, left, right)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/candidates/{domain}")
def candidates(domain: str) -> list[dict]:
    """Policy versions drafted by the amendment gate, with what forced them."""
    _domain_or_404(domain)
    import json as _json
    rows = store.candidate_versions(domain)
    for r in rows:
        r["forced_by"] = _json.loads(r["forced_by"]) if isinstance(r["forced_by"], str) else r["forced_by"]
    return rows


@app.get("/api/summary/{domain}/{version}")
def summary(domain: str, version: str) -> dict:
    d = _domain_or_404(domain)
    # store.policy_summary is the single source the brief also quotes, so the
    # tiles and the brief cannot disagree. It counts per case, not per run.
    s = store.policy_summary(domain, version)
    prec = store.query("SELECT COUNT(*) n FROM precedents WHERE domain=?", (domain,))[0]["n"]
    rows = store.flips_for_policy(domain, version)
    # A cluster of low-confidence flips is a drafting problem, not a cost one,
    # so the dashboard surfaces it as a headline number of its own.
    unsure = sum(1 for r in rows if r["confidence"] < d.review.below_confidence)
    return {"runs": s["runs"], "cases": s["cases_replayed"],
            "flips": s["flips"], "net_impact": s["net_impact"],
            "by_direction": [
                {"direction": "loosening", "n": s["loosening"], "impact": s["impact_loosening"]},
                {"direction": "tightening", "n": s["tightening"], "impact": s["impact_tightening"]},
            ],
            "precedents": prec, "low_confidence": unsure,
            "confidence_threshold": d.review.below_confidence,
            "impact_unit": d.impact_unit}


@app.get("/api/timeline/{domain}/{version}")
def timeline(domain: str, version: str) -> list[dict]:
    """Flips and impact per month of replayed history - one point per backfill run."""
    _domain_or_404(domain)
    return store.query(
        """SELECT substr(c.decided_at, 1, 7) AS month,
                  COUNT(*) AS flips,
                  SUM(CASE WHEN f.direction='loosening' THEN f.impact ELSE 0 END) AS loosening,
                  SUM(CASE WHEN f.direction='tightening' THEN f.impact ELSE 0 END) AS tightening
           FROM flips f JOIN cases c ON c.case_id = f.case_id
           WHERE f.domain=? AND f.policy_version=?
           GROUP BY month ORDER BY month""",
        (domain, version))


@app.get("/api/flips/{domain}/{version}")
def flips(domain: str, version: str, limit: int = 200) -> list[dict]:
    _domain_or_404(domain)
    rows = store.query(
        """SELECT f.case_id, f.actual_outcome, f.new_outcome, f.direction, f.impact,
                  f.confidence, f.rationale, f.reviewed, f.policy_clause,
                  c.decided_at, c.payload, c.actual_rationale,
                  (SELECT correct_outcome FROM precedents p
                    WHERE p.case_id = f.case_id AND p.domain = f.domain) AS precedent
           FROM flips f JOIN cases c ON c.case_id = f.case_id
           WHERE f.domain=? AND f.policy_version=? ORDER BY f.impact DESC LIMIT ?""",
        (domain, version, limit))
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    return rows


@app.get("/api/precedents/{domain}")
def precedents(domain: str) -> list[dict]:
    _domain_or_404(domain)
    return store.query(
        "SELECT * FROM precedents WHERE domain=? ORDER BY established_at DESC", (domain,))


@app.get("/api/insights/{domain}/{version}")
def insights(domain: str, version: str) -> dict:
    """The AI analysis of this policy: the brief, the themes, any amendment."""
    _domain_or_404(domain)
    return store.load_insights(domain, version)


@app.get("/api/policy/{domain}/{version}")
def policy(domain: str, version: str) -> dict:
    """The policy text itself, so the brief can be read beside what it describes."""
    d = _domain_or_404(domain)
    try:
        return {"version": version, "text": d.policy_text(version)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"policy file missing: {exc}") from exc


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).parent / "dashboard.html").read_text()


class PolicyTimeMachinePlugin(AirflowPlugin):
    name = "policy_time_machine"
    fastapi_apps = [
        {"app": app, "name": "Policy Time Machine", "url_prefix": "/ptm"},
    ]
    external_views = [
        {
            "name": "Policy Time Machine",
            "href": "/ptm/",
            "destination": "nav",
            "icon": "fa-solid fa-clock-rotate-left",
            "url_route": "policy-time-machine",
        },
    ]
