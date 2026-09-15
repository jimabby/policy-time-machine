# Demo video — shot-by-shot script (3:00)

Scaffolding for recording, not part of the project. Delete it if you would
rather it were not in the repo.

**Before you start**

```bash
make up                      # Airflow at localhost:8080 (admin/admin), seeds on boot
```

Have three things open and ready to switch between:

- **A** — a terminal in the repo, font large enough to read at 720p
- **B** — the Airflow UI, already logged in, on the DAGs list
- **C** — the Diff Explorer at `localhost:8080/ptm/`, expenses / v2 selected and
  already loaded (it fetches on select; do not let the judges watch it load)

Measured runtimes: `pit_check` 0.4s, the sweep 0.6s, `selftest` 3.0s. Everything
you type finishes while you are still talking. Nothing here needs an API key —
`PTM_OFFLINE=1` is the default and the deterministic judge stands in.

---

## 0:00–0:20 — The problem (talk over screen A, nothing running yet)

> "Every organisation has rules humans apply to messy cases. Refunds, claims,
> expenses, moderation. When someone proposes changing one, the honest answer to
> *what will this actually do* is: nobody knows. People argue from anecdote,
> ship it, and find out in three months."

> "This makes that question computable. Change a rule, and Airflow replays every
> real decision you made over two years as it would have gone under the new
> one."

**Do not** read the README aloud. Twenty seconds, then move.

## 0:20–0:50 — The answer (screen A)

Run:

```bash
make demo        # backfill create: 24 monthly runs, two years of history
```

Cut to **B** while it fans out. Point at the backfill's runs filling in.

> "This is one `backfill create`. Twenty-four monthly runs, each replaying its
> own slice of history. Backfill isn't the scheduler here — it's the simulation
> engine."

If the backfill is slow to show, cut back to **A** and run `python -m
ptm.selftest` instead, which does the whole loop in three seconds with no
Airflow. Say *"same code the DAGs call, driven by a plain loop"* — that is true
and it is a safer shot.

Land on the headline:

```
147 outcomes change (24.5%) · 135 more generous · net GBP 12,014
```

## 0:50–1:25 — Why it is more than a diff (screen C, Diff Explorer)

This is the heart of the demo. Scroll to **What drives the change**.

> "A hundred and forty-seven changed isn't actionable. *Which sentence do I
> edit* is."

Point at `clause 1.1 relaxed — 48 flips`.

> "That clause changed by **ceasing to apply**. It cites nothing in the new
> policy, so reading only the new verdict leaves most changes unexplained.
> That's why the replay judges the old policy too."

Then point at `(reviewer deviated from policy) — 38 flips`.

> "And these thirty-eight aren't the proposal's fault at all. Both policies
> agree; the recorded outcome was already wrong. Charging those to v2 overstates
> it by twenty-six per cent."

If you cut one thing from this video, do not cut this beat.

## 1:25–1:45 — Point-in-time (screen A)

```bash
python -m ptm.pit_check
```

```
point-in-time replay : 147 flips
naive replay         : wrong on 39 / 600 cases
```

> "Each case is replayed with the facts known **on the day it was decided**, not
> today's. Skip that and thirty-nine of six hundred come out wrong. That's what
> data intervals buy you."

## 1:45–2:10 — The human loop and the gate (screen B, then C)

Open `adjudicate_expenses`.

> "The flips asset wakes this. Eight of the hundred and forty-seven go to a
> human — HITL, deferred in the triggerer, holding no worker slot. And it asks
> for the *correct outcome*, not a yes/no."

Show the HITL task waiting if you have one; otherwise show the DAG graph.

> "Those rulings become precedent. The precedents asset wakes the gate, which
> re-judges every one of them against any future policy and **fails** on a
> reversal."

Cut to **C**, the Gate tile.

> "So the first run gives you an estimate. Every run after gives you a
> regression suite for organisational judgment."

## 2:10–2:35 — What should the number be? (screen A)

```bash
python -m ptm.sweep expenses v2 1.1 amount_gbp 25,50,75,100,150,250
```

> "Attribution says *which* clause. This says what the number should be — the
> whole replay re-run at each setting. The choice of threshold stops being an
> argument and becomes a curve."

Let the caveat line at the bottom stay on screen for a beat. Judges notice a
project that undercuts its own output.

## 2:35–2:55 — The model writes the next version (screen A)

```bash
make propose
```

> "And the same typed operator, pointed the other way: `output_type=PolicyPatch`
> has a model write the next version of the policy."

Beat. Then the line that matters:

> "That's only safe because it writes against an oracle it can't influence. The
> draft goes straight through the precedent gate. A patch that argues
> beautifully and breaks a human ruling fails exactly as loudly as one that
> argues badly."

Point at the last line: `(not written; re-run with --write to draft v2-draft1)`.

> "Proposing and adopting are separate acts. Adopting is a person's."

## 2:55–3:00 — Close

> "Airflow 3.1, Common AI, HITL, assets, dynamic task mapping, a UI plugin.
> Runs offline with no API key. Repo's in the description."

---

## Notes

- **Record in one take per section and cut.** Three minutes is tight and the
  sections are independent.
- **Do not narrate what is on screen.** Say what it *means*. The screen already
  says what it says.
- **Have `make up` finished before you hit record.** A container starting is
  thirty seconds of nothing.
- **Cut, in this order, if you overrun:** the sweep (2:10), then the proposer
  (2:35), then point-in-time (1:25). Never cut the attribution beat at 0:50 —
  it is the one thing here nothing else does.
- If the wifi dies, everything above still runs. That is deliberate.
