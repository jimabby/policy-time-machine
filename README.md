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
| **Dynamic DAG generation** | Drop a YAML in `include/domains/` and five new DAGs appear. The DAG code contains zero domain knowledge — [a test asserts it](tests/test_dags.py). `ptm_retention` is the one DAG built outside that loop, because the tables it prunes are shared by every domain. |
| **Structured generation** | The same `LLMOperator`, pointed the other way: `output_type=PolicyPatch` has a model *write* the next version of the policy, which the precedent gate then re-judges. Typed output is what makes that checkable rather than a wall of prose. |
| **`model_id` per run** | The second judge. One connection, the model overridden at trigger time, so cross-checking a replay against a different vendor is a `--conf` flag rather than a DAG edit. |

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

Five DAGs per domain, generated from `include/domains/*.yaml`, plus one
`ptm_retention` for the database they share:

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
                    │ target=stale        │  ← or re-asks rulings whose clause
                    │                     │    has since been rewritten
                    └──────────┬──────────┘
                               │ Asset: ptm://<domain>/precedents
                    ┌──────────▼──────────┐
                    │ precedent_gate_<d>  │  re-judges every precedent
                    │ FAILS on reversal   │  ← the regression suite
                    │ warns on conflicts  │  ← rulings that contradict each other
                    │ scores the judge    │  ← accuracy, against the humans
                    └─────────────────────┘

   manual  ────────▶ judge_stability_<domain>   the error bar on all of the above
                                               (+ a second judge, on request)

   manual  ────────▶ ┌─────────────────────┐
                     │  propose_<domain>   │  reads every number above
                     │  → drafts a patch   │  LLMOperator, output_type=PolicyPatch
                     │  → writes a version │  include/drafts/, judged like any other
                     │  → re-runs the gate │  ← and this is why it is allowed to
                     │  → replay=true      │  ← and what it does to every case
                     │                     │    nobody has ruled on (a full bill)
                     └─────────────────────┘

                        a person adopts it, or does not:
                        python -m ptm.proposal <domain> --list       (make drafts)
                        python -m ptm.proposal <domain> --adopt <v> --by <name>
                        python -m ptm.proposal <domain> --discard <v>
                        make adopt V=<v> BY="<name>"  |  make discard V=<v>

   @weekly ────────▶ ptm_retention               one for the whole database,
                                                 not one per domain
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

**`adjudicate_<domain>`** — the human loop. Each HITL task tells the reviewer
which clause drove the change, so they can argue with the rule rather than only
the result, and **asks them why**. The note is the only free text in the system
written by the person accountable for the decision: it is shown beside any
ruling that contradicts this one, and handed to the drafter that writes the next
version of the policy.

What is recorded is the *circumstances* as well as the answer — which candidate
policy the reviewer was shown, and the verdict they were overturning. A ruling
that does not say what it was a ruling about cannot be re-read later, which
matters because the gate below enforces it forever.

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

It also loads its cases **by id**, never by loading history and filtering: a
regression suite that silently checks less than it reports is worse than none,
and a precedent whose case cannot be loaded fails the run rather than being
skipped.

And it asks one question about its own oracle. Precedent is permanent — that is
the point and also the risk. A reviewer ruled on one case, under one candidate
policy, against one clause of it; if that clause now reads differently, the
ruling is still a fact about the case but no longer a fact about *this* policy's
treatment of it:

```
2 ruling(s) may no longer be about the policy they were made about, and are
still enforced as though they were:
  exp-0478 (finance.lead, 2026-03-04): ruled against clause 1.1 of policy v1;
    that clause reads differently in v2
  exp-0119 (finance.lead, 2026-02-11): recorded before the ruling's circumstances
    were captured, so there is no way to tell what policy text it was about
```

Deliberately a warning and not a gate. Nothing here says the ruling is wrong —
the reviewer may well say the same thing about the rewritten clause. It says the
ruling has not been re-confirmed since the text it was about changed, and
whether it still holds is a person's call rather than a run's. When a reversal
rests on one of these, the gate says so: re-adjudicating it is a different fix
from editing the policy.

So there is a way to re-adjudicate one. `adjudicate_<domain>` with
`target=stale` queues exactly those rulings, and asks a different question from
the flip queue: not *what is the correct outcome* but *does your predecessor's
answer survive the rewrite?* The reviewer is shown that answer, the reason they
gave for it, and the clause as it read then against how it reads now — because
nobody can settle that without all three.

