# Policy Time Machine

**Change a rule today. Airflow replays every real decision your organisation
made over the last two years as it would have gone under the new rule — using
the data as it stood at the time. Humans adjudicate only the cases where the
old and new answers disagree. Those adjudications become a permanent
regression suite that every future rule change must pass.**

Built for **Beyond the DAG**. Airflow 3.1, the Common AI provider, HITL
operators, assets, dynamic task mapping, and a UI plugin.

---

## The problem

Every organisation has consequential rules that humans apply to messy cases:
refund eligibility, claims handling, loan criteria, content moderation,
admissions, expense policy. When someone proposes changing one, the honest
answer to *"what will this actually do?"* is **nobody knows**. People argue
from anecdote, ship it, and find out three months later.

This makes that question computable — and then makes the answer *stick*.

## What it produces

```
replayed 600 decisions under policy v2
  147 outcomes change (24.5%)
  135 more generous  GBP 17,994
  12 more strict     GBP 5,980
  net GBP 12,014

what in policy v2 causes the change (baseline: policy v1):
  clause 1.1 relaxed                48 flips (32.6%)  net GBP  2,304
  (reviewer deviated from policy)   38 flips (25.9%)  net GBP -2,210
  clause 2.1 relaxed                22 flips (15.0%)  net GBP  1,125
  clause 2.1                        15 flips (10.2%)  net GBP  4,245
  clause 6.1                        11 flips ( 7.5%)  net GBP  2,350
  ...

  109 of 147 changes are caused by policy v2 (net GBP 14,224).
  38 are cases the recorded outcome got wrong under policy v1 too,
  so they are not this proposal's doing.

re-judged 25 flips 3x each under the same policy
  25 reproduced, 0 did not

8 flips routed to a human out of 147 (6 caused by v2, 2 pre-existing deviations)
8 precedents established

gate: policy v2 vs 8 precedents -> 2 violation(s)
  exp-0478: finance.lead ruled 'approve', v2 gives 'deny'  (so does v1)
  0 introduced by v2; 2 the policy in force (v1) already reverses
GATE FAILS - but every reversal is one policy v1 already makes.
```

Four things are load-bearing there, and each answers a question the previous
one provokes.

**"147 change" is not actionable.** *Which sentence do I edit?* is. Every change
is attributed to the clause responsible — including a clause that changed by
**ceasing** to apply, which is how most rule changes actually move decisions.
A relaxed threshold cites nothing, so reading only the new policy's verdict
leaves the majority of changes unexplained.

**Not every change is the proposal's fault.** 38 of those 147 are cases where
the recorded outcome disagreed with policy v1 *as well* — a reviewer departing
from the rulebook they already had. Charging those to v2 overstates it by 26%.
Separating them needs the in-force policy judged too, which is why the replay
judges both sides.

**A number without an error bar is not a number.** Before a flip is put in
front of a human it is judged again, several times. One that will not
reproduce is the model changing its mind rather than the policy moving, and it
is held out of the queue and reported separately — because precedent is
permanent and writing model noise into it is not recoverable.

**The gate is the point.** The first run gives you an *estimate*. Every run
after gives you a **regression suite for organisational judgment**. It judges
the policy *in force* alongside the candidate, so a reversal the status quo
already makes is not billed to the proposal — in the run above, the proposal
introduces none of them.

---

## Why this is an Airflow project

Not "an LLM in a DAG". Every capability here is load-bearing.

| Airflow capability | What it does here |
|---|---|
| **Backfill** | Is the simulation engine. One `backfill create` fans out 24 monthly runs that replay two years of real decisions. |
| **Data intervals** | Make the replay *honest*. Each run only sees cases inside its own window, and each case is hydrated with facts known on its decision date. **Skip this and 39 of 600 cases come out wrong** — see below. |
| **Dynamic task mapping** | One judge task per case, with concurrency capped so you don't melt the model endpoint. Doubled when the baseline pass is on. |
| **Common AI provider** | `LLMOperator` with `output_type=Verdict`, so every verdict is typed, not parsed out of prose. `usage_limits` caps spend per task. The vendor lives in a connection — switching models never touches DAG code. |
| **HITL operators** | `HITLOperator` deferred in the triggerer, holding no worker slot, asking a human for the *correct outcome* — not a yes/no. |
| **Assets** | `ptm://<domain>/flips` wakes adjudication; `ptm://<domain>/precedents` wakes the regression gate. Nothing is polled. |
| **Plugin (FastAPI + external view)** | The Policy Diff Explorer, a tab inside the Airflow UI. |
| **Dynamic DAG generation** | Drop a YAML in `include/domains/` and five new DAGs appear. The DAG code contains zero domain knowledge — [a test asserts it](tests/test_dags.py). |
| **Structured generation** | The same `LLMOperator`, pointed the other way: `output_type=PolicyPatch` has a model *write* the next version of the policy, which the precedent gate then re-judges. Typed output is what makes that checkable rather than a wall of prose. |

