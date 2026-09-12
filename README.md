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

brief [do_not_ship]: Policy v2 moves 147 of 600 decisions and costs GBP 12,014.
  theme: 'deny' now 'approve', unruled    76 cases  (loosening)
  theme: 'partial' now 'approve', unruled 36 cases  (loosening)
  theme: Clause 2.1                       17 cases  (loosening)

8 flips routed to a human out of 147
8 precedents established

gate: policy v2 vs 8 precedents -> 3 violation(s)
  exp-0478: finance.lead ruled 'approve', v2 gives 'deny'
GATE FAILS - policy would reverse a human ruling.
amendment drafted: 3 edit(s) proposed

verifying candidate v2+fix:
  v2+fix clears every precedent, and of the 144 other decisions v2 had
  settled it moves none - the narrowing is surgical.
  GATE NOW PASSES.
```

That last block is the point. The first time you run it you get an
*estimate*. Every time after, you get a **regression suite for
organisational judgment**.

---

## Why this is an Airflow project

Not "an LLM in a DAG". Every capability here is load-bearing.

| Airflow capability | What it does here |
|---|---|
| **Backfill** | Is the simulation engine. One `backfill create` fans out 24 monthly runs that replay two years of real decisions. |
| **Data intervals** | Make the replay *honest*. Each run only sees cases inside its own window, and each case is hydrated with facts known on its decision date. **Skip this and 41 of 600 cases come out wrong** — see below. |
| **Dynamic task mapping** | One judge task per case, with concurrency capped so you don't melt the model endpoint. |
| **Common AI provider** | `LLMOperator` with `output_type=Verdict`, so every verdict is typed, not parsed out of prose. `usage_limits` caps spend per task. The vendor lives in a connection — switching models never touches DAG code. |
| **Typed AI analysis** | Three more `output_type` models turn the result into an answer: `PolicyBrief`, `FlipThemes` and `Amendment`. See below. |
| **HITL operators** | `HITLOperator` deferred in the triggerer, holding no worker slot, asking a human for the *correct outcome* — not a yes/no. |
| **Assets** | `ptm://<domain>/flips` wakes adjudication, `ptm://<domain>/precedents` wakes the regression gate, `ptm://<domain>/amendments` wakes the gate that tests the fix. Nothing is polled. |
| **Plugin (FastAPI + external view)** | The Policy Diff Explorer, a tab inside the Airflow UI. |
| **Dynamic DAG generation** | Drop a YAML in `include/domains/` and three new DAGs appear. The DAG code contains zero domain knowledge. |

### The point-in-time trap

The fixture promotes half the employees to grade 3 partway through the
period. Proposed policy v2 exempts grade 3+ from receipts. Join *today's*
grade onto historical cases — which is what every hand-rolled backtest does —
and you wrongly approve claims from before those promotions:

```
$ make pit
point-in-time replay : 147 flips
naive replay         : wrong on 39 / 600 cases
```

Every one of those 39 errors flatters the proposal — a test asserts that, and
it is not a coincidence: each is a later promotion applied to an earlier claim,
so each can only loosen. Airflow's data-interval semantics are what stop it.

---

## Architecture

Four DAGs per domain, generated from `include/domains/*.yaml`. Nothing polls:
each stage is woken by the asset the stage before it emits.

```
                    ┌─────────────────────┐
   backfill ───────▶│  replay_<domain>    │  @monthly × 24 runs
                    │  point-in-time load │
                    │  → map judge/case   │  LLMOperator, output_type=Verdict
                    │  → diff vs history  │
                    │  → brief + themes   │  ← typed AI analysis
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
                    │ drafts an amendment │
                    │ FAILS on reversal   │  ← the regression suite
                    └──────────┬──────────┘
                               │ Asset: ptm://<domain>/amendments
                    ┌──────────▼──────────┐
                    │  amend_<domain>     │  judges the drafted fix
                    │  clears the gate?   │  ← an untested fix is an opinion
                    │  what did it break? │
                    └─────────────────────┘
```

The fourth is the one that makes the third honest. A gate that says "here is a
smaller edit that would work" and never checks is giving advice. `amend_` turns
the drafted prose into a real, judgeable policy version, re-runs every
precedent against it, and measures what the narrowing disturbed among the cases
the original had already settled.

## What the AI does, beyond judging

The judge answers one case at a time. Three more typed calls answer the
questions somebody actually has once 147 outcomes have moved — each a pydantic
`output_type` on an `LLMOperator`, so none of it is parsed out of prose:

| Call | Runs in | Answers |
|---|---|---|
| `PolicyBrief` | `replay_<domain>` | What does this change do? Headline, a ship / ship-with-caveats / do-not-ship verdict, risks, and what to measure afterwards — grounded in the aggregates and the 25 largest movements, with the sign convention spelled out so it cannot mistake a cost for a saving. |
| `FlipThemes` | `replay_<domain>` | *Which kinds* of case moved. 147 rows is data; "76 cases where no clause reaches them any more" is a finding. |
| `Amendment` | `precedent_gate_<domain>` | The gate just failed — what is the smallest edit to the policy text that stops it reversing a human ruling? Drafted *before* the failure is raised, registered as a candidate version, and then actually tested by `amend_<domain>`. |

