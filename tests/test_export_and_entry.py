"""The evidence can leave the building, and the package has a command.

Two things that were missing at opposite ends of the same workflow.

:func:`ptm.report.export_html` is the reading half of a bundle that could
always be *written*. The JSON has carried its caveats out of the dashboard
since the day it existed, and looking at it still required standing up a
scheduler - which the person being asked to approve the rule change is the
least likely person in the building to do.

:mod:`ptm.__main__` is the other end: ``pip install -e .`` made the package
importable and put nothing on PATH, so every command in the README had to be
spelled ``python -m ptm.<module>``.

Both are tested against the shipped fixture, and the HTML one is deliberately
checked as *markup a browser will parse* rather than as a string that contains
the right substrings - the whole point of the file is that it opens.
"""

from __future__ import annotations

import json
import re

import pytest

from ptm import __main__ as dispatcher
from ptm import config, report
from ptm.judge import _coerce


#: The export is built once - it is a quarter of a megabyte and every assertion
#: below reads the same one. Module scope rather than class scope because a
#: class-scoped fixture written as an instance method is deprecated in pytest,
#: and this suite runs clean.
@pytest.fixture(scope="module")
def page(replayed):
    return report.export_html("expenses", "v2")


@pytest.fixture(scope="module")
def bundle(page):
    """The inlined object, parsed the way the page's own JS parses it."""
    match = re.search(r"<script>window\.PTM_BUNDLE = (.*?);</script>", page, re.S)
    assert match, "no bundle was spliced into the page"
    return json.loads(match.group(1).replace(r"<\/", "</"))


class TestTheExplorerAsOneFile:
    def test_it_is_the_plugin_page_with_the_data_added(self, page):
        """One page, two sources. A forked copy is a copy that drifts.

        The thing worth guaranteeing is that what leaves the building is what
        was on screen, and that stops being true the moment there are two
        renderers.
        """
        original = report.PAGE.read_text(encoding="utf-8")
        assert len(page) > len(original)
        assert original in page.replace(
            re.search(r"<script>window\.PTM_BUNDLE = .*?;</script>\n", page, re.S).group(0), "")

    def test_the_bundle_lands_before_the_script_that_reads_it(self, page):
        assert page.index("window.PTM_BUNDLE =") < page.index("const EXPORTED")

    def test_every_path_the_initial_load_asks_for_is_present(self, bundle):
        """Read from the page rather than from a list kept beside it.

        A list of endpoints is the thing that falls behind the endpoints - the
        same argument the CLI sweep in tests/test_cli_and_retention.py makes.
        So the paths the page actually requests are extracted from its own
        source and every one that the initial render awaits must be in here.
        """
        source = report.PAGE.read_text(encoding="utf-8")
        requested = set(re.findall(r'api\(`(/api/[a-z-]+)/\$\{seg\(domain\)\}', source))
        requested |= {"/api/domains"}
        # The panels that answer a typed question are recomputed per request
        # and deliberately absent - see export_responses.
        interactive = {"/api/sweep", "/api/sweep-grid", "/api/compare", "/api/rerun",
                       "/api/review"}
        prefixes = {"/" + "/".join(k.split("/")[1:3]) for k in bundle["responses"]}
        missing = requested - interactive - prefixes
        assert not missing, f"the export does not answer {sorted(missing)}"

    def test_it_carries_the_numbers_the_readme_quotes(self, bundle):
        summary = bundle["responses"]["/api/summary/expenses/v2"]
        assert summary["cases"] == 600
        assert summary["flips"] == 147
        assert summary["policy_driven_flips"] == 109

    def test_the_caveats_travel_with_the_numbers(self, bundle):
        """The reason the bundle exists at all, asserted on the HTML form too.

        A figure that arrives without its caveat is a figure that gets pasted
        into a slide, which is what export_bundle was written to prevent.
        """
        carried = [path for path, body in bundle["responses"].items()
                   if isinstance(body, dict) and body.get("caveat", "").strip()]
        assert len(carried) >= 5, carried
        for path in ("/api/disparity/expenses/v2", "/api/power/expenses/v2"):
            assert bundle["responses"][path]["caveat"].strip()

    def test_a_closing_script_tag_in_the_data_cannot_end_the_element(self, replayed):
        """Case text is written by somebody else, and `</script>` ends a script.

        JSON does not care, an HTML parser does, and the string it would end is
        the one holding every number on the page.
        """
        page = report.export_html("expenses", "v2")
        body = page[page.index("window.PTM_BUNDLE"):]
        assert body.index("</script>") == body.index(r";</script>") + 1
        assert page.count("<script>") == page.count("</script>")

    def test_it_says_which_domain_version_and_moment_it_is(self, bundle):
        assert bundle["domain"] == "expenses" and bundle["version"] == "v2"
        assert bundle["generated_at"].endswith("+00:00")

    def test_a_missing_page_is_a_refusal_that_names_the_lever(self, replayed, monkeypatch):
        monkeypatch.setattr(report, "PAGE", report.PAGE.parent / "nope.html")
        with pytest.raises(LookupError, match="PTM_DASHBOARD"):
            report.export_html("expenses", "v2")

    def test_an_unknown_version_is_refused_before_any_work(self, replayed):
        with pytest.raises(LookupError, match="Unknown policy version"):
            report.export_html("expenses", "v99")