### The point-in-time trap

The fixture promotes half the employees to grade 3 partway through the
period. Proposed policy v2 exempts grade 3+ from receipts. Join *today's*
grade onto historical cases — which is what every hand-rolled backtest does —
and you wrongly approve claims from before those promotions:

```
$ python -m ptm.pit_check
point-in-time replay : 147 flips
naive replay         : wrong on 39 / 600 cases
```

Every one of those 39 errors flatters the proposal — they are **all** in the
loosening direction, which [the test suite
asserts](tests/test_point_in_time.py), because a naive backtest does not add
noise, it adds bias. Airflow's data-interval semantics are what stop it.

---

## Architecture

Five DAGs per domain, generated from `include/domains/*.yaml`:

```
                    ┌─────────────────────┐
   backfill ───────▶│  replay_<domain>    │  @monthly × 24 runs
                    │  read the policy    │  free, offline, before a penny is spent
                    │  point-in-time load │
                    │  cache split        │  skip what this prompt already answered
                    │  → map judge/case   │  LLMOperator, response_model=Verdict
                    │  → map judge/case   │  again, under the in-force policy
                    │  → diff + attribute │  clause, segment and cost breakdowns
                    │  → disparity        │  who carries more of it than the rest
                    └──────────┬──────────┘
                               │ Asset: ptm://<domain>/flips
                    ┌──────────▼──────────┐
                    │ adjudicate_<domain> │  selects ~8 contested flips
                    │ HITLOperator (map)  │  ← a human answers in the Airflow UI
                    │ → precedents        │
                    └──────────┬──────────┘
                               │ Asset: ptm://<domain>/precedents
                    ┌──────────▼──────────┐
                    │ precedent_gate_<d>  │  re-judges every precedent
                    │ FAILS on reversal   │  ← the regression suite
                    │ warns on conflicts  │  ← rulings that contradict each other
                    │ scores the judge    │  ← accuracy, against the humans
                    └─────────────────────┘

   manual  ────────▶ judge_stability_<domain>   the error bar on all of the above

   manual  ────────▶ ┌─────────────────────┐
                     │  propose_<domain>   │  reads every number above
                     │  → drafts a patch   │  LLMOperator, output_type=PolicyPatch
                     │  → writes a version │  include/drafts/, judged like any other
                     │  → re-runs the gate │  ← and this is why it is allowed to
                     └─────────────────────┘
```

`select_for_review` is deliberately stingy: humans see a flip only if the
judge was unsure, the money is large, or the change makes the organisation
more permissive than it chose to be. 147 flips → 8 human decisions.

Two things then narrow it further, and both exist because ranking purely by
money spends that budget badly:

- **A flip that would not reproduce never reaches a human.** See the
  confirmation pass below.
- **Deviations get a reserved minority of the slots, not the top of the list.**
  A deviation is a case both policies decide identically, so the proposal did
  not cause it — and deviations are reliably the *largest* flips by money.
  Left to sort themselves they take most of the queue, and the precedents that
  come back then fail the gate for a candidate that had nothing to do with
  them. `max_deviation_reviews` caps them at 2 of 8. They are still reported in
  full, and still worth those two slots, because such a ruling settles a case
  the policy *already in force* gets wrong.

### What each DAG produces

**`replay_<domain>`** — the simulation. Beyond the diff it records:

- **Clause attribution.** Which clause of the proposal is responsible for which
  share of the change, and which of them changed by *ceasing to apply*.
- **Blast radius.** Per segment (`segment_fields` in the YAML), how many cases
  moved *out of how many* — because 9 flips out of 12 travel claims is a
  different proposal from 9 out of 400.
- **A concentration check.** Which segments carry more of the change than the
  rest of their field — the question the blast-radius table lets a room skim
  past. See below.
- **A cost ledger.** What the judging cost *net of the cache*, plus a forecast
  for a full replay, so "can I afford this backfill" is answerable before
  launching it.