```
1 ruling(s) to re-confirm against policy v2, 1 of which v2 now contradicts:
 ! exp-0054: finance.lead ruled 'deny' on 2025-03-04, v2 gives 'approve'  [clause_changed]
  a reviewer confirming one of these makes it a ruling about the policy as it
  stands; the earlier ruling is kept, not overwritten.
```

Confirming the earlier answer is a real result — it turns a ruling the gate was
enforcing on trust into one that has been checked. And because re-adjudication
is the only operation in this system that overwrites a precedent, it is also the
only one that could lose one: `store.save_precedent` archives the ruling it
replaces to `precedent_history` before writing, so who said what, when, and why
survives being disagreed with.

**`judge_stability_<domain>`** — the error bar, in two modes, plus an optional
second judge (`compare_model`; see *Ask a second judge* below).

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

Both modes still only ever compare the judge to *itself*. Setting
`compare_model` on the same run adds the check that does not:
`--conf '{"compare_model":"anthropic:claude-haiku-4-5"}'` asks a second model
the same questions through the same connection, and reports the cases they split
on. That is the section after next.

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

### Ask a second judge

Calibration is the strongest check here and it can only speak about the handful
of cases a human has ruled on — by construction the contested ones. For the
other five hundred and ninety there is no ground truth at all, and the two
checks that do cover them both compare the judge *to itself*.

A second model is not ground truth either. What it is, is **independent**: two
judges misreading the same clause in the same direction is a far smaller
coincidence than one judge doing it twice. So run the same prompts past a
different model and look at where they split:

```
$ airflow dags trigger judge_stability_expenses \
    --conf '{"compare_model":"anthropic:claude-haiku-4-5","sample_cases":40}'

anthropic:claude-sonnet-5 vs anthropic:claude-haiku-4-5 on 40 case(s) under the same policy
  same outcome on 36  -  90.0% (76.9%-96.0%)
  same clause cited on 31 of 38 (81.6%)
  4 case(s) the two judges split on. These are cases the policy does not settle -
  the disagreement is the finding, and neither answer is the right one to write down.
    which way: 3 loosening, 1 tightening  (direction is sonnet relative to haiku)
    3 of them are flips only sonnet makes - that much of the headline rests on one judge
```

Three things that are worth more than the headline rate.

**The split cases are the output.** A case two independent judges decide
differently is a case the policy does not settle, and unlike a precedent it
costs no human time to find. That is a list of sentences to go and rewrite,
produced for every case rather than for the eight somebody adjudicated.

**It says how much of the flip rate rests on one judge.** A disagreement where
exactly one of the two departs from the recorded outcome is a flip the second
judge would not have made. *"3 of them are flips only sonnet makes"* is a
sentence nothing else in this pipeline can produce.

**It scores the confidence field against something.** If the primary judge
claims *higher* confidence on the cases an independent judge contradicts than
on the ones it confirms, its confidence is not tracking difficulty — and
`review.below_confidence` is spending the scarcest resource here on it.

What it does **not** do is say which judge was right. Where they disagree it
reports both answers and stops. Calibration is still the only thing here that
scores a judge against an answer, and it needs a human to have given one.

There is no offline version of this check, and that is stated rather than
faked: the offline judge standing in for a second opinion would be one rule set
answering twice.

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
  taken away. A flip rate cannot tell those apart.
- **The group the change *misses* is a finding too.** For a loosening proposal
  that is a benefit distributed unevenly — the same question asked from the
  other side. The two findings are labelled (`kind`), and only the exposure one
  can fail a run: a distribution question is not something to stop a deploy for.

  Labelling them was not cosmetic. Both carry a ratio of `0.0` — it means *"the
  rest of the field does not move"* for one and *"this segment does not move"*
  for the other — and the test for a pass-over used to read `0.0 < ratio`, so
  the strongest pass-over there is, a segment the change skips entirely, was the
  one case that could never be reported.

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

**One curve holds every other dial still, and never says so.** That is the
assumption a reader inherits without noticing: *"at 100 you get 161 flips"*
reads as a property of clause 1.1 when it is a property of clause 1.1 *given
where everything else is sitting*. Policy thresholds are exactly where that
breaks — an exemption and the restriction it carves out of interact by
construction. So move two at once:

```
$ python -m ptm.sweep expenses v2 --joint \
    1.1:amount_gbp=25,50,75,100,150 3.1:days_notice=3,7,14

amount_gbp \ days_notice             3           7          14
(rows \ columns)                 flips       flips       flips
25                                104          93          98
50                                127         116         121
75                                158         147*        150
100                               173         161         164
150                               192         179         182
  (* = the settings in force.)

interaction: moving amount_gbp changes 84-88 decisions depending on where
days_notice sits (4 apart)
  the dials interact: the best setting for one depends on the other, which is
  what a pair of single sweeps cannot show
```

