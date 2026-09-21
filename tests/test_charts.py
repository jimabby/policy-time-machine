"""The README's charts, held to the same standard as the README's prose.

``tests/test_docs.py`` exists because four quoted numbers had quietly stopped
being true, and its argument transfers here without a word changed: nothing
breaks when a figure goes stale, which is exactly the problem, because a reader
has no way to tell a stale one from a live one. A chart is worse than a
sentence in that respect, not better - it is read faster, believed harder, and
cannot be grepped. ``docs/charts/*.svg`` are twelve pictures asserting things
about the shipped fixture, and until this file existed nothing checked any of
them.

So they are regenerated here and compared byte for byte with what is committed.
A failure means one of two things and the message says which: the fixture, the
policy or the rules moved and the pictures did not, or somebody edited an SVG
by hand. Both are fixed the same way - ``python scripts/build_charts.py`` - and
both are things a reader would otherwise have had to take on trust.

The heavier half of the file is the other question: whether the pictures are
*drawable*. A chart with a label past its own right edge, a bar longer than its
plot, or a number set in a colour nobody can read against its fill is wrong in
a way byte equality cannot see, and it renders in a README all the same.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
CHARTS = REPO / "docs" / "charts"

pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "build_charts.py").exists(),
    reason="the chart pipeline is not present in this checkout")


@pytest.fixture(scope="module")
def build():
    """The generator, imported from ``scripts/`` the way the storyboard is.

    ``scripts/`` is a directory of entry points rather than a package - it is
    in no ``packages`` list in pyproject.toml, deliberately, because nothing
    imports it at runtime. So the path goes on ``sys.path`` for the duration.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import build_charts as module

        return module
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(scope="module")
def rendered(build, seeded) -> dict:
    """Every chart, drawn from this session's seeded fixture."""
    return build.render()


class TestTheCommittedChartsMatchTheFixture:
    def test_every_chart_is_committed(self, rendered):
        missing = [name for name in rendered if not (CHARTS / name).exists()]
        assert not missing, (
            f"{len(missing)} chart(s) have never been written: "
            f"{', '.join(sorted(missing))}. Run python scripts/build_charts.py")

    def test_nothing_on_disk_is_stale(self, rendered):
        """The whole point of the file. Byte equality, not a tolerance.

        The generator rounds every coordinate before it writes one, precisely
        so this can be an equality rather than a comparison somebody has to
        pick a threshold for - a picture whose bytes move without its data
        moving is a check nobody can keep green.
        """
        stale = [name for name, svg in rendered.items()
                 if (CHARTS / name).read_text(encoding="utf-8") != svg]
        assert not stale, (
            f"{len(stale)} chart(s) no longer match what the fixture produces: "
            f"{', '.join(sorted(stale))}. Either the fixture moved and the "
            f"pictures did not, or an SVG was edited by hand. Run "
            f"python scripts/build_charts.py")

    def test_nothing_is_committed_that_is_no_longer_generated(self, rendered):
        """A chart the README still points at and nothing regenerates.

        The failure this catches is a rename: the generator stops producing
        ``old.svg``, the file stays on disk because nothing deletes it, and it
        goes on being served - frozen at whatever the fixture said the day it
        was last written, with no test able to tell.
        """
        orphans = {p.name for p in CHARTS.glob("*.svg")} - set(rendered)
        assert not orphans, (
            f"docs/charts holds {len(orphans)} file(s) the generator no longer "
            f"produces: {', '.join(sorted(orphans))}")

    def test_both_themes_exist_for_every_chart(self, build, rendered):
        """GitHub picks between them with <picture>; a missing half is a blank."""
        for name in build.CHARTS:
            assert f"{name}.svg" in rendered and f"{name}-dark.svg" in rendered