- **A flip rate with a sampling band.** `24.5% (21.2%–28.1%)` on 600 cases.
  A monthly run sees twenty-five, and without the band two months that differ
  only in size read as two different policies.

**`adjudicate_<domain>`** — the human loop. Each HITL task now tells the
reviewer which clause drove the change, so they can argue with the rule rather
than only the result.

**`precedent_gate_<domain>`** — the regression suite, and the only place the
judge is scored against an answer rather than against itself. Fails on a reversal, and
separately *warns* when two human rulings contradict each other: precedent is
the only durable output here, so it is also the only thing that can quietly
rot. Two reviewers answering materially identical cases differently make the
suite unsatisfiable, and nothing else in the pipeline would notice.

It judges the policy **in force** alongside the candidate, and separates the
reversals the proposal *introduces* from the ones the status quo already makes.
Three violations means something very different when v1 has the same three, and
without that split a gate failure reads as the proposal's fault by default.

It also loads its cases **by id**. Filtering a full history load would be
subject to `load_cases`'s default limit, so past that many cases the gate would
judge a subset of the precedent set and still report a pass — a regression
suite that silently checks less than it reports is worse than none. A precedent
whose case cannot be loaded fails the run rather than being skipped.

**`judge_stability_<domain>`** — the error bar, in two modes.

`target=sample` judges the same cases several times under the *same* policy and
reports how often the judge contradicts itself:

```
judged 25 cases 3x each under the same policy
  2 disagreed with themselves (8.0% disagreement rate)
  against a 24.5% flip rate, up to 33% of the measured change
  could be judge noise rather than policy.
```

*(Illustrative — the numbers above are what a real judge looks like. The
offline judge is deterministic and reports 0%; see below.)*

`target=flips` asks the narrower and far more actionable question: **will this
particular flip survive being judged again?** An aggregate rate tells you how
much of the total change might be noise. It does not tell you whether the
specific verdict a human is about to turn into permanent precedent is real.

```
re-judged 25 flips 3x each under the same policy
  23 reproduced, 2 did not
  the ones that did not are the judge changing its mind, not the policy moving,
  and are held back from the human queue:
    exp-0207: approve x2, partial x1, recorded 'approve'
    exp-0411: deny x3, recorded 'partial'
```

The second of those is the case worth having. A judge can be perfectly
self-consistent on re-judging and still land somewhere other than the run that
recorded the flip; counting that as confirmation would launder a contradiction
into precedent, so it does not count.

The obvious objection to any of this is *"is that the policy, or is that the
model?"*. Without this number there is no answer. `max_disagreement` turns it
into a gate: refuse to trust a judge noisier than you can accept. Offline the
judge is deterministic and this necessarily reports 0% — which is not a clean
bill of health, it means the check is inert until you point it at a real model.

### Consistent is not the same as right

Judge stability asks whether the judge repeats itself. It is a real question and
it is a weak one: a judge can reproduce its own verdicts perfectly and be
reliably wrong, and every number in this project would look exactly the same.

The answer was sitting in the database the whole time. Every precedent is a case
a human looked at and settled, and the gate stores what the judge said about
those same cases — so scoring one against the other costs nothing and runs on
every gate:

```
judge vs 8 human ruling(s) under policy v2
  agreed on 6 of 8  -  75.0% (40.9%-92.8%)
  mean confidence 89%, overconfident by 14% (ECE 24%)
    claimed 60%-75%: right 1/1 (100%), said 60% -> under by 40%
    claimed 90%-100%: right 5/7 (71%), said 93% -> over by 21%
  where it goes wrong:
    human ruled 'approve', judge said 'deny'  x2
  the review threshold (75%) does NOT separate: below it 100% right (1 cases),
  above it 71% right (7 cases)
    a confidence that does not predict correctness is routing the review budget at random
  precedents are the contested flips, so this is a floor on the judge's accuracy,
  not an estimate of it
```

Three things there, in rising order of how much they should worry you.

**Accuracy, with a band.** Six of eight is 75%, and on eight cases that is
anywhere between 41% and 93%. Quoting the point estimate alone from a precedent
set this small would be the same mistake the flip rate makes without its own
band.

**Confidence is load-bearing and had never been checked.**
`review.below_confidence` routes cases to humans on the judge's *own claim*
about how sure it is. Above it the judge is right 71% of the time; below it,
100%. That is one case below the line and proves nothing on its own — which is
the point of the significance test behind `threshold_separates`, and the reason
the line reads *does not separate* rather than *is inverted*. What it does
establish is that nothing has ever demonstrated the threshold works, and the
scarcest resource here is being allocated by it.

