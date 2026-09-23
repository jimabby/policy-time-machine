"""The demo video's storyboard, and the figures its narration quotes.

``tests/test_docs.py`` exists because four numbers in the documentation had
quietly stopped being true, and its argument is worth repeating here in full:
nothing breaks when a quoted figure goes stale, and that is precisely the
problem, because a reader has no way to tell a stale figure from a live one and
a document that is wrong about something checkable invites doubt about
everything it says that is not.

The three-minute video reintroduced exactly that. Its narration quotes "147 out
of 600", "48 changes", "38 of the 147" and "39 wrong"; its terminal and payoff
templates print the same figures again as rendered screen text. All of it sat
outside the check, in the one medium where a reader cannot grep for the number
and re-derive it - they are three minutes into a video, being told. Change the
fixture and README.md fails this suite honestly while the shipped video goes on
saying the old number over a picture of the new one.

So the video is held to the same standard as the prose, and the storyboard it
is cut to is held to its own: the pictures and the voice have to add up to the
same number per scene, or everything after the first mismatch plays under the
wrong sentence.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

pytestmark = pytest.mark.skipif(
    not (SCRIPTS / "storyboard.py").exists(),
    reason="the video pipeline is not present in this checkout")


@pytest.fixture(scope="module")
def storyboard():
    """The storyboard module, imported from scripts/ rather than installed.

    ``scripts/`` is not a package - it is a directory of entry points, named in
    no ``packages`` list in pyproject.toml and deliberately so, because nothing
    imports it at runtime. So the path goes on ``sys.path`` for the duration.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        import storyboard as module

        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def narration(storyboard) -> str:
    """Every word spoken in the video, with the pause directives removed.

    ``[[pause 0.4]]`` carries a number that has nothing to do with the fixture.
    Left in, it is a figure sitting in the text for any check that scans for
    integers.
    """
    return storyboard.spoken(" ".join(scene["narration"] for scene in storyboard.SCENES))


@pytest.fixture(scope="module")
def story(replayed) -> dict:
    """The headline figures, from a real replay of the shipped fixture.

    Same shape ``tests/test_docs.py`` checks the README against, deliberately:
    the video and the README are telling one audience one story, and the two of
    them disagreeing is as much worth catching as either being wrong alone.
    """
    from ptm import diff

    summary = diff.summarise(replayed["flips"], len(replayed["cases"]),
                             replayed["domain"])
    return {
        "cases": len(replayed["cases"]),
        "flips": summary["flips"],
        "policy_driven": summary["policy_driven_flips"],
        "deviations": summary["deviation_flips"],
    }


def rendered_text() -> str:
    """The screen text of the three standalone scene templates, tags stripped."""
    body = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((SCRIPTS / "templates").glob("*.html")))
    return re.sub(r"<[^>]+>", " ", body)


class TestTheStoryboardReconciles:
    """The contract that used to live in three files at once.

    ``build_shots`` had a shot table with a duration each, ``assemble_video``
    had the same table with the titles taken off, and ``generate_audio`` had
    the per-scene totals a third time. They agreed, and nothing compared them.
    The way they stop agreeing is that somebody lengthens one shot, the
    narration keeps its old length, and every scene after the edit plays under
    the wrong sentence - which is invisible until somebody watches all three
    minutes with the text in front of them.
    """

    def test_the_pictures_and_the_voice_add_up(self, storyboard):
        assert storyboard.check() == []

    def test_every_scene_is_covered_by_shots(self, storyboard):
        for scene in storyboard.SCENES:
            assert storyboard.scene_shots(scene["id"]), f"{scene['id']} has no shots"

    def test_the_total_is_derived_rather_than_typed(self, storyboard):
        """Three minutes, and the number the mux trims to is this one.

        It was written out as ``180.0`` in the ffmpeg call, which is a fourth
        copy of the same contract and the one that silently truncates the end
        of the video rather than reporting anything.
        """
        assert storyboard.TOTAL_SECONDS == sum(s["dur"] for s in storyboard.SHOTS)
        assert storyboard.TOTAL_SECONDS == pytest.approx(180.0)

    def test_no_two_shots_ask_the_page_for_the_same_state(self, storyboard):
        """Two shots with one capture directive render one frame, twice.

        ``s6_shot2`` and ``s6_shot3`` shared a branch and were byte-identical
        in the shipped video: eighteen seconds of one still, under narration
        describing the sweep being computed and then the curve it produced.
        """
        directives = [shot["sc"] for shot in storyboard.captures()]
        assert len(set(directives)) == len(directives)

    def test_every_line_is_spoken_over_its_own_shot(self, storyboard):
        """The voice belongs to the shot, so the picture cannot lag the words.

        The first cut had one paragraph per scene, time-stretched to fill it,
        and a picture could change halfway through the sentence about it.
        """
        for shot in storyboard.SHOTS:
            assert storyboard.spoken(shot.get("say", "")), f"{shot['id']} is silent"
        for scene in storyboard.SCENES:
            assert scene["narration"] == " ".join(
                shot["say"] for shot in storyboard.scene_shots(scene["id"]))

    def test_the_scripts_all_read_this_one_table(self):
        """None of the three carries a shot list or a duration of its own."""
        for name in ("build_shots.py", "assemble_video.py", "generate_audio.py"):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            assert "import storyboard" in source, f"{name} does not read the storyboard"
            assert not re.search(r"^SHOTS\s*=\s*\[", source, re.M), \
                f"{name} has its own copy of the shot table again"
            assert not re.search(r"^SEGMENTS\s*=\s*\[", source, re.M), \
                f"{name} has its own copy of the scene table again"


