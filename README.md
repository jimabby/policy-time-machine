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

8 flips routed to a human out of 147
8 precedents established

gate: policy v2 vs 8 precedents -> 3 violation(s)
  exp-0478: finance.lead ruled 'approve', v2 gives 'deny'
GATE FAILS - policy would reverse a human ruling.
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
| **Data intervals** | Make the replay *honest*. Each run only sees cases inside its own window, and each case is hydrated with facts known on its decision date. **Skip this and 39 of 600 cases come out wrong** — see below. |
| **Dynamic task mapping** | One judge task per case, with concurrency capped so you don't melt the model endpoint. |
| **Common AI provider** | `LLMOperator` with `output_type=Verdict`, so every verdict is typed, not parsed out of prose. `usage_limits` caps spend per task. The vendor lives in a connection — switching models never touches DAG code. |
| **HITL operators** | `HITLOperator` deferred in the triggerer, holding no worker slot, asking a human for the *correct outcome* — not a yes/no. |
| **Assets** | `ptm://<domain>/flips` wakes adjudication; `ptm://<domain>/precedents` wakes the regression gate. Nothing is polled. |
| **Plugin (FastAPI + external view)** | The Policy Diff Explorer, a tab inside the Airflow UI. |
| **Dynamic DAG generation** | Drop a YAML in `include/domains/` and three new DAGs appear. The DAG code contains zero domain knowledge. |

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

Every one of those 41 errors flatters the proposal. Airflow's data-interval
semantics are what stop it.

---

## Architecture

Three DAGs per domain, generated from `include/domains/*.yaml`:

```
                    ┌─────────────────────┐
   backfill ───────▶│  replay_<domain>    │  @monthly × 24 runs
                    │  point-in-time load │
                    │  → map judge/case   │  LLMOperator, response_model=Verdict
                    │  → diff vs history  │
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
                    └─────────────────────┘
```

`select_for_review` is deliberately stingy: humans see a flip only if the
judge was unsure, the money is large, or the change makes the organisation
more permissive than it chose to be. 147 flips → 8 human decisions.

---

## Run it

```bash
make up      # Airflow 3.1 at localhost:8080 (admin/admin), history auto-seeded
make demo    # backfill 24 months of replay
```

Then open **http://localhost:8080/ptm/** for the Diff Explorer, and the
`adjudicate_expenses` DAG to answer the human-in-the-loop tasks.

No Airflow, no API key, whole loop in ~2 seconds:

```bash
python3 -m venv .venv && .venv/bin/pip install pydantic pyyaml
make test
```

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
ptm/diff.py                     flips, review selection, precedent violations
ptm/seed.py                     synthetic 2-year decision history
ptm/selftest.py                 whole loop, no Airflow
include/domains/*.yaml          the only domain knowledge in the project
```

## Verified against

Built and run against `apache/airflow:3.1.0` with
`apache-airflow-providers-common-ai==0.8.0` and
`apache-airflow-providers-standard`. Confirmed in-container:

- all six DAGs parse with **zero import errors**, in both offline and
  LLM-backed configurations;
- the plugin registers (`airflow plugins` lists its FastAPI app and external
  view) and serves at `/ptm/`;
- `airflow dags test replay_expenses` completes and persists verdicts, flips
  and a run summary.

## Caveats

- Single-container Airflow on SQLite. Fine for a demo, not a topology.
- Manual runs have no meaningful data interval, so they replay all of history
  capped by the `max_cases` param (default 250). Scheduled and backfilled runs
  use their own interval and ignore the cap.
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
