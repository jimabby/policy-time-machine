"""The numbers the documentation quotes, checked against the thing they count.

This project's whole argument is that a number should carry its provenance, and
the documentation was quoting four that had quietly stopped being true: two
different test counts in one file (849 in the layout section, 689 in the
caveats, neither of them right), a third in SUBMISSION.md, and a caveat count
that had been thirty for a while and was twenty-seven. Nothing was broken by
any of it. That is precisely the problem - a reader has no way to tell a stale
figure from a live one, so a document that is wrong about something checkable
invites doubt about everything it says that is not.

So the counts are computed here rather than remembered. This test fails when
somebody adds a test and does not update the figure, which is the intended
cost: it is one number in one file, and the alternative is what was here
before.

Collection, not execution, because a skip is still a test that exists - and
because the DAG and browser suites skip on a platform that has no SIGALRM and
no Chromium, so an execution count would mean something different on every
machine that ran it.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: The suites that need something other than Python: Airflow for the DAG parse,
#: FastAPI for the plugin routes, Playwright for the browser. "The engine's N
#: tests" is the count with these taken out, which is the number that tells a
#: reader what `make test` will run on their laptop.
NEEDS_MORE_THAN_PYTHON = ("test_dags.py", "test_plugin.py", "test_dashboard.py")


def collect(*ignore: str) -> int:
    """How many tests pytest finds, without running any of them."""
    argv = [sys.executable, "-m", "pytest", "--collect-only", "-q",
            *(f"--ignore=tests/{name}" for name in ignore)]
    result = subprocess.run(argv, cwd=REPO, capture_output=True, text=True)
    # One line per test id. Counted rather than read off the summary line,
    # which pytest has reworded before and will again.
    found = sum(1 for line in result.stdout.splitlines() if "::" in line)
    assert found, f"collection produced nothing:\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    return found


@pytest.fixture(scope="module")
def counts() -> dict:
    return {"total": collect(), "engine": collect(*NEEDS_MORE_THAN_PYTHON)}


def numbers_in(path: str, pattern: str) -> list[int]:
    """Every number ``pattern`` captures, in order, flattened across groups.

    ``re.findall`` hands back tuples once a pattern has more than one group and
    bare strings when it has one, so the flattening is what lets a caller write
    the pattern that reads best rather than the one that returns a tidy shape.
    """
    text = (REPO / path).read_text(encoding="utf-8")
    found: list[int] = []
    for match in re.findall(pattern, text):
        groups = match if isinstance(match, tuple) else (match,)
        found.extend(int(g) for g in groups)
    return found


class TestTheTestCountsAreReal:
    def test_the_design_notes_count_both_correctly(self, counts):
        quoted = numbers_in(
            "docs/DESIGN.md", r"(\d{3,5}) tests; the engine's (\d{3,5}) need nothing")
        assert quoted, "docs/DESIGN.md no longer states the counts in the expected shape"
        assert quoted == [counts["total"], counts["engine"]]

    def test_the_caveat_repeats_the_same_engine_count(self, counts):
        """Two figures for one thing in one document is worse than one wrong
        figure: a reader cannot even tell which is meant to be authoritative.
        They said 849 and 689."""
        quoted = numbers_in("docs/DESIGN.md", r"The engine's (\d{3,5}) tests")
        assert quoted == [counts["engine"]]

    def test_the_submission_counts_match(self, counts):
        quoted = numbers_in(
            "SUBMISSION.md", r"(\d{3,5}) tests \((\d{3,5}) need nothing but Python\)")
        assert quoted, "SUBMISSION.md no longer states the counts in the expected shape"
        assert quoted == [counts["total"], counts["engine"]]


@pytest.fixture(scope="module")
def story(replayed) -> dict:
    """The four headline figures, computed from a real replay of the fixture."""
    from ptm import diff

    summary = diff.summarise(replayed["flips"], len(replayed["cases"]), replayed["domain"])
    return {
        "cases": len(replayed["cases"]),
        "flips": summary["flips"],
        "policy_driven": summary["policy_driven_flips"],
        "deviations": summary["deviation_flips"],
    }


class TestTheThirtySecondStoryIsReal:
    """The four numbers the README opens with, checked against the fixture.

    This file exists because a stale checkable number invites doubt about the
    uncheckable ones, and it guarded the test count and the caveat count - two
    figures about the repository - while leaving the four figures about the
    *product* unchecked. Those are the first thing a reader meets, the ones
    quoted back in the demo, and the only ones a change to the shipped policy or
    the seed can silently falsify.

    Computed from a real replay rather than read from the database, so this
    fails when the fixture, the policy or the rules move - which is the point.
    It is the same argument the project makes everywhere else: a number carries
    its provenance, or it is a number somebody will have to re-derive under
    pressure in front of an audience.
    """

    def test_the_case_count_and_the_flip_count(self, story):
        quoted = numbers_in("README.md", r"\| (\d{2,5}) historical decisions")
        assert quoted == [story["cases"]], "README's case count"
        quoted = numbers_in("README.md", r"\| (\d{2,5}) different answers")
        assert quoted == [story["flips"]], "README's flip count"

    def test_the_split_between_the_policy_and_the_reviewers(self, story):
        """The line the project says costs the most to produce and matters most.

        Both halves in one test because they are one claim: 109 of 147 are the
        proposal's doing and the other 38 are not. A README that updated one and
        not the other would be arithmetic that does not add up, which is worse
        than either number being stale on its own.
        """
        attributed = numbers_in(
            "README.md", r"\| (\d{2,5}) changes attributed to the proposal")
        other = numbers_in("README.md", r"The other (\d{2,5}) differ from the old rulebook")
        assert attributed == [story["policy_driven"]], "README's policy-driven count"
        assert other == [story["deviations"]], "README's deviation count"
        assert story["policy_driven"] + story["deviations"] == story["flips"]

    def test_the_ruling_count(self, story):
        """Established by the selftest, which the ``replayed`` fixture does not run.

        So this asserts the README against ``review.max_reviews`` - what the
        domain says a human queue holds - rather than against whatever happens
        to be in the database when the suite runs. The number in the README is a
        claim about the shipped configuration, and that is where it lives.
        """
        from ptm.config import load_domain

        quoted = numbers_in("README.md", r"\| (\d{1,4}) simulated human rulings")
        assert quoted == [load_domain("expenses").review.max_reviews]


WORDS = {20: "twenty", 30: "thirty", 40: "forty"}


class TestTheCaveatCountIsReal:
    def test_the_readme_says_how_many_there_are(self):
        """The README sends a reader to the caveat list with a count attached,
        and the count is the reason to go: "there are twenty-seven of them" is
        a claim about how much this project declines to assert."""
        design = (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")
        section = design.split("## Caveats", 1)[1].split("\n## ", 1)[0]
        actual = sum(1 for line in section.splitlines() if line.startswith("- "))
        assert actual, "the caveats section is no longer a list of top-level bullets"

        tens, units = divmod(actual, 10)
        spelled = WORDS.get(tens * 10)
        assert spelled, f"{actual} caveats is outside the range this test can spell"
        expected = f"{spelled}-{('one two three four five six seven eight nine'.split())[units - 1]}" \
            if units else spelled

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        assert expected in readme, (
            f"the caveats section has {actual} entries, so the README should say "
            f"{expected!r}; it does not")