class TestTheNumbersOnThemAreTheFixtures:
    """The charts state figures. Those figures are checked, like every other one.

    ``test_docs.py`` holds the README's four headline numbers to a real replay.
    These are the same four numbers in a form a reader trusts more and can
    check less, so they are held to the same replay.
    """

    def test_the_headline_split_is_drawn_correctly(self, build, replayed):
        from ptm import diff

        summary = diff.summarise(replayed["flips"], len(replayed["cases"]),
                                 replayed["domain"])
        svg = (CHARTS / "story.svg").read_text(encoding="utf-8")
        assert f"{summary['flips']} of {summary['cases_replayed']} decisions" in svg
        assert f">{summary['policy_driven_flips']}<" in svg
        assert f">{summary['deviation_flips']}<" in svg

    def test_the_curve_agrees_with_the_sweep_it_is_drawn_from(self, build, seeded):
        """The endpoint label, which is the only value the chart spells out."""
        from ptm import sweep

        result = sweep.sweep("expenses", "v2", "amount_gbp",
                             [25, 50, 75, 100, 150, 250], clause="1.1")
        last = result["points"][-1]
        svg = (CHARTS / "sweep.svg").read_text(encoding="utf-8")
        assert f">{last['flips']}<" in svg
        assert f">{last['policy_driven_flips']}<" in svg

    def test_the_grid_marks_the_settings_actually_in_force(self, build, rendered):
        """One outlined cell, not none and not several.

        A grid that outlines nothing has lost the only annotation saying where
        the policy currently sits, and one that outlines several is drawing a
        claim that is not true of any policy.
        """
        svg = rendered["grid.svg"]
        assert svg.count('fill="none"') == 1