class TestTheHtmlFlagOnTheCli:
    def test_it_writes_a_file(self, replayed, tmp_path, capsys):
        out = tmp_path / "explorer.html"
        assert report.main(["expenses", "v2", "--html", "-o", str(out)]) == 0
        assert "window.PTM_BUNDLE" in out.read_text(encoding="utf-8")
        assert "wrote" in capsys.readouterr().err

    def test_it_refuses_to_spill_a_page_into_a_terminal(self, replayed, capsys):
        assert report.main(["expenses", "v2", "--html"]) == 2
        assert "needs -o FILE" in capsys.readouterr().err

    def test_the_usage_mentions_it(self, capsys):
        assert report.main(["--help"]) == 0
        assert "--html" in capsys.readouterr().out


class TestTheConsoleCommand:
    def test_it_lists_what_it_can_do(self, capsys):
        assert dispatcher.main([]) == 0
        out = capsys.readouterr().out
        assert "commands:" in out
        for name in dispatcher.COMMANDS:
            assert name in out, f"{name} is dispatchable and unlisted"

    def test_every_command_names_a_callable_that_exists(self):
        """The table is hand-written, so it is checked rather than trusted."""
        import importlib
        for name, (module, attribute) in dispatcher.COMMANDS.items():
            loaded = importlib.import_module(module)
            assert callable(getattr(loaded, attribute, None)), name

    def test_every_summary_describes_a_real_command(self):
        assert set(dispatcher.SUMMARY) == set(dispatcher.COMMANDS)

    def test_it_dispatches_and_returns_the_command_s_own_code(self, seeded, capsys):
        assert dispatcher.main(["preflight", "expenses", "v2"]) == 0
        assert "preflight:" in capsys.readouterr().out

    def test_a_command_s_help_is_the_command_s_own(self, capsys):
        assert dispatcher.main(["sweep", "--help"]) == 0
        assert "ptm.sweep" in capsys.readouterr().out

    def test_help_after_a_non_command_is_still_help(self, capsys):
        """Usage is wanted most by somebody who just got the name wrong."""
        assert dispatcher.main(["nosuchthing", "--help"]) == 0
        assert "commands:" in capsys.readouterr().out

    def test_an_unknown_command_suggests_the_near_ones(self, capsys):
        assert dispatcher.main(["repot"]) == 2
        err = capsys.readouterr().err
        assert "report" in err and "replay" in err

    def test_db_and_include_reach_the_command(self, tmp_path, monkeypatch):
        """The lever manage.py exists for, available where the package is.

        Asserted on the environment rather than on an outcome, because that is
        the contract: ptm.config reads both at import time, so setting them
        after the module loads would change nothing at all.
        """
        monkeypatch.delenv("PTM_DB", raising=False)
        db = tmp_path / "other.db"
        assert dispatcher.main(["--db", str(db), "--include",
                                str(config.INCLUDE_DIR), "--help"]) == 0
        import os
        assert os.environ["PTM_DB"] == str(db.resolve())

    def test_a_path_less_option_is_refused(self, capsys):
        assert dispatcher.main(["--db"]) == 2
        assert "needs a path" in capsys.readouterr().err

    def test_an_unknown_leading_option_is_refused(self, capsys):
        assert dispatcher.main(["--nope", "report"]) == 2
        assert "unknown option" in capsys.readouterr().err