**It is a floor, not an estimate.** Precedents are by construction the
*contested* flips — low confidence, large money, or a loosening nobody chose.
Nobody adjudicates the easy ones. The judge's accuracy over all six hundred
cases is higher than 75%, and anyone quoting this as overall accuracy is quoting
it wrong. The report says so on every line it prints.

### Does the change land evenly?

Blast radius already reports a flip rate per segment. In practice nobody reads
down it: twelve rows of percentages is exactly the shape of information a room
skims. The question underneath it — *is this change concentrated on one group?*
— is the first one a compliance or legal function asks, and for refunds,
lending, admissions or moderation it is the question that stops a proposal.

```
1 segment(s) the change does not land on evenly:
  category=meals            61/116 moved (52.6%, 43.6%-61.4%) vs 86/484 (17.8%)
                            - 2.96x the rest of category
      mostly loosening, net GBP 5,004
a concentration is not a fault - it is a question. These are the segments
somebody should be able to explain before the rule ships.
```

Three decisions make this worth having rather than noise:

- **The comparison is pooled.** A segment is measured against the rest of its
  own field, never against the least-affected bucket — which would make the
  smallest, noisiest group the protagonist of every finding.
- **Small buckets are not compared at all.** Below `min_cases` (30 by default)
  nothing is reported, and anything reported carries a significance flag from
  two disjoint Wilson intervals. Nine of twelve travel claims moving is a 75%
  rate and almost no evidence; publish that as a finding and the panel stops
  being read, which is worse than not having one.
- **Direction stays attached.** A group whose cases mostly *loosen* is being
  given something; a group whose cases mostly *tighten* is having something
  taken away. A flip rate cannot tell those apart. A ratio *below* 1 is the
  third case worth seeing — a group the change passes over, which for a
  loosening proposal means a benefit distributed unevenly.

What this is not is evidence of discrimination, and it never says so. Segments
differ in what they contain; a policy that raises the meals cap will always move
meals. Every finding is a request to justify a concentration, and the
justification is often excellent. `disparity.gate: fail` in the domain YAML
turns it into a gate for domains where shipping first and explaining afterwards
is not an option.

### Read the policy before you pay to replay it

A full LLM-backed replay of the shipped fixture is about USD 2, or USD 4 with
the baseline pass. The failure worth avoiding is not an expensive run — it is an
expensive run whose output nobody can use: a third of the cases coming back as
`(no clause applies)` because half the policy's rules were written as unnumbered
prose, or two rules sharing the number 3.1 so attribution merges them and names
the wrong sentence to edit.

Every one of those is visible by reading the policy. `ptm.preflight` reads it,
for free, offline, at the head of every replay:

```
$ python -m ptm.preflight expenses
preflight: policy v1 is structurally sound
preflight: 1 finding(s) in policy v2, 0 of them blocking
  WARNING clause 7.1   [unreachable] clause 7.1 is defined only by reference to
      another version ('Unchanged from v1.'). The judge is shown one policy at a
      time, so this clause is empty at judging time - inline the text if it is
      meant to decide anything.
```

That finding is real and it is in the policy this repository ships. v2's
discretion clause says *"Unchanged from v1."* — and a judge shown only v2 has
nothing to apply.

It divides cleanly with the lint: `ptm.lint` checks the offline *fixtures*
against the policy, this checks the **policy markdown**, which is the artefact
the real judge is shown and the one the lint never looks inside. Neither
subsumes the other, so the lint runs both and `preflight: fail` on the replay
DAG refuses to spend a backfill on a policy whose results could not be
attributed.

The structural pass is free and runs everywhere. The model pass on top of it —
one call, cents, before the four dollars — is for what structure cannot see: two
clauses that contradict each other, a threshold stated twice in different units,
a sentence three readers would apply three ways.

### So what should the number actually be?

Attribution stops at *clause 1.1 accounts for 48 of them*. The question that
follows is always *then what should it say?* — and a clause like "receipts
required above GBP 75" has exactly one dial on it. `ptm.sweep` turns it:

