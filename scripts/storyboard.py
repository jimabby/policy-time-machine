"""The three-minute demo, as one table the whole pipeline reads.

The video is built by three scripts that ran in sequence and each carried its
own copy of the timing: :mod:`build_shots` had a ``SHOTS`` list with a duration
per shot, :mod:`assemble_video` had the same list again with the titles taken
off, and :mod:`generate_audio` had the per-scene totals a third time. Three
copies of one contract, and nothing compared them. They agreed - and the way
they stop agreeing is that somebody lengthens a shot in one file, the narration
keeps its old length, and every scene after the edit plays under the wrong
sentence. Nobody notices until they watch all three minutes.

So the storyboard is stated once here and the three scripts read it.
:func:`check` is the assertion that the two halves still reconcile, and
``tests/test_video.py`` runs it, which is what turns "they agree today" into
something a build can say.

**The voice belongs to the shot, not the scene.** The first cut gave each scene
one paragraph of narration and stretched the synthesised audio to fill the
scene with ffmpeg's ``atempo`` - which is why it sounded smeared, and why a
picture could change halfway through the sentence describing it. Now every
shot carries the words spoken over it (``say``), each shot's audio is placed at
the start of its own shot at the voice's natural speed, and
:mod:`generate_audio` refuses a line that does not fit its shot rather than
squeezing it. A scene's narration is derived from its shots, so there is still
exactly one copy of every word.

**Two kinds of shot.** A shot with ``sc`` is a still of the real dashboard,
captured by :mod:`build_shots` and slowly zoomed from ``zoom[0]`` to
``zoom[1]`` - boxes ``(x, y, width)`` as fractions of the frame, height
following from 16:9. A shot with ``card`` is an animated title card drawn by
:mod:`assemble_video`: the one idea the scene is about, in numbers big enough
to read on a phone.

**The numbers in the narration are the fixture's.** "147 out of 600", "38 of
the 147", "39 wrong": every one is a figure the replay computes, quoted in
prose that cannot recompute it. ``tests/test_docs.py`` exists because exactly
this went stale in the README - see its docstring - and the video is the same
claim in a form nobody can grep while watching. So the narration is checked
against the fixture by the same test, the figures are named in
:data:`FIXTURE_FIGURES` so the check knows what it is looking at rather than
scanning for loose integers, and the cards draw their numbers from those same
names rather than typing them again.
"""

from __future__ import annotations

import re

#: The fixture figures the narration quotes and the cards draw, mapped to the
#: key ``ptm.report.story`` returns them under. ``tests/test_video.py`` checks
#: each one against a live replay of the shipped fixture, so a change to the
#: demo data cannot leave the video saying the old number.
FIXTURE_FIGURES: dict[str, str] = {
    "cases": "600",
    "flips": "147",
    "policy_driven": "109",
    "deviations": "38",
}

#: How many cases the naive (today's-facts) replay gets wrong. Checked against
#: ``ptm.pit_check`` by the test suite, like the terminal template that prints it.
NAIVE_WRONG = "39"

#: How many cases the demo routes to a person. Checked against the expenses
#: domain's ``review.max_reviews``.
REVIEWS = "8"

#: The voice. A neural voice at its natural pace; the pace is not adjusted to
#: fit a slot, because that adjustment is what made the first cut hard to follow.
VOICE = "en-US-AndrewNeural"

#: Seconds of quiet at the head of each shot before its line starts, so a cut
#: lands before the sentence about it rather than on top of its first word.
LEAD_IN = 0.35

#: Seconds a line must finish before its shot ends, so the last word is not
#: clipped by the crossfade into the next shot.
TAIL = 0.25

#: The seven scenes, in order. ``narration`` is filled in from the shots below.
SCENES: list[dict] = [
    {"id": "scene1", "name": "The Bet", "duration": 25.0},
    {"id": "scene2", "name": "The Plot Twist", "duration": 30.0},
    {"id": "scene3", "name": "No Spoilers from the Future", "duration": 25.0},
    {"id": "scene4", "name": "Open the Machine", "duration": 30.0},
    {"id": "scene5", "name": "The Person Gets a Say", "duration": 30.0},
    {"id": "scene6", "name": "Let the Room Choose", "duration": 25.0},
    {"id": "scene7", "name": "Would You Ship It?", "duration": 15.0},
]