The grid reports its own worth on the last line. If moving the second dial
changed the first one's effect by nothing, the two are independent, two curves
said everything, and the panel says so rather than letting itself be looked at
out of habit. It is `|A| × |B|` full replays and still free — pure rule
evaluation over cases already on file — so the expectation is that you narrow
each axis with a single sweep first. The drafter is handed the same grid for the
two clauses attribution blames most.

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

**The agreement number is also a gate.** `rules.min_outcome_agreement` and
`rules.min_clause_agreement` in the domain YAML say how far the rules may drift
before the sweep built on them stops being served. Two things deliberately do
not fire it: a measurement that is *inert* (offline, where the verdicts being
scored came from these same rules) and one where nothing has been measured at
all. A gate that passes because the check is switched off teaches people to
trust a number that means nothing, and one that fires loudest on a project that
has not run yet gets turned off on day one.

The other direction is the interesting one. `ptm.rules` also builds the prompt
that asks a model to *write* the rules from the policy text — the one job here
where a model reading prose and emitting structure is exactly the right tool —
and every generated rule is put through the lint's own checks before it is
allowed near a replay. A generated rule reading a field that does not exist
never matches, and never matching is silent.

#### A rule is not Python

`offline_rules` used to be YAML a person wrote, and `eval` with a trimmed
`__builtins__` was a defensible shortcut for that. `propose_<domain>` changed
the threat model: a **model** writes the rules for a drafted policy, they are
written to `include/drafts/`, and every later replay of that draft evaluates
them on a worker.

A name-level check in front of `eval` does not contain that. This reads no bare
identifier at all, so a scan built on `ast.Name` nodes reports nothing wrong
with it:

```python
().__class__.__base__.__subclasses__()[-1].__init__.__globals__[...]
```

So [`ptm/safe_eval.py`](ptm/safe_eval.py) does not call `eval`. It walks the
parsed expression and computes the result node by node, and anything it does not
explicitly implement is a refusal rather than a fallthrough — attribute access,
subscripting, lambdas, comprehensions and f-strings among them, which is what
removes the object graph the line above walks. `**` carries a ceiling, because a
worker holding a mapped task slot on `9**9**9` is a hang rather than a wrong
answer.

The same whitelist runs without evaluating, which is how `ptm.lint` and
`ptm.rules.validate` reject a rule at the point it arrives instead of the first
time it silently fails to match. The two jobs are separate on purpose: the
validator is the kind of thing that acquires a gap, and a gap in it used to mean
handing over the interpreter. [The escape is asserted against
directly](tests/test_safe_eval.py).

The language that is left is the one the rules actually use — comparisons,
boolean and arithmetic operators, `in`, a conditional expression, literals, and
calls to a fixed list of helpers.

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

### Who takes out the bins

Two tables here grow without bound, and they grow **because the loop works**.
The cache key is the prompt, so every clause edit strands the entire generation
of entries it invalidated — those rows can never be hit again, by construction.
`judge_samples` holds one row per (case, repeat) for every stability run ever
made, and only the newest backs a reported figure.

`ptm.prune` has always known how to drop them. What it had no way to do was
*happen*: the only lever was a command somebody had to remember to run, which in
a project whose argument is that Airflow is the engine rather than the wrapper
made retention the one chore left outside it. So it is a DAG.

```
ptm_retention        @weekly, one for the whole database
  plan               count what is about to go, with the same WHERE clauses
                     the delete uses — a dry run that counts different rows
                     from the one that deletes them is worse than none
  sweep_up           drop them, unless dry_run says only to count
  compact            VACUUM, so the file actually shrinks
```

One DAG rather than one per domain, and that is the only place in this project
where a thing is *not* generated per domain. The tables are shared, the cutoff
is a property of the database and not of any rulebook, and the file it rewrites
is one file — five copies would take five locks on one SQLite database to do the
same work once.

The last step is the one worth separating. SQLite keeps freed pages on a free
list rather than handing them back, so a prune that removed ten thousand rows
changes the file size by nothing at all. That was said out loud in the output
and then left there — *"run VACUUM, or just re-seed"*, addressed to a reader who
would have to go and do by hand, in another tool, against a path the command
already knew, the one thing it had everything it needed to do. Now `--vacuum`
does it, `make vacuum` is prune-then-compact in one, and the step is separate in
the DAG because it is the only one whose cost is proportional to the database
rather than to what was dropped.