```
$ python -m ptm.sweep expenses v2 1.1 amount_gbp 25,50,75,100,150,250
sweeping clause 1.1 amount_gbp in expenses/v2 over 600 cases (baseline v1)
    amount_gbp   flips    rate  loosen  tighten      net GBP  policy-driven
            25      93  15.5%      80       13        9,106             54
            50     116  19.3%     104       12       10,068             78
            75     147  24.5%     135       12       12,014            109  <- current
           100     161  26.8%     149       12       13,248            123
           150     179  29.8%     170        9       16,180            144
           250     212  35.3%     204        8       23,143            178
```

`python -m ptm.sweep expenses v2` lists the dials; the same curve is a panel in
the Diff Explorer.

This reads the `offline_rules`, not the markdown policy, and the caveat is
worth stating plainly: it reports what the *rule evaluator* would do, not what
a model reading a reworded policy would do. That makes it the free first pass
for narrowing a range you then confirm with one real replay — not a
substitute for one. The lint is what keeps the rules and the markdown honest
with each other in between.

---

### Can the sweep be trusted?

The curve above is computed from `offline_rules`, so it is worth exactly what
those rules are worth — and nothing in the project previously said what that
was. `ptm.lint` catches the mechanical half of the drift: a rule naming a field
that does not exist, a rule citing a clause the policy does not contain. Nothing
caught a rule that parses cleanly, cites a real clause, and is simply **wrong
about what the policy says**.

So the rules are run over the real cases and scored against what the judge said
about those same cases:

```
$ python -m ptm.rules expenses v2
offline rules vs the judge, 600 case(s) under policy v2
  same outcome on 600  -  100.0% (99.4%-100.0%)
  same clause cited on 260 of 260 (100.0%) - this is the number the sweep rests on
  340 case(s) matched no rule and took the default outcome; agreement on those is
  agreement by accident
  measured against verdicts produced by ['offline'], which is these same rules -
  so this figure is 100% by construction and means nothing until PTM_OFFLINE=0
```

Two numbers, and the second is the one that matters. Outcome agreement says the
rules reach the same decision. **Clause** agreement says they reach it for the
same stated reason — which is what the attribution panel, and therefore the
sweep, actually rests on. A rule set that agrees on the answer while citing a
different sentence produces a curve about the wrong clause.

Offline this is inert and says so on its own last line, in the same way and for
the same reason as the stability figure: the verdicts it scores against were
produced by these very rules.

The other direction is the interesting one. `ptm.rules` also builds the prompt
that asks a model to *write* the rules from the policy text — the one job here
where a model reading prose and emitting structure is exactly the right tool —
and every generated rule is put through the lint's own checks before it is
allowed near a replay. A generated rule reading a field that does not exist
never matches, and never matching is silent.

### And what should it *say*?

The sweep narrows the question to a number. Attribution narrows it to a
sentence. Nobody has yet done the actual work, which is to open the markdown and
write the sentence differently — so `propose_<domain>` does:

```
$ python -m ptm.proposal expenses v2
drafted: Move the amount_gbp threshold in clause 5.1 from 1000 to 2000.
  clause 5.1
    was: Any claim over **GBP 1000** (raised from GBP 500) requires prior director
         approval. Without it the claim is **denied**.
    now: Any claim over **GBP 2000** (raised from GBP 500) requires prior director
         approval. Without it the claim is **denied**.
    why: At 2000 the policy reverses 1 human ruling(s) against 2 today, the fewest
         of the 31 settings searched.
  expected: Precedent reversals 2 -> 1; policy-driven changes 118.
  risks: ... only the number is moved, so a parenthetical explaining the old value,
         or a neighbouring clause that assumes it, is left stale for a person to fix.
  gate: 1 reversal(s) against 8 under v1 - fixed 7, introduced 0
```

The model is handed the policy and everything measured about it — the
attribution table, the sweep curves, the human rulings it reverses, with the
reviewers' notes — and returns a typed `PolicyPatch`: clause-level edits, each
tied to the evidence that motivated it. The draft is written to
`include/drafts/<domain>/` where `ptm.config.merge_drafts` picks it up as an
ordinary policy version, so from the next parse it can be replayed, swept,
compared and gated exactly like a version a person wrote.

**Why letting a model write here is not the usual bad idea.** It is not asked to
judge anything, and nothing it produces is believed. The output is a proposal
against an oracle that already existed and that it does not get to influence:
the precedent gate re-judges every human ruling under the draft and fails on a
reversal. A patch that argues beautifully and breaks precedent fails exactly as
loudly as one that argues badly. That is the whole safety story, and it is why
the DAG ends in the gate rather than in a summary.

Two smaller decisions carry most of the rest of it:

