"""Airflow plugin: the Policy Diff Explorer.

Registers a FastAPI app (read-only API + a self-contained dashboard page) and
an external view so it appears as a tab in the Airflow UI. Airflow 3.1 also
supports `react_apps`, but those are still marked experimental, so the
dashboard is served as a single dependency-free page - see README for how to
switch to the React registration instead.

This module is deliberately nothing but routing: every read model lives in
:mod:`ptm.report`, so the whole API surface is covered by the test suite without
FastAPI installed, and is callable from a script without starting Airflow.

Airflow mounts a plugin's ``fastapi_apps`` with ``app.mount()``, and a mounted
sub-application has its own route table and inherits none of the parent's
dependencies - so Airflow's access control never reaches these routes. Every
one of them would be readable by anybody who can reach the port. :func:`require_user`
closes that, applied once to the whole app rather than per route, so a route
added later cannot forget it.
"""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

from airflow.plugins_manager import AirflowPlugin
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ptm import report

#: The cookie Airflow's UI stores its JWT in.
AIRFLOW_TOKEN_COOKIE = "_token"

#: Escape hatch for a context with no Airflow session to present: the test
#: suite, and a local demo run behind nothing. Read per request rather than at
#: import, so a test can set it without rebuilding the app - and named as an
#: allowance rather than a switch, because "off" has to be the thing you type.
ALLOW_ANONYMOUS = "PTM_ALLOW_ANONYMOUS"


async def require_user(request: Request):
    """Authenticate the caller the way Airflow's own API does.

    The token is taken from ``Authorization: Bearer``, then from the UI's
    ``_token`` cookie. The cookie is needed because it is **HttpOnly**: the
    browser sends it automatically and this page's JavaScript cannot read it,
    so a header-only check would 401 the dashboard for a reader who is already
    logged in. Verification itself is Airflow's ``resolve_user_from_token`` -
    signature, expiry and auth manager - never anything reimplemented here.

    **``async def``, and that is the whole security property.**
    ``resolve_user_from_token`` is a coroutine function: it raises 401 for a
    missing or expired token and 403 for an invalid one, but only once it is
    awaited. Written as a plain ``def``, this returned the *coroutine object*
    instead. FastAPI runs a sync dependency in a threadpool, saw a perfectly
    ordinary return value, raised nothing - and served every route to anybody
    who could reach the port, which is the exact failure the dependency was
    added to close. Nothing about the request looked wrong; the only trace was
    a "coroutine was never awaited" warning in a log nobody reads. Asserting
    that the app *carries* the dependency passed the whole time, which is why
    ``tests/test_plugin.py::TestTheRoutesAreNotPublic`` drives a real
    ``TestClient`` against a real Airflow instead: the only check worth having
    here is one that asks what a caller with no token actually gets back.

    Reading a cookie would be a CSRF hole on a route that changes something.
    Every route here is a read, the cookie is ``SameSite=Lax`` so a cross-site
    fetch does not carry it, and the responses are JSON another origin cannot
    read back without CORS. Keep it that way: a POST added below this line
    needs its own defence.
    """
    if os.environ.get(ALLOW_ANONYMOUS) == "1":
        return None
    try:
        from airflow.api_fastapi.core_api.security import resolve_user_from_token
    except ImportError as exc:  # pragma: no cover - a future Airflow moving it
        # Fail closed. A version that has moved this should 401 until somebody
        # looks, rather than serve every case file to anyone who asks.
        raise HTTPException(
            status_code=500,
            detail="cannot locate Airflow's token verifier, so this plugin "
                   "cannot authenticate you; set PTM_ALLOW_ANONYMOUS=1 only if "
                   "this deployment is genuinely meant to be public",
        ) from exc
    header = request.headers.get("Authorization", "")
    token = (header[7:].strip() if header[:7].lower() == "bearer "
             else request.cookies.get(AIRFLOW_TOKEN_COOKIE))
    resolved = resolve_user_from_token(token)
    # Awaited when it is awaitable rather than unconditionally, so a future
    # Airflow making this synchronous does not turn the fix back into the bug
    # with the sign flipped - a TypeError on every request instead of a 200.
    return await resolved if inspect.isawaitable(resolved) else resolved


app = FastAPI(title="Policy Time Machine", dependencies=[Depends(require_user)])


#: Anything that is not a plain filename character. A download filename is built
#: from the domain and version in the path, and those are server data rather than
#: user input - a domain is a file in include/domains/ and a version is a key of
#: the policies block or a draft's filename stem. Which is why this is a
#: correctness fix rather than a hole: a quote or a newline in either would
#: produce a header that means something other than what it says, and a draft
#: version is a filename stem, which is a wider input than the YAML ever was.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def filename(*parts: str) -> str:
    """A Content-Disposition filename that cannot be anything but a filename."""
    return "-".join(
        _UNSAFE_IN_FILENAME.sub("_", part).strip("_") or "x" for part in parts)


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


