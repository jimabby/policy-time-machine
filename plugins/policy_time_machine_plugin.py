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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ptm import store
from ptm.config import available_domains, load_domain

app = FastAPI(title="Policy Time Machine")


@app.get("/api/domains")
def domains() -> list[dict]:
    out = []
    for name in available_domains():
        d = load_domain(name)
        out.append({"name": name, "label": d.label, "outcomes": d.outcomes,
                    "policies": sorted(d.policies), "impact_unit": d.impact_unit})
    return out


@app.get("/api/summary/{domain}/{version}")
def summary(domain: str, version: str) -> dict:
    runs = store.query(
        "SELECT COUNT(*) runs, SUM(cases_replayed) cases, SUM(flips) flips, SUM(impact) impact "
        "FROM runs WHERE domain=? AND policy_version=?", (domain, version))[0]
    dirs = store.query(
        "SELECT direction, COUNT(*) n, SUM(impact) impact FROM flips "
        "WHERE domain=? AND policy_version=? GROUP BY direction", (domain, version))
    prec = store.query("SELECT COUNT(*) n FROM precedents WHERE domain=?", (domain,))[0]["n"]
    return {"runs": runs["runs"] or 0, "cases": runs["cases"] or 0,
            "flips": runs["flips"] or 0, "net_impact": round(runs["impact"] or 0, 2),
            "by_direction": dirs, "precedents": prec,
            "impact_unit": load_domain(domain).impact_unit}


@app.get("/api/flips/{domain}/{version}")
def flips(domain: str, version: str, limit: int = 200) -> list[dict]:
    rows = store.query(
        """SELECT f.case_id, f.actual_outcome, f.new_outcome, f.direction, f.impact,
                  f.confidence, f.rationale, f.reviewed, c.decided_at, c.payload,
                  c.actual_rationale,
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
    return store.query(
        "SELECT * FROM precedents WHERE domain=? ORDER BY established_at DESC", (domain,))


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
