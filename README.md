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

8 flips routed to a human out of 147
8 precedents established

gate: policy v2 vs 8 precedents -> 3 violation(s)
  exp-0478: finance.lead ruled 'approve', v2 gives 'deny'
GATE FAILS - policy would reverse a human ruling.
```

Three things are load-bearing there, and each answers a question the previous
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

**The gate is the point.** The first run gives you an *estimate*. Every run
after gives you a **regression suite for organisational judgment**.

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
| **Dynamic DAG generation** | Drop a YAML in `include/domains/` and four new DAGs appear. The DAG code contains zero domain knowledge — [a test asserts it](tests/test_dags.py). |

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

Four DAGs per domain, generated from `include/domains/*.yaml`:

```
                    ┌─────────────────────┐
   backfill ───────▶│  replay_<domain>    │  @monthly × 24 runs
                    │  point-in-time load │
                    │  → map judge/case   │  LLMOperator, response_model=Verdict
                    │  → map judge/case   │  again, under the in-force policy
                    │  → diff + attribute │  clause, segment and cost breakdowns
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
                    └─────────────────────┘

   manual  ────────▶ judge_stability_<domain>   the error bar on all of the above
```

`select_for_review` is deliberately stingy: humans see a flip only if the
judge was unsure, the money is large, or the change makes the organisation
more permissive than it chose to be. 147 flips → 8 human decisions.

### What each DAG produces

**`replay_<domain>`** — the simulation. Beyond the diff it records:

- **Clause attribution.** Which clause of the proposal is responsible for which
  share of the change, and which of them changed by *ceasing to apply*.
- **Blast radius.** Per segment (`segment_fields` in the YAML), how many cases
  moved *out of how many* — because 9 flips out of 12 travel claims is a
  different proposal from 9 out of 400.
- **A cost ledger.** What the judging cost, plus a forecast for a full replay,
  so "can I afford this backfill" is answerable before launching it.

**`adjudicate_<domain>`** — the human loop. Each HITL task now tells the
reviewer which clause drove the change, so they can argue with the rule rather
than only the result.

**`precedent_gate_<domain>`** — the regression suite. Fails on a reversal, and
separately *warns* when two human rulings contradict each other: precedent is
the only durable output here, so it is also the only thing that can quietly
rot. Two reviewers answering materially identical cases differently make the
suite unsatisfiable, and nothing else in the pipeline would notice.

**`judge_stability_<domain>`** — the error bar. Judges the same cases several
times under the *same* policy and reports how often the judge contradicts
itself:

```
judged 25 cases 3x each under the same policy
  2 disagreed with themselves (8.0% disagreement rate)
  against a 24.5% flip rate, up to 33% of the measured change
  could be judge noise rather than policy.
```

*(Illustrative — the numbers above are what a real judge looks like. The
offline judge is deterministic and reports 0%; see below.)*

The obvious objection to any of this is *"is that the policy, or is that the
model?"*. Without this number there is no answer. `max_disagreement` turns it
into a gate: refuse to trust a judge noisier than you can accept. Offline the
judge is deterministic and this necessarily reports 0% — which is not a clean
bill of health, it means the check is inert until you point it at a real model.

---

## Run it

```bash
make up        # Airflow 3.1 at localhost:8080 (admin/admin), history auto-seeded
make demo      # backfill 24 months of replay
make stability # measure the judge's noise floor
```

Then open **http://localhost:8080/ptm/** for the Diff Explorer, and the
`adjudicate_expenses` DAG to answer the human-in-the-loop tasks.

No Airflow, no API key, whole loop in about a second:

```bash
make dev       # create .venv with pydantic, pyyaml, pytest
make test      # lint + 203 tests + the whole loop end to end
make cost      # forecast a full LLM-backed replay
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
   `segment_fields` you care about, and where the policies live.
3. Load cases into the `cases` table, and any slowly-changing attributes into
   `subject_facts` with a `known_from` date.
4. Run `python -m ptm.lint` before you trust the result.

Four DAGs appear on the next parse. That is the demo's closing move: swap
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
dags/policy_time_machine.py     the four-DAG factory
plugins/                        FastAPI plugin + Diff Explorer dashboard
ptm/config.py                   domain YAML loading
ptm/store.py                    SQLite, incl. the point-in-time case query
ptm/judge.py                    prompt construction + offline stand-in judge
ptm/diff.py                     flips, attribution, segments, precedent checks
ptm/report.py                   the read models behind the plugin's API
ptm/stability.py                how often the judge contradicts itself
ptm/cost.py                     what a replay costs, and will cost
ptm/lint.py                     domain YAML vs the policies it claims to implement
ptm/seed.py                     synthetic 2-year decision history
ptm/selftest.py                 whole loop, no Airflow
tests/                          203 tests; only the 7 DAG-parse ones need Airflow
include/domains/*.yaml          the only domain knowledge in the project
```

The plugin is deliberately nothing but routing — every read model lives in
`ptm/report.py`, so the whole API surface is covered by the test suite without
FastAPI installed.

## Verified against

Built and run against `apache/airflow:3.1.0` with
`apache-airflow-providers-common-ai==0.8.0` and
`apache-airflow-providers-standard`. Confirmed in-container:

- all eight DAGs parse with **zero import errors**, in both offline and
  LLM-backed configurations — [CI checks both](.github/workflows/ci.yml);
- the plugin registers (`airflow plugins` lists its FastAPI app and external
  view) and serves at `/ptm/`;
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
  between two humans, not a code change.
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