```
$ make vacuum
removed 4,182 row(s) from every domain, older than 90 day(s)
  cross_checks                 3
  judge_samples              600
  verdict_cache            3,579
vacuumed: 12,058,624 -> 3,342,336 bytes (8,716,288 reclaimed)
```

Nothing here touches precedents, their history, the drafts table, or the
aggregates the dashboard reads. Age is not a reason to forget a human ruling,
and a run row is what a trend is drawn from.

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

**And the estimate is scored.** Every cost figure here is measured from prompt
size at four characters per token — the right precision for *"can I afford this
backfill"*, and until recently a constant nobody could be shown to be wrong
about. `LLMOperator` hands `result.usage` to its own logger and returns the
output alone, so the vendor's real counts reached the task log and nothing else.
[`ptm/metered.py`](ptm/metered.py) keeps them, by wrapping the *hook* rather than
re-implementing `execute` — the approval path and the serialisation rules belong
to the provider and are exactly what a copy drifts away from.

Both numbers go in the ledger, because the gap is the point:

```
judged by anthropic:claude-sonnet-5 for an estimated USD 2.1471
measured USD 2.4980 against an estimated USD 2.1471 (+16.3% out); these prompts
ran at 3.44 characters per token, not the 4.0 the estimate assumes
```

The estimate is never corrected from the measurement — replacing it would
destroy the only evidence of how good it is. `implied_chars_per_token` is the
part to act on: it is the ratio that would have made the forecast right *for
your policies*, in your language, with your markdown.

The baseline pass is keyed separately from the candidate, which is where most of
the saving comes from in practice — editing the candidate policy does not change
the one in force, so half the judging is served from cache on every iteration.

**One fan-out deliberately opts out.** `judge_stability` carries no cache keys
at all. Judging the same prompt repeatedly *is* the measurement; a cache would
serve the first answer to every repeat and report a judge that never contradicts
itself. That would not make the error bar wrong, it would make it *reassuring* —
[a test asserts the opt-out](tests/test_dags.py), and it is the only one in the
file.

**And one lever the key cannot provide.** Hashing the prompt covers everything
this project controls; it cannot cover a vendor changing what sits behind an
unchanged model identifier. Nothing can detect that, so rather than pretend,
`PTM_CACHE_EPOCH` is mixed into every key: bump it and no earlier answer is
found again. It leaves the old entries on disk — unlike clearing the table —
so *what did the judge say before the model changed underneath us* stays a
question the database can answer.

### So did the edit help?

Every panel above is about one version. The question the whole loop exists for
compares two, and answering it meant opening two tabs and doing arithmetic:

```
version    cases  outcomes change   caused by it   net GBP   rulings reversed
v1  in force   600    0     0.0%                0         0          8
v2             600  147    24.5% (21.2–28.1%) 109   +12,014          2
v2-draft1      600  118    19.7% (16.6–23.1%)  94    +9,880          1   draft
```

Three numbers, because a version is judged on all three: what it moves, how
much of that it is *responsible* for, and how many human rulings it reverses.
The band is there because two versions measured on different numbers of cases
differ by sample size before they differ by policy, and a version nothing has
replayed says so rather than reporting a confident zero — no reversals and no
evidence look identical in a column of integers.

Reversals are read from verdicts already stored, so the whole table costs
nothing, and the rows are deduplicated the same latest-row-wins way as every
other read model here: a manual run overlapping a backfill must not make one
version look twice as busy as the one beside it.

### And is the judge worth listening to?

Every number above is one of the judge's verdicts. Stability asks whether it
repeats itself and the cross-check asks whether a second model agrees; only
calibration asks whether it is **right**, because only the precedent set has an
answer in it. That figure was computed, printed and rendered from the day it
was written, and nothing anywhere acted on it — exactly the gap the rule
agreement gate was added to close, and a worse one.

```
judge vs 8 human ruling(s) under policy v2
  agreed on 6 of 8  -  75.0% (40.9%-92.8%)
  mean confidence 89%, overconfident by 14% (ECE 24%)
  the review threshold (75%) does NOT separate:
      below it 100% right (1 cases), above it 71% right (7 cases)
  the domain requires 70% accuracy, at most 15% overconfidence and a review
  threshold that separates (gate=warn, from 10 scored rulings)
```

Three settings in `calibration:`, because there are three ways for this to be
bad news and they have different owners. `min_accuracy` is the floor under
every flip in the replay. `max_overconfidence` catches a judge that is right as
often as before but has stopped knowing when it is not, which nothing else here
would ever mention. And `require_threshold_separation` is the one worth
understanding: `review.below_confidence` routes the scarcest resource in this
project — human attention — on the judge's own claim about how sure it is, so
if verdicts above that line are right no more often than the ones below it,
that queue is being picked at random and **so was every precedent established
from it**.