- **A draft is never indistinguishable from a policy.** Drafts live in their own
  directory, are listed in `draft_versions` separately from `policies`, are
  labelled `(draft)` in the dashboard's version picker, carry a banner when
  selected, say *"Not approved by anyone"* in their own first line, and are
  called out by the lint. `ptm.proposal.discard` deletes one; they are as cheap
  to drop as to make.
- **A draft carries its own offline rules.** Published without them, an offline
  replay of the draft would match nothing and return the most generous outcome
  for every case — a wildly permissive policy nobody wrote, reported as the
  draft's effect. Offline they are derived from the edits; with a model, a
  second structured call writes them and they are validated before they land.

Offline there is still a proposer, and it is deliberately dumb: it searches the
dials the sweep exposes for the setting that reverses the fewest human rulings,
ties broken by the smallest change to the decision base. It is optimising
exactly what the gate measures, which is precisely why its output is put through
the gate like everything else — and it is a fair floor. If a model cannot beat
a threshold search, that is worth knowing before paying for one.

### The second measurement is nearly free

The loop this project is built around is *edit a clause, measure again*. As
originally shipped the second measurement cost exactly what the first did: a
threshold moved from 75 to 100 re-judged all six hundred cases, including the
hundreds the edit cannot possibly reach. At USD 4 that is survivable once and a
habit nobody forms.

```
re-running the same replay: 600 of 600 verdicts come from cache, 0 would be
judged again
  against anthropic:claude-sonnet-5 that is USD 2.15 of the USD 2.15 not spent twice.
  the key is the prompt, so editing one clause invalidates exactly the cases whose
  prompt changed - and nothing else.
```

**The key is the prompt, not the case.** `sha256(model + prompt)`. Keying on
`(case_id, policy_version)` is the obvious choice and is quietly wrong: editing
a clause does not change the version label, so every stale verdict would be
served as though the policy had not moved. Hashing the prompt puts the policy
text, the case payload, the rendering template, the outcome list and the judge
instructions all in the key — change any of them and the entry misses, which is
the behaviour you want from a cache standing between you and a number you are
going to act on. The model is in the key too: the same question put to a cheaper
model is a different question.

The baseline pass is keyed separately from the candidate, which is where most of
the saving comes from in practice — editing the candidate policy does not change
the one in force, so half the judging is served from cache on every iteration.

**One fan-out deliberately opts out.** `judge_stability` carries no cache keys
at all. Judging the same prompt repeatedly *is* the measurement; a cache would
serve the first answer to every repeat and report a judge that never contradicts
itself. That would not make the error bar wrong, it would make it *reassuring* —
[a test asserts the opt-out](tests/test_dags.py), and it is the only one in the
file.

## Run it

```bash
make up        # Airflow 3.1 at localhost:8080 (admin/admin), history auto-seeded
make demo      # backfill 24 months of replay
make confirm   # re-judge the biggest flips to check each one reproduces
make stability # measure the judge's noise floor
make draft     # have the proposal DAG write the next version of the policy
```

Then open **http://localhost:8080/ptm/** for the Diff Explorer, and the
`adjudicate_expenses` DAG to answer the human-in-the-loop tasks.

No Airflow, no API key, whole loop in about a second:

```bash
make dev       # create .venv with pydantic, pyyaml, pytest
make test      # lint + 414 engine tests + the whole loop end to end
make preflight # read the policies for problems before paying to replay them
make cost      # forecast a full LLM-backed replay
make sweep     # what should the threshold be?
make rules     # do the offline rules agree with the judge they stand in for?
make calibrate # is the judge right, scored against the humans who ruled?
make propose   # draft the next version of the policy (writes nothing)
```

### Offline vs the real judge

`PTM_OFFLINE=1` (the default) swaps the `LLMOperator` for a deterministic
rule evaluator declared in the domain YAML. Everything else — the mapping,
the diff, the attribution, the HITL gate, the assets, the plugin — is
identical. It exists so the demo survives conference wifi and so CI needs no
key.

For the real thing, set `PTM_OFFLINE=0` and add a pydantic-ai connection:

```bash
AIRFLOW_CONN_PYDANTICAI_DEFAULT='{"conn_type":"pydanticai","host":"anthropic:claude-sonnet-5","password":"sk-ant-..."}'
```

`make cost` will tell you what that costs first. For the shipped 600-case
fixture it is about **USD 2** for a full replay, or **USD 4** with the baseline
pass that makes attribution possible.

---

## Adding a domain