#: Every shot, in play order. ``title`` is what the shot is *of*; ``say`` is
#: what is spoken over it, where ``[[pause N]]`` is N seconds of silence.
SHOTS: list[dict] = [
    # -- 1 · The bet -----------------------------------------------------------
    {"id": "s1_hook", "scene": "scene1", "dur": 6.0, "card": "hook",
     "title": "What if you could try tomorrow's rules on yesterday?",
     "say": "What if you could test a new rule on the past, before it goes live?"},
    {"id": "s1_shot1", "scene": "scene1", "dur": 9.0, "sc": "s1_1",
     "zoom": ((0.12, 0.05, 0.76), (0.13, 0.22, 0.64)),
     "title": "The Bet: Place your prediction",
     "say": "We want to change our expense rules. So, a quick bet: out of 600 past "
            "decisions, how many would now get a different answer?"},
    {"id": "s1_reveal", "scene": "scene1", "dur": 10.0, "card": "reveal",
     "title": "The Bet: 147 of 600 decisions change",
     "say": "The answer: 147 out of 600. [[pause 0.5]] Almost one in four. "
            "[[pause 0.4]] A small rule change, with a big footprint."},

    # -- 2 · The plot twist ----------------------------------------------------
    {"id": "s2_shot2", "scene": "scene2", "dur": 8.0, "sc": "s2_2",
     "zoom": ((0.12, 0.04, 0.76), (0.13, 0.08, 0.52)),
     "title": "The Plot Twist: Clause 1.1 (48 changes)",
     "say": "So which sentence did it? One clause, the receipt rule, "
            "accounts for 48 of those changes."},
    {"id": "s2_split", "scene": "scene2", "dur": 12.0, "card": "split",
     "title": "The Plot Twist: 109 caused by the proposal, 38 already there",
     "say": "But here's the twist. 38 of the 147 already broke the old rulebook. "
            "[[pause 0.4]] The new rule didn't cause them. Only 109 are its doing."},
    {"id": "s2_shot4", "scene": "scene2", "dur": 10.0, "sc": "s2_4",
     "zoom": ((0.22, 0.08, 0.56), (0.24, 0.42, 0.52)),
     "title": "The Evidence: Case review dialog & historical facts",
     "say": "And every change is one click from its evidence: the facts on the day, "
            "and what each rulebook said."},

    # -- 3 · No spoilers from the future ---------------------------------------
    {"id": "s3_promo", "scene": "scene3", "dur": 7.0, "card": "promotion",
     "title": "No spoilers: the promotion question",
     "say": "Someone was promoted last year. Should that change what they could "
            "claim two years ago?"},
    {"id": "s3_shot2", "scene": "scene3", "dur": 10.0, "sc": "term_2",
     "zoom": ((0.14, 0.14, 0.72), (0.17, 0.26, 0.46)),
     "title": "Terminal: 39 naive replay errors & future bias",
     "say": "Of course not. So the replay only uses what was known on the day. "
            "Use today's facts instead, and 39 answers come out wrong."},
    {"id": "s3_wrong", "scene": "scene3", "dur": 8.0, "card": "wrong",
     "title": "No spoilers: 39 wrong with today's facts",
     "say": "Point-in-time replay keeps tomorrow's knowledge out of yesterday's "
            "decisions."},

    # -- 4 · Open the machine --------------------------------------------------
    {"id": "s4_loop", "scene": "scene4", "dur": 15.0, "card": "loop",
     "title": "Under the Hood: Four core steps",
     "say": "The whole machine is four steps. [[pause 0.3]] Remember the old facts. "
            "[[pause 0.3]] Replay both rulebooks. [[pause 0.3]] Ask a person about "
            "the cases that matter. [[pause 0.3]] Then check every future rule "
            "against their answers."},
    {"id": "s4_shot3", "scene": "scene4", "dur": 6.0, "sc": "af_runs",
     "zoom": ((0.02, 0.00, 0.98), (0.11, 0.10, 0.66)),
     "title": "Orchestration: the monthly backfill runs in Airflow",
     "say": "Airflow runs each step as a scheduled workflow."},
    {"id": "s4_shot2", "scene": "scene4", "dur": 9.0, "sc": "s4_2",
     "zoom": ((0.12, 0.08, 0.76), (0.22, 0.44, 0.56)),
     "title": "Engine Room: Architecture diagram",
     "say": "The evidence lands in one shared database, and this dashboard reads "
            "it back."},

    # -- 5 · The person gets a say ---------------------------------------------
    {"id": "s5_eight", "scene": "scene5", "dur": 8.0, "card": "eight",
     "title": "Human Decisions: 8 cases, not 600",
     "say": "Nobody has to read 600 cases. The demo picks just eight for a person "
            "to judge."},
    {"id": "s5_shot1", "scene": "scene5", "dur": 11.0, "sc": "af_review",
     "zoom": ((0.00, 0.00, 1.00), (0.35, 0.22, 0.64)),
     "title": "Human Decisions: a reviewer rules in Airflow",
     "say": "Each answer becomes a precedent: a test every future rule must pass. "
            "Reverse one, and the check fails until somebody explains why."},
    {"id": "s5_shot2", "scene": "scene5", "dur": 11.0, "sc": "s5_2",
     "zoom": ((0.12, 0.06, 0.76), (0.13, 0.07, 0.40)),
     "title": "Precedent Gate: Baseline comparison",
     "say": "And it's fair. If the old policy already broke that precedent, "
            "the new rule doesn't take the blame. A red result needs a real reason, "
            "not a scapegoat."},

    # -- 6 · Let the room choose -----------------------------------------------
    {"id": "s6_shot1", "scene": "scene6", "dur": 7.0, "sc": "s6_1",
     "zoom": ((0.12, 0.07, 0.42), (0.13, 0.08, 0.34)),
     "title": "Sweep: Threshold dial selection",
     "say": "Still arguing about the limit? 50 pounds? 100? 150?"},
    {"id": "s6_shot3", "scene": "scene6", "dur": 9.0, "sc": "s6_3",
     "zoom": ((0.12, 0.06, 0.76), (0.12, 0.10, 0.64)),
     "title": "Sweep: receipt limit at 50, 75, 100, 150 GBP",
     "say": "Sweep them all at once. Each row shows how many decisions would "
            "change, and what it costs."},
    {"id": "s6_choice", "scene": "scene6", "dur": 9.0, "card": "tradeoff",
     "title": "Sweep: trade-offs, not a recommendation",
     "say": "These use the offline rules. They show the trade-offs, not a "
            "recommendation. The choice stays with you."},

    # -- 7 · Would you ship it? ------------------------------------------------
    {"id": "s7_shot2", "scene": "scene7", "dur": 9.0, "sc": "s7_2",
     "zoom": ((0.10, 0.10, 0.80), (0.18, 0.20, 0.64)),
     "title": "Payoff: Would you ship this rule?",
     "say": "So, would you ship this rule? Now you know who it affects, what it "
            "costs, and which decisions it must respect."},
    {"id": "s7_end", "scene": "scene7", "dur": 6.0, "card": "end",
     "title": "Policy Time Machine",
     "say": "Policy Time Machine. Try tomorrow's rules on yesterday's decisions."},
]

