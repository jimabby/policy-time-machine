# Policy Time Machine — written description

*Built for **Beyond the DAG**. Airflow 3.1, the Common AI provider, HITL
operators, assets, dynamic task mapping, and a UI plugin.*

Repository: <https://github.com/jimabby/policy-time-machine> · License:
[Apache-2.0](LICENSE) · Full technical detail: [README.md](README.md)

---

## What it does

Every organisation has consequential rules that humans apply to messy cases:
refund eligibility, claims handling, loan criteria, content moderation,
admissions, expense policy. When somebody proposes changing one, the honest
answer to *"what will this actually do?"* is **nobody knows**. People argue from
anecdote, ship it, and find out three months later.

Policy Time Machine makes that question computable, and then makes the answer
*stick*.

Change a rule today, and Airflow replays every real decision your organisation
made over the last two years as it would have gone under the new rule — using
the data **as it stood at the time**. Humans adjudicate only the cases where the
old and new answers disagree. Those adjudications become a permanent regression
suite that every future rule change must pass.

On the shipped fixture — 600 expense decisions across two years — a candidate
policy produces this:

```
replayed 600 decisions under policy v2
  147 outcomes change (24.5%, 21.2%-28.1% at 95% on 600 cases)
  135 more generous  GBP 17,994
  12 more strict     GBP 5,980
  net GBP 12,014

what in policy v2 causes the change (baseline: policy v1):
  clause 1.1 relaxed                48 flips (32.6%)  net GBP  2,304
  (reviewer deviated from policy)   38 flips (25.9%)  net GBP -2,210
  clause 2.1 relaxed                22 flips (15.0%)  net GBP  1,125
  ...
  109 of 147 changes are caused by policy v2 (net GBP 14,224).
  38 are cases the recorded outcome got wrong under policy v1 too,
  so they are not this proposal's doing.

re-judged 25 flips 3x each under the same policy
  25 reproduced, 0 did not

8 flips routed to a human out of 147
gate: policy v2 vs 8 precedents -> 2 violation(s); 0 introduced by v2
```

Four things there are load-bearing, and each answers a question the previous one
provokes:

1. **"147 change" is not actionable. *Which sentence do I edit?* is.** Every
   change is attributed to the clause responsible — including a clause that
   changed by **ceasing to apply**, which is how most rule changes actually move
   decisions.
2. **Not every change is the proposal's fault.** 38 of the 147 are cases where
   the recorded outcome disagreed with the policy *already in force* — a
   reviewer departing from the rulebook they had. Charging those to v2
   overstates its impact by 26%.
3. **A number without an error bar is not a number.** Before a flip reaches a
   human it is re-judged several times. One that will not reproduce is the model
   changing its mind rather than the policy moving, and it is held back.
4. **The gate is the point.** The first run gives an estimate. Every run after
   gives a **regression suite for organisational judgment**.

## How it works

**Backfill is the simulation engine.** One `backfill create` fans out 24 monthly
runs that replay two years of history. This is not Airflow-as-scheduler; it is
Airflow-as-experiment-harness.

**Data intervals make the replay honest.** Each run sees only cases inside its
own window, and every case is hydrated with the facts known *on its decision
date* — via a `known_from` column on slowly-changing subject facts. Skip this and
**39 of 600 cases come out wrong**; `python -m ptm.pit_check` demonstrates it by
running the replay both ways.

**Eleven DAGs, generated from YAML.** Five per domain plus one for the shared
database. The DAG module contains *zero* domain knowledge — [a test asserts
that](tests/test_dags.py) by grepping the source for domain words. Drop a YAML
into `include/domains/` and five new DAGs appear.

| DAG | What it does |
|---|---|
| `replay_<domain>` | Reads the policy for structural problems before spending anything, then replays each case under both the candidate and the in-force policy. Emits the `flips` asset. |
| `adjudicate_<domain>` | Woken by that asset. Puts the handful of genuinely contested flips to a human via `HITLOperator`, deferred in the triggerer. Emits `precedents`. |
| `precedent_gate_<domain>` | Woken by *that* asset. Re-judges every human ruling under the candidate and **fails** on a reversal. |
| `judge_stability_<domain>` | Judges the same cases repeatedly to measure how often the judge contradicts itself — the error bar on everything else. |
| `propose_<domain>` | Points the same `LLMOperator` the other way: `output_type=PolicyPatch` has a model *write* the next version, which the gate then re-judges. |
| `ptm_retention` | Weekly. Drops the cache, sample, verdict and flip rows that have stopped earning their disk, and compacts the file. |