class TestTheChartsAreActuallyDrawable:
    """Geometry, which byte equality cannot see and a browser would show.

    Every one of these caught something while the charts were being written: a
    legend below its own bottom edge, an axis title under the marks, a number
    set in white on the palette's lightest step.
    """

    @staticmethod
    def viewbox(svg: str) -> tuple[float, float]:
        match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
        assert match, "the chart lost its viewBox"
        return float(match.group(1)), float(match.group(2))

    @staticmethod
    def marks(svg: str) -> list[tuple[float, float]]:
        """Every point the chart draws, already offset by the heading group.

        The body of a chart is inside one ``translate(28,24)``, so a
        coordinate in the source is not a coordinate on the canvas - which is
        exactly how a legend ended up 10px past the bottom edge while its y
        looked comfortably inside the height.
        """
        found = []
        for attrs in (("x", "y"), ("cx", "cy"), ("x1", "y1"), ("x2", "y2")):
            for match in re.finditer(
                    rf'{attrs[0]}="([-\d.]+)"[^>]*?{attrs[1]}="([-\d.]+)"', svg):
                found.append((float(match.group(1)) + 28, float(match.group(2)) + 24))
        return found

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_nothing_is_drawn_outside_the_canvas(self, build, rendered, theme):
        for name in build.CHARTS:
            svg = rendered[f"{name}{theme}.svg"]
            width, height = self.viewbox(svg)
            # The surface rect is the canvas itself, so it is measured from the
            # untranslated origin - every other coordinate is inside the group.
            body = svg.split("<g transform", 1)[1]
            for x, y in self.marks(body):
                assert -1 <= x <= width + 1, f"{name}{theme}: x={x} outside {width}"
                assert -1 <= y <= height + 1, f"{name}{theme}: y={y} outside {height}"

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_every_label_inside_a_fill_clears_contrast(self, build, theme):
        """The one rule a generated chart is most likely to break silently.

        A number set inside a heatmap tile or a stacked segment picks its ink
        from the fill's luminance. Get that backwards - as a version of this
        did, by reading a ramp position instead of a colour - and it is correct
        on one surface and unreadable on the other, which is the half nobody
        looks at.
        """
        palette = build.THEMES["dark" if theme else "light"]
        fills = [*palette["ramp"], palette["series1"], palette["series2"],
                 palette["neutral"]]
        for fill in fills:
            ink = build.ink_on(fill)
            assert self.contrast(ink, fill) >= 4.5, (
                f"{ink} on {fill} is {self.contrast(ink, fill):.2f}:1")

    @staticmethod
    def contrast(a: str, b: str) -> float:
        def luminance(colour: str) -> float:
            rgb = (int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
            channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                        for c in rgb]
            return (0.2126 * channels[0] + 0.7152 * channels[1]
                    + 0.0722 * channels[2])

        lo, hi = sorted((luminance(a), luminance(b)))
        return (hi + 0.05) / (lo + 0.05)

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_every_chart_is_well_formed_xml(self, build, rendered, theme):
        """The check that would have caught the one real bug in this file.

        A browser loading an SVG through ``<img src=...>`` - which is exactly
        how a README uses one - parses it as XML, and XML has no error
        recovery: one bad attribute and the whole picture renders as nothing.
        Inlined into an HTML page the same file looked perfect, because the
        HTML parser is lenient about precisely the thing that was wrong with
        it: the font stack was spelled with double quotes around ``"Segoe
        UI"``, inside a double-quoted attribute, so every ``<text>`` element
        in all twelve files closed its ``font-family`` a third of the way
        through itself.

        Six charts rendered correctly in every preview and would have been six
        blank panels on the page they were drawn for.
        """
        from xml.etree import ElementTree

        for name in build.CHARTS:
            svg = rendered[f"{name}{theme}.svg"]
            try:
                ElementTree.fromstring(svg)
            except ElementTree.ParseError as exc:  # pragma: no cover - the message
                line, column = exc.position
                context = svg.splitlines()[line - 1][max(0, column - 60):column + 20]
                pytest.fail(f"{name}{theme}.svg is not well-formed XML at "
                            f"{line}:{column} ({exc}), near: ...{context}")

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_an_attribute_value_never_contains_a_double_quote(self, build, rendered,
                                                              theme):
        """The specific shape of the bug above, asserted directly.

        Well-formedness catches it today. This catches the *class* of it: a
        value that closes its own attribute is not always a parse error - a
        stray quote can land somewhere that still parses and silently drops the
        rest of the attribute - and the fix is always the same, which is to
        quote with the other kind.
        """
        import re as regex

        for name in build.CHARTS:
            svg = rendered[f"{name}{theme}.svg"]
            for value in regex.findall(r'="([^"]*)"', svg):
                assert '"' not in value, f"{name}{theme}.svg: {value!r}"

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_no_chart_carries_a_stylesheet(self, build, rendered, theme):
        """GitHub strips ``<style>`` from an SVG it serves into a README.

        So the charts have to be styled with presentation attributes, and a
        ``<style>`` block here is a chart that looks right locally and arrives
        unstyled in the one place it exists for. Likewise a media query: the
        theme is chosen by the ``<picture>`` element around the image.
        """
        for name in build.CHARTS:
            svg = rendered[f"{name}{theme}.svg"]
            assert "<style" not in svg and "prefers-color-scheme" not in svg

    @pytest.mark.parametrize("theme", ["", "-dark"])
    def test_every_chart_says_what_it_is_for_a_screen_reader(self, build, rendered,
                                                             theme):
        for name in build.CHARTS:
            svg = rendered[f"{name}{theme}.svg"]
            assert 'role="img"' in svg
            assert "<title" in svg and "<desc" in svg


class TestTheGeneratorIsReproducible:
    def test_drawing_twice_produces_the_same_bytes(self, build, seeded):
        """Otherwise the freshness check above fails at random.

        Float formatting is the usual culprit: the same arithmetic written two
        ways prints a different number of digits, and a picture whose bytes
        move without its data moving is a red build nobody can act on.
        """
        assert build.render() == build.render()

    def test_check_mode_passes_against_what_is_committed(self, build, seeded, capsys):
        """The CI step, run here so a laptop finds out before the runner does."""
        assert build.main(["--check"]) == 0, capsys.readouterr().err