It refuses to fire in three situations, and each refusal is the difference
between a gate and a nuisance. Nothing adjudicated is no evidence, not 0%
accuracy — failing there would make the gate loudest on a project that has not
run yet. Too little adjudicated is the same problem one step on: accuracy on
three contested cases has a band running most of the way from 0 to 1, so
`min_judged` is a floor and falling below it reports as unmeasured rather than
as a pass. And offline the verdicts came from the same `offline_rules` the
sweep uses, so the figure describes the fixture; a gate that passes because the
check is switched off is worse than no gate.

## Run it

```bash
make up        # Airflow 3.1 at localhost:8080 (admin/admin), history auto-seeded
make demo      # backfill 24 months of replay
make confirm   # re-judge the biggest flips to check each one reproduces
make stability # measure the judge's noise floor
make crosscheck# ask a second model the same questions (needs PTM_OFFLINE=0)
make draft     # have the proposal DAG write the next version of the policy
```

Then open **http://localhost:8080/ptm/** for the Diff Explorer, and the
`adjudicate_expenses` DAG to answer the human-in-the-loop tasks.

The Explorer opens with **How to read this page** at the top: what each panel
answers, in the order the panels answer it, and which DAG to trigger when one
of them is empty. It is a `<details>`, so it costs one line once somebody knows
the page.

A **language switcher** in the header renders the whole page in English or
Chinese (中文). Every string lives in one table at the top of
`plugins/dashboard.html`, and anything a translation has no entry for falls
back to English rather than going blank. The read models' own `caveat` and
`hint` strings travel with a `caveat_key` / `hint_key`, so the limit on a
number is rendered in the same language as the number — a page that states a
figure in one language and its caveat in another is how a figure gets quoted
without them. The English text is sent unchanged either way, because the CLIs
print it and the export bundle carries it. Content that is genuinely *data* —
a reviewer's note, an outcome name a domain defines — is shown as written.

**The selection is addressable.** Domain and version go into the query string
(`/ptm/?domain=expenses&version=v2`) as you pick them, so the URL in the
address bar is the link to paste into the ticket. Open it and you get that
replay: a link beats the reader's own last selection, because otherwise two
people open the same URL, see different replays, and neither can tell. With no
query string the last selection is restored instead, and a link naming a domain
or version this deployment does not have falls back to the default rather than
rendering a blank page.

**The routes are not public.** Airflow mounts a plugin's `fastapi_apps` with
`app.mount()`, and a mounted sub-application inherits none of the parent's
dependencies — so Airflow's access control never reaches them and every case
file behind them would be readable by anyone who can reach the port. The plugin
applies its own dependency to the whole app, once, so a route added later
cannot forget it. The token is taken from `Authorization: Bearer` and then from
Airflow's `_token` cookie — the cookie is needed because it is `HttpOnly`, so
the page's own JavaScript cannot read it to build a header — and verification is
Airflow's `resolve_user_from_token`, never anything reimplemented here. Reading
a cookie is only safe because every route is a read: the cookie is
`SameSite=Lax` and the responses are JSON another origin cannot read back.
Logging into Airflow is all a reader has to do. `PTM_ALLOW_ANONYMOUS=1` opens
it for a context with no session to present — the test suite, or a local demo
behind nothing — and is the only thing that does.

No Airflow, no API key, whole loop in about a second:

```bash
make dev       # create .venv with pydantic, pyyaml, pytest, ruff
make test      # style + lint + 689 engine tests + the whole loop end to end
make preflight # read the policies for problems before paying to replay them
make cost      # forecast a full LLM-backed replay
make sweep     # what should the threshold be?
make grid      # two thresholds at once - one curve cannot show them interacting
make rules     # do the offline rules agree with the judge they stand in for?
make calibrate # is the judge right, scored against the humans who ruled?
make propose   # draft the next version of the policy (writes nothing)
make drafts    # what has been drafted, and what the gate made of each
make export    # everything the Explorer shows, as one file
make prune     # count the cache and sample rows that stopped earning their disk
make vacuum    # drop them for real, and shrink the file
make adopt V=v2-draft1 BY="your name"   # promote a draft into the policy set
make discard V=v2-draft1                # or throw it away
```

