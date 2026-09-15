"""Policy Time Machine - five DAGs per domain, generated from include/domains/*.yaml.

Drop a new YAML in and Airflow grows a new set of DAGs on the next parse. The
DAG code below contains no domain knowledge at all.

    replay_<domain>          @monthly, backfilled across history.
                             Reads the policy before spending anything on it,
                             then replays every real decision in its data
                             interval under it, point-in-time correct.
                             Attributes every change to the clause that caused
                             it, breaks the blast radius down by segment, says
                             which segments carry more of it than the rest of
                             their field, and records what the judging cost -
                             net of everything served from cache. Emits the
                             flips asset.

    adjudicate_<domain>      Triggered by the flips asset. Puts the handful of
                             genuinely contested flips in front of a human via
                             HITL, and turns their rulings into precedent.
                             Emits the precedents asset.

    precedent_gate_<domain>  Triggered by the precedents asset. Re-judges every
                             established precedent under the candidate policy
                             and FAILS if the policy would reverse one. This is
                             the regression suite for organisational judgment.
                             Also warns when two human rulings contradict each
                             other, which would make that suite unsatisfiable.

    judge_stability_<domain> Manual. Judges the same cases repeatedly under the
                             same policy to measure how often the judge
                             contradicts itself - the error bar on every flip
                             rate the other DAGs report. Deliberately the one
                             DAG that never reads the verdict cache: serving a
                             repeat judgement from cache would report a judge
                             that never contradicts itself, which is not a
                             clean bill of health but a broken instrument.

    propose_<domain>         Manual. Reads everything the pipeline measured and
                             drafts the next version of the policy, then puts
                             that draft through the regression suite that
                             guards every other version. The only DAG here
                             where a model writes rather than judges, and the
                             gate at the end of it is why that is allowed.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Asset, Param, dag, task

from ptm import (
    cache,
    calibration,
    cost,
    crosscheck,
    diff,
    disparity,
    preflight,
    proposal,
    prune,
    rules,
    stability,
    store,
)
from ptm.config import JUDGE_MODEL, LLM_CONN_ID, OFFLINE, available_domains, load_domain
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Case, PolicyPatch, Precedent, RuleSet, Verdict

if not OFFLINE:
    from pydantic_ai.usage import UsageLimits

    from ptm.metered import USAGE_KEY, metered_operator

    # The provider's LLMOperator with the vendor's own token counts kept on a
    # second XCom key. Same operator, same return value; see ptm/metered.py for
    # why it wraps the hook rather than re-implementing execute.
    LLMOperator = metered_operator()
from airflow.providers.standard.operators.hitl import HITLOperator
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator

START = pendulum.datetime(2024, 9, 1, tz="UTC")
SYSTEM_PROMPT = (
    "You are a policy adjudicator. You apply written policy to historical cases "
    "exactly as written, without sympathy, precedent or hindsight. You are shown "
    "each case as it was recorded on the day it was decided; you must not reason "
    "about anything that happened after that date. When the policy does not "
    "settle a case, you say so with low confidence rather than inventing a rule."
)
DEFAULTS = {"owner": "policy-time-machine", "retries": 1}
#: What answered a prompt, for the cache key and the ledger. The offline judge
#: is a different answerer from any model, and serving one's verdict as the
#: other's would make a comparison between them agree with itself perfectly.
JUDGE_ID = JUDGE_MODEL if not OFFLINE else "offline"


def _as_verdict(raw) -> Verdict:
    """LLMOperator pushes the pydantic model to XCom; offline pushes a dict."""
    if isinstance(raw, Verdict):
        return raw
    if isinstance(raw, dict):
        return Verdict(**raw)
    return Verdict.model_validate(raw)


def _case(item: dict) -> Case:
    return Case(
        case_id=item["case_id"],
        domain=item["domain"],
        decided_at=pendulum.parse(item["decided_at"]),
        payload=item["payload"],
        actual_outcome=item["actual_outcome"],
        actual_rationale=item.get("actual_rationale", ""),
    )


def _item(case: Case, domain, version: str, baseline_version: str = "",
          cacheable: bool = True) -> dict:
    """One case as it travels through XCom.

    Note what is *absent*: the rendered prompt. Every prompt embeds the whole
    policy text, so carrying prompts here would duplicate the policy once per
    case through every downstream task - and the offline judge never reads them
    at all. What survives is the *size*, which the cost ledger needs, and the
    *hash*, which is how the cache recognises a question already answered. Both
    are derived from the prompt here and neither is the prompt.

    ``cacheable=False`` leaves the key off, which is how the stability fan-out
    opts out: judging the same prompt repeatedly is the measurement, and serving
    the second one from cache would report a judge with no noise floor.
    """
    prompt = build_prompt(case, domain, version)
    item = {
        "case_id": case.case_id,
        "domain": case.domain,
        "decided_at": case.decided_at.isoformat(),
        "payload": case.payload,
        "actual_outcome": case.actual_outcome,
        "actual_rationale": case.actual_rationale,
        "prompt_chars": len(prompt),
        "baseline_prompt_chars": 0,
    }
    if cacheable:
        item["cache_key"] = cache.key(prompt, JUDGE_ID)
    if baseline_version:
        # A baseline pass judges every case twice. Its prompt is sized and keyed
        # separately rather than added to the candidate's, because the two
        # passes hit the cache independently: editing the candidate policy does
        # not change the in-force one, so its half is served from cache and the
        # ledger has to be able to say so.
        baseline_prompt = build_prompt(case, domain, baseline_version)
        item["baseline_prompt_chars"] = len(baseline_prompt)
        if cacheable:
            item["baseline_cache_key"] = cache.key(baseline_prompt, JUDGE_ID)
    return item


def _ledger(items: list[dict], chars_field: str = "prompt_chars",
            usage: list[dict] | None = None) -> dict:
    """Price one pass over ``items`` - the cases actually sent to the judge.

    Offline the answer is genuinely zero, and the ledger says so rather than
    quoting a counterfactual as though money had moved. The forecast of what a
    real judge *would* have cost lives on the plugin's /api/cost endpoint, where
    it is clearly labelled as a forecast.

    ``usage`` is what the judge tasks actually reported, when they were metered.
    It is added to the estimate rather than replacing it: keeping both is what
    turns "4 characters per token" from an assumption into something the next
    run can be told it got wrong. See :func:`ptm.cost.reconcile`.
    """
    if OFFLINE:
        return cost.zero()
    prompt_chars = sum(int(i.get(chars_field) or 0) for i in items)
    ledger = cost.estimate(prompt_chars, len(items), JUDGE_MODEL)
    ledger["prompt_chars"] = prompt_chars
    if usage:
        ledger.update(cost.from_usage(usage, JUDGE_MODEL))
    return ledger


def _add_ledgers(*ledgers: dict) -> dict:
    """One bill from several passes. Offline they are all zero and stay zero."""
    total = cost.zero(JUDGE_ID)
    total.update({"actual_requests": 0, "actual_input_tokens": 0,
                  "actual_output_tokens": 0, "actual_cost_usd": 0.0, "prompt_chars": 0})
    for ledger in ledgers:
        for field in ("estimated_requests", "estimated_input_tokens",
                      "estimated_output_tokens", "actual_requests",
                      "actual_input_tokens", "actual_output_tokens", "prompt_chars"):
            total[field] += int(ledger.get(field) or 0)
        for field in ("estimated_cost_usd", "actual_cost_usd"):
            total[field] = round(total[field] + float(ledger.get(field) or 0), 4)
    return total


@task
def to_judge(items: list[dict], key_field: str = "cache_key") -> list[dict]:
    """The cases in a fan-out that nobody has answered yet.

    A plain list, because that is what a mapped task can expand over: Airflow
    refuses to map over one key of a multiple-output task. So the answers the
    cache already holds are fetched again by :func:`merge` rather than returned
    alongside the misses here - which also means this read must not count as a
    hit, because ``merge`` is where a verdict is actually served.

    Defined once at module level and called by both the replay and the gate,
    because they have the same problem: the gate re-judges the same handful of
    precedent cases every time a ruling is recorded, and the candidate policy
    has usually not changed between two of those runs.
    """
    misses, hits = cache.split(items, key_field, count=False)
    print(cache.describe(len(hits), len(misses)))
    return misses


def _measured_usage(judge_task_id: str) -> list[dict]:
    """What the judge tasks reported spending, if anything did.

    A mapped task's XComs come back as a list, one per map index. Pulled by key
    rather than from the return value because the return value is a verdict that
    downstream zips positionally against a case - wrapping it to carry the
    usage figures alongside would make every consumer unwrap it.

    Best-effort on purpose. This is accounting running inside a paid task, and a
    usage key that is absent because the provider changed, or because the judge
    was the offline one, must cost the *measurement* and never the run.
    """
    if OFFLINE or not judge_task_id:
        return []
    try:
        from airflow.sdk import get_current_context

        pulled = get_current_context()["ti"].xcom_pull(
            task_ids=judge_task_id, key=USAGE_KEY)
    except Exception as exc:
        print(f"no usage reported by {judge_task_id}: {exc}")
        return []
    if pulled is None:
        return []
    if isinstance(pulled, dict):
        return [pulled]
    return [row for row in pulled if isinstance(row, dict)]


# none_failed, and on the task rather than at the call sites. A fan-out with
# nothing in it - every case already answered - expands to zero mapped instances
# and Airflow marks the judge *skipped*, which under the default all_success
# rule skips this too and leaves the run with no verdicts at all. That is not a
# corner case, it is the steady state of both callers: the gate re-asks the same
# questions about the same precedents every time a ruling is recorded, and a
# replay re-run after an edit that reaches none of its cases asks nothing new
# either. ptm.selftest prints the state as the headline win - "600 of 600
# verdicts come from cache, 0 would be judged again" - and it was the one thing
# this DAG could not survive.
#
# Declared here so a future call site cannot forget it. merge itself already
# handles the empty pass correctly: no misses and no fresh verdicts is a length
# match of zero, and every case is then served from the cache below.
@task(trigger_rule="none_failed")
def merge(items: list[dict], misses: list[dict], fresh: list,
          key_field: str = "cache_key", chars_field: str = "prompt_chars",
          judge_task_id: str = "") -> dict:
    """Fresh verdicts for the cases judged, cached verdicts for the rest, in order.

    Restoring the one-to-one alignment between ``items`` and verdicts is the
    whole job. Everything downstream zips the two positionally and refuses a
    length mismatch, and that check is only meaningful if a cache hit puts a
    verdict back exactly where the case it answers sits.
    """
    fresh = list(fresh or [])
    if len(fresh) != len(misses):
        raise AirflowFailException(
            f"Judge returned {len(fresh)} verdict(s) for {len(misses)} case(s) sent to "
            f"it; refusing to merge a partial pass with cached results.")
    judged = {m["case_id"]: _as_verdict(v) for m, v in zip(misses, fresh)}

    if judged:
        version = _version_from_context(
            "baseline_version" if key_field != "cache_key" else "policy_version")
        by_key = {m["case_id"]: (m.get(key_field, ""), int(m.get(chars_field) or 0))
                  for m in misses}
        cache.remember(
            items[0]["domain"] if items else "", version, JUDGE_ID,
            {case_id: (by_key[case_id][0], by_key[case_id][1], verdict)
             for case_id, verdict in judged.items() if by_key.get(case_id, ("",))[0]})

    # Looked up here rather than carried from the task above, and after the
    # fresh verdicts are stored: a case answered by a concurrent run in the
    # meantime is a hit like any other, because the key is the prompt and the
    # prompt is identical.
    rest = [i for i in items if i["case_id"] not in judged]
    cached = cache.lookup([i[key_field] for i in rest if i.get(key_field)])
    by_case = {i["case_id"]: cached[i[key_field]] for i in rest
               if i.get(key_field) and i[key_field] in cached}

    out, hits = [], []
    for item in items:
        case_id = item["case_id"]
        if case_id in judged:
            out.append(judged[case_id].model_dump())
        elif case_id in by_case:
            out.append(by_case[case_id].model_dump())
            hits.append(item)
        else:
            raise AirflowFailException(
                f"{case_id} was neither judged nor found in the cache. A replay missing "
                f"a verdict would report a case as unchanged, which is indistinguishable "
                f"from a case the policy agrees with.")
    return {"verdicts": out,
            "ledger": _ledger(misses, chars_field, _measured_usage(judge_task_id)),
            **cache.saving(hits, JUDGE_ID, chars_field)}


def build(domain_name: str) -> None:
    domain = load_domain(domain_name)
    flips_asset = Asset(f"ptm://{domain_name}/flips")
    precedents_asset = Asset(f"ptm://{domain_name}/precedents")
    policy_param = Param("v2", type="string", title="Policy version to test",
                         description=f"One of: {', '.join(sorted(domain.policies))}")

    # ------------------------------------------------------------------ replay
    @dag(
        dag_id=f"replay_{domain_name}",
        schedule="@monthly",
        start_date=START,
        catchup=False,
        max_active_runs=4,
        default_args=DEFAULTS,
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
            run_id = ctx["run_id"]
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
                              baseline_verdicts=baseline)

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

    # -------------------------------------------------------------- adjudicate
    @dag(
        dag_id=f"adjudicate_{domain_name}",
        schedule=[flips_asset],
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULTS,
        params={
            "policy_version": policy_param,
            # Two queues, one operator. "flips" is what the asset triggers and
            # what the replay produces. "stale" is the other half of the same
            # job and had nowhere to go: the gate has always warned that a
            # ruling was made about a clause since rewritten, and enforced it
            # anyway, and the README has always said re-adjudicating is how
            # that stops being a guess. Nothing could do it.
            "target": Param(
                "flips", type="string", enum=["flips", "stale"],
                title="What to put in front of a human",
                description="flips: contested changes this policy causes. stale: rulings "
                            "made about a clause that has since been rewritten, re-asked "
                            "against the policy as it now reads."),
            "max_reviews": Param(
                0, type="integer", minimum=0,
                title="Cap on the queue (0 = the domain's review policy)",
                description="Only read for target=stale; the flip queue is sized by the "
                            "domain's review block."),
        },
        tags=["policy-time-machine", domain_name, "human-in-the-loop"],
        doc_md=(
            "Ask a human to settle only the contested cases, and keep their answers "
            "forever.\n\n"
            "`target=flips` (what the flips asset triggers) queues the changes this "
            "policy causes. `target=stale` queues the opposite problem: rulings already "
            "on file that were made about a clause the policy has since rewritten. The "
            "gate enforces those exactly as hard as a ruling made this morning, so "
            "re-confirming one is the only thing that turns it back into evidence - and "
            "the ruling it replaces is archived, never overwritten."
        ),
    )
    def adjudicate():
        def _stale_queue(version: str, cap: int) -> list[dict]:
            """Rulings that are no longer about the text they were made about.

            Read entirely from what is already on file - the precedents, the
            cases, and whatever the gate last stored for this version - so
            queueing these costs nothing. A case the candidate has never been
            judged on is skipped rather than judged here: asking a reviewer to
            re-confirm a ruling against a policy nothing has applied is asking
            them to guess, and the fix is to run the gate.
            """
            precedents = store.load_precedents(domain_name)
            stale = diff.stale_precedents(precedents, domain, version)
            if not stale:
                print(diff.describe_readjudication([], version))
                return []
            cases = store.load_cases(domain_name, until=pendulum.now("UTC"),
                                     case_ids=[r["case_id"] for r in stale])
            stored = store.latest_verdicts(domain_name, version)
            verdicts = {
                case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                                 confidence=row["confidence"],
                                 policy_clause=row["policy_clause"] or "")
                for case_id, row in stored.items()
            }
            items = diff.stale_review_items(
                stale, precedents, cases, verdicts, domain,
                limit=cap or domain.review.max_reviews)
            print(diff.describe_readjudication(items, version))
            unjudged = {r["case_id"] for r in stale} - set(verdicts)
            if unjudged:
                print(f"{len(unjudged)} stale ruling(s) not queued because {version} has "
                      f"no verdict on file for them: {sorted(unjudged)[:10]}. Run "
                      f"precedent_gate_{domain_name} under {version} first.")
            return items

        @task
        def contested(**ctx) -> list[dict]:
            """The few cases worth a human's attention, from whichever queue is asked for.

            Both targets return the same shape, so everything downstream - the
            HITL fan-out, the rendering, the recording of precedent - is one
            path. A re-adjudication carries an extra ``readjudication`` block
            that the body renders and nothing else has to know about.
            """
            version = ctx["params"]["policy_version"]
            if (ctx["params"].get("target") or "flips").strip() == "stale":
                return _stale_queue(version, int(ctx["params"].get("max_reviews") or 0))
            rows = store.flips_for_policy(domain_name, version)
            import json as _json
            flips = [
                diff.Flip(
                    case_id=r["case_id"], decided_at=pendulum.parse(r["decided_at"]),
                    actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
                    rationale=r["rationale"], confidence=r["confidence"],
                    policy_clause=r["policy_clause"] or "", impact=r["impact"],
                    payload=_json.loads(r["payload"]), direction=r["direction"],
                    segments=_json.loads(r["segments"] or "{}"),
                    attribution=r["attribution"] or "",
                    baseline_outcome=r["baseline_outcome"] or "",
                    stability=r.get("stability") or "",
                )
                for r in rows if not r["reviewed"]
            ]
            return [f.model_dump(mode="json") for f in diff.select_for_review(flips, domain)]

        @task
        def unconfirmed(**ctx) -> list[dict]:
            """Flips a confirmation pass could not reproduce, reported not queued.

            Keeping them out of the queue is only half the job; saying so is
            the other half, because a flip silently dropped looks exactly like
            a flip that never happened.
            """
            version = ctx["params"]["policy_version"]
            measured = store.flip_stability(domain_name, version)
            shaky = [r for r in measured.values() if not r["stable"]]
            for r in sorted(shaky, key=lambda r: r["agreement"]):
                print(f"held back {r['case_id']}: re-judging gave {r['outcomes']}, "
                      f"the replay recorded '{r['recorded_outcome']}'")
            print(f"{len(shaky)} flip(s) held back as unconfirmed out of "
                  f"{len(measured)} measured")
            return [dict(r) for r in shaky]

        @task
        def subjects(flips: list[dict]) -> list[str]:
            out = []
            for f in flips:
                again = f.get("readjudication")
                if again:
                    out.append(f"{f['case_id']}: does '{again['precedent_outcome']}' still "
                               f"hold, now that clause {again['clause'] or '?'} reads "
                               f"differently?")
                else:
                    out.append(f"{f['case_id']}: should this have been "
                               f"'{f['new_outcome']}' rather than '{f['actual_outcome']}'?")
            return out

        @task
        def bodies(flips: list[dict]) -> list[str]:
            out = []
            for f in flips:
                clause = f.get("attribution") or (
                    f"clause {f['policy_clause']}" if f.get("policy_clause") else "")
                body = (
                    f"### The case, as decided on {f['decided_at'][:10]}\n\n"
                    f"```\n{domain.render_case(f['payload'])}```\n\n"
                    f"**What actually happened:** `{f['actual_outcome']}`\n\n"
                    f"**What the proposed policy gives:** `{f['new_outcome']}` "
                    f"(confidence {f['confidence']:.0%}, {domain.impact_unit} {f['impact']:,.2f} at stake)\n\n"
                    # Telling the reviewer which clause drove the change lets them
                    # argue with the rule rather than just the result.
                    + (f"**Driven by:** `{clause}`\n\n" if clause else "")
                    + f"> {f['rationale']}\n\n"
                )
                again = f.get("readjudication")
                if not again:
                    out.append(body + (
                        "Pick the outcome that is *actually* correct for this case. Your "
                        "answer becomes a permanent precedent that every future policy "
                        "change is tested against."))
                    continue
                # A re-adjudication is a different question and has to look like
                # one. The reviewer is not settling a case, they are deciding
                # whether somebody else's answer survives a rewrite - which they
                # cannot do without seeing that answer, the reason given for it,
                # and both versions of the sentence it was about.
                body += (
                    f"---\n\n### This case has already been ruled on\n\n"
                    f"**{again['ruled_by']}** ruled `{again['precedent_outcome']}` on "
                    f"{again['ruled_at']}"
                    + (f", against policy {again['ruled_under']}" if again["ruled_under"] else "")
                    + ".\n\n"
                    + (f"> {again['note']}\n\n" if again["note"]
                       else "_No reason was recorded with that ruling._\n\n")
                    + f"**Why you are being asked again:** {again['detail']}\n\n")
                if again["was"] and again["now"]:
                    body += (f"**Clause {again['clause']} then:**\n\n> {again['was']}\n\n"
                             f"**Clause {again['clause']} now:**\n\n> {again['now']}\n\n")
                out.append(body + (
                    "Pick the outcome that is correct under the policy **as it now reads**. "
                    "Confirming the earlier answer is a real and useful result - it turns a "
                    "ruling the gate was enforcing on trust into one that has been checked. "
                    "The earlier ruling is kept either way."))
            return out

        flips = contested()
        held_back = unconfirmed()

        reviews = HITLOperator.partial(
            task_id="review",
            options=domain.outcomes,
            defaults=[domain.outcomes[0]],
            # The reviewer's reasoning, not just their answer. `record` below
            # has always read this out of `params_input`, but nothing ever
            # declared the parameter, so every precedent on file carries an
            # empty note - including the ones ptm.proposal shows a drafter
            # under the heading "their note". An outcome with no reason behind
            # it is the one thing a permanent record cannot afford to lose:
            # it is what a second reviewer needs to settle a conflict, and what
            # tells a future reader whether a ruling still applies.
            params={"note": Param(
                "", type="string", title="Why is this the correct outcome?",
                description="Your reasoning, in a sentence or two. It is kept with the "
                            "ruling forever, shown beside any ruling that contradicts "
                            "this one, and given to the drafter that writes the next "
                            "version of the policy.")},
            task_display_name="Adjudicate contested case",
        ).expand(subject=subjects(flips), body=bodies(flips))

        @task(outlets=[precedents_asset], trigger_rule="all_done")
        def record(flips: list[dict], responses: list, held_back: list[dict],
                   **ctx) -> dict:
            """Turn human answers into precedent. This is the only durable output.

            ``all_done`` lets this run even when a review task failed or timed
            out - but a short response list would then be zipped against the
            full flip list positionally and file one reviewer's ruling against
            somebody else's case. Precedent is the only thing here that cannot
            be recomputed, so a mismatch refuses rather than guesses.
            """
            responses = list(responses or [])
            if len(responses) != len(flips):
                raise AirflowFailException(
                    f"{len(responses)} review response(s) for {len(flips)} contested "
                    f"flip(s). Responses are matched to cases by position, so recording "
                    f"a partial set would attribute a ruling to the wrong case. Re-run "
                    f"the failed review task(s) instead."
                )
            version = ctx["params"]["policy_version"]
            saved, reconfirmed, revised = [], [], []
            for f, resp in zip(flips, responses):
                chosen = (resp or {}).get("chosen_options") or []
                if not chosen:
                    continue
                # A re-adjudication replaces a ruling rather than making a first
                # one, and which of those happened is the result: a reviewer
                # confirming the earlier answer has turned a ruling the gate was
                # enforcing on trust into one that has been checked, and a
                # reviewer changing it has moved the regression suite.
                again = f.get("readjudication")
                if again:
                    (reconfirmed if chosen[0] == again["precedent_outcome"]
                     else revised).append(f["case_id"])
                store.save_precedent(Precedent(
                    case_id=f["case_id"], domain=domain_name, correct_outcome=chosen[0],
                    ruled_by=(resp.get("user_id") or "unknown"),
                    note=(resp.get("params_input") or {}).get("note", ""),
                    established_at=pendulum.now("UTC"), established_by_run=ctx["run_id"],
                    # The circumstances, not just the answer. Which policy the
                    # reviewer was shown and what it gave for this case is what
                    # makes the ruling re-readable later: a precedent the gate
                    # enforces forever, against a clause that has since been
                    # rewritten, is a fact about a sentence nobody can find.
                    policy_version=version,
                    judged_outcome=f.get("new_outcome", ""),
                    judged_clause=f.get("policy_clause", ""),
                ))
                saved.append(f["case_id"])
            store.mark_reviewed(domain_name, version, saved)
            if reconfirmed or revised:
                print(f"{len(reconfirmed)} ruling(s) re-confirmed against {version}, "
                      f"{len(revised)} changed. Each earlier ruling is archived, not "
                      f"replaced - store.precedent_history has what it said.")
                for case_id in revised:
                    print(f"  {case_id}: the ruling on file has been superseded")
            return {"precedents_recorded": len(saved), "case_ids": saved,
                    "held_back_unconfirmed": len(held_back),
                    "reconfirmed": reconfirmed, "revised": revised}

        record(flips, reviews.output, held_back)

    adjudicate()

    # ----------------------------------------------------------- precedent gate
    @dag(
        dag_id=f"precedent_gate_{domain_name}",
        schedule=[precedents_asset],
        start_date=START,
        catchup=False,
        default_args=DEFAULTS,
        params={
            "policy_version": policy_param,
            "baseline_version": Param(
                domain.in_force, type=["string", "null"],
                title="Policy in force (blank to skip)",
                description="Judged alongside the candidate so a reversal the status quo "
                            "already makes is not reported as this proposal's doing."),
        },
        tags=["policy-time-machine", domain_name, "regression"],
        doc_md="Fails if the candidate policy would reverse a ruling a human already made.",
    )
    def precedent_gate():
        @task
        def precedent_cases(**ctx) -> list[dict]:
            """Every case a human has ruled on - by id, and all of them.

            Loading the whole history and filtering would be subject to
            ``load_cases``'s default limit, so past that many cases the gate
            would quietly judge a subset of the precedent set and pass. A
            regression suite that silently checks less than it reports is
            worse than no regression suite, so a missing case fails the run.
            """
            version = ctx["params"]["policy_version"]
            baseline_version = (ctx["params"].get("baseline_version") or "").strip()
            precedents = store.load_precedents(domain_name)
            ids = [p.case_id for p in precedents]
            cases = store.load_cases(domain_name, until=pendulum.now("UTC"), case_ids=ids)
            missing = sorted(set(ids) - {c.case_id for c in cases})
            if missing:
                raise AirflowFailException(
                    f"{len(missing)} precedent(s) have no case on file and cannot be "
                    f"re-judged: {missing[:10]}. The gate refuses to report a pass it "
                    f"did not actually check."
                )
            print(f"re-judging all {len(cases)} precedent case(s) under policy {version}"
                  + (f", and under {baseline_version} for comparison" if baseline_version else ""))
            return [_item(c, domain, version, baseline_version) for c in cases]

        @task
        def gate_baseline_items(items: list[dict], **ctx) -> list[dict]:
            """The same precedent cases under the policy in force, or none of them."""
            return items if (ctx["params"].get("baseline_version") or "").strip() else []

        @task
        def gate_baseline_prompts(items: list[dict]) -> list[str]:
            version = _version_from_context("baseline_version")
            return [build_prompt(_case(i), domain, version) for i in items]

        @task
        def gate_prompts(items: list[dict]) -> list[str]:
            version = _version_from_context()
            return [build_prompt(_case(i), domain, version) for i in items]

        @task
        def conflicts() -> list[dict]:
            """Warn where two human rulings contradict each other.

            Not a failure: the policy has done nothing wrong, two reviewers have.
            But a self-contradictory precedent set makes the gate below
            unsatisfiable - some policy must always reverse one of them - so it
            needs to be visible rather than mysterious.
            """
            found = diff.precedent_conflicts(store.precedents_with_payload(domain_name), domain)
            if not domain.conflicts.key:
                print("conflict detection disabled: domain declares no conflicts.key")
            for c in found:
                where = ", ".join(f"{k}={v}" for k, v in c.signature)
                print(f"CONFLICT on [{where}]: "
                      + "; ".join(f"{o} ({', '.join(ids)})" for o, ids in c.outcomes.items())
                      + f" - ruled by {', '.join(c.ruled_by)}")
            print(f"{len(found)} precedent conflict(s)")
            return [c.model_dump(mode="json") for c in found]

        cases = precedent_cases()
        # The gate fires every time a ruling is recorded, and re-asks the same
        # questions about the same handful of cases. Unless the candidate policy
        # changed between two runs, every one of those questions has an answer
        # on file already.
        gate_misses = to_judge.override(task_id="gate_to_judge")(cases)
        gate_baseline_case_items = gate_baseline_items(cases)
        gate_baseline_misses = to_judge.override(task_id="gate_to_judge_baseline")(
            gate_baseline_case_items, "baseline_cache_key")

        if OFFLINE:
            @task
            def gate_judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            @task
            def gate_baseline_offline(item: dict, **ctx) -> dict:
                version = (ctx["params"].get("baseline_version") or "").strip()
                return offline_verdict(_case(item), domain, version).model_dump()

            gate_verdicts = gate_judge_offline.expand(item=gate_misses)
            gate_baseline_verdicts = gate_baseline_offline.expand(
                item=gate_baseline_misses)
        else:
            gate_verdicts = LLMOperator.partial(
                task_id="gate_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=gate_prompts(gate_misses)).output
            gate_baseline_verdicts = LLMOperator.partial(
                task_id="gate_judge_baseline",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=gate_baseline_prompts(gate_baseline_misses)).output

        gate_candidate = merge.override(task_id="gate_merge")(
            cases, gate_misses, gate_verdicts,
            judge_task_id="" if OFFLINE else "gate_judge")
        gate_baseline = merge.override(task_id="gate_merge_baseline")(
            gate_baseline_case_items, gate_baseline_misses, gate_baseline_verdicts,
            "baseline_cache_key", "baseline_prompt_chars",
            judge_task_id="" if OFFLINE else "gate_judge_baseline")

        @task(trigger_rule="none_failed")
        def enforce(items: list[dict], candidate: dict, found_conflicts: list[dict],
                    baseline_pass: dict | None = None, **ctx) -> dict:
            """Fail the run if the candidate policy reverses a human ruling."""
            version = ctx["params"]["policy_version"]
            verdicts = (candidate or {}).get("verdicts") or []
            baseline_verdicts = (baseline_pass or {}).get("verdicts") or []
            if len(verdicts) != len(items):
                raise AirflowFailException(
                    f"Judge returned {len(verdicts)} verdicts for {len(items)} precedents; refusing partial gate."
                )
            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            for verdict in by_case.values():
                domain.validate_outcome(verdict.outcome)
            precedents = store.load_precedents(domain_name)
            violations = diff.precedent_violations(by_case, precedents)

            # Before trusting the oracle, ask whether it is still about this
            # policy. A ruling made against a clause that has since been
            # rewritten is enforced here exactly as hard as one made this
            # morning, and nothing else in the pipeline would ever say so.
            stale = diff.stale_precedents(precedents, domain, version)
            print(diff.describe_stale(stale, version))
            # Stored so the dashboard can show the gate's answer without paying
            # to judge these cases all over again.
            store.save_verdicts(ctx["run_id"], domain_name, version, by_case)

            # The gate asks whether the policy agrees with the humans. The same
            # verdicts answer a question nothing else in this pipeline asks:
            # whether the *judge* does. Stability says the judge repeats itself;
            # only this says whether repeating itself is worth anything - and the
            # confidence it reports is what routes the review budget.
            scored = calibration.score(domain, version, by_case, precedents)
            print(calibration.describe(scored))
            # And now something acts on it. Offline the verdicts came from
            # offline_rules, so the figure describes the fixture and the gate is
            # held off - the same exemption the rules gate takes, for the same
            # reason. See ptm/calibration.py:gate.
            judge_problems: list[str] = [] if OFFLINE else calibration.gate(scored, domain)
            for problem in judge_problems:
                print(f"JUDGE GATE  {problem}")
            if judge_problems and domain.calibration.gate == "fail":
                raise AirflowFailException(
                    f"the judge scored against {scored.judged} human ruling(s) under "
                    f"{version} is outside what this domain allows, and "
                    f"calibration.gate=fail:\n"
                    + "\n".join(f"  - {problem}" for problem in judge_problems)
                    + "\n\nThis is not a finding about policy " + version + ". It says "
                      "the verdicts every other number here is built from cannot be "
                      "relied on yet, so passing the precedent check would not have "
                      "meant much.")
            elif judge_problems:
                print("calibration.gate=warn, so the run continues - but the flip rate "
                      "above is this judge's, and this is what that judge is worth.")

            # The same question asked of the policy already in force. Without
            # it, "this proposal reverses 3 rulings" reads as the proposal's
            # fault even when the status quo reverses the same 3.
            baseline_version = (ctx["params"].get("baseline_version") or "").strip()
            pre_existing: set[str] = set()
            if baseline_version and baseline_verdicts:
                if len(baseline_verdicts) != len(items):
                    raise AirflowFailException(
                        f"Baseline judge returned {len(baseline_verdicts)} verdicts for "
                        f"{len(items)} precedents; refusing to separate pre-existing "
                        f"reversals from a partial baseline."
                    )
                base = {i["case_id"]: _as_verdict(v) for i, v in zip(items, baseline_verdicts)}
                for verdict in base.values():
                    domain.validate_outcome(verdict.outcome)
                store.save_verdicts(ctx["run_id"] + store.BASELINE_RUN_SUFFIX,
                                    domain_name, baseline_version, base)
                pre_existing = {v["case_id"]
                                for v in diff.precedent_violations(base, precedents)}
                print(f"policy {baseline_version}, in force today, reverses "
                      f"{len(pre_existing)} of the same {len(precedents)} precedent(s)")

            if violations:
                introduced = [v for v in violations if v["case_id"] not in pre_existing]
                lines = "\n".join(
                    f"  - {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}' on "
                    f"{v['established_at']}, policy {version} gives '{v['proposed_outcome']}'"
                    + ("   (policy " + baseline_version + " reverses it too)"
                       if v["case_id"] in pre_existing else "")
                    for v in violations
                )
                hint = ("\nNote: the precedent set also contains "
                        f"{len(found_conflicts)} internal conflict(s), so some violation here may "
                        "be unavoidable until two humans agree with each other."
                        ) if found_conflicts else ""
                shaky = {r["case_id"] for r in stale} & {v["case_id"] for v in violations}
                if shaky:
                    hint += (f"\n{len(shaky)} of these reverse a ruling made about a clause "
                             f"that has since changed ({sorted(shaky)[:5]}); re-adjudicating "
                             f"those is a different fix from editing the policy.")
                if pre_existing:
                    hint += (f"\n{len(introduced)} of these are introduced by {version}; "
                             f"{len(violations) - len(introduced)} are reversals the policy "
                             f"in force already makes, so fixing them is a separate job "
                             f"from this proposal.")
                raise AirflowFailException(
                    f"Policy {version} reverses {len(violations)} established precedent(s):\n{lines}{hint}"
                )
            return {"precedents_checked": len(by_case), "violations": 0,
                    "in_force_violations": len(pre_existing),
                    "precedent_conflicts": len(found_conflicts),
                    "stale_precedents": len(stale),
                    "judge_accuracy": scored.accuracy,
                    "judge_gate_problems": judge_problems}

        enforce(cases, gate_candidate, conflicts(), gate_baseline)

    precedent_gate()

    # --------------------------------------------------------- judge stability
    @dag(
        dag_id=f"judge_stability_{domain_name}",
        schedule=None,
        start_date=START,
        catchup=False,
        default_args=DEFAULTS,
        params={
            "policy_version": policy_param,
            # Two questions share one fan-out. "sample" asks how noisy the
            # judge is in general; "flips" asks whether the specific verdicts
            # this policy is about to be judged on actually reproduce - which
            # is the one the human queue depends on.
            "target": Param("sample", type="string", enum=["sample", "flips"],
                            title="What to re-judge",
                            description="sample: cases spread across the period, for the "
                                        "judge's overall noise floor. flips: the recorded "
                                        "flips for this policy, to confirm each one before "
                                        "a human is asked to rule on it."),
            "sample_cases": Param(25, type="integer", title="Cases to sample",
                                  description="Spread across the whole period, deterministically. "
                                              "With target=flips, the number of highest-impact "
                                              "flips to confirm (0 = all of them)."),
            "samples_per_case": Param(3, type="integer", minimum=2,
                                      title="Times to judge each case"),
            "seed": Param(7, type="integer", title="Sampling seed",
                          description="Same seed, same sample - so two runs are comparable."),
            "max_disagreement": Param(
                1.0, type="number", minimum=0.0, maximum=1.0,
                title="Fail above this disagreement rate",
                description="1.0 measures without gating. Lower it to refuse to trust a noisy judge."),
            # A second, independent judge on the same cases. Self-consistency is
            # satisfied completely by a judge that misreads a clause the same way
            # every time; two judges misreading it the same way is a far smaller
            # coincidence. Blank skips the pass entirely, so it costs nothing
            # unless asked for.
            "compare_model": Param(
                "", type=["string", "null"], title="Second judge (blank to skip)",
                description="A pydantic-ai model identifier, e.g. 'anthropic:claude-haiku-4-5'. "
                            "Judged through the same connection with the model overridden, so "
                            "no second connection is needed. Cases the two judges split on are "
                            "cases the policy does not settle."),
        },
        tags=["policy-time-machine", domain_name, "stability"],
        doc_md=(
            "Judge the same cases repeatedly under the **same** policy and count how "
            "often the judge contradicts itself.\n\n"
            "This is the error bar on every flip rate the other DAGs report: a "
            "disagreement rate of 6% against a measured 24% flip rate means up to a "
            "quarter of the change you are looking at is the model, not the policy.\n\n"
            "With `PTM_OFFLINE=1` the judge is deterministic and this necessarily "
            "reports 0%. That is not a clean bill of health - it means the check is "
            "inert until you point it at a real model."
        ),
    )
    def judge_stability():
        @task
        def units(**ctx) -> list[dict]:
            """One row per (case, repeat): the fan-out this DAG measures.

            With ``target=flips`` the rows are the recorded flips for this
            policy rather than a spread of history, and the point changes with
            them: not "how noisy is this judge" but "will this particular
            verdict survive being asked again". A flip that will not is the
            model changing its mind, and turning one into permanent precedent
            writes noise into the only durable artefact this system has.
            """
            store.init_db()
            params = ctx["params"]
            version = params["policy_version"]
            repeats = max(2, int(params["samples_per_case"]))
            target = (params.get("target") or "sample").strip()
            wanted = int(params["sample_cases"])

            if target == "flips":
                rows = store.flips_for_policy(domain_name, version)
                if not rows:
                    raise AirflowFailException(
                        f"no recorded flips for {domain_name}/{version} to confirm; "
                        f"run replay_{domain_name} first")
                import json as _json
                flips = [
                    diff.Flip(
                        case_id=r["case_id"], decided_at=pendulum.parse(r["decided_at"]),
                        actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
                        rationale=r["rationale"], confidence=r["confidence"],
                        policy_clause=r["policy_clause"] or "", impact=r["impact"],
                        payload=_json.loads(r["payload"]), direction=r["direction"],
                    )
                    for r in rows
                ]
                chosen = stability.flips_to_confirm(flips, wanted)
                ids = [f.case_id for f in chosen]
                # By id, so the confirmation set is exactly the flips selected
                # rather than whatever a default limit happened to keep.
                picked = store.load_cases(domain_name, until=pendulum.now("UTC"),
                                          case_ids=ids)
                if len(picked) != len(ids):
                    raise AirflowFailException(
                        f"{len(ids) - len(picked)} flipped case(s) could not be loaded; "
                        f"refusing to report a confirmation pass over a subset.")
                recorded = {f.case_id: f.new_outcome for f in chosen}
                print(f"confirming {len(picked)} recorded flips x {repeats} judgements "
                      f"under policy {version}")
            else:
                cases = store.load_cases(domain_name, until=pendulum.now("UTC"))
                if not cases:
                    raise AirflowFailException(
                        f"no {domain_name} cases to sample; seed the history first")
                picked = stability.sample_cases(cases, wanted, seed=int(params["seed"]))
                recorded = {}
                print(f"sampling {len(picked)} cases x {repeats} judgements under policy {version}")

            # cacheable=False, and it is the only fan-out in this file that says
            # so. Every row here is the same prompt as the row before it - that
            # repetition *is* the measurement - and a cache would answer all of
            # them with the first verdict and report a judge that never
            # contradicts itself. That is not a wrong number, it is a number
            # that says the instrument is working when it is switched off.
            return [
                {**_item(c, domain, version, cacheable=False), "sample_idx": i,
                 "recorded_outcome": recorded.get(c.case_id, "")}
                for c in picked for i in range(repeats)
            ]

        @task
        def stability_prompts(rows: list[dict]) -> list[str]:
            version = _version_from_context()
            return [build_prompt(_case(r), domain, version) for r in rows]

        @task
        def cross_units(rows: list[dict], **ctx) -> list[dict]:
            """One row per case for the second judge - or none at all.

            Deduplicated off the stability fan-out rather than loaded again, so
            both judges are asked about exactly the same cases. The repeats are
            dropped: asking a second judge the same question three times
            measures *its* self-consistency, which is a different check and not
            the one being paid for here.
            """
            if OFFLINE or not (ctx["params"].get("compare_model") or "").strip():
                return []
            seen, out = set(), []
            for row in rows:
                if row["case_id"] in seen:
                    continue
                seen.add(row["case_id"])
                out.append(row)
            print(f"second judge {ctx['params']['compare_model']} will see "
                  f"{len(out)} case(s)")
            return out

        @task
        def cross_prompts(rows: list[dict]) -> list[str]:
            version = _version_from_context()
            return [build_prompt(_case(r), domain, version) for r in rows]

        rows = units()
        cross_rows = cross_units(rows)
        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def stability_judge_offline(row: dict, **ctx) -> dict:
                return offline_verdict(_case(row), domain, ctx["params"]["policy_version"]).model_dump()

            sampled = stability_judge_offline.expand(row=rows)

            @task
            def cross_judge_offline(row: dict) -> dict:
                """Never runs: cross_units returns nothing offline.

                Present so the graph has the same shape in both configurations -
                a task that exists only in the paid branch is a task nobody sees
                fail until they are paying.
                """
                raise AirflowFailException(
                    "the offline judge cannot stand in for a second opinion: it would "
                    "be the same rules answering twice, and a 100% agreement rate that "
                    "means nothing is worse than no cross-check at all")

            cross_verdicts = cross_judge_offline.expand(row=cross_rows)
        else:
            sampled = LLMOperator.partial(
                task_id="stability_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=stability_prompts(rows)).output
            cross_verdicts = LLMOperator.partial(
                task_id="cross_judge",
                llm_conn_id=LLM_CONN_ID,
                # The connection's model, overridden per run. model_id is a
                # template field, so the second judge is chosen at trigger time
                # without a second connection or a DAG edit.
                model_id="{{ params.compare_model }}",
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=cross_prompts(cross_rows)).output

        @task(trigger_rule="none_failed")
        def report(rows: list[dict], verdicts: list, cross_rows: list[dict],
                   cross_verdicts: list, **ctx) -> dict:
            params = ctx["params"]
            version = params["policy_version"]
            if len(verdicts) != len(rows):
                raise AirflowFailException(
                    f"Judge returned {len(verdicts)} verdicts for {len(rows)} samples; "
                    f"refusing a stability figure computed from a partial fan-out."
                )
            samples = []
            for row, raw in zip(rows, verdicts):
                v = _as_verdict(raw)
                domain.validate_outcome(v.outcome)
                samples.append({
                    "case_id": row["case_id"], "sample_idx": row["sample_idx"],
                    "outcome": v.outcome, "confidence": v.confidence,
                    "policy_clause": v.policy_clause,
                })

            repeats = max(2, int(params["samples_per_case"]))
            result = stability.analyse(samples, repeats)
            ledger = _ledger(rows, usage=_measured_usage(
                "" if OFFLINE else "stability_judge"))
            store.save_stability(ctx["run_id"], domain_name, version,
                                 samples, result.model_dump(), ledger)

            confirmed = 0
            if (params.get("target") or "sample").strip() == "flips":
                recorded = {r["case_id"]: r.get("recorded_outcome", "") for r in rows}
                confirmations = stability.confirm(samples, recorded)
                confirmed = store.save_flip_stability(domain_name, version, confirmations,
                                                      run_id=ctx["run_id"])
                print(stability.describe_confirmations(confirmations))

            # The second judge, if one ran. Scored against the primary judge's
            # first sample of each case, so both answers are one judgement of
            # the same prompt rather than a modal vote against a single call.
            cross: dict = {}
            cross_rows = list(cross_rows or [])
            cross_verdicts = list(cross_verdicts or [])
            if cross_rows and cross_verdicts:
                if len(cross_verdicts) != len(cross_rows):
                    raise AirflowFailException(
                        f"Second judge returned {len(cross_verdicts)} verdict(s) for "
                        f"{len(cross_rows)} case(s); refusing a cross-check computed "
                        f"from a partial fan-out.")
                first = {}
                for sample, row in zip(samples, rows):
                    first.setdefault(row["case_id"], Verdict(
                        outcome=sample["outcome"], rationale="",
                        confidence=sample["confidence"],
                        policy_clause=sample["policy_clause"] or ""))
                second = {}
                for row, raw in zip(cross_rows, cross_verdicts):
                    verdict = _as_verdict(raw)
                    domain.validate_outcome(verdict.outcome)
                    second[row["case_id"]] = verdict
                actual = {r["case_id"]: r["actual_outcome"] for r in cross_rows}
                report_model = crosscheck.analyse(
                    first, second, domain,
                    primary_label=JUDGE_ID,
                    secondary_label=(params.get("compare_model") or "second judge").strip(),
                    actual=actual)
                cross = report_model.model_dump(mode="json")
                store.save_cross_check(ctx["run_id"], domain_name, version, cross)
                print()
                print(crosscheck.describe(report_model))

            print(stability.describe(result))
            for row in result.unstable[:10]:
                print(f"  {row['case_id']}: {row['outcomes']} "
                      f"(agreement {row['agreement']:.0%}, "
                      f"confidence spread {row['confidence_spread']:.2f})")
            if OFFLINE:
                print("PTM_OFFLINE=1: the judge is deterministic, so 0% here measures "
                      "nothing. Set PTM_OFFLINE=0 for a real figure.")

            ceiling = float(params["max_disagreement"])
            if result.disagreement_rate > ceiling:
                raise AirflowFailException(
                    f"judge disagreed with itself on {result.disagreement_rate:.1%} of sampled "
                    f"cases, above the {ceiling:.1%} ceiling. Flip rates measured with this "
                    f"judge are not trustworthy enough to act on."
                )
            return {**result.model_dump(exclude={"unstable"}), **ledger,
                    "target": (params.get("target") or "sample").strip(),
                    "flips_confirmed": confirmed,
                    "cross_check": {k: v for k, v in cross.items()
                                    if k != "disagreements"}}

        report(rows, sampled, cross_rows, cross_verdicts)

    judge_stability()

    # ---------------------------------------------------------------- propose
    @dag(
        dag_id=f"propose_{domain_name}",
        schedule=None,
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULTS,
        params={
            "policy_version": policy_param,
            "publish": Param(
                True, type="boolean", title="Write the draft to include/drafts/",
                description="Off drafts and prints without creating a policy version. "
                            "Proposing and adopting are separate acts."),
            "replay": Param(
                False, type="boolean",
                title="Replay the draft over all of history once it is written",
                description="The gate below checks the draft against the precedent set, "
                            "which is a handful of cases. This answers what it does to "
                            "every other one - and costs a full replay to do it, which "
                            "is why it is off by default."),
        },
        tags=["policy-time-machine", domain_name, "proposal"],
        doc_md=(
            "Draft the next version of the policy from everything the pipeline "
            "measured, then put the draft through the regression suite.\n\n"
            "This is the only DAG here where a model **writes** rather than judges, "
            "and the last task is why that is allowed: the draft is re-judged "
            "against every ruling a human has made, and a draft that reverses one "
            "is reported as such however well it argues for itself.\n\n"
            "The draft lands in `include/drafts/<domain>/` and is picked up as an "
            "ordinary policy version on the next parse, so it can be replayed, "
            "swept, compared and gated like any version somebody wrote by hand. "
            "It is listed separately everywhere it appears - nothing here presents "
            "a machine's draft as approved policy.\n\n"
            "With `PTM_OFFLINE=1` the drafter is deterministic: it searches the "
            "numeric thresholds for the setting that reverses the fewest rulings. "
            "Cruder than a model, optimising exactly what the gate measures, and "
            "therefore the floor a model-backed draft has to beat."
        ),
    )
    def propose():
        @task
        def gather(**ctx) -> dict:
            """Everything measured about the policy, as the drafter will see it."""
            store.init_db()
            version = ctx["params"]["policy_version"]
            found = proposal.evidence(domain, version)
            print(f"policy {version}: {found['flips']} of {found['cases']} decisions "
                  f"change, {len(found['violations'])} human ruling(s) reversed, "
                  f"{len(found['dials'])} numeric threshold(s) to consider")
            if not found["cases"]:
                raise AirflowFailException(
                    f"nothing has been replayed under {domain_name}/{version}, so there "
                    f"is no evidence to draft from. Run replay_{domain_name} first - a "
                    f"draft argued from no measurements is the failure mode this whole "
                    f"pipeline exists to prevent.")
            return found

        @task
        def draft_prompts(found: dict, **ctx) -> list[str]:
            """One prompt, as a one-element list, so the LLM branch maps over it."""
            return [proposal.build_prompt(domain, ctx["params"]["policy_version"], found)]

        found = gather()
        if OFFLINE:
            @task
            def draft_offline(found: dict, **ctx) -> list[dict]:
                patch = proposal.offline_patch(domain, ctx["params"]["policy_version"], found)
                print(proposal.describe(patch))
                return [patch.model_dump()]

            patches = draft_offline(found)
        else:
            patches = LLMOperator.partial(
                task_id="draft",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=proposal.DRAFT_SYSTEM_PROMPT,
                output_type=PolicyPatch,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=draft_prompts(found)).output

        @task
        def compose(found: dict, patches: list, **ctx) -> dict:
            """Turn the patch into a policy document, and name the version.

            Refuses an empty patch rather than writing a version identical to the
            one it came from. A draft that changes nothing would still be
            replayed, gated and compared by whoever found it in the list, and
            every one of those runs would cost money to reproduce a result
            already on file.
            """
            version = ctx["params"]["policy_version"]
            raw = list(patches or [])
            if not raw:
                raise AirflowFailException("the drafter returned nothing at all")
            patch = raw[0] if isinstance(raw[0], PolicyPatch) else PolicyPatch(**raw[0])
            if not patch.edits:
                raise AirflowFailException(
                    f"no amendment drafted: {patch.risks or patch.summary}. Nothing was "
                    f"written, which is the right outcome when the evidence does not "
                    f"support an edit.")
            draft_version = proposal.next_version(proposal.reload_domain(domain_name), version)
            markdown = proposal.apply_to_markdown(domain, version, patch, draft_version)
            print(proposal.describe(patch))
            return {"version": version, "draft_version": draft_version,
                    "markdown": markdown, "patch": patch.model_dump(),
                    # Derived mechanically from the edits, and only possible for
                    # the threshold moves the offline proposer makes. A
                    # model-drafted patch gets its rules written by the pass
                    # below instead.
                    "rules": proposal.rules_for(domain, version, patch, found) or []}

        composed = compose(found, patches)

        @task
        def rule_prompts(composed: dict, **ctx) -> list[str]:
            """Ask a model for the offline rules that implement the drafted text.

            Not decoration. Without rules the draft cannot be swept, and an
            offline replay of it would return the most generous outcome for every
            case - a wildly permissive policy that nobody wrote.
            """
            return [rules.build_prompt(domain, ctx["params"]["policy_version"],
                                       policy_text=composed["markdown"])]

        if OFFLINE:
            @task
            def synthesise_offline(composed: dict) -> list[dict]:
                """Offline the rules were derived from the edits; nothing to ask."""
                return [{"rules": composed["rules"], "notes": "derived from the patch"}]

            rulesets = synthesise_offline(composed)
        else:
            rulesets = LLMOperator.partial(
                task_id="synthesise_rules",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=rules.SYNTHESIS_SYSTEM_PROMPT,
                output_type=RuleSet,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=rule_prompts(composed)).output

        @task
        def write_draft(composed: dict, rulesets: list, **ctx) -> dict:
            """Write the draft where every other DAG can already read it.

            The rules are validated before they are written, with the same checks
            the lint runs over hand-written ones. A generated rule reading a field
            that does not exist never matches, and never matching is silent - the
            exact failure ptm.lint was written for, from a far more prolific
            source than a person editing YAML.

            Validated against the *drafted markdown*, not against the version it
            was derived from. The draft is not a resolvable version yet - it is
            written a few lines below this - and a drafter adding a clause is the
            normal case, so checking the citations against the base policy
            rejected every rule implementing the new clause and, because a rule
            set is ordered, dropped the whole set with it.
            """
            draft_version = composed["draft_version"]
            raw = list(rulesets or [])
            first = raw[0] if raw else {}
            candidate = (first.get("rules") if isinstance(first, dict)
                         else [r.model_dump() for r in first.rules])
            candidate = [r if isinstance(r, dict) else r.model_dump() for r in (candidate or [])]

            problems = rules.validate(candidate, domain, draft_version,
                                      policy_text=composed["markdown"]) if candidate else []
            if problems:
                for problem in problems:
                    print(f"REJECTED {problem}")
                print(f"{len(problems)} problem(s) in the generated rules; writing the "
                      f"draft without them. It can still be judged by a model - only the "
                      f"offline judge and the threshold sweep need rules.")
                candidate = []

            if not ctx["params"].get("publish"):
                print(f"publish=false: {draft_version} was drafted and not written")
                return {**composed, "written": False, "files": [], "offline_rules": 0}

            written = proposal.materialise(domain_name, draft_version,
                                           composed["markdown"], candidate or None)
            print(f"wrote {', '.join(written['files'])}")
            return {**composed, "written": True, **written}

        written = write_draft(composed, rulesets)

        @task
        def verification_items(written: dict, **ctx) -> list[dict]:
            """The precedent cases, to be judged under the draft.

            Bounded on purpose. This asks the one question that fails a run -
            does the draft reverse a ruling a human made - for the price of a
            handful of judgements rather than a full replay. What the draft does
            to the other cases still costs a replay, and the summary says so.
            """
            if not written.get("written"):
                return []
            precedents = store.load_precedents(domain_name)
            if not precedents:
                print("no human rulings on file; the draft cannot be checked against "
                      "anything yet")
                return []
            ids = [p.case_id for p in precedents]
            cases = store.load_cases(domain_name, until=pendulum.now("UTC"), case_ids=ids)
            if len(cases) != len(ids):
                raise AirflowFailException(
                    f"{len(ids) - len(cases)} precedent(s) have no case on file, so the "
                    f"draft would be reported as passing a check it did not run.")
            # Reloaded rather than closed over. Every worker populated its
            # domain cache while parsing this file - before the draft existed -
            # so asking for the domain without clearing it hands back a config
            # with no such version.
            fresh = proposal.reload_domain(domain_name)
            draft_version = written["draft_version"]
            return [{**_item(c, fresh, draft_version), "policy_version": draft_version}
                    for c in cases]

        vitems = verification_items(written)

        @task
        def verification_prompts(items: list[dict]) -> list[str]:
            fresh = proposal.reload_domain(domain_name)
            return [build_prompt(_case(i), fresh, i["policy_version"]) for i in items]

        if OFFLINE:
            @task
            def verify_offline(item: dict) -> dict:
                # Reloaded per task: this process read the domain before the
                # draft existed, so the version it is asked to judge is not in
                # the config it has.
                fresh = proposal.reload_domain(domain_name)
                return offline_verdict(_case(item), fresh,
                                       item["policy_version"]).model_dump()

            vverdicts = verify_offline.expand(item=vitems)
        else:
            vverdicts = LLMOperator.partial(
                task_id="verify_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=verification_prompts(vitems)).output

        # Also none_failed: with publish=false there is nothing to verify, the
        # fan-out is empty, and this task still has a report to make.
        @task(trigger_rule="none_failed")
        def record(written: dict, items: list[dict], verdicts: list, found: dict,
                   **ctx) -> dict:
            """Report what the draft does to the precedent set, and keep the provenance."""
            patch = PolicyPatch(**written["patch"])
            if not written.get("written"):
                print(proposal.describe(patch))
                return {"drafted": True, "written": False,
                        "draft_version": written["draft_version"]}

            verdicts = list(verdicts or [])
            if len(verdicts) != len(items):
                raise AirflowFailException(
                    f"Judge returned {len(verdicts)} verdict(s) for {len(items)} precedent "
                    f"case(s); refusing to report a check it did not complete.")
            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            for verdict in by_case.values():
                domain.validate_outcome(verdict.outcome)

            verification = proposal.verify(
                domain_name, written["draft_version"], domain.in_force,
                verdicts=by_case or None)
            store.save_draft(domain_name, written["draft_version"], written["version"],
                             written["patch"],
                             evidence={k: v for k, v in found.items() if k not in ("curves", "grid")},
                             verification=verification, drafted_by=JUDGE_ID,
                             run_id=ctx["run_id"])
            print(proposal.describe(patch, verification))
            print(f"\n{written['draft_version']} is now a policy version of "
                  f"{domain_name}. Replay it to find out what it does to the cases "
                  f"nobody has ruled on.")
            return {"drafted": True, "written": True,
                    "draft_version": written["draft_version"],
                    "reverses": len(verification.get("violations", [])),
                    "fixed": len(verification.get("fixed", [])),
                    "introduced": len(verification.get("introduced", []))}

        reported = record(written, vitems, vverdicts, found)

        # The step the draft's own caveat names. verify() re-judges the
        # precedent set - a handful of contested cases - which answers "does
        # this reverse a human ruling" and nothing else; what the draft does to
        # the other several hundred still costs a replay, and until now that
        # was a sentence in the output rather than something the pipeline could
        # do. Off by default because it is a full replay and a full bill.
        @task.short_circuit
        def should_replay(written: dict, **ctx) -> bool:
            if not written.get("written"):
                return False
            if not ctx["params"].get("replay"):
                print(f"replay=false: {written['draft_version']} has been checked against "
                      f"the precedent set only. Replay it to find out what it does to "
                      f"every other case.")
                return False
            print(f"triggering replay_{domain_name} for {written['draft_version']} over "
                  f"all of history")
            return True

        replay_the_draft = TriggerDagRunOperator(
            task_id="replay_the_draft",
            trigger_dag_id=f"replay_{domain_name}",
            # Templated, so the version comes from the task that wrote it rather
            # than from a second guess at what it was named.
            conf={
                "policy_version":
                    "{{ ti.xcom_pull(task_ids='write_draft')['draft_version'] }}",
                "baseline_version": domain.in_force,
                # A triggered run has no data interval, so replay treats it as
                # "all of history" and applies its manual cap. Zero turns the cap
                # off: a draft measured on the most recent 250 cases would be
                # compared against a backfill that saw every one of them.
                "max_cases": 0,
            },
            wait_for_completion=False,
        )

        reported >> should_replay(written) >> replay_the_draft

    propose()


def build_retention() -> None:
    """One maintenance DAG for the whole database, not one per domain.

    Every other DAG here is generated per domain because a policy, its cases and
    its precedents are a domain's own. Retention is not: ``verdict_cache`` and
    ``judge_samples`` are single tables shared by every domain, the cutoff is a
    property of the database rather than of any rulebook, and a VACUUM rewrites
    one file. Five copies of this would take five locks on one SQLite file to do
    the same work once.

    Why it exists at all: these two tables grow without bound, and they grow
    *because the loop works*. The cache key is the prompt, so every clause edit
    strands the whole generation of entries it invalidated - they can never be
    hit again, by construction - and a stability run leaves one row per (case,
    repeat) of which only the newest run backs a reported figure. The engine has
    had ``ptm.prune`` for a while and the only way to run it was by hand, in a
    shell, on a schedule somebody had to remember. In a project whose argument is
    that Airflow is the engine rather than the wrapper, that was the one chore
    left outside it.
    """
    @dag(
        dag_id="ptm_retention",
        # Weekly, because the thing it drops is ninety days old by default: a
        # daily run would spend six days a week proving there is nothing to do.
        schedule="@weekly",
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULTS,
        params={
            "days": Param(90, type="integer", minimum=0,
                          title="How old a row must be before it is dropped",
                          description="Dated by last use for a cache entry, and by its "
                                      "run for a stability sample."),
            "domain": Param("", type=["string", "null"],
                            title="Limit to one domain (blank = every domain)"),
            "keep_unhit": Param(False, type="boolean",
                                title="Keep cache entries nothing has ever served",
                                description="The right setting straight after a big "
                                            "replay nothing has re-run yet."),
            "dry_run": Param(False, type="boolean",
                             title="Count what would go without removing it"),
            "vacuum": Param(True, type="boolean",
                            title="Rewrite the database afterwards so the file shrinks",
                            description="SQLite keeps freed pages on a free list, so "
                                        "without this a prune that worked changes no "
                                        "file size at all."),
        },
        tags=["policy-time-machine", "maintenance"],
        doc_md=__doc__.split("propose_<domain>")[0] + (
            "\n\n`ptm_retention` drops the cache and sample rows that have stopped "
            "earning their disk. It never touches precedents, their history, the "
            "drafts table, or the aggregates the dashboard reads."),
    )
    def retention():
        @task
        def plan(**ctx) -> dict:
            """Count what is about to go, before anything goes.

            Run unconditionally rather than only under ``dry_run``: the count is
            what makes the next task's result checkable, and
            :func:`ptm.store.prune_preview` counts with the very same WHERE
            clauses :func:`ptm.store.prune` deletes by - one definition, because
            a dry run that counts different rows from the one that deletes them
            is worse than no dry run at all.
            """
            store.init_db()
            days = int(ctx["params"].get("days") or 0)
            domain = (ctx["params"].get("domain") or "").strip() or None
            keep_unhit = bool(ctx["params"].get("keep_unhit"))
            if domain and domain not in available_domains():
                raise AirflowFailException(
                    f"unknown domain {domain!r}; have {available_domains()}. Leaving it "
                    f"blank sweeps every domain.")
            found = store.prune_preview(domain, days, keep_unhit)
            print(prune.describe(found, domain, days, dry_run=True))
            return {"preview": found, "days": days, "domain": domain or "",
                    "keep_unhit": keep_unhit}

        @task
        def sweep_up(planned: dict, **ctx) -> dict:
            """Actually remove them, unless this run was only ever a count."""
            if ctx["params"].get("dry_run"):
                print("dry_run=true: nothing was removed. The counts above are what a "
                      "real run would drop.")
                return {"removed": {}, "dry_run": True}
            domain = planned["domain"] or None
            removed = store.prune(domain, planned["days"], planned["keep_unhit"])
            print(prune.describe(removed, domain, planned["days"], dry_run=False,
                                 vacuum=bool(ctx["params"].get("vacuum"))))
            return {"removed": removed, "dry_run": False}

        @task
        def compact(swept: dict, **ctx) -> dict:
            """Give the freed pages back to the filesystem.

            Separate from the delete on purpose. VACUUM cannot run inside a
            transaction and rewrites the entire file, so it is the one step here
            with a cost proportional to the database rather than to what was
            dropped - worth being able to see, retry and switch off on its own.
            """
            if not ctx["params"].get("vacuum"):
                print("vacuum=false: rows are gone, the file keeps its size. SQLite "
                      "reuses the freed pages for the next write.")
                return {"vacuumed": False}
            if swept.get("dry_run"):
                print("dry_run=true: rewriting the database is a change, so it is "
                      "skipped along with everything else this run would have done.")
                return {"vacuumed": False}
            result = store.vacuum()
            print(prune.describe_vacuum(result))
            return {"vacuumed": True, **result}

        planned = plan()
        compact(sweep_up(planned))

    retention()


def _version_from_context(param: str = "policy_version") -> str:
    """The policy version for the running task.

    Read from the task context rather than passed in, so the prompt-rendering
    tasks stay one-liners and cannot drift from the judge's own version.
    """
    from airflow.sdk import get_current_context

    return (get_current_context()["params"].get(param) or "").strip()


for _name in available_domains():
    build(_name)

# Once, not per domain: the tables it prunes are shared and the file it rewrites
# is one file. See build_retention.
build_retention()