Every one of those has a `python -m ptm.*` counterpart that needs no Airflow —
including the gate, which for a long time was the exception: the regression
suite this project is *for* could only be reached by starting Airflow and
triggering a DAG, while the lint, the preflight, the sweep and the calibration
score all ran in CI. `python -m ptm.gate` is that DAG's enforce step reading
stored verdicts, with three exit codes, because a precedent nothing has judged
is not a pass and a shell testing for zero has to be able to tell the two apart.

**Typed verdicts, not parsed prose.** `LLMOperator` with `output_type=Verdict`
means every answer arrives as a validated object with an outcome, a confidence
and a cited clause. `usage_limits` caps spend per task. The vendor lives in a
connection, so switching models never touches DAG code — and setting
`compare_model` on a run asks a *second* model the same questions, surfacing the
cases two judges split on. Those are sentences the policy does not settle, found
without spending a human on any of them.

**Assets, not polling.** `ptm://<domain>/flips` wakes adjudication;
`ptm://<domain>/precedents` wakes the gate. The pipeline is event-driven end to
end.

**A UI plugin.** The Policy Diff Explorer is a FastAPI app plus an external view,
so it appears as a tab inside the Airflow UI.

The whole thing also runs **offline**, with a deterministic rule evaluator
standing in for the model — no API key, no network, about a second end to end.
That is how CI tests it and how the demo is rehearsed.

## What was hard

**Attribution, not counting.** Counting flips is easy. Naming the clause
responsible is not, because the most common way a rule change moves a decision
is by a restriction *ceasing to fire* — and nothing is cited in that case. The
candidate's own verdict leaves the majority of changes unexplained. The fix was
to judge the in-force policy alongside the candidate, which doubles the bill and
is the only way to get `clause 1.1 relaxed` as an answer.

**Telling the policy's effect from the reviewer's.** That same baseline pass
turned out to answer a second question nobody had asked: when *both* policies
agree and the recorded outcome differs, the proposal caused nothing — a human
departed from the rulebook they already had. Folding those into "the impact of
v2" overstated it by 26%. They are now a separate bucket, capped at 2 of the 8
human review slots, because they are reliably the largest flips by money and
left uncapped they crowd out the cases the proposal is actually responsible for.

**Precedent is permanent, so noise in it is unrecoverable.** A flip the judge
will not reproduce when asked again is the model changing its mind, not the
policy moving. Writing one into the precedent set poisons a regression suite
forever. So flips are confirmed by re-judging before they reach a human — and
the confirmation is keyed on the *outcome* it was about, so a later replay
reaching a different answer does not inherit a measurement made about a
different verdict.

**A sweep that was confidently wrong rather than broken.** Asking *what should
the threshold be* means rewriting the number in a rule and re-running. The
rewrite moves every literal the field is compared against — right for the
one-sided threshold a clause usually states, and destructive for a band like
`40 < amount <= 100`, which collapses to `60 < amount <= 60` at every setting
tried. Nothing raised. The rewrite reported two hits and drew a smooth curve for
a rule that matched no case at any point on it. That is the worst failure this
project can have, so such a dial is now detected by name and refused, the lint
warns before anybody asks, and the Explorer lists it as un-sweepable rather than
dropping it — a field that vanished from the menu reads as a policy with no such
threshold, which is a different and equally wrong thing to believe.

**Letting a model write policy without letting it grade itself.** The proposer
is the one place a model *writes* rather than judges. That is only defensible
because it writes against an oracle it cannot influence: the precedent gate
re-judges every human ruling under the draft and fails on a reversal. A patch
that argues beautifully and breaks precedent fails exactly as loudly as one that
argues badly. Drafts live in their own directory, are labelled `(draft)`
everywhere they appear, and say *"Not approved by anyone"* in their own first
line. Adopting one is a separate, deliberate human act.