Every one of those is a `python -m ptm.*` entry point underneath, and every one
of them answers `--help`. That is worth a sentence only because it did not: the
modules parse their own arguments, and `--help` was read as the name of a
*domain*. The best of them answered `Unknown domain: --help`; `ptm.selftest` —
the command whose entire job is to demonstrate that the project runs cleanly —
got as far as trying to seed a domain by that name and exited on an uncaught
`KeyError`. `ptm/cli.py` is one flag set, checked before any argument is
interpreted as a name, so the usage string is reachable at the moment it is
actually wanted: when you have just got the arguments wrong.

`make export` matters more than it looks. Every measurement here can be reached
from a shell with no Airflow and no key — except the one artefact built to
*leave* the room. The bundle assembles every panel with its caveats attached,
precisely so the numbers cannot be pasted into a slide without them, and it
lived only behind a FastAPI route behind an Airflow login. A policy decision
gets argued about away from the dashboard, which is exactly when nobody can
start the dashboard:

```bash
python -m ptm.report expenses v2 -o bundle.json   # every panel, with its caveats
python -m ptm.report expenses v2 --csv            # the flip set, for the spreadsheet
python -m ptm.prune --dry-run                     # count it before removing it
```

`make prune` is the other side of running for a while. `verdict_cache` grows
*because* the loop works: edit a clause, measure again, and since the key is the
prompt, every edit strands the generation of entries it invalidated — rows that
can never be hit again by construction. Until now the only lever was
`cache_clear`, which also destroys the entries about to save the next replay.
Precedents, their history and the aggregates behind the dashboard are never
touched: age is not a reason to forget a human ruling. Under Airflow none of
this needs remembering — `ptm_retention` runs it weekly, and `make retain`
triggers it now; see *Who takes out the bins*.

A draft is a proposal, so the last step is a person's:

```bash
python -m ptm.proposal expenses --list
python -m ptm.proposal expenses --adopt v2-draft1 --by "jim (finance)"
python -m ptm.proposal expenses --discard v2-draft1
```

`--adopt` moves the markdown into `include/policies/`, registers it **and its
offline rules** in the domain YAML without touching a single comment in that
file, and records who adopted it in the document. It refuses to adopt anything
that is not a draft, refuses to write over an existing version, and refuses to
do any of it anonymously. Adopting does not make the policy *in force* — that
is `in_force` in the YAML, one more deliberate edit, because a version existing
and a version governing are different claims.

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
ptm/safe_eval.py                computes a rule by walking it, never by eval()
ptm/diff.py                     flips, attribution, segments, precedent checks
ptm/report.py                   the read models behind the plugin's API
ptm/stability.py                how often the judge contradicts itself
ptm/calibration.py              whether it is right, scored against the humans
ptm/crosscheck.py               whether a second, independent judge agrees
ptm/stats.py                    the intervals, in one place so each band means one thing
ptm/disparity.py                who carries more of the change than the rest of their field
ptm/preflight.py                what is wrong with the policy before it is replayed
ptm/sweep.py                    what the threshold should be, not just which clause
ptm/rules.py                    are the offline rules the policy they stand in for?
ptm/proposal.py                 drafts the next version, then makes the gate check it
ptm/cache.py                    do not pay twice for a prompt already answered
ptm/cost.py                     what a replay costs, and did cost
ptm/metered.py                  keeps the token counts LLMOperator only logs
ptm/lint.py                     domain YAML vs the policies it claims to implement
ptm/prune.py                    drops the rows that stopped earning their disk
ptm/cli.py                      one definition of --help, for all nine entry points
ptm/seed.py                     synthetic 2-year decision history
ptm/selftest.py                 whole loop, no Airflow
ruff.toml                       the style gate, and why each rule is on
tests/                          831 tests; the engine's 689 need nothing but Python
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
headline into a slide. The same two are reachable from a shell as
`python -m ptm.report <domain> <version> [--csv]`, because the moment a decision
is being argued about away from the dashboard is exactly the moment nobody can
start the dashboard.

## Verified against

Built and run against `apache/airflow:3.1.0` with
`apache-airflow-providers-common-ai==0.8.0` and
`apache-airflow-providers-standard`. Checked on every push, and in-container:

- all eleven DAGs parse with **zero import errors**, in both offline and
  LLM-backed configurations — [CI checks both](.github/workflows/ci.yml), and
  fails if it cannot (`PTM_REQUIRE_AIRFLOW`), because a DAG job that quietly
  skips its own tests is worse than no DAG job. The structure each DAG claims
  is asserted against the parsed `DagBag`, not against the source: the cache
  sits in front of both fan-outs that repeat work, the proposer is manual-only
  and ends in the gate, the stability fan-out carries no cache key, and
  retention counts before it deletes and compacts after;
