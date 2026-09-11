"""The Diff Explorer, in a real browser, against the real API.

Everything else in this suite proves the *data* is right. None of it proves the
page renders it: a renderer reading a field the API stopped returning, an id
that no longer matches, an exception halfway through a panel - all of those
leave the tests green and the dashboard blank.

So this serves the actual plugin app, loads the actual page in Chromium, and
fails on any console error as well as on any panel that came up empty. Needs
Airflow, FastAPI and Playwright, so it skips locally and runs in the CI job
that installs them; ``PTM_REQUIRE_BROWSER=1`` makes a skip fatal there.
"""

from __future__ import annotations

import os
import pathlib
import socket
import threading

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

import_error: str | None = None
try:  # pragma: no cover - depends on the environment
    import airflow  # noqa: F401
    import uvicorn
    from fastapi import FastAPI
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover
    import_error = f"{type(exc).__name__}: {exc}"

available = import_error is None

if not available and os.environ.get("PTM_REQUIRE_BROWSER") == "1":  # pragma: no cover
    raise RuntimeError(
        "PTM_REQUIRE_BROWSER=1 but the browser test could not be set up, so it "
        f"would have silently skipped. Import failed with: {import_error}"
    )

needs_browser = pytest.mark.skipif(
    not available, reason=f"Airflow, FastAPI and Playwright are needed ({import_error})")


