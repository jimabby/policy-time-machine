"""The FastAPI plugin: the routing layer the read models sit behind.

:mod:`ptm.report` is covered without a web framework, which is the point of
keeping the plugin thin. But "thin" is a claim, and the parts that only exist
in the plugin - status codes, query-parameter bounds, download headers, and the
exact URLs the dashboard asks for - are not exercised by that coverage at all.

These need Airflow (the module imports ``AirflowPlugin``) and FastAPI, so they
skip locally and run in the CI job that installs both. ``PTM_REQUIRE_PLUGIN=1``
turns a skip into a failure there, because a suite that silently declines to
run is the failure mode this project keeps finding in itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "policy_time_machine_plugin.py"
DASHBOARD = REPO / "plugins" / "dashboard.html"

import_error: str | None = None
try:  # pragma: no cover - depends on the environment
    import airflow  # noqa: F401
    from fastapi.testclient import TestClient
except Exception as exc:  # pragma: no cover
    import_error = f"{type(exc).__name__}: {exc}"

available = import_error is None

if not available and os.environ.get("PTM_REQUIRE_PLUGIN") == "1":  # pragma: no cover
    raise RuntimeError(
        "PTM_REQUIRE_PLUGIN=1 but the plugin could not be imported, so these tests "
        f"would have silently skipped. Import failed with: {import_error}"
    )

needs_plugin = pytest.mark.skipif(
    not available, reason=f"Airflow and FastAPI are needed ({import_error})")


def load_plugin():
    """Import the plugin by path - ``plugins/`` is a DAGs-folder sibling, not a package."""
    spec = importlib.util.spec_from_file_location("ptm_plugin_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Values to fill the dashboard's URL templates with.
CONCRETE = {"domain": "expenses", "version": "v2", "left": "v1", "right": "v2"}

#: Endpoints the page calls with a query string it assembles separately, so the
#: bare path extracted from the source would be a 422 rather than a real call.
REQUIRED_QUERY = {
    "/api/sweep/": "field=amount_gbp&values=50,75&clause=1.1",
    "/api/sweep-grid/": ("field=amount_gbp&values=50,75&clause=1.1"
                         "&field2=days_notice&values2=3,7&clause2=3.1"),
}


def dashboard_api_urls() -> list[str]:
    """Every /api/ URL the dashboard actually asks for, made concrete.

    Read out of the page rather than restated here, so a renderer that starts
    calling a new endpoint is covered without anyone remembering to add it -
    and a typo in one becomes a failing test instead of an empty panel.

    All three quote styles, not only backticks. A URL with nothing to
    interpolate is written as a plain string, so a backtick-only scan silently
    skipped those - /api/domains among them, the one call every render makes.
    """
    html = DASHBOARD.read_text(encoding="utf-8")
    urls = set()
    for raw in re.findall(r"""["'`](/(?:ptm/)?api/[^"'`]*)["'`]""", html):
        url = raw.replace("/ptm/api/", "/api/", 1)
        if "?" in url:  # the sweep builds its query string separately
            url = url.split("?", 1)[0]
        if "${" in url:
            url = re.sub(r"\$\{(\w+)\}",
                         lambda m: CONCRETE.get(m.group(1), m.group(1)), url)
        if "${" in url or "$" in url:
            continue  # anything still interpolated is not a fixed endpoint
        for prefix, query in REQUIRED_QUERY.items():
            if url.startswith(prefix):
                url = f"{url}?{query}"
        urls.add(url)
    return sorted(urls)


@pytest.fixture(scope="module")
def client(replayed):
    return TestClient(load_plugin().app)


@needs_plugin
class TestEveryUrlTheDashboardAsksFor:
    """The contract between the page and the API, checked in both directions."""

    def test_the_page_asks_for_something(self):
        urls = dashboard_api_urls()
        assert len(urls) >= 10, f"only found {urls}; the extraction is probably broken"

    def test_every_one_of_them_resolves(self, client):
        failures = {}
        for url in dashboard_api_urls():
            response = client.get(url)
            if response.status_code != 200:
                failures[url] = response.status_code
        assert not failures, f"the dashboard calls URLs the API does not serve: {failures}"

    def test_and_returns_json_or_a_download(self, client):
        for url in dashboard_api_urls():
            response = client.get(url)
            kind = response.headers["content-type"].split(";")[0]
            assert kind in {"application/json", "text/csv", "text/plain"}, (url, kind)

    def test_and_no_endpoint_exists_that_nothing_asks_for(self):
        """The other direction, and the one that rots quietly.

        Coverage of this API comes from the URLs the page asks for, so an
        endpoint the page never calls is an endpoint no test ever reaches - it
        can 500 for a release without anybody noticing. Adding a read model and
        forgetting to render it is the normal way that happens.
        """
        from plugins.policy_time_machine_plugin import app

        asked = {url.split("?")[0] for url in dashboard_api_urls()}
        unused = []
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/"):
                continue
            # Compare on shape: the page's URLs are concrete, the routes are
            # templated, so both are reduced to their fixed prefix.
            prefix = path.split("{")[0]
            if not any(url.startswith(prefix) for url in asked):
                unused.append(path)
        assert not unused, (
            f"these endpoints are served but nothing on the page calls them, so no "
            f"test reaches them: {unused}")


@needs_plugin
class TestReadRoutes:
    @pytest.mark.parametrize("path", [
        "/api/domains",
        "/api/summary/expenses/v2",
        "/api/flips/expenses/v2",
        "/api/clauses/expenses/v2",
        "/api/segments/expenses/v2",
        "/api/deviations/expenses/v2",
        "/api/precedent-check/expenses/v2",
        "/api/thresholds/expenses/v2",
        "/api/cost/expenses/v2",
        "/api/stability/expenses/v2",
        "/api/conflicts/expenses",
        "/api/precedents/expenses",
        "/api/compare/expenses/v1/v2",
    ])
    def test_returns_data(self, client, path):
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert response.json() is not None

    def test_the_summary_carries_the_headline_numbers(self, client):
        body = client.get("/api/summary/expenses/v2").json()
        assert body["flips"] == 147 and body["cases"] == 600
        assert body["deviation_flips"] > 0
        assert "flip_confirmation" in body

    def test_flips_come_back_biggest_first(self, client):
        rows = client.get("/api/flips/expenses/v2").json()
        impacts = [r["impact"] for r in rows]
        assert impacts == sorted(impacts, reverse=True)

    def test_the_dashboard_page_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Policy Time Machine" in response.text
        assert response.headers["content-type"].startswith("text/html")


@needs_plugin
class TestNotFound:
    """LookupError becomes a 404 rather than an opaque 500."""

    @pytest.mark.parametrize("path", [
        "/api/summary/nope/v2",
        "/api/summary/expenses/v99",
        "/api/flips/nope/v2",
        "/api/deviations/expenses/v99",
        "/api/precedent-check/nope/v2",
        "/api/conflicts/nope",
        "/api/export/nope/v2.json",
        "/api/export/expenses/v99.csv",
    ])
    def test_unknown_domain_or_version(self, client, path):
        response = client.get(path)
        assert response.status_code == 404, response.text
        assert "Unknown" in response.json()["detail"]


@needs_plugin
class TestQueryBounds:
    """Bounds live on the route, so nothing else covers them."""

    def test_a_limit_over_the_cap_is_rejected(self, client):
        assert client.get("/api/flips/expenses/v2?limit=501").status_code == 422

    def test_a_limit_of_zero_is_rejected(self, client):
        assert client.get("/api/flips/expenses/v2?limit=0").status_code == 422

    def test_a_limit_inside_the_cap_is_honoured(self, client):
        assert len(client.get("/api/flips/expenses/v2?limit=5").json()) == 5

    def test_sweep_needs_its_parameters(self, client):
        assert client.get("/api/sweep/expenses/v2").status_code == 422


@needs_plugin
class TestSweepRoute:
    def test_runs_over_the_query_string(self, client):
        body = client.get(
            "/api/sweep/expenses/v2?field=amount_gbp&values=50,75&clause=1.1").json()
        assert [p["value"] for p in body["points"]] == [50, 75]
        assert body["current_value"] == 75

    def test_a_malformed_value_list_is_refused_not_crashed(self, client):
        response = client.get("/api/sweep/expenses/v2?field=amount_gbp&values=50,banana")
        assert response.status_code == 404
        assert "comma-separated numbers" in response.json()["detail"]

    def test_an_unknown_dial_says_which_ones_exist(self, client):
        response = client.get("/api/sweep/expenses/v2?field=nonsense&values=1")
        assert response.status_code == 404
        assert "available dials" in response.json()["detail"]

    def test_too_many_values_is_refused(self, client):
        values = ",".join(str(i) for i in range(50))
        response = client.get(f"/api/sweep/expenses/v2?field=amount_gbp&values={values}")
        assert response.status_code == 404
        assert "cap is 40" in response.json()["detail"]


@needs_plugin
class TestExport:
    """The numbers have to be able to leave the dashboard."""

    def test_csv_is_served_as_a_download(self, client):
        response = client.get("/api/export/expenses/v2.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert "ptm-expenses-v2-flips.csv" in response.headers["content-disposition"]

    def test_csv_has_a_header_and_a_row_per_flip(self, client):
        lines = client.get("/api/export/expenses/v2.csv").text.strip().split("\n")
        assert lines[0].startswith("case_id,")
        assert len(lines) - 1 == 147

    def test_json_is_served_as_a_download(self, client):
        response = client.get("/api/export/expenses/v2.json")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        assert "ptm-expenses-v2.json" in response.headers["content-disposition"]

    def test_json_carries_the_whole_picture_with_its_caveats(self, client):
        body = json.loads(client.get("/api/export/expenses/v2.json").text)
        for key in ("summary", "clauses", "segments", "deviations", "precedent_check",
                    "conflicts", "precedents", "cost", "stability", "flips", "caveats"):
            assert key in body, key
        assert body["caveats"], "figures leaving the dashboard must take their caveats"


@needs_plugin
class TestPluginRegistration:
    """Airflow finds the app and the nav entry through these attributes."""

    def test_registers_the_fastapi_app_under_ptm(self):
        plugin = load_plugin().PolicyTimeMachinePlugin
        [entry] = plugin.fastapi_apps
        assert entry["url_prefix"] == "/ptm"
        assert entry["app"] is load_plugin().app or entry["app"] is not None

    def test_registers_the_nav_view(self):
        [view] = load_plugin().PolicyTimeMachinePlugin.external_views
        assert view["href"] == "/ptm/"
        assert view["destination"] == "nav"

    def test_the_dashboard_fetches_through_that_same_prefix(self):
        """A mismatch here serves a page whose every request 404s."""
        html = DASHBOARD.read_text(encoding="utf-8")
        assert 'fetch("/ptm" + path)' in html