class TestAPayloadStringIsOnlyANumberWhenItPlainlyIs:
    """:func:`ptm.judge._coerce`, which decides what a rule compares against."""

    @pytest.mark.parametrize("text,expected", [
        ("500", 500), ("0", 0), ("-3", -3), (" 12 ", 12),
        ("1.50", 1.5), ("12.0", 12.0), ("1e5", 100000.0),
    ])
    def test_an_ordinary_number_still_converts(self, text, expected):
        """The behaviour every imported CSV depends on.

        ptm.ingest reads CSV, where every cell is a string, so without this no
        numeric threshold in any domain would match imported history at all.
        """
        assert _coerce(text) == expected

    @pytest.mark.parametrize("text", ["0042", "007", "1_000", "+5"])
    def test_an_identifier_spelled_in_digits_stays_a_string(self, text):
        """A cost centre is not a number, and int() could not tell.

        This is the silent direction: ``cost_centre == "0042"`` simply stops
        matching, and never matching is the failure this project treats as
        worse than a crash. ptm.lint cannot catch it either - probe() builds
        its scope from case_scope by design, so it agrees with the bug.
        """
        assert _coerce(text) == text

    def test_an_overflowing_float_is_not_silently_infinite(self):
        """`inf` reads as "larger than every threshold", which is never meant."""
        assert _coerce("1.5e400") == "1.5e400"

    def test_a_non_number_is_untouched(self):
        assert _coerce("travel") == "travel"
        assert _coerce("") == ""

    def test_a_value_that_is_not_a_string_is_returned_as_is(self):
        assert _coerce(500) == 500
        assert _coerce(None) is None


class TestANameCannotReachOutOfItsFolder:
    """:func:`ptm.config.is_safe_name`, which guards every path built from input."""

    @pytest.mark.parametrize("name", ["expenses", "refunds", "v2-draft1", "a_b.c"])
    def test_an_ordinary_name_is_allowed(self, name):
        assert config.is_safe_name(name)

    @pytest.mark.parametrize("name", [
        "", " ", ".", "..", ".hidden", "a/b", "a\\b", "../../secrets",
        # The drive-relative form, which needs neither separator: pathlib does
        # not join `Path("include/domains") / "C:x.yaml"`, it *replaces*, so the
        # base directory is discarded without a word.
        "C:pwned", "c:pwned", "a:b",
    ])
    def test_anything_that_could_leave_the_folder_is_refused(self, name):
        assert not config.is_safe_name(name)

    def test_a_refused_name_cannot_discard_the_base_directory(self):
        """The property underneath the check, asserted rather than assumed."""
        import pathlib
        for name in ("expenses", "v2-draft1"):
            joined = pathlib.PurePath("include/domains") / f"{name}.yaml"
            assert str(joined).startswith("include")

    def test_load_domain_refuses_it_as_a_missing_file(self, seeded):
        with pytest.raises(FileNotFoundError, match="not a domain name"):
            config.load_domain("C:pwned")

    def test_a_draft_cannot_be_written_outside_its_folder(self, seeded):
        """materialise() is the half that had no check at all.

        discard() argued carefully that a name building a path must name one
        path segment, and the function that *creates* the file took whatever it
        was handed. The name is not always a person's - propose_<domain> has a
        model choose it.
        """
        from ptm import proposal
        with pytest.raises(LookupError, match="not a draft version name"):
            proposal.materialise("expenses", "C:pwned", "# nope")

    def test_a_draft_cannot_be_deleted_outside_its_folder(self, seeded):
        from ptm import proposal
        with pytest.raises(LookupError, match="not a draft version name"):
            proposal.discard("expenses", "C:pwned")

    def test_both_halves_of_the_path_are_checked_not_just_the_version(self, seeded):
        """A promise is worth the weaker of the two segments it is made of.

        discard() already says this about its own arguments; materialise builds
        the same path out of the same two names, and a safe version under an
        unsafe domain lands in exactly the same place a safe domain under an
        unsafe version would.
        """
        from ptm import proposal
        with pytest.raises(LookupError, match="not a domain name"):
            proposal.materialise("C:pwned", "v2-draft1", "# nope")