#: The pause directive inside ``say``: ``[[pause 0.4]]`` is 0.4 s of silence.
PAUSE = re.compile(r"\[\[pause ([0-9.]+)\]\]")

#: The cards :mod:`assemble_video` knows how to draw.
CARDS = ("hook", "reveal", "split", "promotion", "wrong", "loop", "eight",
         "tradeoff", "end")


def spoken(text: str) -> str:
    """A line with its pause directives removed: what is actually said."""
    return re.sub(r"\s+", " ", PAUSE.sub(" ", text)).strip()


def scene_shots(scene_id: str) -> list[dict]:
    """The shots belonging to one scene, in play order."""
    return [shot for shot in SHOTS if shot["scene"] == scene_id]


# A scene's narration is its shots' lines, joined: derived, so the words exist
# once. Kept on the scene because the tests and DEMO_SCRIPT read it per scene.
for _scene in SCENES:
    _scene["narration"] = " ".join(shot["say"] for shot in scene_shots(_scene["id"]))

#: The total the mux trims to. Derived rather than typed: it was written out as
#: ``180.0`` in the ffmpeg call, which is a fourth copy of the same contract and
#: the one that silently truncates the end of the video when it falls behind.
TOTAL_SECONDS: float = sum(scene["duration"] for scene in SCENES)


