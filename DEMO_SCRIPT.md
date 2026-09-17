# Demo video — shot-by-shot script (3:00)

Scaffolding for recording, not part of the project. Delete it if you would
rather it were not in the repo.

**The whole video in one sentence, so you never lose the thread:** *a rule
change is accused of a hundred and forty-seven crimes, and this thing works out
which ones it actually committed.*

Everything below is in service of that. If a beat does not move that story
forward, it is the beat to cut.

---

## Before you hit record

```bash
make up                      # Airflow at localhost:8080 (no login), seeds on boot
```

Three things open, ready to switch between:

- **A** — a terminal in the repo, font big enough to read at 720p. Bigger than
  you think — assume a laptop, in a browser tab, at half size.
- **B** — the Airflow UI (no login needed), on the DAGs list.
- **C** — the Diff Explorer at `localhost:8080/ptm/`, expenses / v2 selected and
  **already loaded**. It fetches on select; nobody needs to watch a spinner.
  Leave it on the **Plain summary** it opens with — switching to **Full detail**
  on camera is a beat, not an accident.

Measured runtimes: `pit_check` 0.4s, the sweep 0.6s, `selftest` 3.0s. Every
command here finishes while you are still talking, which is the only reason
three minutes is enough. Nothing needs an API key — `PTM_OFFLINE=1` is the
default and the deterministic judge stands in.

---

## 0:00–0:20 — Cold open: nobody knows

*Screen A. Nothing running. Just you.*

> "Every organisation has rules that humans apply to messy cases. Refunds,
> claims, expenses, moderation. Somebody proposes changing one, and the honest
> answer to *what will this actually do* is: nobody knows."

Beat.

> "So we argue from anecdote, ship it, and find out in three months."

Beat. Then land it:

> "This makes that question computable. Change a rule, and Airflow replays every
> real decision you made over two years as it would have gone under the new one."

**Do not** read the README aloud. You have twenty seconds and one job: make them
want the next shot.

## 0:20–0:50 — Two years, twenty seconds

*Screen A, then cut to B.*

```bash
make demo        # backfill create: 24 monthly runs, two years of history
```

Cut to **B** while it fans out. Point at the runs filling in — this is the shot
that looks like something is happening, so let it breathe for two seconds.

> "That's one `backfill create`. Twenty-four monthly runs, each replaying its
> own slice of history. Backfill isn't the scheduler here — it's the simulation
> engine."

**Escape hatch.** If the backfill is slow to paint, cut back to **A** and run
`python -m ptm.selftest` instead: the whole loop in three seconds, no Airflow.
Say *"same code the DAGs call, driven by a plain loop"* — true, and a much safer
shot than a grid that has not finished rendering.

Land on the headline:

```
147 outcomes change (24.5%) · 135 more generous · net GBP 12,014
```

> "A hundred and forty-seven decisions come out differently. Twelve thousand
> pounds. Now — what do you do with that number?"

That question is the hinge of the whole video. Ask it, then cut.

## 0:50–1:25 — The reveal, and the alibi

*Screen C. This is the heart of the demo. If you nail one beat, nail this one.*

Start on the plain summary, at the headline.

> "This is what the person who owns the rule sees. One sentence: a hundred and
> forty-seven of the last six hundred decisions change."

> "And straight underneath — a hundred and nine of those are the proposal's
> doing. The other thirty-eight were *already* wrong under the rule they have
> today."

Let that sit for a second. It is the most interesting sentence in the video.

Point at the three stacked bars under **What changes**, then hit **Full detail**.

> "Everything behind that answer is one click away. None of it is recomputed —
> same numbers, more of them."

Scroll to **What drives the change**.

> "A hundred and forty-seven changed isn't actionable. *Which sentence do I
> edit* is."

Point at `clause 1.1 relaxed — 48 flips`.

> "That clause did the damage by **ceasing to apply**. It cites nothing in the
> new policy — so if you only read the new verdict, most of your changes have no
> explanation at all. That's why the replay judges the old policy too."

Then point at `(reviewer deviated from policy) — 38 flips`.

> "And these thirty-eight aren't the proposal's fault. Both policies agree on
> them; the *recorded* outcome was already wrong. Blame those on v2 and you have
> overstated it by twenty-six per cent."

> "Every diff tool on earth would have charged v2 for all one hundred and
> forty-seven."

If you cut one thing from this video, do not cut this beat. It is the one thing
here that nothing else does.

## 1:25–1:45 — No hindsight allowed