All three are commentary on the pipeline, never a gate in it: those tasks are
wired `all_done`, so a model outage cannot stop an adjudication or let a
precedent violation through.

The brief is also handed the two computations below, so its `blind_spots` are
grounded in arithmetic rather than invented.

Under `PTM_OFFLINE=1` each has a deterministic stand-in computed from the same
aggregates the prompt would have shown the model, so the storage, the DAG
wiring and the dashboard panels are all exercised with no API key. The
stand-ins do not fake the one thing that genuinely needs a model — drafting
policy prose — and say so instead.

## What the flip list cannot show you

Two computations, deliberately not sampled from a model. The model's job is to
explain them; arithmetic should be arithmetic.

**Clause coverage.** Which rules of the proposed policy has two years of real
history never once exercised? Those are the ones you are shipping untested, and
no amount of replay will surface them, because the whole point is that nothing
hit them. On the expenses fixture:

```
clause coverage: 6/7 exercised, never reached: 7.1
  340 cases (56.7%) decided by no clause at all
```

This found a real bug the first time it ran: clause 6.1 — the grade-3 exemption
that the entire point-in-time trap is about — was reported untested, because the
offline fixture encoded the exemption as a *fall-through* instead of citing the
clause. The policy and its fixture had drifted, and nothing else would have said so.

**Who bears it.** "Costs GBP 12,014" is an incomplete answer if it all lands on
one group. Declared per domain via `cohort_fields`, since only the domain knows
which of its fields describe a person rather than a transaction:

```
who bears it:
  category=meals   52.6% flip rate, 2.15x the population, net GBP 5,004
```

Cohorts are resolved point-in-time like everything else: an employee promoted
last year is counted in the grade they held on the day of the claim.

## Comparing two policies

`compare` puts two versions against the same history and the same precedents —
flip rate, net effect, precedent reversals, clause coverage. Running the
*current* policy through it is the control that validates the whole apparatus:

```
control: replaying the policy already in force (v1)
  44 of 600 differ (7.3%) - reviewer discretion, not the proposal
```

The fixture gives reviewers an 8% deviation rate, and the replay recovers 7.3%.
So the 24.5% under v2 is the change, not the harness measuring itself.

One caveat the comparison states in its own output: precedents are established
while reviewing one particular proposal, so a version that predates them is
judged on questions it was never asked.

## The dashboard

`/ptm/` is a dependency-free page (no CDN, no build step) that reads the same
SQLite database: the brief and its verdict, the impact tiles, a diverging
monthly chart of the replayed history, clause coverage and cohort bars, the
themes, any drafted amendment with a **Verified / Untested / Fix fails** badge,
a side-by-side comparison against any other version, and the flips themselves —
filterable by direction, confidence and precedent, sortable, searchable, paged,
and expandable to the case record as it stood on the day. It works in either
Airflow theme and collapses to cards on a phone.

Computed panels are labelled as computed, so arithmetic is never presented as
model output.

`select_for_review` is deliberately stingy: humans see a flip only if the
judge was unsure, the money is large, or the change makes the organisation
more permissive than it chose to be. 147 flips → 8 human decisions, drawn
round-robin from each ground so eight big claims cannot crowd out every
ambiguous one.

---

## Run it

```bash
make up      # Airflow 3.1 at localhost:8080 (admin/admin), history auto-seeded
make demo    # backfill 24 months of replay
```

Then open **http://localhost:8080/ptm/** for the Diff Explorer, and the
`adjudicate_expenses` DAG to answer the human-in-the-loop tasks.

