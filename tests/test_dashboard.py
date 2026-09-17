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

# Chromium here has no Airflow login, and this suite is about what the page
# renders rather than who may see it. The gate itself is proved in
# tests/test_plugin.py, which takes this allowance away again.
os.environ["PTM_ALLOW_ANONYMOUS"] = "1"


def replay_domain(domain_name: str, version: str) -> None:
    """A full point-in-time replay, the way conftest does it for expenses."""
    import datetime

    from ptm import cost, diff, store
    from ptm.config import load_domain
    from ptm.judge import offline_verdict

    domain = load_domain(domain_name)
    cases = store.load_cases(domain_name, until=datetime.datetime(2026, 9, 1))
    candidate = {c.case_id: offline_verdict(c, domain, version) for c in cases}
    baseline = {c.case_id: offline_verdict(c, domain, domain.in_force) for c in cases}
    flips = diff.flips(cases, candidate, domain, baseline=baseline)
    store.save_replay(
        f"browsertest__{domain_name}", domain_name, version, "actual", len(cases), flips,
        diff.summarise(flips, len(cases), domain)["net_impact"], candidate,
        segments=diff.segment_stats(cases, flips, domain),
        case_segments=diff.case_segment_rows(cases, domain),
        ledger=cost.zero(), baseline_version=domain.in_force, baseline_verdicts=baseline)


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

    # The other domain is replayed too: switching to it is the demo's closing
    # move, and a domain with no results exercises the empty states rather than
    # the rendering this is here to check.
    replay_domain("refunds", "v2")

    flips = report.flips("expenses", "v2", limit=3)
    agreed, reversed_, shaky = flips[0], flips[1], flips[2]

    # Start from no rulings at all, for the two domains this fixture sets up.
    #
    # This suite is the only one whose assertions are about the *absence* of a
    # ruling - the Superseded panel says "no ruling has been superseded", and it
    # is true only if nothing has been. Precedents and their history are
    # deliberately outside DERIVED_TABLES, which is right (a re-seed must not
    # forget a human ruling) and means they accumulate across the whole session
    # in the one database conftest builds. Any earlier module that recorded a
    # ruling twice on the same case - test_calibration_gate did, once per test
    # using its fixture - left a precedent_history row here and failed two tests
    # in this file. Only when the full suite ran: CI runs this job on its own,
    # so the leak was invisible to it.
    #
    # Raw SQL because there is no API for this and there should not be. Deleting
    # a precedent is the one operation the engine deliberately does not offer.
    with store.conn() as connection:
        for table in ("precedents", "precedent_history"):
            connection.execute(f"DELETE FROM {table} WHERE domain IN ('expenses', 'refunds')")

    # One ruling with its circumstances recorded and one without, so the panel
    # renders both halves: a ruling that can be re-read later, and one made
    # before the circumstances were captured - which the page has to mark rather
    # than show as though it were fresh.
    for row, outcome, circumstances in (
            (agreed, agreed["new_outcome"],
             {"policy_version": "v2", "judged_outcome": agreed["new_outcome"],
              "judged_clause": agreed["policy_clause"] or "1.1"}),
            (reversed_, reversed_["actual_outcome"], {})):
        store.save_precedent(Precedent(
            case_id=row["case_id"], domain="expenses", correct_outcome=outcome,
            ruled_by="finance.lead", note="Ruled during the browser test.",
            established_at=datetime.datetime(2026, 1, 1), **circumstances))

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
    """A loaded dashboard, switched to the detail view, console errors collected.

    Errors are asserted on in one place rather than per test, because a page
    that throws halfway through rendering still leaves most assertions passing.

    The page opens on the plain summary, which is what somebody deciding
    whether to ship a rule change should meet first. Almost everything below is
    about the detail view behind it, so the switch happens here rather than in
    thirty tests - and ``TestThePlainSummary`` is the one class that switches
    back, including to prove the plain view is what an untouched visit lands on.
    """
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    raw = context.new_page()
    errors: list[str] = []
    raw.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    raw.on("console",
           lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    raw.goto(server, wait_until="networkidle")
    raw.click("#viewdetail")
    raw.wait_for_selector("#tiles .tile", timeout=15_000)
    yield Dashboard(raw, errors)
    context.close()


@pytest.fixture
def visit(browser, server):
    """Open the dashboard at a chosen query string, in a reusable context.

    The ``page`` fixture is one page in a fresh context, which is what most of
    this file wants. Deep links and a remembered selection are both about what a
    *second* visit does, so they need the context to outlive the page.
    """
    contexts = []

    def open_at(suffix: str = "", context=None):
        if context is None:
            context = browser.new_context(viewport={"width": 1400, "height": 1000})
            contexts.append(context)
        raw = context.new_page()
        errors: list[str] = []
        raw.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        raw.on("console",
               lambda m: errors.append(f"console.{m.type}: {m.text}")
               if m.type == "error" else None)
        raw.goto(server + suffix, wait_until="domcontentloaded")
        raw.click("#viewdetail")
        raw.wait_for_selector("#tiles .tile", timeout=15_000)
        return Dashboard(raw, errors), context

    yield open_at
    for context in contexts:
        context.close()


@needs_browser
class TestTheSelectionIsAddressable:
    """Which replay you are looking at is the one thing about this page worth
    sending to somebody else, and the one thing a refresh used to throw away.
    """

    def test_a_plain_visit_still_lands_on_the_default(self, visit):
        page, _ = visit()
        assert page.eval_on_selector("#domain", "e => e.value") == "expenses"
        assert page.eval_on_selector("#version", "e => e.value") == "v2"

    def test_the_address_bar_becomes_the_link(self, visit):
        """Nobody copies a URL they had to construct."""
        page, _ = visit()
        page.wait_for_function(
            "() => location.search.includes('domain=') && location.search.includes('version=')",
            timeout=10_000)
        query = page.evaluate("location.search")
        assert "domain=expenses" in query and "version=v2" in query

    def test_a_link_opens_the_replay_it_names(self, visit):
        page, _ = visit("?domain=refunds&version=v2")
        assert page.eval_on_selector("#domain", "e => e.value") == "refunds"
        page.wait_for_function(
            "() => document.querySelector('#segments').innerText.toLowerCase()"
            ".includes('by tier')", timeout=15_000)
        assert page.errors == []

    def test_a_link_beats_what_that_reader_looked_at_last(self, visit):
        """Otherwise the link is a lie: two people open the same URL and see
        different replays, and neither of them can tell."""
        first, context = visit()
        first.select_option("#version", "v1")
        first.wait_for_function(
            "() => location.search.includes('version=v1')", timeout=10_000)
        first.page.close()

        second, _ = visit("?domain=expenses&version=v2", context=context)
        assert second.eval_on_selector("#version", "e => e.value") == "v2"

    def test_with_no_link_the_last_selection_comes_back(self, visit):
        first, context = visit()
        first.select_option("#domain", "refunds")
        first.wait_for_function(
            "() => location.search.includes('domain=refunds')", timeout=10_000)
        first.page.close()

        second, _ = visit(context=context)
        assert second.eval_on_selector("#domain", "e => e.value") == "refunds"

    def test_a_link_to_something_this_deployment_lacks_falls_back(self, visit):
        """A stale link out of an old ticket must not render a blank page."""
        page, _ = visit("?domain=nope&version=v99")
        assert page.eval_on_selector("#domain", "e => e.value") == "expenses"
        assert page.eval_on_selector("#version", "e => e.value") == "v2"
        assert page.inner_text("#tiles").strip()
        assert page.errors == []


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
        for panel in ("#tiles", "#preflight", "#clauses", "#segments", "#disparity",
                      "#deviations", "#flips", "#precedents", "#conflicts", "#stability",
                      "#calibration", "#drafts", "#sweep", "#sweepgrid", "#crosscheck",
                      "#rules", "#history", "#superseded"):
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

    def test_the_precedent_panel_says_which_rulings_predate_the_current_text(self, page):
        """The gate enforces every ruling as hard as one made this morning. A
        ruling with no circumstances on file is the one case nothing else on the
        page could ever distinguish, so it has to be marked here."""
        text = page.text("#precedents")
        assert "not recorded" in text, \
            "a ruling whose circumstances were never captured must say so"
        assert "were made about a version of the policy" in text

    def test_the_cross_check_panel_says_what_has_not_been_measured(self, page):
        """Nothing in the fixture runs a second model, so this panel renders its
        unmeasured state - which has to explain itself rather than look like a
        check that passed."""
        text = page.text("#crosscheck")
        assert "compares the judge to itself" in text

    def test_the_grid_invites_the_question_a_curve_cannot_answer(self, page):
        text = page.text("#sweepgrid")
        assert "holds every other" in text

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

    def test_the_preflight_panel_reports_on_the_policy_itself(self, page):
        """It finds one real thing in the shipped v2 - a clause defined only by
        reference to v1 - so this panel is never the empty state here."""
        text = page.text("#preflight")
        assert "clause 7.1" in text
        assert "unreachable" in text

    def test_the_disparity_panel_names_the_segment_and_what_it_is_measured_against(self, page):
        text = page.text("#disparity")
        assert "meals" in text
        assert "rest of field" in text
        assert "question, not a verdict" in text, "it never calls a concentration unfair"

    def test_the_calibration_panel_scores_the_judge_against_the_humans(self, page):
        """Seeded with two rulings, one of which the policy reverses - so the
        panel has both an agreement and a disagreement to render."""
        text = page.text("#calibration")
        assert "human ruling" in text
        assert "confident by" in text
        assert "floor on the judge's accuracy" in text

    def test_the_rules_panel_says_the_offline_figure_measures_nothing(self, page):
        """The verdicts it scores against were produced by these same rules. A
        100% that does not say why is worse than no number."""
        text = page.text("#rules")
        assert "by construction" in text

    def test_the_drafts_panel_has_an_empty_state_rather_than_a_blank(self, page):
        assert "no drafts yet" in page.text("#drafts")

    def test_the_history_panel_puts_the_versions_side_by_side(self, page):
        """The question no single-version panel can answer: did the edit help?"""
        text = page.text("#history")
        assert "v1" in text and "v2" in text
        assert "in force" in text, "which one is the status quo has to be visible"

    def test_the_superseded_panel_says_so_when_nothing_was_re_adjudicated(self, page):
        """Re-adjudication is the only thing that overwrites a precedent, so an
        empty panel here is the normal state and has to read as one."""
        assert "no ruling has been re-adjudicated" in page.text("#superseded")

    def test_the_history_panel_says_what_it_is_not_claiming(self, page):
        """Two versions measured on different numbers of cases differ by
        sample size before they differ by policy."""
        assert "only comparable over the same cases" in page.text("#history")


@needs_browser
class TestTheInstructions:
    """The panel that explains the rest of the page.

    Its steps are built from the translation table rather than written into the
    markup, so a key that goes missing empties the list rather than announcing
    itself - which is the failure this catches.
    """

    def opened(self, page) -> str:
        """The panel's text with the panel open.

        It ships collapsed, and a collapsed ``<details>`` renders none of its
        content - so ``inner_text`` returns the summary alone until something
        opens it, exactly as it would for a reader.
        """
        page.click("#howto > summary")
        page.wait_for_function("() => document.querySelector('#howto').open",
                               timeout=5_000)
        return page.text("#howto")

    def test_it_offers_a_reading_order(self, page):
        assert "how to read this page" in page.text("#howto")
        assert page.locator("#howtosteps li").count() == 8

    def test_it_names_the_panel_that_is_easiest_to_misread(self, page):
        """The deviation bucket is the one number here that is routinely charged
        to the wrong party, so the instructions have to say whose it is."""
        assert "not this policy's doing" in self.opened(page)

    def test_it_says_the_page_triggers_nothing(self, page):
        """An empty panel is a DAG that has not run, not a page that is broken,
        and only the instructions are in a position to say so."""
        assert "nothing on this page triggers a dag" in self.opened(page)

    def test_it_starts_collapsed(self, page):
        """Every reader after the first has already read it."""
        assert page.eval_on_selector("#howto", "e => e.open") is False


@needs_browser
class TestTheLanguageSwitcher:
    """Both languages, rendered by the same code against the same API.

    English is asserted everywhere else in this file, so what is left to prove
    is that a switch redraws the whole page rather than most of it: a panel the
    switch does not reach keeps its old language and reads as a translation
    nobody wrote.
    """

    def to_chinese(self, page):
        page.select_option("#lang", "zh")
        page.wait_for_function(
            "() => document.documentElement.lang === 'zh-Hans'", timeout=15_000)
        page.wait_for_selector("#tiles .tile", timeout=15_000)
        # A tile is redrawn synchronously; the panels below it are redrawn from
        # a fresh round of requests, so wait for the last of those to land.
        page.wait_for_function(
            "() => document.querySelector('#drafts').innerText.includes('尚无草案')",
            timeout=15_000)

    def test_it_defaults_to_english(self, page):
        assert page.eval_on_selector("#lang", "e => e.value") == "en"
        assert page.get_attribute("html", "lang") == "en"

    def test_switching_redraws_every_kind_of_string(self, page):
        self.to_chinese(page)
        assert page.title() == "政策时光机"
        # A tile, a table header, a row cell, an empty state and the
        # instructions: every shape of string the page builds.
        for panel, chinese in (("#tiles", "不是本政策造成的"),   # deviations tile
                               ("#clauses", "责任条款"),         # a column header
                               ("#flips", "未能复现"),           # a row cell
                               ("#superseded", "没有任何裁定"),  # an empty state
                               ("#howto", "如何阅读本页")):      # the instructions
            assert chinese in page.inner_text(panel), panel
        assert page.errors == []

    def test_nothing_is_left_blank_by_the_switch(self, page):
        """A key the Chinese table has no entry for falls back to English. A
        panel that comes up empty instead is what this is here to catch."""
        self.to_chinese(page)
        empty = [panel for panel in
                 ("#tiles", "#preflight", "#clauses", "#segments", "#disparity",
                  "#deviations", "#flips", "#precedents", "#conflicts", "#stability",
                  "#calibration", "#drafts", "#crosscheck", "#rules", "#history",
                  "#superseded")
                 if not page.inner_text(panel).strip()]
        assert not empty, f"panels emptied by the language switch: {empty}"

    def test_the_caveats_the_API_wrote_arrive_translated(self, page):
        """A caveat is the limit on the number above it. A page that renders the
        numbers in one language and their limits in another is the version of
        this that gets a figure quoted without them."""
        self.to_chinese(page)
        page.wait_for_function(
            "() => document.querySelector('#disparity').innerText.includes('分组')",
            timeout=15_000)
        assert "两个版本只有在相同的案例集上才具可比性" in page.inner_text("#history")
        assert "集中现象是一个问题" in page.inner_text("#disparity")
        assert "准确率的下限" in page.inner_text("#calibration")

    def test_a_hint_keeps_the_DAG_name_it_tells_you_to_run(self, page):
        """The hint's whole job is naming a command. Translating the sentence
        around an identifier must not translate the identifier."""
        self.to_chinese(page)
        assert "judge_stability_expenses" in page.inner_text("#crosscheck")

    def test_data_from_the_API_is_shown_as_written(self, page):
        """A reviewer's note is evidence, not chrome. Translating it would put
        words into a person's mouth, so it stays exactly as it was recorded."""
        self.to_chinese(page)
        assert "finance.lead" in page.text("#precedents")
        assert "ruled during the browser test" in page.text("#precedents")

    def test_switching_back_restores_english(self, page):
        self.to_chinese(page)
        page.select_option("#lang", "en")
        page.wait_for_function(
            "() => document.documentElement.lang === 'en'", timeout=15_000)
        page.wait_for_function(
            "() => document.querySelector('#drafts').innerText.includes('No drafts yet')",
            timeout=15_000)
        assert "not this policy's doing" in page.text("#tiles")

    def test_it_does_not_move_the_reader_off_their_version(self, page):
        """Relabelling the page is not navigation. Switching language redraws
        through the same path that picks the default version, so without care it
        quietly returns a reader from v1 to v2 while they are reading it."""
        page.select_option("#version", "v1")
        page.wait_for_function(
            "() => location.search.includes('version=v1')", timeout=10_000)
        self.to_chinese(page)
        assert page.eval_on_selector("#version", "e => e.value") == "v1"

    def test_the_choice_survives_a_reload(self, page):
        self.to_chinese(page)
        page.reload(wait_until="networkidle")
        page.wait_for_selector("#tiles .tile", timeout=15_000)
        assert page.eval_on_selector("#lang", "e => e.value") == "zh"


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
class TestThePlainSummary:
    """The view a visit lands on, for the person the decision belongs to.

    Everything it shows is rendered from the objects the detail panels render
    from, so what is worth asserting here is not the arithmetic - that is
    covered against the read models directly - but that the summary says the
    things somebody could otherwise get wrong: which way the change goes, how
    much of it this proposal caused, whose cases move, and whether a
    measurement was taken at all.
    """

    @pytest.fixture
    def plain(self, page):
        page.click("#viewplain")
        page.wait_for_selector("#verdict .big", timeout=15_000)
        return page

    def test_it_is_what_an_untouched_visit_lands_on(self, browser, server):
        """The detail view is a click away; it must not be the doorstep."""
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        raw = context.new_page()
        raw.goto(server, wait_until="networkidle")
        raw.wait_for_selector("#verdict .big", timeout=15_000)
        assert raw.is_visible("#plain")
        assert not raw.is_visible("#detail")
        context.close()

    def test_the_headline_is_one_sentence_with_both_numbers_in_it(self, plain):
        text = plain.text("#verdict")
        assert "147" in text and "600" in text and "v2" in text
        assert "135 more generous" in text and "12 stricter" in text

    def test_the_headline_separates_what_this_proposal_caused(self, plain):
        """The number most often quoted wrong. In the detail view it is a tile
        somebody has to know to read; here it has to be unavoidable."""
        assert "109 of those are this proposal's doing" in plain.text("#verdict")

    def test_the_gate_result_is_on_the_headline(self, plain):
        assert "overturns 1 ruling a person already made" in plain.text("#verdict")

    def test_how_it_works_is_a_diagram_rather_than_a_paragraph(self, plain):
        """Five steps, drawn. The prose version of this is still in the how-to
        panel; what a reader meets first is the picture."""
        assert plain.locator("#howdiagram svg").count() == 1
        text = plain.text("#howdiagram")
        for step in ("past decisions", "decide again", "compare",
                     "a person rules", "kept as tests"):
            assert step in text, step

    def test_the_flow_splits_the_change_three_ways(self, plain):
        text = plain.text("#flowdiagram")
        assert "453 same answer" in text and "147 different answer" in text
        assert "135 more generous" in text and "12 stricter" in text
        assert "109 caused by this change" in text
        assert "38 already at odds" in text

    def test_every_bar_segment_that_exists_is_drawn(self, plain):
        """A slice floored out of existence is a picture saying a category is
        empty while the legend beside it says it is not."""
        widths = plain.page.eval_on_selector_all(
            "#flowdiagram svg.flowbar rect", "els => els.map(e => +e.getAttribute('width'))")
        assert widths, "no bars were drawn"
        assert min(widths) > 0
        # Six segments across three bars, and each bar fills its own viewBox.
        assert len(widths) == 6
        for start in (0, 2, 4):
            assert abs(sum(widths[start:start + 2]) - 1000) < 1

    def test_who_it_affects_keeps_the_denominator(self, plain):
        """"52.6%" is one case in two as readily as sixty-one in a hundred."""
        text = plain.text("#whodiagram")
        assert "by category" in text and "by grade" in text
        assert "61 of 116" in text

    def test_the_clause_bar_names_the_sentence_to_edit(self, plain):
        text = plain.text("#whydiagram")
        assert "clause 1.1 relaxed" in text
        assert "not caused by this change" in text, "the deviation bucket is marked"

    def test_the_trust_cards_say_what_was_never_measured(self, plain):
        """"Not checked yet" and "fine" are different statements, and a summary
        that renders the first as the second is the one way this page could
        actively mislead somebody."""
        text = plain.text("#trustcards")
        assert plain.locator("#trustcards .card").count() == 4
        assert "not checked yet" in text
        assert "1 of 2 cases" in text, "calibration was measured, so it is reported"

    def test_the_gate_checklist_separates_who_caused_the_reversal(self, plain):
        text = plain.text("#gatecard")
        assert "overturns 1 of them" in text
        assert "none of those are this proposal's doing" in text
        assert "rule in force today already overturns" in text

    def test_the_switch_shows_one_view_at_a_time(self, plain):
        assert plain.page.is_visible("#plain")
        assert not plain.page.is_visible("#detail")
        plain.click("#viewdetail")
        assert plain.page.is_visible("#detail")
        assert not plain.page.is_visible("#plain")

    def test_see_all_the_numbers_goes_to_the_detail_view(self, plain):
        plain.click("#godetail")
        assert plain.page.is_visible("#detail")

    def test_it_renders_without_a_console_error(self, plain):
        assert plain.errors == []

    def test_it_speaks_the_other_language_too(self, plain):
        plain.select_option("#lang", "zh")
        plain.wait_for_function(
            "() => document.querySelector('#verdict').innerText.includes('决策')",
            timeout=15_000)
        for panel, chinese in (("#howdiagram", "既往决策"),
                               ("#flowdiagram", "结果相同"),
                               ("#trustcards", "尚未检查"),
                               ("#gatecard", "人工裁定")):
            assert chinese in plain.page.inner_text(panel), panel
        assert plain.errors == []


@needs_browser
class TestExportAndNavigation:
    def test_the_download_links_point_at_the_export_routes(self, page):
        assert page.get_attribute("#dlcsv", "href") == "/ptm/api/export/expenses/v2.csv"
        assert page.get_attribute("#dljson", "href") == "/ptm/api/export/expenses/v2.json"

    def test_an_unreplayed_version_says_so_rather_than_blaming_the_config(self, page):
        """The panel has two empty states and they are not interchangeable:
        telling someone their segment_fields are missing sends them to edit a
        YAML file that is already correct."""
        page.select_option("#version", "v1")
        page.wait_for_function(
            "() => document.querySelector('#segments').innerText"
            ".toLowerCase().includes('no replay results')",
            timeout=15_000)
        text = page.text("#segments")
        assert "declares no" not in text
        assert "category" in text and "grade" in text, "it still names the segments"

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

    def test_filtering_asks_the_API_for_nothing(self, page):
        """The rows are already in hand and the query changes no other panel.
        Re-fetching all nineteen read models on every keystroke is what this
        replaced, and nothing about the rendering would show it came back."""
        calls: list[str] = []
        page.on("request", lambda r: calls.append(r.url) if "/api/" in r.url else None)
        before = page.locator("#flips tr.row").count()
        page.fill("#search", "exp-0084")
        page.wait_for_function(
            f"() => document.querySelectorAll('#flips tr.row').length < {before}",
            timeout=10_000)
        assert calls == []