*Screen A.*

```bash
python -m ptm.pit_check
```

```
point-in-time replay : 147 flips
naive replay         : wrong on 39 / 600 cases
```

> "Every case is replayed with the facts known **on the day it was decided** —
> not today's. Skip that, and thirty-nine of six hundred come out wrong."

> "You can't ask what a rule would have done using facts it couldn't have had.
> That's what data intervals buy you, and it's the bug you'd never find in
> production, because the wrong answer looks exactly like the right one."

Short, sharp, move on. This beat is a punch, not a paragraph.

## 1:45–2:10 — The jury, and the precedent it sets

*Screen B, then C.*

Open `adjudicate_expenses`.

> "The flips asset wakes this one. Eight of the hundred and forty-seven go to a
> human — HITL, deferred in the triggerer, holding no worker slot. And it asks
> for the *correct outcome*, not a yes or no."

Show the HITL task waiting if you have one; the DAG graph if you don't.

> "Those rulings become precedent. The precedents asset wakes the gate, which
> re-judges every one of them against any future policy — and **fails** on a
> reversal."

Cut to **C**, the Gate tile.

> "So the first run gives you an estimate. Every run after it gives you a
> regression suite for organisational judgment."

> "Your rules now have tests. Written by the people who actually make the calls."

## 2:10–2:35 — Stop arguing, draw the curve

*Screen A.*

```bash
python -m ptm.sweep expenses v2 1.1 amount_gbp 25,50,75,100,150,250
```

> "Attribution tells you *which* clause. This tells you what the number should
> be — the entire replay, re-run at every setting."

Let the table land, then:

> "The threshold stops being an argument in a meeting and becomes a curve."

Now leave the caveat line at the bottom on screen for a beat, and say so out
loud:

> "And it tells you not to trust it too far — these came from the offline rules,
> not a real judge."

Judges notice a project that undercuts its own output, and almost nothing else
you could do with that second buys as much credibility.

## 2:35–2:55 — The model takes a swing

*Screen A.*

```bash
make propose
```

> "Same typed operator, pointed the other way: `output_type=PolicyPatch` has a
> model write the next version of the policy."

Beat — let them worry about that for a moment. Then:

> "Which is only safe because it writes against an oracle it can't influence.
> The draft goes straight through the precedent gate. A patch that argues
> beautifully and breaks a human ruling fails exactly as loudly as one that
> argues badly."

Point at the last line: `(not written; re-run with --write to draft v2-draft1)`.

> "Proposing and adopting are separate acts. Adopting is a person's."

## 2:55–3:00 — Close

> "Airflow 3.1, Common AI, HITL, assets, dynamic task mapping, a UI plugin. Runs
> offline, no API key. Repo's in the description."

Stop talking. Do not add a summary — you just gave them three minutes of one.

---

## Director's notes

- **One take per section, then cut.** Three minutes is tight, the sections are
  independent, and there is no prize for a single take.
- **Do not narrate what is on screen.** Say what it *means*. The screen already
  says what it says, and it says it faster than you can.
- **Silence is a tool.** The marked beats — after "nobody knows", after the
  thirty-eight, after "a model writes the policy" — are doing more work than any
  sentence around them. One full second. It feels like ten from behind the
  microphone and reads as deliberate on playback.
- **`make up` finishes before you hit record.** A container starting is thirty
  seconds of nothing, and thirty seconds is a sixth of the film.
- **Cut, in this order, if you overrun:** the sweep (2:10), then the proposer
  (2:35), then point-in-time (1:25). Never cut the attribution beat at 0:50.
- **If the wifi dies, everything above still runs.** That is deliberate, and if
  it happens live it is worth saying out loud.

### When the demo gods intervene

| It breaks | You say | You do |
| --- | --- | --- |
| Backfill grid slow to paint | *"same code the DAGs call, driven by a plain loop"* | Cut to **A**, `python -m ptm.selftest` |
| No HITL task waiting | *"this is the queue it raises"* | Show the DAG graph instead |
| A panel is empty | *"that DAG hasn't run in this environment"* | Move on — the panel names the DAG, so do not read it aloud |
| The gate is red | *"and that's the point — it fails on a reversal"* | Nothing. It is supposed to fail on the shipped fixture |

That last row is not a save. `ptm.gate expenses v2` fails on the shipped data
because policy v1 reverses the same two rulings, and the proposal introduces
neither. A red gate on camera is the feature working. Know that cold, because it
is the one thing a sharp judge will ask about.
