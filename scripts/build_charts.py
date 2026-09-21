"""The README's charts, drawn from the fixture rather than from memory.

``docs/*.gif`` show the tool running. They are the right artefact for *this is
what it looks like* and the wrong one for *this is what it found*: a reader
cannot compare two numbers in a clip, and neither can a build. So the figures
that carry an argument - the split between the policy and the reviewers, the
threshold curve, where the change lands - are drawn here, as SVG, from a real
offline replay of the shipped fixture.

**They are generated, not drawn.** ``tests/test_charts.py`` regenerates every
one of them and fails when a committed file no longer matches what the fixture
produces, which is the same contract ``tests/test_docs.py`` holds the README's
prose to. A chart is a number in a shape a reader trusts more than prose, so a
stale one is worse than a stale sentence.

Two files per chart, ``<name>.svg`` and ``<name>-dark.svg``, because GitHub
strips ``<style>`` out of an SVG it renders in a README and a media query with
it: the theme has to be chosen by the ``<picture>`` element around the image,
not inside it. Dark is stepped for the dark surface rather than flipped - see
the palette below.

No dependency, and deliberately: the engine installs two packages, and adding a
plotting stack so a README can have pictures would be the largest dependency in
the project serving the smallest purpose. The shapes here are rectangles, lines
and text.

    python scripts/build_charts.py           # write them
    python scripts/build_charts.py --check   # fail if what is committed is stale
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "docs" / "charts"

sys.path.insert(0, str(REPO))

#: The validated categorical palette, both modes. The dark column is the same
#: hues stepped for the dark surface, not an automatic inversion of the light
#: one - a light-mode hue on a dark surface is either glaring or invisible, and
#: which of the two it is depends on the hue.
THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink_soft": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        # Slot 1 (blue) is always the thing the chart is about; slot 2 (orange)
        # is always its counterpart. Held to those roles across all six charts,
        # so a reader who learns the pair once reads the rest for free.
        "series1": "#2a78d6",
        "series2": "#eb6834",
        "series3": "#1baf7a",
        "neutral": "#dcdbd4",
        "ramp": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"],
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#ffffff",
        "ink_soft": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series1": "#3987e5",
        "series2": "#d95926",
        "series3": "#199e70",
        "neutral": "#3a3a37",
        "ramp": ["#0d366b", "#104281", "#184f95", "#256abf", "#3987e5", "#6da7ec"],
    },
}

#: The font stack, with **single** quotes around the multi-word family. This
#: string is written straight into a double-quoted XML attribute, and the
#: obvious spelling - `"Segoe UI"` - closes that attribute in the middle of
#: itself. An SVG is parsed as XML when a browser loads it through `<img
#: src=...>`, which is exactly how a README uses one, so the whole chart failed
#: to render there while looking perfect inlined in a page: the HTML parser is
#: lenient about it and the XML parser is not. See the well-formedness test in
#: tests/test_charts.py, which exists because of this.
FONT = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

#: The relative luminance at which black and white are equally readable against
#: a fill - ``sqrt(1.05 * 0.05) - 0.05``, straight out of the WCAG ratio. See
#: :func:`ink_on`.
CROSSOVER = 0.1791287847


# --------------------------------------------------------------------------
# SVG primitives. Presentation attributes rather than a stylesheet, because
# GitHub's sanitiser drops <style> from an SVG served into a README - a chart
# that renders in a browser and arrives unstyled in the one place it is for.
# --------------------------------------------------------------------------

def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def num(value: float) -> str:
    """A coordinate, rounded so the same input always writes the same file.

    Binary floats print differently on either side of an arithmetic that is
    mathematically the same, and a chart whose bytes move without its data
    moving is a freshness check nobody can keep green.
    """
    rounded = round(float(value), 2)
    return f"{rounded:.2f}".rstrip("0").rstrip(".") or "0"


def text(x: float, y: float, body: str, fill: str, size: float = 13,
         weight: str = "400", anchor: str = "start", opacity: float = 1.0) -> str:
    extra = f' opacity="{num(opacity)}"' if opacity < 1 else ""
    return (f'<text x="{num(x)}" y="{num(y)}" font-family="{FONT}" '
            f'font-size="{num(size)}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}"{extra}>{esc(body)}</text>')


def rect(x: float, y: float, w: float, h: float, fill: str, radius: float = 0,
         opacity: float = 1.0) -> str:
    if w <= 0 or h <= 0:
        return ""
    extra = f' opacity="{num(opacity)}"' if opacity < 1 else ""
    r = f' rx="{num(radius)}" ry="{num(radius)}"' if radius else ""
    return (f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" '
            f'fill="{fill}"{r}{extra}/>')


def line(x1: float, y1: float, x2: float, y2: float, stroke: str,
         width: float = 1, cap: str = "butt") -> str:
    return (f'<line x1="{num(x1)}" y1="{num(y1)}" x2="{num(x2)}" y2="{num(y2)}" '
            f'stroke="{stroke}" stroke-width="{num(width)}" stroke-linecap="{cap}"/>')


def circle(cx: float, cy: float, r: float, fill: str, stroke: str = "",
           stroke_width: float = 0) -> str:
    ring = (f' stroke="{stroke}" stroke-width="{num(stroke_width)}"'
            if stroke and stroke_width else "")
    return f'<circle cx="{num(cx)}" cy="{num(cy)}" r="{num(r)}" fill="{fill}"{ring}/>'


def polyline(points: list[tuple[float, float]], stroke: str, width: float = 2) -> str:
    joined = " ".join(f"{num(x)},{num(y)}" for x, y in points)
    return (f'<polyline points="{joined}" fill="none" stroke="{stroke}" '
            f'stroke-width="{num(width)}" stroke-linejoin="round" '
            f'stroke-linecap="round"/>')


def frame(width: float, height: float, title: str, subtitle: str, theme: dict,
          body: str) -> str:
    """One chart, surface and heading included.

    ``role="img"`` with a ``<title>`` is what a screen reader announces, and the
    ``<desc>`` carries the subtitle - the sentence saying what the chart claims,
    which is the part a reader who cannot see the marks most needs.
    """
    head = [
        text(0, 22, title, theme["ink"], size=17, weight="600"),
        text(0, 44, subtitle, theme["ink_soft"], size=13),
    ]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {num(width)} '
        f'{num(height)}" width="{num(width)}" height="{num(height)}" '
        f'role="img" aria-labelledby="t d">'
        f'<title id="t">{esc(title)}</title><desc id="d">{esc(subtitle)}</desc>'
        + rect(0, 0, width, height, theme["surface"], radius=10)
        + '<g transform="translate(28,24)">' + "".join(head) + body + "</g>"
        + "</svg>\n"
    )


def legend(x: float, y: float, entries: list[tuple[str, str]], theme: dict) -> str:
    """Swatch, then label in an ink token - never the label in the series colour."""
    out, cursor = [], x
    for colour, label in entries:
        out.append(circle(cursor + 5, y - 4, 5, colour))
        out.append(text(cursor + 16, y, label, theme["ink_soft"], size=12.5))
        cursor += 26 + len(label) * 6.6
    return "".join(out)


def money(value: float, unit: str) -> str:
    return f"{unit} {value:,.0f}"


def ink_on(fill: str) -> str:
    """Black or white for a label set *inside* a coloured fill.

    Chosen from the fill's own luminance rather than from its position in a
    ramp, which is the mistake this replaced: a dark theme's ramp runs the
    other way, so "step 5 is dark" was true on one surface and exactly wrong on
    the other, and the brightest tiles came out with white numbers on them.

    The crossover is where black and white give *equal* contrast against the
    same fill, which falls out of the WCAG ratio rather than being picked by
    eye: ``sqrt(1.05 * 0.05) - 0.05``. Choosing the better ink at every
    luminance then guarantees at least 4.58:1 for any fill whatsoever, which is
    past AA for normal text - and these labels are normal text, 13.5px bold
    being well under the 18.7px that counts as large. An eyeballed 0.45 put
    white on the palette's mid-blue at 3.6:1.

    The two inks are pure black and pure white rather than the theme's ink
    tokens, and that is the one place in these charts where they should be:
    against the worst fill in the palette the near-black `#0b0b0b` measures
    4.46:1 and black measures 4.70:1, and the whole job of a label set
    inside a fill is to survive the fill it landed on.
    """
    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                for c in (r, g, b)]
    luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    return "#000000" if luminance > CROSSOVER else "#ffffff"


# --------------------------------------------------------------------------
# The figures, computed from a real offline replay of the shipped fixture.
# --------------------------------------------------------------------------

def figures() -> dict:
    """Everything the six charts draw, from one replay and two sweeps.

    Computed here rather than read from a stored replay on purpose: a chart
    that depends on which DAG last ran is a chart that changes without its
    subject changing. The offline judge is deterministic and the seed is fixed,
    so these numbers are a property of the files in this repository.
    """
    from ptm import diff, disparity, stats, store, sweep
    from ptm.config import load_domain
    from ptm.judge import offline_verdict
    from ptm.seed import seed_domain

    store.init_db()
    seed_domain("expenses")
    domain = load_domain("expenses")
    cases = store.load_cases("expenses", until=datetime(2026, 9, 1))
    candidate = {c.case_id: offline_verdict(c, domain, "v2") for c in cases}
    baseline = {c.case_id: offline_verdict(c, domain, "v1") for c in cases}
    flips = diff.flips(cases, candidate, domain, baseline=baseline)
    summary = diff.summarise(flips, len(cases), domain)
    segments = diff.segment_stats(cases, flips, domain)
    findings = {(f.field, f.value) for f in disparity.analyse(segments, domain)}

    curve = sweep.sweep("expenses", "v2", "amount_gbp",
                        [25, 50, 75, 100, 150, 250], clause="1.1", cases=cases)
    grid = sweep.joint("expenses", "v2",
                       {"clause": "1.1", "field": "amount_gbp", "edge": "",
                        "values": [25, 50, 75, 100, 150]},
                       {"clause": "3.1", "field": "days_notice", "edge": "",
                        "values": [3, 7, 14, 21]}, cases=cases)
    power = stats.power_report(summary["flip_rate"], len(cases))

    return {
        "unit": domain.impact_unit,
        "summary": summary,
        "clauses": summary["by_clause"],
        "segments": [s for s in segments if s["field"] == "category"],
        "flagged": findings,
        "curve": curve,
        "grid": grid,
        "power": power,
    }


# --------------------------------------------------------------------------
# The six charts.
# --------------------------------------------------------------------------

def chart_story(data: dict, theme: dict) -> str:
    """The headline, as one bar of 600 cases.

    A percentage in prose asks the reader to picture a proportion. A bar of the
    whole history with the changed part cut out of it does not, and the part
    that matters is the *second* cut: how much of the change the proposal is
    actually responsible for.
    """
    summary, unit = data["summary"], data["unit"]
    cases = summary["cases_replayed"]
    driven, deviation = summary["policy_driven_flips"], summary["deviation_flips"]
    same = cases - driven - deviation

    width, height, bar_h = 824, 232, 54
    top, plot_w = 84, 768
    segs = [
        (driven, theme["series1"], "caused by v2", f"{driven}"),
        (deviation, theme["series2"], "already off the old rulebook", f"{deviation}"),
        (same, theme["neutral"], "same answer as before", f"{same}"),
    ]
    body, cursor = [], 0.0
    for value, colour, _label, caption in segs:
        w = plot_w * value / cases
        # A 2px gap in the surface colour is what separates the segments; no
        # segment carries a stroke, which would be ink that is not data.
        body.append(rect(cursor, top, max(w - 2, 0), bar_h, colour, radius=4))
        if w > 46:
            body.append(text(cursor + (w - 2) / 2, top + bar_h / 2 + 5, caption,
                             ink_on(colour), size=15, weight="600",
                             anchor="middle"))
        cursor += w

    # A span bracket over the changed part of the bar, with ends turned down so
    # it reads as a measurement of the two segments under it rather than as an
    # underline of the sentence above it.
    flips = summary["flips"]
    span = plot_w * flips / cases - 2
    body.append(line(0, top - 12, span, top - 12, theme["ink_soft"], 1.5))
    body.append(line(0, top - 12, 0, top - 5, theme["ink_soft"], 1.5))
    body.append(line(span, top - 12, span, top - 5, theme["ink_soft"], 1.5))
    body.append(text(0, top - 22,
                     f"{flips} of {cases} decisions come out differently "
                     f"({summary['flip_rate']:.1%})",
                     theme["ink"], size=13.5, weight="600"))
    body.append(text(0, top + bar_h + 32,
                     f"net {money(summary['policy_driven_net_impact'], unit)} "
                     f"attributable to the proposal",
                     theme["ink_soft"], size=13))
    body.append(legend(0, top + bar_h + 60,
                       [(theme["series1"], "caused by policy v2"),
                        (theme["series2"], "reviewer already departed from v1"),
                        (theme["neutral"], "unchanged")], theme))
    return frame(width, height, "What a 600-case replay found",
                 "A changed answer is not automatically the proposal's doing - "
                 "the replay judges the old rulebook too.",
                 theme, "".join(body))


def chart_attribution(data: dict, theme: dict) -> str:
    """Which sentence to edit - the whole reason attribution exists."""
    rows = data["clauses"][:7]
    width, bar_h, gap = 824, 22, 14
    top, label_w = 82, 214
    plot_w = 768 - label_w - 52
    height = top + len(rows) * (bar_h + gap) + 56
    biggest = max(r["flips"] for r in rows)

    body = []
    for i, row in enumerate(rows):
        y = top + i * (bar_h + gap)
        colour = theme["series1"] if row["policy_driven"] else theme["series2"]
        w = plot_w * row["flips"] / biggest
        body.append(text(label_w - 10, y + bar_h - 6, row["clause"],
                         theme["ink_soft"], size=12.5, anchor="end"))
        body.append(rect(label_w, y, w, bar_h, colour, radius=4))
        body.append(text(label_w + w + 9, y + bar_h - 6, str(row["flips"]),
                         theme["ink"], size=13, weight="600"))
    body.append(legend(label_w, height - 30,
                       [(theme["series1"], "the proposal's doing"),
                        (theme["series2"], "the reviewers'")], theme))
    return frame(width, height, "Which clause moves the decisions",
                 "Including clauses that changed a decision by ceasing to apply, "
                 "which is how most rule changes actually move one.",
                 theme, "".join(body))


def chart_sweep(data: dict, theme: dict) -> str:
    """The curve: what the threshold could be, rather than what it is."""
    points = data["curve"]["points"]
    # Deep enough for the legend below the axis label. Every y in a chart body
    # is inside :func:`frame`'s heading offset, so the last mark's coordinate is
    # not the height - it was, and the legend fell off the bottom edge.
    width, height = 824, 376
    left, top, plot_w, plot_h = 48, 92, 700, 168
    xs = [p["value"] for p in points]
    lo, hi = min(xs), max(xs)
    top_y = max(p["flips"] for p in points)
    ceiling = ((top_y // 50) + 1) * 50

    def px(value: float) -> float:
        return left + plot_w * (value - lo) / (hi - lo)

    def py(value: float) -> float:
        return top + plot_h - plot_h * value / ceiling

    body = []
    for tick in range(0, ceiling + 1, 50):
        body.append(line(left, py(tick), left + plot_w, py(tick), theme["grid"], 1))
        body.append(text(left - 10, py(tick) + 4, str(tick), theme["muted"],
                         size=11.5, anchor="end"))
    for p in points:
        body.append(text(px(p["value"]), top + plot_h + 20, str(p["value"]),
                         theme["muted"], size=11.5, anchor="middle"))

    # The current setting behind the curves, so the marker reads as background
    # rather than as a third series crossing them.
    current = next(p for p in points if p["is_current"])
    body.append(line(px(current["value"]), top - 6, px(current["value"]),
                     top + plot_h, theme["axis"], 1))
    body.append(text(px(current["value"]), top - 12,
                     f"in force: {current['value']}", theme["ink_soft"],
                     size=12, weight="600", anchor="middle"))

    for key, colour in (("flips", theme["series1"]),
                        ("policy_driven_flips", theme["series2"])):
        body.append(polyline([(px(p["value"]), py(p[key])) for p in points], colour, 2))
        for p in points:
            body.append(circle(px(p["value"]), py(p[key]), 4.5, colour,
                               theme["surface"], 2))
        last = points[-1]
        # Only the endpoint is labelled. A number on every point is the
        # fixed-width table this chart exists instead of.
        body.append(text(px(last["value"]) + 12, py(last[key]) + 4, str(last[key]),
                         theme["ink"], size=12.5, weight="600"))

    body.append(text(left, top + plot_h + 46, "receipt threshold, clause 1.1 (GBP)",
                     theme["muted"], size=12))
    body.append(legend(left, top + plot_h + 74,
                       [(theme["series1"], "decisions that change"),
                        (theme["series2"], "attributable to the policy")], theme))
    return frame(width, height, "So what should the number actually be?",
                 "Six full replays of 600 cases, free and offline - it is "
                 "arithmetic over rules, not a model reading a reworded policy.",
                 theme, "".join(body))


def chart_grid(data: dict, theme: dict) -> str:
    """Two dials at once. One curve holds the other still and never says so."""
    grid = data["grid"]
    firsts = list(dict.fromkeys(p["first_value"] for p in grid["points"]))
    seconds = list(dict.fromkeys(p["second_value"] for p in grid["points"]))
    by_pair = {(p["first_value"], p["second_value"]): p for p in grid["points"]}
    values = [p["flips"] for p in grid["points"]]
    lo, hi = min(values), max(values)

    cell_w, cell_h, gap = 116, 42, 2
    left, top = 122, 122
    width = 824
    height = top + len(firsts) * (cell_h + gap) + 96
    # The last step of the ramp is the one furthest from the surface, whichever
    # surface that is. On light that means darkest and on dark it means
    # lightest, so the sentence explaining the encoding cannot be a constant.
    furthest = "darker" if theme["surface"] == "#fcfcfb" else "brighter"

    body = [text(left, top - 46, "days_notice, clause 3.1  →",
                 theme["muted"], size=12),
            text(left - 14, top - 46, "amount_gbp ↓", theme["muted"],
                 size=12, anchor="end")]
    for j, second in enumerate(seconds):
        body.append(text(left + j * (cell_w + gap) + cell_w / 2, top - 14,
                         str(second), theme["ink_soft"], size=12.5, weight="600",
                         anchor="middle"))
    body.append(text(left - 14, top - 14, "clause 1.1", theme["muted"], size=12,
                     anchor="end"))
    for i, first in enumerate(firsts):
        y = top + i * (cell_h + gap)
        body.append(text(left - 14, y + cell_h / 2 + 5, str(first),
                         theme["ink_soft"], size=12.5, weight="600", anchor="end"))
        for j, second in enumerate(seconds):
            point = by_pair[(first, second)]
            step = int(round((len(theme["ramp"]) - 1)
                             * (point["flips"] - lo) / max(hi - lo, 1)))
            x = left + j * (cell_w + gap)
            fill = theme["ramp"][step]
            body.append(rect(x, y, cell_w, cell_h, fill, radius=3))
            body.append(text(x + cell_w / 2, y + cell_h / 2 + 5,
                             str(point["flips"]), ink_on(fill),
                             size=13.5, weight="600", anchor="middle"))
            if point["is_current"]:
                # An annotation, not a mark: it says which cell the policy is
                # actually sitting in, which no amount of colour could.
                body.append(
                    f'<rect x="{num(x)}" y="{num(y)}" width="{num(cell_w)}" '
                    f'height="{num(cell_h)}" rx="3" ry="3" fill="none" '
                    f'stroke="{theme["ink"]}" stroke-width="2"/>')
    interaction = grid["interaction"]
    body.append(text(0, height - 62,
                     f"moving amount_gbp changes "
                     f"{interaction['effect_min_flips']}–"
                     f"{interaction['effect_max_flips']} decisions depending on "
                     f"where days_notice sits",
                     theme["ink_soft"], size=13))
    body.append(text(0, height - 40,
                     f"the outlined cell is the pair of settings in force; "
                     f"{furthest} is more decisions moved",
                     theme["muted"], size=12))
    return frame(width, height, "One curve holds every other dial still",
                 "A threshold's effect is a property of that clause given where "
                 "everything else is sitting. So move two at once.",
                 theme, "".join(body))


def chart_segments(data: dict, theme: dict) -> str:
    """Who the change lands on - the first question compliance asks.

    A dot with its interval rather than a bar: the question is whether one
    segment's rate is separable from the rest, and a bar chart of point
    estimates is exactly the picture that makes an unseparable pair look
    settled.
    """
    from ptm import stats

    rows = sorted(data["segments"], key=lambda r: -r["flip_rate"])
    width, row_h = 824, 34
    left, top, plot_w = 190, 96, 520
    height = top + len(rows) * row_h + 74
    ceiling = 0.7

    def px(rate: float) -> float:
        return left + plot_w * rate / ceiling

    body = []
    for tick in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        body.append(line(px(tick), top - 18, px(tick), top + len(rows) * row_h - 8,
                         theme["grid"], 1))
        body.append(text(px(tick), top - 24, f"{tick:.0%}", theme["muted"],
                         size=11.5, anchor="middle"))
    for i, row in enumerate(rows):
        y = top + i * row_h + 6
        flagged = (row["field"], row["value"]) in data["flagged"]
        colour = theme["series2"] if flagged else theme["series1"]
        lo, hi = stats.wilson_interval(row["flips"], row["cases"])
        body.append(text(left - 14, y + 5, row["value"].replace("_", " "),
                         theme["ink_soft"], size=12.5, anchor="end"))
        body.append(line(px(lo), y, px(hi), y, colour, 2, cap="round"))
        body.append(circle(px(row["flip_rate"]), y, 5, colour, theme["surface"], 2))
        body.append(text(px(hi) + 12, y + 5,
                         f"{row['flips']}/{row['cases']}", theme["muted"], size=11.5))
    body.append(legend(left, height - 40,
                       [(theme["series1"], "within range of the rest of the field"),
                        (theme["series2"], "worth explaining before the rule ships")],
                       theme))
    return frame(width, height, "Does the change land evenly?",
                 "Point estimate with its 95% interval. A concentration is a "
                 "question, not a finding of unfairness.",
                 theme, "".join(body))


def chart_power(data: dict, theme: dict) -> str:
    """The band that runs *before* you spend anything, and reports a refusal."""
    power = data["power"]
    width, height = 824, 216
    left, top, plot_w = 40, 108, 700
    lo_axis, hi_axis = 0.0, 0.45

    def px(rate: float) -> float:
        return left + plot_w * (rate - lo_axis) / (hi_axis - lo_axis)

    band_lo, band_hi = power["detectable_rate_lo"], power["detectable_rate_hi"]
    body = [
        rect(px(band_lo), top - 22, px(band_hi) - px(band_lo), 44,
             theme["series1"], radius=4, opacity=0.16),
        line(left, top, left + plot_w, top, theme["axis"], 1),
    ]
    for tick in (0.0, 0.1, 0.2, 0.3, 0.4):
        body.append(line(px(tick), top, px(tick), top + 7, theme["axis"], 1))
        body.append(text(px(tick), top + 24, f"{tick:.0%}", theme["muted"],
                         size=11.5, anchor="middle"))
    body.append(line(px(power["baseline_rate"]), top - 26,
                     px(power["baseline_rate"]), top + 26, theme["series1"], 2,
                     cap="round"))
    body.append(circle(px(power["baseline_rate"]), top, 5.5, theme["series1"],
                       theme["surface"], 2))
    body.append(text(px(power["baseline_rate"]), top - 36,
                     f"measured {power['baseline_rate']:.1%}", theme["ink"],
                     size=12.5, weight="600", anchor="middle"))
    body.append(text(px(band_lo), top + 52,
                     f"{band_lo:.1%}", theme["ink_soft"], size=12, anchor="middle"))
    body.append(text(px(band_hi), top + 52,
                     f"{band_hi:.1%}", theme["ink_soft"], size=12, anchor="middle"))
    body.append(text(left, top + 78,
                     f"anything inside this band is this sample's noise: "
                     f"{power['cases']:,} cases cannot separate "
                     f"{power['baseline_rate']:.1%} from "
                     f"{power['detectable_rate_hi']:.1%}",
                     theme["ink_soft"], size=13))
    return frame(width, height,
                 "What this much history simply cannot tell you",
                 "Sampling error alone, at 95% confidence and 80% power - a "
                 "judge that contradicts itself moves the answer further.",
                 theme, "".join(body))


CHARTS = {
    "story": chart_story,
    "attribution": chart_attribution,
    "sweep": chart_sweep,
    "grid": chart_grid,
    "segments": chart_segments,
    "power": chart_power,
}


def render() -> dict[str, str]:
    """Every chart in both themes, keyed by the filename it belongs in."""
    data = figures()
    out = {}
    for name, draw in CHARTS.items():
        out[f"{name}.svg"] = draw(data, THEMES["light"])
        out[f"{name}-dark.svg"] = draw(data, THEMES["dark"])
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    check = "--check" in args
    files = render()
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, svg in files.items():
        path = OUT / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == svg:
            continue
        if check:
            stale.append(name)
            continue
        # An explicit newline, so the file on disk is the string this just
        # built, on every platform. Without it Python's text mode writes CRLF
        # on Windows and the committed picture depends on who last ran the
        # generator. The comparison above reads with universal newlines and so
        # would pass either way - this is about what gets committed.
        path.write_text(svg, encoding="utf-8", newline="\n")
    if check and stale:
        print(f"ERROR {len(stale)} chart(s) no longer match the fixture: "
              f"{', '.join(sorted(stale))}\n"
              f"  run: python scripts/build_charts.py", file=sys.stderr)
        return 1
    print(f"{len(files)} chart(s) {'checked' if check else 'written'} in "
          f"{OUT.relative_to(REPO).as_posix()}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
