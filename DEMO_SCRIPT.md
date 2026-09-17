# A rule walks into a time machine

A three-minute demo for people who care about decisions, not DAG syntax.
The audience's question throughout: **“Would you ship this rule?”**

## Set the stage

Start Docker, then run `docker compose up --build -d`. Once Airflow is ready,
open [the dashboard](http://localhost:8080/ptm/?domain=expenses&version=v2) and
[Airflow](http://localhost:8080). The included demo permits access without login.

Prepare results before presenting. Run `python demo.py --setup --step` the first
time, then `python demo.py --step` for rehearsals. It writes offline results to
`include/ptm.db`; the compose setup mounts that directory for the dashboard too.

To show Airflow doing the replay itself, run this beforehand and wait for completion:

```bash
docker compose exec airflow airflow backfill create --dag-id replay_expenses --from-date 2024-09-01 --to-date 2026-09-01 --run-backwards
```

Keep expenses / v2 selected in Plain summary, a terminal ready, and browser zoom comfortable for
the back row. Verify the counts before recording: persisted rulings or modified
fixtures can change them. Narrate what the screen actually shows.

**Say once:** “This is synthetic demo data. The offline judge uses deterministic
rules, and the local self-test simulates the reviewers. In a live workflow,
people answer the review requests.” No model call or API key is needed.

## 0:00–0:25 · The bet

**Show:** the dashboard's opening question and **Place your prediction**.
Keep the impact chart below the fold until the audience has guessed.

> “We're thinking about changing our expense rules. Before we announce anything:
> how many old decisions do you think would get a different answer? Ten? Fifty?
> Half of them?”

Pause for a guess. Move the slider to the room's prediction and click
**Compare my guess**. Then scroll to the impact chart. The slider is a guess,
not a policy setting; the reveal uses the selected replay's actual counts.

> “In this demo, 147 out of 600. Almost one in four. That small rule change just
> became a much more interesting conversation.”

## 0:25–0:55 · The plot twist

**Click:** **Find the cause**. Point to clause 1.1 and the deviation row.

> “Which sentence did it? This receipt clause accounts for 48 changes. But
> there's a twist: 38 of the 147 differences also disagree with the old rulebook.
> The proposal didn't create those differences.”

> “We're separating the effect of a new rule from the mess already there.”

Say “different answer”; explain once that the tables call these “flips”.

## 0:55–1:20 · No spoilers from the future

**Show:** the terminal. With the project's virtual environment active, run:

```bash
python -m ptm.pit_check
```

> “Imagine someone was promoted last year. Should today's seniority change what
> they were entitled to two years ago? This replay uses what was known on the
> day. Using today's facts gets 39 of these 600 cases wrong.”

Let the result sit for a beat. These numbers apply to the shipped expenses fixture.

## 1:20–1:50 · Open the machine

**Click:** **Look under the hood** on the dashboard.

Trace the four boxes: remember, rewind, ask a person, check again.
Open **Open the engine room** for the diagram of workflows, shared memory,
and the dashboard. Trace it from top to bottom with the pointer.

> “Bring back the old facts. Try both rulebooks. Ask a person about selected
> changes. Save their answer so the next proposal has to face it too.
> Airflow coordinates those steps.”

If someone asks where the chart comes from: “The machine stores the evidence
in one shared memory. This screen reads it back so we can discuss it together.”

For a technical audience, briefly show the monthly runs in Airflow.

## 1:50–2:20 · The person gets a say

**Click:** **Meet the human decisions**. Point to a ruling and its reason.

> “We don't ask someone to read 600 cases. The demo selects eight. Each answer
> becomes an example future rules are checked against. If a proposal reverses
> one, the check fails and somebody has to resolve it.”

Point to the gate's baseline comparison if available.

> “And we still ask whether the old policy already made the same reversal.
> A red result needs an explanation, not a convenient scapegoat.”

If showing an actual Airflow review request, identify it as awaiting a real person;
do not present simulated rulings as live reviewer activity.

## 2:20–2:45 · Let the room choose

**Switch to Full detail. Show:** **Where should the threshold be?** Select the expenses receipt threshold
(clause 1.1, `amount_gbp`), enter `25,50,75,100,150`, and click **Sweep**.

> “What would you choose: 50, 100, or 150 pounds? We can compare the consequences
> before we pick. These results use the offline rules; they show trade-offs,
> not a recommendation.”

Ask for one vote, then point to that setting's result. Keep the caveat visible.

## 2:45–3:00 · Pay off the opening question

**Show:** the impact summary again.

> “Would you ship this rule? Now we can discuss who it affects, what it costs,
> and which human decisions it must respect. Try tomorrow's rules on yesterday's
> decisions—before tomorrow becomes a surprise.”

## Keep the show moving

- Rehearse with `python demo.py --step`; Enter is your scene change.
- If the site is unavailable, show the offline tour and explain that it exercises
  the engine, not the Airflow UI. A fresh `--setup` needs internet for dependencies.
- If short on time, cut the terminal check and threshold vote. Keep the impact,
  attribution twist, and human decision.
- If asked about AI drafting, explain that proposals can be drafted and checked
  against rulings. Passing that check is not approval; adoption is a human action.
- End on the audience's decision, not a list of libraries.

---

## Director's notes

- **One take per section, then cut.** Three minutes is tight, the sections are
  independent, and there is no prize for a single take.
- **Do not narrate what is on screen.** Say what it *means*. The screen already
  says what it says, and it says it faster than you can.
- **Silence is a tool.** The beats after the audience's guess, after the
  thirty-eight, and after the threshold vote are doing more work than any
  sentence around them. One full second. It feels like ten from behind the
  microphone and reads as deliberate on playback.
- **`make up` finishes before you hit record.** A container starting is thirty
  seconds of nothing, and thirty seconds is a sixth of the film.
- **Cut, in this order, if you overrun:** the threshold vote (2:20), then the
  point-in-time check (0:55). Keep the attribution reveal at 0:25.
- **If the wifi dies, everything above still runs.** That is deliberate, and if
  it happens live it is worth saying out loud.

### When the demo gods intervene

| It breaks | You say | You do |
| --- | --- | --- |
| Backfill grid slow to paint | *"Let's replay the story in the terminal."* | Switch to the prepared terminal and run `python -m ptm.selftest` |
| No HITL task waiting | *"this is the queue it raises"* | Show the DAG graph instead |
| A panel is empty | *"that DAG hasn't run in this environment"* | Move on — the panel names the DAG, so do not read it aloud |
| The gate is red | *"and that's the point — it fails on a reversal"* | Nothing. It is supposed to fail on the shipped fixture |

That last row is not a save. `ptm.gate expenses v2` fails on the shipped data
because policy v1 reverses the same two rulings, and the proposal introduces
neither. A red gate on camera is the feature working. Know that cold, because it
is the one thing a sharp judge will ask about.