- the plugin registers (`airflow plugins` lists its FastAPI app and external
  view) and serves at `/ptm/`. Its routes are driven through `TestClient`, and
  the list of URLs to check is extracted from `dashboard.html` rather than
  restated, so a renderer calling a new endpoint is covered without anyone
  remembering to add it;
- the Diff Explorer is loaded in Chromium against the real API and fails on any
  console error or any panel that renders nothing;
- the export bundle is written to a file and parsed back, because nothing else
  serialises every read model at once — a traceback there is a panel that would
  have been blank in front of an audience;
- `ruff check` passes under [`ruff.toml`](ruff.toml), which is a deliberately
  narrow selection: rules that would have caught a real bug here, not rules
  about how a docstring is worded. It exists because the code carried `# noqa`
  directives and nothing in CI could honour them, so three of them had gone
  stale and no build said so;
- `airflow dags test replay_expenses` completes and persists verdicts, flips
  and a run summary.

## Caveats

- Single-container Airflow on SQLite. Fine for a demo, not a topology.
- **The DAG tests do not run on Windows, and say so rather than failing.**
  Airflow supports POSIX and warns about it on import; `DagBag` bounds a DAG
  file's import time by arming `signal.SIGALRM`, which Windows does not have, so
  collection raises before a single DAG is built and every structural assertion
  fails with `'replay_<domain>' not in {}` — seventeen failures that look like a
  broken DAG module and are nothing of the kind. They now skip with that reason
  attached. The skip is *not* allowed to hide anything in CI: `PTM_REQUIRE_AIRFLOW`
  turns it back into a hard error, and CI runs on Linux where the alarm exists,
  so a skip there means something has genuinely changed. The engine's 689 tests,
  the lint, the style gate and the whole end-to-end loop need none of this and
  run on a Windows checkout unchanged — which is what `make dev && make test` is
  for, and why the Makefile picks the interpreter per platform.
- `verdict_cache` and `judge_samples` grow without bound, and they grow
  *because* the loop works: the key is the prompt, so every clause edit strands
  the generation of entries it invalidated. `ptm_retention` drops what can no
  longer be hit, weekly and on its own; `make prune` counts what would go and
  `make vacuum` does it now. Precedents, their history, the drafts table and the
  aggregates behind the dashboard are never touched. SQLite does not hand freed
  pages back to the filesystem, so the file does not shrink until it is
  rewritten — which is what `--vacuum` and the DAG's third step are for.
- Manual runs have no meaningful data interval, so they replay all of history
  capped by the `max_cases` param (default 250). When that cap bites they keep
  the **most recent** cases: slowly-changing facts have not changed yet at the
  start of the period, so a cap taken from the front of history misses exactly
  the interactions this engine exists to get right. Scheduled and backfilled
  runs use their own interval and ignore the cap.
- The baseline pass (`baseline_version`, on by default) is what makes clause
  attribution and deviation detection possible, and it **doubles** the judging.
  Blank it to diff against recorded history alone.
- Cost figures named `estimated_*` are measured from the prompts this project
  builds, at a fixed 4 characters per token. `actual_*` are the vendor's own
  counts and exist only for runs a real judge answered; `reconciliation` scores
  one against the other. Neither is an invoice: the estimate is a forecast and
  the measurement is a usage report, and a bill has rounding, minimums and
  discounts in it that this knows nothing about.
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
- **A dial that states a band cannot be swept, and says so rather than lying.**
  The rewrite moves *every* number a field is compared against, which is right
  for the one-sided threshold a clause normally states and destructive for
  `40 < amount <= 100` — both ends land on the same value and the rule then
  matches nothing at any point of the curve. Nothing raised: the rewrite
  reported two hits and drew a confident curve for a rule that had stopped
  existing. That is the worst failure this module can have, so the sweep and the
  grid now refuse such a dial by name, `ptm.lint` warns about it before anybody
  asks, and the Explorer lists it as un-sweepable instead of dropping it — a
  field that vanished from the menu reads as a policy with no such threshold,
  which is a different and equally wrong thing to believe. Split the band across
  two rules to move either end.
- Flip confirmation shares the judge's own sampling behaviour, so offline it
  confirms everything by construction — the same caveat as the stability
  figure, and for the same reason.
- The Common AI provider also offers built-in approval on `LLMOperator`
  (`require_approval`). This project uses a separate `HITLOperator` instead,
  because the reviewer picks the *correct outcome* from the domain's options
  rather than approving a verdict, and that answer becomes precedent.
