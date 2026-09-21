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

**The numbers in the narration are the fixture's.** "147 out of 600", "48
changes", "38 of the 147", "39 wrong": every one is a figure the replay
computes, quoted in prose that cannot recompute it. ``tests/test_docs.py``
exists because exactly this went stale in the README - see its docstring - and
the video is the same claim in a form nobody can grep while watching. So the
narration is checked against the fixture by the same test, and the figures are
named in :data:`FIXTURE_FIGURES` so the check knows what it is looking at
rather than scanning for loose integers.
"""

from __future__ import annotations

#: The seven scenes, in order: how long each runs and what is said over it.
#:
#: ``narration`` is fed to the speech synthesiser. ``[[slnc N]]`` is macOS
#: ``say``'s pause directive and is stripped before the text is checked for the
#: figures below, so a pause never hides a number.
SCENES: list[dict] = [
    {
        "id": "scene1",
        "name": "The Bet",
        "duration": 25.0,
        "narration": (
            "We're thinking about changing our expense rules. Before we announce "
            "anything: how many old decisions do you think would get a different "
            "answer? Ten? Fifty? Half of them? [[slnc 1200]] In this demo, 147 out of "
            "600. Almost one in four. That small rule change just became a much more "
            "interesting conversation."
        ),
    },
    {
        "id": "scene2",
        "name": "The Plot Twist",
        "duration": 30.0,
        "narration": (
            "Which sentence did it? This receipt clause accounts for 48 changes. But "
            "there's a twist: 38 of the 147 differences also disagree with the old "
            "rulebook. [[slnc 1000]] The proposal didn't create those differences. In "
            "the flips table, we can inspect each case's historical facts, candidate "
            "clauses, and human rulings."
        ),
    },
    {
        "id": "scene3",
        "name": "No Spoilers from the Future",
        "duration": 25.0,
        "narration": (
            "Imagine someone was promoted last year. Should today's seniority change "
            "what they were entitled to two years ago? [[slnc 1000]] This replay uses "
            "what was known on the day. Using today's facts gets 39 of these 600 cases "
            "wrong. [[slnc 1500]] Point in time replay protects you from tomorrow's "
            "bias."
        ),
    },
    {
        "id": "scene4",
        "name": "Open the Machine",
        "duration": 30.0,
        "narration": (
            "Bring back the old facts. Try both rulebooks. Ask a person about selected "
            "changes. Save their answer so the next proposal has to face it too. "
            "[[slnc 1000]] Airflow coordinates those steps. The machine stores the "
            "evidence in one shared memory, and this screen reads it back so we can "
            "discuss it together."
        ),
    },
    {
        "id": "scene5",
        "name": "The Person Gets a Say",
        "duration": 30.0,
        "narration": (
            "We don't ask someone to read 600 cases. The demo selects eight. Each "
            "answer becomes an example future rules are checked against. If a proposal "
            "reverses one, the check fails and somebody has to resolve it. [[slnc "
            "1200]] And we still ask whether the old policy already made the same "
            "reversal. A red result needs an explanation, not a convenient scapegoat."
        ),
    },
    {
        "id": "scene6",
        "name": "Let the Room Choose",
        "duration": 25.0,
        "narration": (
            "What would you choose: 50, 100, or 150 pounds? We can compare the "
            "consequences before we pick. [[slnc 1200]] These results use the offline "
            "rules; they show trade-offs, not a recommendation."
        ),
    },
    {
        "id": "scene7",
        "name": "Pay Off the Opening Question",
        "duration": 15.0,
        "narration": (
            "Would you ship this rule? Now we can discuss who it affects, what it "
            "costs, and which human decisions it must respect. [[slnc 600]] Try "
            "tomorrow's rules on yesterday's decisions, before tomorrow becomes a "
            "surprise."
        ),
    },
]

#: Every shot, in play order. ``scene`` ties it to :data:`SCENES`, ``sc`` is the
#: capture directive :mod:`build_shots` hands the page, and ``title`` is what
#: the shot is *of* - which is also the claim the test suite checks against the
#: capture, because a title saying one thing over a frame showing another is how
#: seventeen seconds of this video came to hold a single repeated image.
SHOTS: list[dict] = [
    {"id": "s1_shot1", "scene": "scene1", "dur": 8.0, "sc": "s1_1",
     "title": "The Bet: Place your prediction"},
    {"id": "s1_shot2", "scene": "scene1", "dur": 5.0, "sc": "s1_2",
     "title": "The Bet: Moving slider to 20%"},
    {"id": "s1_shot3", "scene": "scene1", "dur": 5.0, "sc": "s1_3",
     "title": "The Bet: Guess comparison reveal"},
    {"id": "s1_shot4", "scene": "scene1", "dur": 7.0, "sc": "s1_4",
     "title": "The Bet: 147 flips impact chart & coverage"},

    {"id": "s2_shot1", "scene": "scene2", "dur": 7.0, "sc": "s2_1",
     "title": "The Plot Twist: Clauses overview"},
    {"id": "s2_shot2", "scene": "scene2", "dur": 8.0, "sc": "s2_2",
     "title": "The Plot Twist: Clause 1.1 (48 changes)"},
    {"id": "s2_shot3", "scene": "scene2", "dur": 7.0, "sc": "s2_3",
     "title": "The Plot Twist: 38 pre-existing deviations"},
    {"id": "s2_shot4", "scene": "scene2", "dur": 8.0, "sc": "s2_4",
     "title": "The Evidence: Case review dialog & historical facts"},

    {"id": "s3_shot1", "scene": "scene3", "dur": 8.0, "sc": "term_1",
     "title": "Terminal: pit_check command & replay"},
    {"id": "s3_shot2", "scene": "scene3", "dur": 9.0, "sc": "term_2",
     "title": "Terminal: 39 naive replay errors & future bias"},
    {"id": "s3_shot3", "scene": "scene3", "dur": 8.0, "sc": "term_3",
     "title": "Terminal: manage.py coverage & provenance"},

    {"id": "s4_shot1", "scene": "scene4", "dur": 8.0, "sc": "s4_1",
     "title": "Under the Hood: Four core steps"},
    {"id": "s4_shot2", "scene": "scene4", "dur": 12.0, "sc": "s4_2",
     "title": "Engine Room: Architecture diagram"},
    {"id": "s4_shot3", "scene": "scene4", "dur": 10.0, "sc": "dag_view",
     "title": "Orchestration: Airflow DAG code"},

    {"id": "s5_shot1", "scene": "scene5", "dur": 10.0, "sc": "s5_1",
     "title": "Human Decisions: 8 Precedents"},
    {"id": "s5_shot2", "scene": "scene5", "dur": 10.0, "sc": "s5_2",
     "title": "Precedent Gate: Baseline comparison"},
    {"id": "s5_shot3", "scene": "scene5", "dur": 10.0, "sc": "s5_3",
     "title": "Human Rationale: Resolution details"},

    {"id": "s6_shot1", "scene": "scene6", "dur": 7.0, "sc": "s6_1",
     "title": "Sweep: Threshold dial selection"},
    {"id": "s6_shot2", "scene": "scene6", "dur": 10.0, "sc": "s6_2",
     "title": "Sweep: Computing trade-offs"},
    {"id": "s6_shot3", "scene": "scene6", "dur": 8.0, "sc": "s6_3",
     "title": "Sweep: 25, 50, 75, 100, 150 GBP curve"},

    {"id": "s7_shot1", "scene": "scene7", "dur": 7.0, "sc": "s7_1",
     "title": "Summary: Would you ship this rule?"},
    {"id": "s7_shot2", "scene": "scene7", "dur": 8.0, "sc": "s7_2",
     "title": "Payoff: Final metrics & quickstart"},
]

#: The fixture figures the narration quotes, mapped to the key
#: ``ptm.report.story`` returns them under. ``tests/test_docs.py`` reads this
#: and checks each one against a live replay of the shipped fixture, so a
#: change to the demo data cannot leave the video saying the old number.
FIXTURE_FIGURES: dict[str, str] = {
    "cases": "600",
    "flips": "147",
    "deviations": "38",
}

#: The total the mux trims to. Derived rather than typed: it was written out as
#: ``180.0`` in the ffmpeg call, which is a fourth copy of the same contract and
#: the one that silently truncates the end of the video when it falls behind.
TOTAL_SECONDS: float = sum(scene["duration"] for scene in SCENES)


def scene_shots(scene_id: str) -> list[dict]:
    """The shots belonging to one scene, in play order."""
    return [shot for shot in SHOTS if shot["scene"] == scene_id]


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

    # Every capture directive is distinct. Two shots asking the page for the
    # same state render the same frame, which is how s6_shot2 and s6_shot3
    # became one image played twice under two different pieces of narration.
    directives = [shot["sc"] for shot in SHOTS]
    duplicated = sorted({d for d in directives if directives.count(d) > 1})
    if duplicated:
        problems.append(
            f"capture directive(s) {duplicated} are used by more than one shot, so "
            f"those shots render identical frames")

    for shot in SHOTS:
        if shot["scene"] not in scene_ids:
            problems.append(f"{shot['id']} belongs to unknown scene {shot['scene']!r}")

    # The reconciliation this module exists for: the pictures and the voice have
    # to add up to the same number, per scene and not merely overall. A total
    # that matches while two scenes are wrong in opposite directions is the
    # failure that looks fine in a summary line.
    for scene in SCENES:
        shots = sum(shot["dur"] for shot in scene_shots(scene["id"]))
        if not shots:
            problems.append(f"scene {scene['id']} has no shots")
        elif abs(shots - scene["duration"]) > 1e-9:
            problems.append(
                f"scene {scene['id']} ({scene['name']}) has {shots}s of shots against "
                f"{scene['duration']}s of narration; the voice and the pictures would "
                f"drift apart from here on")
    return problems


if __name__ == "__main__":  # pragma: no cover
    import sys

    found = check()
    for line in found:
        print(f"ERROR {line}", file=sys.stderr)
    print(f"{len(SCENES)} scene(s), {len(SHOTS)} shot(s), {TOTAL_SECONDS}s total")
    raise SystemExit(1 if found else 0)