def load_plugin():
    """Import the plugin by path - ``plugins/`` is a DAGs-folder sibling, not a package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "ptm_plugin_under_browser_test", REPO / "plugins" / "policy_time_machine_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Dashboard:
    """A loaded page, plus everything the browser complained about.

    A thin wrapper rather than an attribute hung off Playwright's Page, which
    is not ours to extend.
    """

    def __init__(self, page, errors: list[str]) -> None:
        self.page = page
        self.errors = errors

    def __getattr__(self, name):
        return getattr(self.page, name)

    def text(self, selector: str) -> str:
        """Panel text, lowercased.

        ``text-transform: uppercase`` is a style, not content - inner_text
        returns it uppercased and an assertion on the real wording would fail
        for a page that is rendering perfectly.
        """
        return self.page.inner_text(selector).lower()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def adjudicated(replayed):
    """A replay that has also been ruled on and confirmed.

    Without this the page only ever renders its empty states - no precedent,
    nothing re-judged - and the branches that draw a reversal or a flip that
    would not reproduce go untested.
    """
    import datetime

    from ptm import report, store
    from ptm.models import FlipConfirmation, Precedent

    flips = report.flips("expenses", "v2", limit=3)
    agreed, reversed_, shaky = flips[0], flips[1], flips[2]

    for row, outcome in ((agreed, agreed["new_outcome"]),
                         (reversed_, reversed_["actual_outcome"])):
        store.save_precedent(Precedent(
            case_id=row["case_id"], domain="expenses", correct_outcome=outcome,
            ruled_by="finance.lead", note="Ruled during the browser test.",
            established_at=datetime.datetime(2026, 1, 1)))

    store.save_flip_stability("expenses", "v2", [
        FlipConfirmation(case_id=agreed["case_id"], samples=3,
                         outcomes={agreed["new_outcome"]: 3},
                         modal_outcome=agreed["new_outcome"], agreement=1.0,
                         stable=True, recorded_outcome=agreed["new_outcome"]),
        FlipConfirmation(case_id=shaky["case_id"], samples=3,
                         outcomes={shaky["new_outcome"]: 2, shaky["actual_outcome"]: 1},
                         modal_outcome=shaky["new_outcome"], agreement=0.667,
                         stable=False, recorded_outcome=shaky["new_outcome"]),
    ])
    return {"agreed": agreed["case_id"], "reversed": reversed_["case_id"],
            "shaky": shaky["case_id"]}


@pytest.fixture(scope="module")
def server(adjudicated):
    """The plugin app under /ptm, exactly where Airflow mounts it.

    The prefix is load-bearing: the page fetches "/ptm" + path, so serving it
    anywhere else would pass while the real deployment 404s on every request.
    """
    parent = FastAPI()
    parent.mount("/ptm", load_plugin().app)

    port = free_port()
    config = uvicorn.Config(parent, host="127.0.0.1", port=port, log_level="warning")
    uvicorn_server = uvicorn.Server(config)
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    for _ in range(200):  # ~10s
        if uvicorn_server.started:
            break
        threading.Event().wait(0.05)
    else:  # pragma: no cover
        raise RuntimeError("the test server never started")
    yield f"http://127.0.0.1:{port}/ptm/"
    uvicorn_server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        chromium = p.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser, server):
    """A loaded dashboard, with every console error collected.

    Errors are asserted on in one place rather than per test, because a page
    that throws halfway through rendering still leaves most assertions passing.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    raw = context.new_page()
    errors: list[str] = []
    raw.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    raw.on("console",
           lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    raw.goto(server, wait_until="networkidle")
    raw.wait_for_selector("#tiles .tile", timeout=15_000)
    yield Dashboard(raw, errors)
    context.close()


@needs_browser
class TestItRenders:
    def test_without_a_single_console_error(self, page):
        assert page.errors == []

    def test_the_page_is_titled(self, page):
        assert "Policy Time Machine" in page.title()

    def test_the_status_line_reports_the_replay(self, page):
        assert "run" in page.text("#status")

    def test_every_panel_has_content(self, page):
        """An empty panel is the shape a broken renderer takes."""
        empty = []
        for panel in ("#tiles", "#clauses", "#segments", "#deviations", "#flips",
                      "#precedents", "#conflicts", "#stability", "#sweep"):
            if not page.inner_text(panel).strip():
                empty.append(panel)
        assert not empty, f"panels rendered nothing: {empty}"


@needs_browser
class TestTheHeadline:
    def test_the_tiles_show_the_numbers_the_api_returned(self, page):
        text = page.text("#tiles")
        assert "600" in text, "decisions replayed"
        assert "147" in text, "outcomes that change"

    def test_the_deviation_tile_separates_what_the_policy_did_not_cause(self, page):
        text = page.text("#tiles")
        assert "not this policy's doing" in text
        assert "38" in text and "109 caused by this policy" in text

    def test_the_gate_tile_reports_the_reversal_and_who_caused_it(self, page):
        text = page.text("#tiles")
        assert "gate" in text
        assert "reversed" in text
        assert "introduced by this policy" in text

    def test_the_confirmation_tile_counts_what_held(self, page):
        text = page.text("#tiles")
        assert "flips confirmed" in text
        assert "would not reproduce" in text, "one flip was seeded as unstable"


@needs_browser
class TestPanels:
    def test_clause_attribution_names_the_clause_and_the_deviation_bucket(self, page):
        text = page.text("#clauses")
        assert "clause 1.1 relaxed" in text
        assert "reviewer deviated from policy" in text
        assert "not caused by this policy" in text

    def test_blast_radius_shows_denominators(self, page):
        text = page.text("#segments")
        assert "by category" in text and "by grade" in text
        assert "/" in text, "flips out of cases"

    def test_the_flip_table_has_the_confirmation_column(self, page):
        headers = page.text("#flips thead")
        for column in ("case", "was", "becomes", "responsible for", "confirmed",
                       "precedent"):
            assert column in headers, column

    def test_the_flip_table_marks_what_reproduced_and_what_did_not(self, page):
        text = page.text("#flips")
        assert "reproduced" in text
        assert "did not reproduce" in text

    def test_a_flip_that_reverses_a_ruling_is_called_out(self, page):
        assert "violates" in page.text("#flips")

    def test_the_precedent_panel_lists_the_rulings(self, page):
        text = page.text("#precedents")
        assert "finance.lead" in text
        assert "ruled during the browser test" in text

    def test_a_flip_row_expands_to_show_the_case(self, page):
        """The detail row's colspan has to match the header or it renders wrong."""
        page.click("#flips tr.row >> nth=0")
        detail = page.locator("#flips tr.detail >> nth=0")
        detail.wait_for(state="visible", timeout=5_000)
        assert "under the proposed policy" in detail.inner_text().lower()

    def test_the_deviation_panel_explains_whose_problem_it_is(self, page):
        text = page.text("#deviations")
        assert "in force today" in text
        assert "both policies say" in text

    def test_the_stability_panel_gives_the_error_bar_or_says_it_is_missing(self, page):
        text = page.text("#stability")
        assert "judge" in text or "not measured" in text


@needs_browser
class TestTheSweep:
    def test_it_offers_the_dials_from_the_policy(self, page):
        options = page.text("#swdial")
        assert "clause 1.1" in options and "amount_gbp" in options

    def test_it_prefills_values_around_the_current_setting(self, page):
        assert page.input_value("#swvalues").strip()

    def test_running_it_renders_the_curve(self, page):
        page.click("#swgo")
        page.wait_for_selector("#sweep table tbody tr", timeout=20_000)
        assert "current" in page.text("#sweep")
        assert page.locator("#sweep .spark i").count() > 1
        assert page.errors == []

    def test_a_bad_value_list_fails_visibly_rather_than_silently(self, page):
        page.fill("#swvalues", "not-a-number")
        page.click("#swgo")
        # The idle state is also an .empty div, so wait on the text.
        page.wait_for_function(
            "() => document.querySelector('#sweep').innerText.includes('could not be run')",
            timeout=20_000)


@needs_browser
class TestExportAndNavigation:
    def test_the_download_links_point_at_the_export_routes(self, page):
        assert page.get_attribute("#dlcsv", "href") == "/ptm/api/export/expenses/v2.csv"
        assert page.get_attribute("#dljson", "href") == "/ptm/api/export/expenses/v2.json"

    def test_switching_domain_re_renders_against_the_other_domain(self, page):
        page.select_option("#domain", "refunds")
        page.wait_for_function(
            "() => document.querySelector('#segments').innerText.toLowerCase()"
            ".includes('by tier')",
            timeout=15_000)
        assert "gym membership" in page.text("#label")
        assert page.get_attribute("#dlcsv", "href") == "/ptm/api/export/refunds/v2.csv"
        assert page.errors == []

    def test_filtering_narrows_the_flip_table(self, page):
        before = page.locator("#flips tr.row").count()
        page.fill("#search", "exp-0084")
        page.wait_for_function(
            f"() => document.querySelectorAll('#flips tr.row').length < {before}",
            timeout=10_000)
        assert page.locator("#flips tr.row").count() >= 1
        assert "shown" in page.text("#status")
