"""``replay_<domain>`` - what would this rule change actually do?

Monthly, and backfilled across history. Reads the policy before spending
anything on it, then replays every real decision in its data interval under it,
point-in-time correct. Attributes every change to the clause that caused it,
breaks the blast radius down by segment, says which segments carry more of it
than the rest of their field, and records what the judging cost - net of
everything served from cache. Emits the flips asset, which is what wakes
adjudication.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Param, dag, task

from ptm import cost, diff, disparity, preflight, provenance, store
from ptm.config import LLM_CONN_ID, OFFLINE
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Verdict
from ptm_dags.common import (
    DEFAULTS,
    START,
    SYSTEM_PROMPT,
    DomainDags,
    _add_ledgers,
    _as_verdict,
    _case,
    _item,
    _version_from_context,
    judge_configuration,
    merge,
    to_judge,
)

# Only when a model is actually going to be asked something. Both names are
# absent offline - as they were when this was one module - and every use of
# them below sits behind the same `if not OFFLINE`. Importing them
# unconditionally is what the split got wrong first: it turned a name that
# simply does not exist offline into an ImportError that took every DAG in the
# file with it.
if not OFFLINE:
    from ptm_dags.common import LLMOperator, UsageLimits


def build(ctx: DomainDags) -> None:
    """Build ``replay_{domain}``: read the policy, replay the interval, publish.

    Everything domain-specific comes off ``ctx``; nothing below reads a YAML or
    knows what a case contains.
    """
    domain_name = ctx.name
    domain = ctx.domain
    flips_asset = ctx.flips
    policy_param = ctx.policy_param

    def record_failure(context):
        provenance.failed(f"{context['dag'].dag_id}::{context['run_id']}", domain_name)

    # ------------------------------------------------------------------ replay
    @dag(
        dag_id=f"replay_{domain_name}",
        schedule="@monthly",
        start_date=START,
        catchup=False,
        max_active_runs=4,
        default_args=DEFAULTS,
        on_failure_callback=record_failure,
        params={
            "policy_version": policy_param,
            # Manual runs replay all of history, so they are capped by default:
            # a curious click should not fan out thousands of judge tasks.
            "max_cases": Param(250, type="integer", title="Max cases on a manual run (0 = no cap)",
                               description="Ignored by scheduled and backfilled runs, which replay their own interval."),
            # Judging each case under the in-force policy as well is what lets a
            # change be blamed on the clause that relaxed, and what separates a
            # policy effect from a reviewer who ignored the rulebook. It doubles
            # the judging, so it is a parameter rather than an assumption.
            "baseline_version": Param(
                domain.in_force, type=["string", "null"],
                title="Policy version in force (blank to skip the baseline pass)",
                description="Judging both sides doubles cost and is what makes clause "
                            "attribution possible. Blank diffs against recorded history only."),
            # Reading the policy costs nothing and catches the problems that
            # make a paid replay unusable rather than merely expensive.
            "preflight": Param(
                "warn", type="string", enum=["warn", "fail", "off"],
                title="What to do about problems in the policy text",
                description="'fail' refuses to spend a backfill on a policy whose clauses "
                            "are duplicated, unnumbered or cross-referenced to nothing."),
            "disparity_gate": Param(
                "domain", type="string", enum=["domain", "warn", "fail", "off"],
                title="What to do when the change lands on one segment far harder",
                description="'domain' uses the setting in the domain YAML. 'fail' stops "
                            "the run on a concentration that is both large and supported "
                            "by the sample size."),
        },
        tags=["policy-time-machine", domain_name, "replay"],
        doc_md=f"Replay historical {domain.label} decisions under a proposed policy. "
               f"Backfill this DAG to simulate the whole of history.",
    )
    def replay():
        @task
        def read_the_policy(**ctx) -> dict:
            """Check the policy is applicable before spending anything on it.

            Structural only - clause numbering, cross-references, outcomes the
            policy never mentions. It reads the shape of the document, not its
            meaning, and it runs in milliseconds with no model and no network.

            The failure it exists to prevent is not an expensive run, it is an
            expensive run whose output nobody can use: a policy whose rules are
            written as unnumbered prose produces six hundred verdicts attributed
            to nothing, which costs the same as a good run and answers nothing.
            """
            version = ctx["params"]["policy_version"]
            mode = (ctx["params"].get("preflight") or "warn").strip()
            if mode == "off":
                print("preflight skipped by parameter")
                return {"findings": 0, "blocking": 0, "mode": mode}
            findings = preflight.structural(domain, version)
            print(preflight.describe(findings, version))
            blocking = preflight.blocking(findings)
            if blocking and mode == "fail":
                raise AirflowFailException(
                    f"policy {version} has {len(blocking)} blocking problem(s) and "
                    f"preflight=fail. A replay would run and its results would not be "
                    f"attributable:\n"
                    + "\n".join(f"  - {f.detail}" for f in blocking))
            return {"findings": len(findings), "blocking": len(blocking), "mode": mode,
                    "detail": [f.model_dump(mode="json") for f in findings]}

        @task
        def prepare(**ctx) -> list[dict]:
            """Load this run's cases, as the world knew them at the time.

            The data interval is what makes the replay honest: a scheduled or
            backfilled run only ever sees cases decided inside its own window,
            hydrated with facts already known on each case's decision date.

            A manual trigger has no meaningful interval (Airflow gives it a
            zero-width one), so we treat it as "replay everything up to now",
            capped by the max_cases param. When that cap bites we keep the
            *most recent* cases, not the oldest: slowly-changing facts have not
            changed yet at the start of the period, so a cap taken from the
            front of history systematically misses the interactions this engine
            exists to get right. Point-in-time hydration is unaffected either
            way - it is per-case, not per-run.
            """
            store.init_db()
            version = ctx["params"]["policy_version"]
            baseline_version = (ctx["params"].get("baseline_version") or "").strip()
            lo, hi = ctx["data_interval_start"], ctx["data_interval_end"]
            cap = int(ctx["params"].get("max_cases") or 0)
            manual = lo >= hi

            if manual:
                lo, hi = pendulum.datetime(1970, 1, 1), pendulum.now("UTC")
                print(f"manual run: replaying all history up to {hi:%Y-%m-%d}"
                      f"{f', capped at the {cap} most recent cases' if cap else ''}")
            else:
                print(f"scheduled run: replaying {lo:%Y-%m-%d} to {hi:%Y-%m-%d}")

            # The cap is a manual-run guard and the parameter says so, but it
            # used to be passed on every run - so a scheduled window holding
            # more cases than the cap quietly replayed its oldest 250 and
            # reported a flip rate for a month it had only partly seen.
            limit = (cap or None) if manual else None
            cases = store.load_cases(domain_name, until=hi, since=lo,
                                     limit=limit, newest_first=manual and bool(cap))
            eligible = store.query("SELECT COUNT(*) n FROM cases WHERE domain=? "
                                   "AND decided_at>=? AND decided_at<?",
                                   (domain_name, store._bound(lo), store._bound(hi)))[0]["n"]
            provenance.begin(f"{ctx['dag'].dag_id}::{ctx['run_id']}", domain_name, version,
                             provenance.capture(domain, version, cases, baseline_version,
                                                lo, hi, eligible, judge_configuration()))
            print(f"{len(cases)} cases to replay under policy {version}")
            if cases:
                print(f"covering {cases[0].decided_at:%Y-%m-%d} to {cases[-1].decided_at:%Y-%m-%d}")
            if baseline_version:
                print(f"baseline pass enabled against policy {baseline_version}: "
                      f"each case is judged twice")
            return [_item(c, domain, version, baseline_version) for c in cases]

        @task
        def prompts(items: list[dict]) -> list[str]:
            """Render prompts for the LLM judge, which is the only consumer.

            Built here rather than in ``prepare`` so the prompt text exists in
            exactly one XCom entry instead of being copied into every
            downstream task that only wanted the case payload.
            """
            version = _version_from_context()
            return [build_prompt(_case(i), domain, version) for i in items]

        @task
        def baseline_items(items: list[dict], **ctx) -> list[dict]:
            """The cases to judge under the in-force policy - or none of them.

            Returning an empty list when no baseline is configured is what makes
            the second pass cost zero mapped tasks rather than a fan-out of
            no-ops, and keeps the LLM branch from rendering a prompt against a
            policy version that does not exist.
            """
            return items if (ctx["params"].get("baseline_version") or "").strip() else []

        @task
        def baseline_prompts(items: list[dict]) -> list[str]:
            version = _version_from_context("baseline_version")
            return [build_prompt(_case(i), domain, version) for i in items]

        # none_failed, not all_success: with baseline_version blank the baseline
        # fan-out expands to zero mapped instances and Airflow marks it skipped.
        # Under the default rule that skips this task, and the replay silently
        # produces nothing at all - for a configuration the README documents.
        @task(trigger_rule="none_failed")
        def reconcile(items: list[dict], candidate: dict, baseline_pass: dict | None = None,
                      **ctx) -> dict:
            version = ctx["params"]["policy_version"]
            baseline_version = (ctx["params"].get("baseline_version") or "").strip()
            run_id = f"{ctx['dag'].dag_id}::{ctx['run_id']}"
            snapshot = provenance.prepared(run_id, domain_name)
            if snapshot and provenance.digest(provenance.policy_inputs(
                    domain, version, judge_configuration())) != snapshot["policy_hash"]:
                raise AirflowFailException("Policy or judge changed during replay; start a new run")
            if snapshot and baseline_version and provenance.digest(provenance.policy_inputs(
                    domain, baseline_version, judge_configuration())) != snapshot["baseline_hash"]:
                raise AirflowFailException("Baseline changed during replay; start a new run")
            cases = [_case(i) for i in items]
            verdicts = (candidate or {}).get("verdicts") or []
            baseline_verdicts = (baseline_pass or {}).get("verdicts") or []
            if len(verdicts) != len(cases):
                raise AirflowFailException(
                    f"Judge returned {len(verdicts)} verdicts for {len(cases)} cases; refusing partial replay."
                )
            by_case = {c.case_id: _as_verdict(v) for c, v in zip(cases, verdicts)}
            for verdict in by_case.values():
                domain.validate_outcome(verdict.outcome)

            baseline = None
            if baseline_version and baseline_verdicts:
                if len(baseline_verdicts) != len(cases):
                    raise AirflowFailException(
                        f"Baseline judge returned {len(baseline_verdicts)} verdicts for "
                        f"{len(cases)} cases; refusing to attribute from a partial baseline."
                    )
                baseline = {c.case_id: _as_verdict(v) for c, v in zip(cases, baseline_verdicts)}
                for verdict in baseline.values():
                    domain.validate_outcome(verdict.outcome)

            found = diff.flips(cases, by_case, domain, baseline=baseline)
            summary = diff.summarise(found, len(cases), domain)
            segments = diff.segment_stats(cases, found, domain)
            ledger = _add_ledgers((candidate or {}).get("ledger") or {},
                                  (baseline_pass or {}).get("ledger") or {})
            cache_hits = (int((candidate or {}).get("cache_hits") or 0)
                          + int((baseline_pass or {}).get("cache_hits") or 0))
            cache_saved = round(float((candidate or {}).get("estimated_saved_usd") or 0)
                                + float((baseline_pass or {}).get("estimated_saved_usd") or 0), 4)
            store.save_replay(run_id, domain_name, version, "actual", len(cases),
                              found, summary["net_impact"], by_case,
                              segments=segments,
                              # Per case as well as pre-aggregated. Manual runs
                              # overlap backfills by design, and only the
                              # per-case rows can be deduplicated afterwards -
                              # summing the aggregates counts a case once per
                              # run that saw it.
                              case_segments=diff.case_segment_rows(cases, domain),
                              ledger=ledger,
                              baseline_version=baseline_version if baseline else "",
                              baseline_verdicts=baseline, snapshot=snapshot)

            for row in summary["by_clause"]:
                print(f"{row['clause']:>34}  {row['flips']:>4} flips "
                      f"({row['share']:>5.1%})  net {domain.impact_unit} {row['net_impact']:>10,.0f}")
            if summary["deviation_flips"]:
                print(f"of {summary['flips']} changes, {summary['policy_driven_flips']} are caused "
                      f"by policy {version}; {summary['deviation_flips']} are cases where the "
                      f"recorded outcome never matched policy {baseline_version} either")
            print(f"flip rate {summary['flip_rate']:.1%} "
                  f"({summary['flip_rate_lo']:.1%}-{summary['flip_rate_hi']:.1%} at 95% on "
                  f"{len(cases)} cases) - sampling error only, not the judge's noise floor")

            # Who it lands on, asked rather than tabulated. This run's own cases
            # are the sample, so a monthly backfill run sees one month; the
            # pooled answer across every run is the one the dashboard shows.
            findings = disparity.analyse(segments, domain)
            print(disparity.describe(findings, domain))
            mode = (ctx["params"].get("disparity_gate") or "domain").strip()
            if mode == "domain":
                mode = domain.disparity.gate
            gating = disparity.gated(findings)
            if gating and mode == "fail":
                raise AirflowFailException(
                    f"policy {version} lands on {len(gating)} segment(s) far harder than the "
                    f"rest of their field, and disparity_gate=fail:\n"
                    + "\n".join(
                        f"  - {f.field}={f.value}: {f.flip_rate:.1%} of {f.cases} cases move "
                        f"against {f.rest_flip_rate:.1%} of the other {f.rest_cases}"
                        for f in gating))

            if ledger["estimated_cost_usd"] or cache_hits:
                print(f"judged by {ledger['judge_model']} for an estimated "
                      f"USD {ledger['estimated_cost_usd']:.4f}"
                      + (f"; {cache_hits} verdict(s) came from cache, saving an estimated "
                         f"USD {cache_saved:.4f}" if cache_hits else ""))
            # What it actually was, when the judge reported it. The estimate is
            # not corrected - it is scored, because the gap is what prices the
            # next backfill. See ptm/cost.py:reconcile.
            check = cost.reconcile(ledger, int(ledger.get("prompt_chars") or 0))
            if check["measured"]:
                print(f"measured USD {check['actual_cost_usd']:.4f} against an estimated "
                      f"USD {check['estimated_cost_usd']:.4f} "
                      f"({check['cost_error']:+.1%} out)"
                      + (f"; these prompts ran at {check['implied_chars_per_token']} "
                         f"characters per token, not the "
                         f"{check['assumed_chars_per_token']} the estimate assumes"
                         if check["implied_chars_per_token"] else ""))
            return {**summary, **ledger, "segments": segments,
                    "cache_hits": cache_hits, "estimated_saved_usd": cache_saved,
                    "cost_check": check,
                    "disparity": [f.model_dump(mode="json") for f in findings]}

        items = prepare()
        read_the_policy() >> items

        # Everything already answered is taken out of the fan-out here, so the
        # judge below - offline or not - only ever sees cases nobody has asked
        # about yet. The second measurement of an edited clause is the one this
        # is for: most of the history is untouched by the edit and its verdicts
        # are still valid, because the key is the prompt and the prompt did not
        # change for those cases.
        candidate_misses = to_judge(items)
        baseline_case_items = baseline_items(items)
        baseline_misses = to_judge.override(task_id="to_judge_baseline")(
            baseline_case_items, "baseline_cache_key")

        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            @task(max_active_tis_per_dag=8)
            def judge_baseline_offline(item: dict, **ctx) -> dict:
                """The same case under the policy already in force."""
                version = (ctx["params"]["baseline_version"] or "").strip()
                return offline_verdict(_case(item), domain, version).model_dump()

            verdicts = judge_offline.expand(item=candidate_misses)
            baseline_verdicts = judge_baseline_offline.expand(item=baseline_misses)
        else:
            verdicts = LLMOperator.partial(
                task_id="judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                # A runaway judge on a 600-case backfill is a real bill, so cap
                # every task rather than trusting the prompt.
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=prompts(candidate_misses)).output
            baseline_verdicts = LLMOperator.partial(
                task_id="judge_baseline",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=baseline_prompts(baseline_misses)).output

        candidate = merge(items, candidate_misses, verdicts,
                          judge_task_id="" if OFFLINE else "judge")
        baseline_pass = merge.override(task_id="merge_baseline")(
            baseline_case_items, baseline_misses, baseline_verdicts,
            "baseline_cache_key", "baseline_prompt_chars",
            judge_task_id="" if OFFLINE else "judge_baseline")

        @task(outlets=[flips_asset])
        def publish(summary: dict) -> dict:
            """Emitting the asset is what wakes the adjudication DAG."""
            return summary

        publish(reconcile(items, candidate, baseline_pass))

    replay()