class TestTheProseAroundTheChartsQuotesTheFixture:
    """Alt text is prose, and the sentences beside a chart are prose too.

    ``tests/test_docs.py`` holds the README's four headline numbers to a real
    replay for the reason it states at length: a stale checkable figure invites
    doubt about every uncheckable one. Putting six charts on that page added
    figures in two more places that nothing was checking - the sentence under
    each picture, and the ``alt`` text, which is the *only* version of the
    chart a screen reader gets and therefore the one least worth being wrong.

    Both documents are scanned, because the demo script quotes the same figures
    at a room that cannot re-derive them.
    """

    DOCS = ("README.md", "DEMO_SCRIPT.md")

    def prose(self) -> str:
        return "\n".join((REPO / name).read_text(encoding="utf-8")
                         for name in self.DOCS)

    def test_the_sweep_endpoints_in_the_alt_text(self, seeded):
        from ptm import sweep

        points = {p["value"]: p["flips"] for p in sweep.sweep(
            "expenses", "v2", "amount_gbp", [25, 75, 250], clause="1.1")["points"]}
        quoted = re.search(
            r"(\d+) at GBP 25, (\d+) at the 75 in force, (\d+) at 250", self.prose())
        assert quoted, "the sweep chart's alt text no longer states the endpoints"
        assert [int(g) for g in quoted.groups()] == [points[25], points[75], points[250]]

    def test_the_grid_interaction_range(self, seeded):
        """The number the grid exists to produce, quoted twice in one sentence."""
        from ptm import sweep

        grid = sweep.joint(
            "expenses", "v2",
            {"clause": "1.1", "field": "amount_gbp", "edge": "",
             "values": [25, 50, 75, 100, 150]},
            {"clause": "3.1", "field": "days_notice", "edge": "",
             "values": [3, 7, 14, 21]})
        interaction = grid["interaction"]
        text = self.prose()
        assert re.search(
            rf"between {interaction['effect_min_flips']} and "
            rf"{interaction['effect_max_flips']} decisions", text), \
            "the grid chart's alt text no longer matches the interaction it draws"
        assert re.search(
            rf"{interaction['effect_min_flips']}\s*\ndecisions at one setting of "
            rf"`days_notice` and {interaction['effect_max_flips']} at another", text), \
            "the sentence under the grid no longer matches the interaction"

    def test_the_concentrated_segment(self, replayed):
        """52.6% against 17.8% - the one segment the check actually flags."""
        from ptm import diff, disparity

        segments = diff.segment_stats(replayed["cases"], replayed["flips"],
                                      replayed["domain"])
        flagged = disparity.analyse(segments, replayed["domain"])
        assert flagged, "the fixture no longer concentrates anywhere"
        worst = max(flagged, key=lambda f: f.ratio)
        quoted = re.search(
            r"meals at ([\d.]+)% against ([\d.]+)% for the rest", self.prose())
        assert quoted, "the segment chart's alt text no longer states the comparison"
        assert float(quoted.group(1)) == round(worst.flip_rate * 100, 1)
        assert float(quoted.group(2)) == round(worst.rest_flip_rate * 100, 1)

    def test_every_chart_the_documents_point_at_exists(self, build):
        """Both halves of every <picture>, in both files.

        A ``srcset`` naming a file that is not there is a blank panel in dark
        mode only, which is the half of the readership that would never
        report it.
        """
        for name in self.DOCS:
            text = (REPO / name).read_text(encoding="utf-8")
            for referenced in re.findall(r'docs/charts/([\w-]+\.svg)', text):
                assert (CHARTS / referenced).exists(), f"{name} points at {referenced}"

    def test_a_referenced_chart_offers_both_themes(self):
        """A <picture> with no dark <source> is a white slab on a dark page."""
        for name in self.DOCS:
            text = (REPO / name).read_text(encoding="utf-8")
            for block in re.findall(r"<picture>.*?</picture>", text, re.S):
                assert "prefers-color-scheme: dark" in block, f"{name}: {block[:80]}"
                assert "-dark.svg" in block and block.count("docs/charts/") == 2