No Airflow, no API key, no network:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test       # 168 tests, ~1s
make selftest   # the whole loop end to end, printing what it found
make pit        # what a naive, non-point-in-time replay gets wrong
make check      # all three
```

The tests assert the claims this README makes, not just that the code runs:
that a promotion cannot reach backwards into an earlier claim, that a naive
replay only ever flatters the proposal, that re-seeding does not move the
numbers, that an ambiguous case cannot be crowded out of review by expensive
ones, and that nothing reaches the dashboard's markup unescaped.

### Offline vs the real judge

`PTM_OFFLINE=1` (the default) swaps the `LLMOperator` for a deterministic
rule evaluator declared in the domain YAML. Everything else — the mapping,
the diff, the HITL gate, the assets, the plugin — is identical. It exists so
the demo survives conference wifi and so CI needs no key.

For the real thing, set `PTM_OFFLINE=0` and add a pydantic-ai connection:

```bash
AIRFLOW_CONN_PYDANTICAI_DEFAULT='{"conn_type":"pydanticai","host":"anthropic:claude-sonnet-5","password":"sk-ant-..."}'
```

---

## Adding a domain

Nothing in `dags/` or `ptm/` knows what an expense is. To run this on
insurance claims, moderation decisions, loan applications or admissions:

1. Write the policy versions as markdown in `include/policies/<domain>/`.
2. Write `include/domains/<domain>.yaml` — outcomes ordered most-generous to
   most-strict, a case template, and where the policies live.
3. Load cases into the `cases` table, and any slowly-changing attributes into
   `subject_facts` with a `known_from` date.

Three DAGs appear on the next parse. That is the demo's closing move: swap
the config, run the same pipeline on a completely different domain.

## Layout

```
dags/policy_time_machine.py     the three-DAG factory
plugins/                        FastAPI plugin + Diff Explorer dashboard
ptm/config.py                   domain YAML loading
ptm/store.py                    SQLite, incl. the point-in-time case query
ptm/judge.py                    prompt construction + offline stand-in judge
ptm/ai.py                       brief, themes and amendment - prompts, types, fallbacks
ptm/analysis.py                 clause coverage, cohort impact, policy comparison
ptm/amend.py                    a drafted amendment -> a judgeable policy version
ptm/diff.py                     flips, review selection, precedent violations
ptm/seed.py                     synthetic 2-year decision history
ptm/selftest.py                 whole loop, no Airflow
tests/                          168 tests, no Airflow required
include/domains/*.yaml          the only domain knowledge in the project
```

## Verified against

The engine, the plugin API and the dashboard are verified on every change:

- `make test` — 168 tests over the store, the diff, the judge, the AI layer,
  the seed and the dashboard's escaping. No Airflow, no network, ~1s.
- the FastAPI app and the page are exercised against a real replayed database
  (every endpoint, the filters, sorting, search, pagination, the empty states
  for an unreplayed domain, and no horizontal overflow at 390px).

Confirmed in-container against `apache/airflow:3.1.0` with
`apache-airflow-providers-common-ai==0.8.0` and
`apache-airflow-providers-standard`:

- all **eight** DAGs parse with **zero import errors**, in both the offline and
  the LLM-backed configuration — including every `LLMOperator` kwarg
  (`output_type`, `usage_limits`) and the `HITLOperator` `params` block;
- `airflow dags test replay_expenses` completes and persists verdicts, flips, a
  run summary, the brief, the themes and the computed panels;
- `airflow dags test precedent_gate_expenses` registers the candidate `v2+fix`
  and then fails, as designed, on three reversed precedents;
- `airflow dags test amend_expenses` judges that candidate and **passes**,
  reporting that it clears every precedent with no collateral.

That last pair is the whole loop, in Airflow: the gate condemns the policy, the
drafter proposes a fix, and a separate DAG proves the fix works.

## Caveats

- Single-container Airflow on SQLite. Fine for a demo, not a topology.
- Manual runs have no meaningful data interval, so they replay all of history
  capped by the `max_cases` param (default 250), sampled evenly across the
  period rather than taken from its start. Scheduled and backfilled runs use
  their own interval and ignore the cap.
- The Common AI provider also offers built-in approval on `LLMOperator`
  (`require_approval`). This project uses a separate `HITLOperator` instead,
  because the reviewer picks the *correct outcome* from the domain's options
  rather than approving a verdict, and that answer becomes precedent.
- FastAPI plugin routes are read-only and **not** behind Airflow auth — Airflow
  does not protect plugin endpoints automatically.
- Airflow 3.1's `react_apps` plugin slot is marked experimental, so the
  dashboard is served as a dependency-free page from the FastAPI app instead.
- `offline_rules` are evaluated with `eval` under an empty builtins scope.
  They are a local demo fixture, not a sandbox; only point them at YAML you
  wrote. A rule that fails to evaluate is logged once rather than silently
  skipped, since a typo'd field name is otherwise indistinguishable from a
  field the case legitimately lacks.
- The offline analysis stand-ins are deterministic functions, not a model.
  They are labelled as such in the dashboard so a screenshot cannot be
  mistaken for model output.
- **The offline amendment is a carve-out, not a policy.** Narrowing a *clause*
  needs a model; with `PTM_OFFLINE=1` the fix pins the specific cases the gate
  objected to and leaves everything else alone. Its one virtue is that it is a
  true lower bound — if even a literal carve-out cannot clear the gate, the
  precedents contradict each other. Set `PTM_OFFLINE=0` to verify drafted prose.
- A candidate version lives in the database, not in `include/policies/`. A DAG
  should not rewrite a file a human owns, and the registry lets a candidate
  carry its provenance: which precedents forced it, and which version it amends.
- Precedents are established while reviewing one specific proposal, so they
  inherit its framing. Comparing an older policy against them is not
  like-for-like, and `compare` says so in its own output rather than letting the
  older version look worse than it is.
- Clause coverage parses `N.N` at the start of a line in the policy markdown.
  That matches the policies in `include/`; a differently formatted policy would
  need a different parser.