@app.get("/api/flip-page/{domain}/{version}")
def flip_page(domain: str, version: str, limit: int = Query(default=200, ge=1, le=500),
              offset: int = Query(default=0, ge=0), search: str = Query(default="", max_length=200)) -> dict:
    return found(report.flip_page, domain, version, limit, offset, search)


@app.get("/api/coverage/{domain}/{version}")
def coverage(domain: str, version: str) -> dict:
    return found(report.coverage, domain, version)


@app.get("/api/snapshots/{domain}/{version}")
def snapshots(domain: str, version: str) -> list[dict]:
    return found(report.snapshots, domain, version)


@app.get("/api/review/{domain}/{version}/{case_id}")
def review_case(domain: str, version: str, case_id: str) -> dict:
    return found(report.review_case, domain, version, case_id)


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


@app.get("/api/power/{domain}/{version}")
def power(domain: str, version: str,
          target: float | None = Query(default=None, ge=0.0, le=1.0)) -> dict:
    """How big a change this much history could have detected, before believing one."""
    return found(report.power, domain, version, target)


@app.get("/api/cross-check/{domain}/{version}")
def cross_check(domain: str, version: str) -> dict:
    """A second, independent judge on the same cases - and where the two split."""
    return found(report.cross_check, domain, version)


@app.get("/api/conflicts/{domain}")
def conflicts(domain: str) -> list[dict]:
    """Human rulings that contradict each other rather than the policy."""
    return found(report.conflicts, domain)


@app.get("/api/precedents/{domain}")
def precedents(domain: str) -> list[dict]:
    return found(report.precedents, domain)


@app.get("/api/precedent-history/{domain}")
def precedent_history(domain: str) -> dict:
    """Rulings a later ruling replaced, when a stale one was re-adjudicated."""
    return found(report.precedent_history, domain)


@app.get("/api/deviations/{domain}/{version}")
def deviations(domain: str, version: str,
               limit: int = Query(default=200, ge=1, le=500)) -> dict:
    """Recorded outcomes the policy in force already disagreed with."""
    return found(report.deviations, domain, version, limit)


@app.get("/api/precedent-check/{domain}/{version}")
def precedent_check(domain: str, version: str) -> dict:
    """Which precedents this policy reverses, and which the status quo already does."""
    return found(report.precedent_check, domain, version)


@app.get("/api/calibration/{domain}/{version}")
def calibration(domain: str, version: str) -> dict:
    """Is the judge right? Scored against the humans who ruled on the same cases."""
    return found(report.calibration, domain, version)


@app.get("/api/disparity/{domain}/{version}")
def disparity(domain: str, version: str) -> dict:
    """Segments the change lands on far harder than the rest of their field."""
    return found(report.disparity, domain, version)


@app.get("/api/preflight/{domain}/{version}")
def preflight(domain: str, version: str) -> dict:
    """Problems readable in the policy text itself, before a replay is paid for."""
    return found(report.preflight, domain, version)


@app.get("/api/rules/{domain}/{version}")
def rule_agreement(domain: str, version: str) -> dict:
    """Do the offline rules implement the policy? The number the sweep rests on."""
    return found(report.rule_agreement, domain, version)


@app.get("/api/drafts/{domain}")
def drafts(domain: str) -> list[dict]:
    """Amendments drafted by the proposal DAG, with the evidence behind each."""
    return found(report.drafts, domain)


@app.get("/api/history/{domain}")
def history(domain: str) -> dict:
    """Every version replayed, side by side: did the edit actually help?"""
    return found(report.history, domain)


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


@app.get("/api/sweep-grid/{domain}/{version}")
def sweep_grid(domain: str, version: str,
               field: str = Query(..., min_length=1, max_length=64),
               values: str = Query(..., min_length=1, max_length=256),
               field2: str = Query(..., min_length=1, max_length=64),
               values2: str = Query(..., min_length=1, max_length=256),
               clause: str = Query(default="", max_length=16),
               clause2: str = Query(default="", max_length=16)) -> dict:
    """Two thresholds at once, as a grid. Single sweeps cannot show them interacting."""
    return found(report.joint_sweep, domain, version, field, values, field2, values2,
                 clause, clause2)


@app.get("/api/export/{domain}/{version}.json")
def export_json(domain: str, version: str) -> JSONResponse:
    """Everything the Explorer shows, in one file, with the caveats attached."""
    bundle = found(report.export_bundle, domain, version)
    return JSONResponse(
        bundle,
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("ptm", domain, version)}.json"'},
    )


@app.get("/api/export/{domain}/{version}.csv")
def export_csv(domain: str, version: str) -> PlainTextResponse:
    """The flip set as CSV, for the spreadsheet the decision gets argued in."""
    body = found(report.flips_csv, domain, version)
    return PlainTextResponse(
        body,
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{filename("ptm", domain, version, "flips")}.csv"'},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


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
