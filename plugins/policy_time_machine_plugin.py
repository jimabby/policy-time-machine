"""Airflow plugin: the Policy Diff Explorer.

Registers a FastAPI app (read-only API + a self-contained dashboard page) and
an external view so it appears as a tab in the Airflow UI. Airflow 3.1 also
supports `react_apps`, but those are still marked experimental, so the
dashboard is served as a single dependency-free page - see README for how to
switch to the React registration instead.

This module is deliberately nothing but routing: every read model lives in
:mod:`ptm.report`, so the whole API surface is covered by the test suite without
FastAPI installed, and is callable from a script without starting Airflow.

Note: FastAPI plugin endpoints are NOT covered by Airflow's own auth. These
routes are read-only and this is a demo, but do not expose them as-is.
"""

from __future__ import annotations

from pathlib import Path

from airflow.plugins_manager import AirflowPlugin
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ptm import report

app = FastAPI(title="Policy Time Machine")


def found(fn, *args, **kwargs):
    """Turn a read model's LookupError into a 404 instead of an opaque 500."""
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/domains")
def domains() -> list[dict]:
    return report.domains()


@app.get("/api/summary/{domain}/{version}")
def summary(domain: str, version: str) -> dict:
    return found(report.summary, domain, version)


@app.get("/api/flips/{domain}/{version}")
def flips(domain: str, version: str, limit: int = Query(default=200, ge=1, le=500)) -> list[dict]:
    return found(report.flips, domain, version, limit)


@app.get("/api/clauses/{domain}/{version}")
def clauses(domain: str, version: str) -> list[dict]:
    """Which clause is responsible for which share of the change."""
    return found(report.clauses, domain, version)


@app.get("/api/segments/{domain}/{version}")
def segments(domain: str, version: str) -> list[dict]:
    """Blast radius: who the change lands on, with denominators."""
    return found(report.segments, domain, version)


@app.get("/api/compare/{domain}/{left}/{right}")
def compare(domain: str, left: str, right: str,
            limit: int = Query(default=200, ge=1, le=500)) -> dict:
    """Two candidate policies side by side: did the edit to clause 1.1 help?"""
    return found(report.compare, domain, left, right, limit)


@app.get("/api/cost/{domain}/{version}")
def cost_report(domain: str, version: str) -> dict:
    """What judging has cost, and what a full replay would cost."""
    return found(report.cost_report, domain, version)


@app.get("/api/stability/{domain}/{version}")
def stability(domain: str, version: str) -> dict:
    """The error bar on this policy's flip rate."""
    return found(report.stability, domain, version)


@app.get("/api/conflicts/{domain}")
def conflicts(domain: str) -> list[dict]:
    """Human rulings that contradict each other rather than the policy."""
    return found(report.conflicts, domain)


@app.get("/api/precedents/{domain}")
def precedents(domain: str) -> list[dict]:
    return found(report.precedents, domain)


@app.get("/api/deviations/{domain}/{version}")
def deviations(domain: str, version: str,
               limit: int = Query(default=200, ge=1, le=500)) -> dict:
    """Recorded outcomes the policy in force already disagreed with."""
    return found(report.deviations, domain, version, limit)


@app.get("/api/precedent-check/{domain}/{version}")
def precedent_check(domain: str, version: str) -> dict:
    """Which precedents this policy reverses, and which the status quo already does."""
    return found(report.precedent_check, domain, version)


@app.get("/api/thresholds/{domain}/{version}")
def thresholds(domain: str, version: str) -> list[dict]:
    """The numeric dials in this version's offline rules that a sweep can move."""
    return found(report.thresholds, domain, version)


@app.get("/api/sweep/{domain}/{version}")
def sweep(domain: str, version: str,
          field: str = Query(..., min_length=1, max_length=64),
          values: str = Query(..., min_length=1, max_length=256),
          clause: str = Query(default="", max_length=16)) -> dict:
    """Re-run the replay at each candidate threshold. ``values`` is comma-separated."""
    return found(report.sweep, domain, version, field, values, clause)


@app.get("/api/export/{domain}/{version}.json")
def export_json(domain: str, version: str) -> JSONResponse:
    """Everything the Explorer shows, in one file, with the caveats attached."""
    bundle = found(report.export_bundle, domain, version)
    return JSONResponse(
        bundle,
        headers={"Content-Disposition":
                 f'attachment; filename="ptm-{domain}-{version}.json"'},
    )


@app.get("/api/export/{domain}/{version}.csv")
def export_csv(domain: str, version: str) -> PlainTextResponse:
    """The flip set as CSV, for the spreadsheet the decision gets argued in."""
    body = found(report.flips_csv, domain, version)
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="ptm-{domain}-{version}-flips.csv"'},
    )


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