Nothing in `dags/` or `ptm/` knows what an expense is. To run this on
insurance claims, moderation decisions, loan applications or admissions:

1. Write the policy versions as markdown in `include/policies/<domain>/`, with
   numbered clauses (`1.1`, `2.1`, …) — that numbering is what attribution
   reports against.
2. Write `include/domains/<domain>.yaml` — outcomes ordered most-generous to
   most-strict, a case template, which version is `in_force`, the
   `segment_fields` you care about, whether an uneven landing should `warn` or
   `fail` (`disparity`), and where the policies live.
3. Load cases into the `cases` table, and any slowly-changing attributes into
   `subject_facts` with a `known_from` date.
4. Run `python -m ptm.lint` before you trust the result. It now reads the
   policies as well as the fixtures, so one command covers both halves of the
   drift.

Five DAGs appear on the next parse. That is the demo's closing move: swap
the config, run the same pipeline on a completely different domain.

### Lint your domain first

`offline_rules` are maintained by hand next to, but separate from, the markdown
policy — so they can drift. Two ways that drift is silent:

```
$ python -m ptm.lint
ERROR broken/offline_rules/v1[0]: 'when' reads unknown field(s) ['employee_grade'];
      the rule would never match and the replay would be silently wrong
ERROR broken/offline_rules/v1[0]: cites clause '9.9', which policy v1 does not contain
```

The first is the dangerous one. `offline_verdict` swallows exceptions by design
— a rule naming a field that does not exist does not crash, it simply never
matches, and the replay comes out wrong while looking completely healthy.

## Layout

```
dags/policy_time_machine.py     the five-DAG factory
plugins/                        FastAPI plugin + Diff Explorer dashboard
ptm/config.py                   domain YAML loading, and drafts merged in from disk
ptm/store.py                    SQLite, incl. the point-in-time case query
ptm/judge.py                    prompt construction + offline stand-in judge
ptm/diff.py                     flips, attribution, segments, precedent checks
ptm/report.py                   the read models behind the plugin's API
ptm/stability.py                how often the judge contradicts itself
ptm/calibration.py              whether it is right, scored against the humans
ptm/stats.py                    the intervals, in one place so each band means one thing
ptm/disparity.py                who carries more of the change than the rest of their field
ptm/preflight.py                what is wrong with the policy before it is replayed
ptm/sweep.py                    what the threshold should be, not just which clause
ptm/rules.py                    are the offline rules the policy they stand in for?
ptm/proposal.py                 drafts the next version, then makes the gate check it
ptm/cache.py                    do not pay twice for a prompt already answered
ptm/cost.py                     what a replay costs, and will cost
ptm/lint.py                     domain YAML vs the policies it claims to implement
ptm/seed.py                     synthetic 2-year decision history
ptm/selftest.py                 whole loop, no Airflow
tests/                          501 tests; the engine's 414 need nothing but Python
include/domains/*.yaml          the only domain knowledge in the project
include/drafts/<domain>/        policy versions a model wrote, never mixed in with
                                the ones a person did
```

The plugin is deliberately nothing but routing — every read model lives in
`ptm/report.py`, so the whole API surface is covered by the test suite without
FastAPI installed. Beyond the panels it also serves the numbers *out*:
`/ptm/api/export/<domain>/<version>.csv` for the flip set and `.json` for the
whole bundle. A policy decision gets argued about away from the dashboard, so
the figures have to be able to leave it — and the JSON carries its caveats
along with them, rather than having them stripped off by whoever pastes the
headline into a slide.

## Verified against

Built and run against `apache/airflow:3.1.0` with
`apache-airflow-providers-common-ai==0.8.0` and
`apache-airflow-providers-standard`. Checked on every push, and in-container:

- all ten DAGs parse with **zero import errors**, in both offline and
  LLM-backed configurations — [CI checks both](.github/workflows/ci.yml), and
  fails if it cannot (`PTM_REQUIRE_AIRFLOW`), because a DAG job that quietly
  skips its own tests is worse than no DAG job. The structure each DAG claims
  is asserted against the parsed `DagBag`, not against the source: the cache
  sits in front of both fan-outs that repeat work, the proposer is manual-only
  and ends in the gate, and the stability fan-out carries no cache key;
- the plugin registers (`airflow plugins` lists its FastAPI app and external
  view) and serves at `/ptm/`. Its routes are driven through `TestClient`, and
  the list of URLs to check is extracted from `dashboard.html` rather than
  restated, so a renderer calling a new endpoint is covered without anyone
  remembering to add it;