def shot_starts() -> dict[str, float]:
    """When each shot begins on the video's clock, in seconds."""
    out, cursor = {}, 0.0
    for shot in SHOTS:
        out[shot["id"]] = cursor
        cursor += shot["dur"]
    return out


def captures() -> list[dict]:
    """Every still, whichever script captures it."""
    return [shot for shot in SHOTS if "sc" in shot]


#: Capture directives taken from a running Airflow rather than from the
#: dashboard. The video's claim is that Airflow does the work, so two of its
#: stills are Airflow itself - the backfill's runs and a reviewer's form - and
#: those only exist once a stack has replayed and queued a review.
#: :mod:`build_airflow_shots` captures them; :mod:`build_shots` leaves them be.
AIRFLOW_PREFIX = "af_"


def is_airflow(shot: dict) -> bool:
    return shot.get("sc", "").startswith(AIRFLOW_PREFIX)


def check() -> list[str]:
    """Everything that must hold for the storyboard to render as intended.

    Returned as sentences rather than raised, so a caller can print all of them
    at once. Empty means the table is coherent.
    """
    problems: list[str] = []
    scene_ids = [scene["id"] for scene in SCENES]
    if len(set(scene_ids)) != len(scene_ids):
        problems.append("two scenes share an id")
    shot_ids = [shot["id"] for shot in SHOTS]
    if len(set(shot_ids)) != len(shot_ids):
        problems.append("two shots share an id")

    for shot in SHOTS:
        if shot["scene"] not in scene_ids:
            problems.append(f"{shot['id']} belongs to unknown scene {shot['scene']!r}")
        if ("sc" in shot) == ("card" in shot):
            problems.append(f"{shot['id']} must be exactly one of a capture (sc) or a card")
        if "card" in shot and shot["card"] not in CARDS:
            problems.append(f"{shot['id']} asks for unknown card {shot['card']!r}")
        if "sc" in shot:
            zoom = shot.get("zoom")
            if not zoom or len(zoom) != 2:
                problems.append(f"{shot['id']} is a capture without a start and end zoom")
            else:
                for x, y, w in zoom:
                    if not (0 <= x and 0 <= y and 0 < w <= 1
                            and x + w <= 1 + 1e-9 and y + w <= 1 + 1e-9):
                        problems.append(f"{shot['id']} zooms outside the frame: {(x, y, w)}")
        if not spoken(shot.get("say", "")):
            problems.append(f"{shot['id']} has nothing said over it")

    # Every capture directive is distinct. Two shots asking the page for the
    # same state render the same frame, which is how s6_shot2 and s6_shot3
    # became one image played twice under two different pieces of narration.
    directives = [shot["sc"] for shot in captures()]
    duplicated = sorted({d for d in directives if directives.count(d) > 1})
    if duplicated:
        problems.append(
            f"capture directive(s) {duplicated} are used by more than one shot, so "
            f"those shots render identical frames")

    # The reconciliation this module exists for: the pictures have to add up to
    # the scene's running time, per scene and not merely overall. A total that
    # matches while two scenes are wrong in opposite directions is the failure
    # that looks fine in a summary line.
    for scene in SCENES:
        shots = sum(shot["dur"] for shot in scene_shots(scene["id"]))
        if not shots:
            problems.append(f"scene {scene['id']} has no shots")
        elif abs(shots - scene["duration"]) > 1e-9:
            problems.append(
                f"scene {scene['id']} ({scene['name']}) has {shots}s of shots against "
                f"a {scene['duration']}s scene; everything after it would start late")
    return problems


if __name__ == "__main__":  # pragma: no cover
    import sys

    found = check()
    for line in found:
        print(f"ERROR {line}", file=sys.stderr)
    print(f"{len(SCENES)} scene(s), {len(SHOTS)} shot(s) "
          f"({len(captures())} captured), {TOTAL_SECONDS}s total")
    raise SystemExit(1 if found else 0)