**Evaluating generated rules safely.** When the rules were hand-written YAML,
`eval` with trimmed builtins was a defensible shortcut. The moment a model began
writing them and a worker began evaluating them, it was not — and a name-level
check in front of `eval` does not contain
`().__class__.__base__.__subclasses__()`, which reads no bare names at all. The
evaluator now walks the parsed expression and computes it node by node, refusing
anything it does not explicitly implement. It also bounds *cost*, not just
reach: a whitelist that cannot be escaped can still be made to run forever, and
these run on a worker holding a mapped task slot.

**Airflow does not protect plugin endpoints.** A plugin's FastAPI app is
`mount()`ed, and a mounted sub-application inherits none of the parent's
dependencies — so every route was readable by anyone who could reach the port,
with the whole case file behind it. The plugin now applies its own dependency
once to the entire app. The subtle part: `resolve_user_from_token` is a
coroutine function, so written as a plain `def` the dependency returned a
*coroutine object*, FastAPI saw an ordinary return value, and every route stayed
open — with nothing in the logs but an unawaited-coroutine warning. Asserting
that the app *carried* the dependency passed the whole time, which is why the
test now drives a real client with no token and asserts on what it gets back.

**A rule that can never fire.** `offline_rules` are tried in order and the first
match decides the case, so a general restriction written above the exemption it
was meant to carve out of makes that exemption unreachable. It passes every
other check here — it parses, reads real fields, cites a real clause — and it
decides nothing, so its outcome is never produced and its clause never cited.
Silent in exactly the way a misspelled field name is, and now arriving from a
model as well as from a person, because the proposer writes these. The lint
proves it statically and one-directionally: it reports a shadow only where it
can show one, so it never fires on a rule set somebody has thought about.

**A curve that was flat for the wrong reason.** A threshold sweep that returns
the same number at every setting reads as "this dial is not very sensitive". The
shipped fixture has one where the truth is much stronger — the grade exemption
in clause 6.1 is only ever reached by cases clause 1.1 has already declined to
decide, and the outcome it gives is the one they fall through to anyway, so
moving it changes which clause is *cited* and nothing else. The grid reported
the two dials "independent", which is true and reads as the opposite of the
finding. A dial that moves no decision at any setting now says so.

**The question that comes before the backfill.** Every band here was
retrospective: how precise a rate turned out to be, once it had been paid for.
Nothing could answer *how many cases do I need to tell 20% from 24%* — and the
answer for the shipped fixture is 1,340 per arm against the 600 that exist, so a
version comparison turning on four points is a comparison about sample size.
That refusal now sits under the tiles, in the export bundle and in CI.

**The boring things that only fail in production.** Timestamps written in three
different zones into columns SQLite compares as *text* (wrong for one hour a
year, silently). A data-interval bound serialising with a `+00:00` suffix that
sorts *above* a naive timestamp, dropping cases decided exactly on a boundary —
the one loss a point-in-time replay must never have. A cache keyed on
`(case, version)` instead of on the prompt, which would serve a stale verdict
after every clause edit because editing a clause does not change the version
label. A HITL queue addressed to nobody, with no timeout and no notifier, so a
contested case waited indefinitely and was answerable by whoever found it — and
the reviewer's id read off a key the operator does not send, so every precedent
a real run recorded was filed against `unknown`, on the one field whose entire
point is that a named person is accountable. Adding the timeout meant first
making the timeout safe: Airflow answers an expired HITL task with `defaults`,
which here is the most generous outcome in the domain, and a review the clock
answered is now refused rather than written into the permanent record.

And a login that did not exist. `_AIRFLOW_WWW_USER_USERNAME`/`PASSWORD` set to
`admin`/`admin` is what an Airflow 2 image reads; 3.1 has no `airflow users`
command and no FAB user table, so the demo's first instruction — in the README,
the Makefile and the demo script — named a credential that could not work, while
the one that did was a random password regenerated on every container start and
printed once into the logs.

---

**Verified:** 1064 tests (891 need nothing but Python), `ruff` clean, all eleven
DAGs parsing under a real Airflow in both offline and LLM-backed configurations,
the plugin's routes driven through a real client, and the Diff Explorer loaded in
Chromium and clicked through — failing on any console error or any panel that
renders nothing. The precedent gate runs as an ordinary CI step, and the
precedent set is exported and imported back, because an export nothing can read
back is a backup nobody has tested.