- the Diff Explorer is loaded in Chromium against the real API and fails on any
  console error or any panel that renders nothing;
- `airflow dags test replay_expenses` completes and persists verdicts, flips
  and a run summary.

## Caveats

- Single-container Airflow on SQLite. Fine for a demo, not a topology.
- Manual runs have no meaningful data interval, so they replay all of history
  capped by the `max_cases` param (default 250). When that cap bites they keep
  the **most recent** cases: slowly-changing facts have not changed yet at the
  start of the period, so a cap taken from the front of history misses exactly
  the interactions this engine exists to get right. Scheduled and backfilled
  runs use their own interval and ignore the cap.
- The baseline pass (`baseline_version`, on by default) is what makes clause
  attribution and deviation detection possible, and it **doubles** the judging.
  Blank it to diff against recorded history alone.
- Cost figures are named `estimated_*` because they are measured from the
  prompts this project builds, at a fixed 4 characters per token — not read
  back from the vendor's usage reporting. Right precision for "can I afford
  this backfill", wrong precision for reconciling an invoice.
- Judge stability measures the vendor's default sampling behaviour. Offline it
  is identically zero and tells you so.
- Precedent conflict detection needs a `conflicts.key` in the domain YAML, and
  reads the case record rather than point-in-time facts — two rulings are
  compared as filed. It warns rather than failing: the fix is a conversation
  between two humans, not a code change. Because of that, naming the
  `pit_field` in `conflicts.key` is a lint **error**: the fact is merged in per
  case and is not on the stored record, so it would read as `unknown` for every
  precedent and quietly coarsen every signature.
- The threshold sweep is computed from `offline_rules`, so it answers what the
  rule evaluator would do. Treat it as a free way to narrow a range, then
  confirm the shortlist with a real replay.
- Flip confirmation shares the judge's own sampling behaviour, so offline it
  confirms everything by construction — the same caveat as the stability
  figure, and for the same reason.
- The Common AI provider also offers built-in approval on `LLMOperator`
  (`require_approval`). This project uses a separate `HITLOperator` instead,
  because the reviewer picks the *correct outcome* from the domain's options
  rather than approving a verdict, and that answer becomes precedent.
- FastAPI plugin routes are read-only and **not** behind Airflow auth — Airflow
  does not protect plugin endpoints automatically.
- Airflow 3.1's `react_apps` plugin slot is marked experimental, so the
  dashboard is served as a dependency-free page from the FastAPI app instead.
- `offline_rules` are evaluated with `eval` under an empty builtins scope.
  They are a local demo fixture; only point them at YAML you wrote.
- **Judge accuracy is measured on the precedent set**, which is by construction
  the contested flips. It is a floor on the judge's accuracy over all cases, not
  an estimate of it, and on eight rulings its confidence band is very wide.
- **A segment carrying more of the change than the rest of its field is a
  question, not a finding of unfairness.** Segments differ in what they contain.
  The check never claims otherwise, and `min_cases` means small groups are not
  compared at all rather than compared badly.
- The flip rate's `lo`/`hi` band is **sampling error only** — how much the rate
  could move on a different sample of the same size. It is a different quantity
  from the judge's noise floor and from judge accuracy, and the three do not
  add. `ptm/stats.py` keeps them apart deliberately.
- The preflight is **structural**: it reads the policy's shape, not its meaning.
  Contradiction and ambiguity need the model pass, which is one call and is off
  by default offline.
- **The verdict cache is keyed on the prompt**, so anything that changes the
  prompt misses — including a change to the case template or the judge
  instructions. What it cannot detect is a *vendor-side* change behind an
  unchanged model identifier. `PTM_CACHE=0` stops reads without stopping writes,
  and `judge_stability` never reads it at all.
- **A drafted amendment is a proposal, not a policy.** It is checked against the
  precedent set — a handful of contested cases — which tells you it reverses no
  human ruling, not that it is a good rule. Finding out what it does to the
  other 592 cases still costs a replay, and the draft says so.
- The offline proposer optimises the gate directly, which is exactly what a
  proposer should not be trusted to do on its own. It moves numbers and nothing
  else: a parenthetical explaining the old value, or a neighbouring clause that
  assumes it, is left stale for a person to fix.
- `include/drafts/` is written by a DAG task, so a multi-worker deployment needs
  it on shared storage. The single-container demo and the compose file already
  mount it.