- FastAPI plugin routes are read-only, and **Airflow does not protect plugin
  endpoints automatically** — a mounted sub-application inherits none of the
  parent's dependencies. The plugin therefore applies its own, once, to the
  whole app, and verifies Airflow's own token; logging into the UI is all a
  reader needs. `PTM_ALLOW_ANONYMOUS=1` is the only thing that opens them, and
  it is for a context with no session to present. **The routes are not public**,
  under the Diff Explorer above, has the rest: why the `_token` cookie has to be
  read, and why that is only safe while every route is a read.
- Airflow 3.1's `react_apps` plugin slot is marked experimental, so the
  dashboard is served as a dependency-free page from the FastAPI app instead.
- `offline_rules` are computed by `ptm/safe_eval.py`, which walks the parsed
  expression rather than calling `eval`, and refuses everything outside
  comparisons, boolean and arithmetic operators, literals and a fixed helper
  list. That is what makes it safe to let `propose_<domain>` have a model write
  rules that later replays evaluate. It bounds cost as well as reach, because a
  whitelist that cannot be escaped can still be made to run forever on a worker
  holding a mapped task slot: `MAX_EXPONENT` caps one `**`, chained
  exponentiation is refused outright — every exponent in `((b**64)**64)**64` is
  a legal 64 while the base grows — and `MAX_RESULT_SIZE` caps what an
  expression may build, which is the bound that also covers `'x' * 10**9`.
- **A second judge is independent, not correct.** Where two judges split, the
  cross-check reports both answers and stops; it is evidence the *policy* does
  not settle that case, never evidence about which model was right. It also has
  no offline form — the offline judge standing in for a second opinion would be
  one rule set answering twice — so unlike every other check here it simply does
  not run without `PTM_OFFLINE=0`.
- **A ruling is not re-confirmed when the clause it was about changes.** The
  staleness check says which rulings predate the text now in front of them; it
  does not decide whether they still hold, and the gate goes on enforcing every
  one of them. Rulings recorded before the circumstances were captured are
  reported as *unknown* rather than assumed fresh. Deciding is a person's job
  and now has a route to a person: `adjudicate_<domain>` with `target=stale`
  puts those rulings back in front of a reviewer, showing them the earlier
  answer, the reason given for it, and both versions of the sentence it was
  about. Re-adjudication is the only thing that overwrites a precedent, so the
  ruling it replaces is archived rather than lost — `precedent_history` keeps
  it, the precedents panel says a ruling has been revised, and confirming the
  earlier answer is a real result, not a no-op.
- **The rule-agreement gate is inert offline** and deliberately does not fire
  when the measurement is inert or absent. A gate that passes because the check
  is switched off is worse than no gate; one that fires on a project that has
  not run yet gets turned off on day one.
- **A threshold grid is still fixture arithmetic.** It shows that two dials
  interact under the *rules*, which is the same caveat the single sweep carries,
  and it is capped at 64 points because `|A| × |B|` full replays stops being
  cheap somewhere.
- **Judge accuracy is measured on the precedent set**, which is by construction
  the contested flips. It is a floor on the judge's accuracy over all cases, not
  an estimate of it, and on eight rulings its confidence band is very wide.
  That width is why the gate on it has a `min_judged` floor and reports below it
  as *unmeasured* rather than as a pass: a gate that fails a run on three
  contested cases is failing it for the sample size. Like the rule-agreement
  gate it is inert offline and does not fire, both shipped domains set it to
  `warn`, and every threshold defaults to off — a project that has never
  measured this must not start failing runs the first time it does.
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
  unchanged model identifier — so that gets a lever rather than a pretence:
  `PTM_CACHE_EPOCH` is mixed into every key, and bumping it stops every earlier
  answer being served while leaving the entries on disk to be read. `PTM_CACHE=0`
  stops reads without stopping writes, and `judge_stability` never reads it at all.
- **A drafted amendment is a proposal, not a policy.** It is checked against the
  precedent set — a handful of contested cases — which tells you it reverses no
  human ruling, not that it is a good rule. Finding out what it does to the
  other 592 cases still costs a replay; `propose_<domain>` will trigger that
  replay for you with `replay=true`, off by default because it is a full bill.
  Adopting it is separate again, and deliberately a person's act.
- The offline proposer optimises the gate directly, which is exactly what a
  proposer should not be trusted to do on its own. It moves numbers and nothing
  else: a parenthetical explaining the old value, or a neighbouring clause that
  assumes it, is left stale for a person to fix.
- `include/drafts/` is written by a DAG task, so a multi-worker deployment needs
  it on shared storage. The single-container demo and the compose file already
  mount it.

## License

[Apache-2.0](LICENSE) — the same license as Airflow itself, which is the
ecosystem this is built for.