class TestTheNarrationQuotesTheFixture:
    """The figures the video states, against a real replay of the shipped data.

    Same fixture and the same ``story`` shape ``tests/test_docs.py`` checks the
    README against, deliberately: the video and the README are telling one
    audience one story, and the failure worth catching is the two of them
    disagreeing as much as either being wrong on its own.
    """

    def test_the_headline_split_is_spoken_correctly(self, storyboard, story):
        """"147 out of 600" and "38 of the 147", in the voiceover.

        Read as the sentences they are rather than as loose integers: "38 of
        the 147" states the flip count a second time, and a check that only
        counted occurrences would pass on a narration that had updated one
        mention and not the other.
        """
        spoken = narration(storyboard)
        assert re.search(rf"\b{story['flips']} out of {story['cases']}\b", spoken), \
            f"the narration no longer says '{story['flips']} out of {story['cases']}'"
        assert re.search(rf"\b{story['deviations']} of the {story['flips']}\b", spoken), \
            f"the narration no longer says '{story['deviations']} of the {story['flips']}'"

    def test_the_figures_named_in_the_storyboard_are_the_fixture_s(self,
                                                                  storyboard, story):
        """Every figure :data:`FIXTURE_FIGURES` claims, checked and then found.

        The mapping is what stops this being a scan for any number that happens
        to match: it names which key each figure is, so a narration that
        replaced the flip count with the case count would fail rather than
        quietly still contain two plausible integers.
        """
        spoken = narration(storyboard)
        for key, quoted in storyboard.FIXTURE_FIGURES.items():
            assert int(quoted) == story[key], \
                f"storyboard.FIXTURE_FIGURES[{key!r}] is {quoted}, the fixture says " \
                f"{story[key]}"
            assert re.search(rf"\b{quoted}\b", spoken), \
                f"the narration no longer quotes the {key} figure {quoted}"

    def test_the_review_queue_size_is_spoken_correctly(self, storyboard):
        """"The demo selects eight" - a claim about the shipped configuration.

        Asserted against ``review.max_reviews`` rather than against whatever is
        in the database when the suite runs, which is the same choice
        ``tests/test_docs.py`` makes about the README's ruling count and for
        the same reason.
        """
        from ptm.config import load_domain

        words = {8: "eight", 10: "ten", 12: "twelve"}
        expected = words.get(load_domain("expenses").review.max_reviews)
        assert expected, "the review queue size is outside the range this test can spell"
        assert expected in narration(storyboard).lower()

    def test_the_cards_figures_are_the_fixture_s(self, storyboard):
        """The two figures the cards draw that ``story`` does not carry.

        "39 wrong" is drawn on the scene-three card and "8" on the scene-five
        one; both are pictures of numbers, checked here against what computes
        them rather than trusted.
        """
        from ptm import pit_check
        from ptm.config import load_domain

        assert int(storyboard.NAIVE_WRONG) == pit_check.compare("expenses", "v2")["naive_wrong"]
        assert int(storyboard.REVIEWS) == load_domain("expenses").review.max_reviews
        spoken = narration(storyboard)
        assert re.search(rf"\b{storyboard.NAIVE_WRONG}\b", spoken)

    def test_the_rendered_templates_agree_with_the_voiceover(self, story):
        """The figures printed on screen, not merely spoken over it.

        ``term_scene.html`` prints the flip count and the naive-replay error
        count as terminal output, and ``payoff_scene.html`` prints the headline
        figures again. Those are pictures of numbers: nothing recomputes them,
        and a reader watching cannot check them.
        """
        shown = rendered_text()
        assert re.search(rf"\b{story['flips']} flips\b", shown), \
            f"the terminal template no longer shows '{story['flips']} flips'"
        assert re.search(rf"\b{story['cases']}\b", shown), \
            f"the templates no longer show the case count {story['cases']}"

    def test_the_naive_backtest_error_count_is_real(self):
        """"39 of these 600 cases wrong" - the claim scene three is built on.

        Computed here rather than trusted, because it is the one figure in the
        video that no other test covers and the one the whole point-in-time
        argument rests on. ``ptm.pit_check`` is what the scene is a picture of,
        so it is what the scene is checked against.
        """
        from ptm import pit_check

        result = pit_check.compare("expenses", "v2")
        wrong = result["naive_wrong"]
        shown = rendered_text()
        assert re.search(rf"\b{wrong} / \d+ cases\b", shown) or \
            re.search(rf"\bwrong on {wrong}\b", shown), \
            f"the terminal template no longer shows the naive error count {wrong}"
        assert re.search(rf"\b{wrong} false approvals\b", shown), \
            f"the trap card no longer shows {wrong} false approvals"


class TestTheDemoScriptRunsToTheSameClock:
    """The presenter's script and the video are the same three minutes.

    ``scripts/storyboard.py`` exists because three files each carried their own
    copy of the shot timings and nothing compared them. DEMO_SCRIPT.md is the
    fourth copy, and it carries the timings *three more times*: in the section
    headings a presenter reads off, in the run-of-show gantt, and in the beat
    table beside it. Every one of them is the same contract, and the way they
    stop agreeing is that somebody lengthens a beat in one place and the other
    two keep the old number - which reads as deliberate to anyone rehearsing
    from it, right up until the recording overruns.

    So the storyboard is still the one statement of the timings and this is the
    check that the script has not drifted from it.
    """

    SCRIPT = REPO / "DEMO_SCRIPT.md"

    @staticmethod
    def seconds(stamp: str) -> int:
        minutes, secs = stamp.split(":")
        return int(minutes) * 60 + int(secs)

    def text(self) -> str:
        return self.SCRIPT.read_text(encoding="utf-8")

    def starts(self, storyboard) -> list[int]:
        """When each scene begins, accumulated from the storyboard's durations."""
        out, cursor = [], 0.0
        for scene in storyboard.SCENES:
            out.append(int(cursor))
            cursor += scene["duration"]
        return out

    def test_the_gantt_has_one_bar_per_scene_at_the_right_time(self, storyboard):
        """The run-of-show diagram, parsed rather than looked at.

        A mermaid gantt renders whatever numbers it is given, so a wrong one is
        a clean-looking picture of the wrong three minutes.
        """
        block = re.search(r"```mermaid\n(gantt.*?)```", self.text(), re.S)
        assert block, "DEMO_SCRIPT.md no longer carries the run-of-show gantt"
        bars = re.findall(r":\w+, (\d+:\d+), (\d+)s", block.group(1))
        assert len(bars) == len(storyboard.SCENES), (
            f"the gantt draws {len(bars)} bar(s) for "
            f"{len(storyboard.SCENES)} scene(s)")
        for (stamp, length), start, scene in zip(bars, self.starts(storyboard),
                                                 storyboard.SCENES):
            assert self.seconds(stamp) == start, f"{scene['name']} starts wrong"
            assert int(length) == int(scene["duration"]), \
                f"{scene['name']} runs {length}s in the gantt"

    def test_the_gantt_adds_up_to_three_minutes(self, storyboard):
        block = re.search(r"```mermaid\n(gantt.*?)```", self.text(), re.S)
        total = sum(int(n) for n in re.findall(r":\w+, \d+:\d+, (\d+)s",
                                               block.group(1)))
        assert total == int(sum(s["duration"] for s in storyboard.SCENES)) == 180

    def test_the_beat_table_names_the_same_starts(self, storyboard):
        """One row per scene, each stamped with when the presenter gets there."""
        stamps = [self.seconds(s)
                  for s in re.findall(r"\| \*\*(\d+:\d+)\*\* ", self.text())]
        assert stamps == self.starts(storyboard), (
            f"the beat table starts at {stamps}, the storyboard at "
            f"{self.starts(storyboard)}")

    def test_the_section_headings_are_the_same_clock(self, storyboard):
        """``## 0:25-0:55`` - the timings a presenter actually reads.

        Checked as spans rather than starts, because a heading states both
        ends: an overlapping pair is a script that has a scene beginning before
        the one before it finished.
        """
        spans = re.findall(r"^## (\d+:\d+)[–-](\d+:\d+) ", self.text(),
                           re.M)
        assert len(spans) == len(storyboard.SCENES), (
            f"{len(spans)} timed section(s) for {len(storyboard.SCENES)} scene(s)")
        for (opens, closes), start, scene in zip(spans, self.starts(storyboard),
                                                 storyboard.SCENES):
            assert self.seconds(opens) == start, f"{scene['name']} opens wrong"
            assert self.seconds(closes) - self.seconds(opens) == \
                int(scene["duration"]), f"{scene['name']} runs the wrong length"
