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

from ptm import (cache, calibration, cost, diff, disparity, preflight, proposal,
                 rules, stability, store)
from ptm.config import JUDGE_MODEL, LLM_CONN_ID, OFFLINE, available_domains, load_domain
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Case, PolicyPatch, Precedent, RuleSet, Verdict

if not OFFLINE:
    from airflow.providers.common.ai.operators.llm import LLMOperator
    from pydantic_ai.usage import UsageLimits
from airflow.providers.standard.operators.hitl import HITLOperator

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


def _ledger(items: list[dict], chars_field: str = "prompt_chars") -> dict:
    """Price one pass over ``items`` - the cases actually sent to the judge.

    Offline the answer is genuinely zero, and the ledger says so rather than
    quoting a counterfactual as though money had moved. The forecast of what a
    real judge *would* have cost lives on the plugin's /api/cost endpoint, where
    it is clearly labelled as a forecast.
    """
    if OFFLINE:
        return cost.zero()
    prompt_chars = sum(int(i.get(chars_field) or 0) for i in items)
    return cost.estimate(prompt_chars, len(items), JUDGE_MODEL)


def _add_ledgers(*ledgers: dict) -> dict:
    """One bill from several passes. Offline they are all zero and stay zero."""
    total = cost.zero(JUDGE_ID)
    for ledger in ledgers:
        for field in ("estimated_requests", "estimated_input_tokens",
                      "estimated_output_tokens"):
            total[field] += int(ledger.get(field) or 0)
        total["estimated_cost_usd"] = round(
            total["estimated_cost_usd"] + float(ledger.get("estimated_cost_usd") or 0), 4)
    return total


@task(multiple_outputs=True)
def to_judge(items: list[dict], key_field: str = "cache_key") -> dict:
    """Split a fan-out into what still needs judging and what is already answered.

    Defined once at module level and called by both the replay and the gate,
    because they have the same problem: the gate re-judges the same handful of
    precedent cases every time a ruling is recorded, and the candidate policy
    has usually not changed between two of those runs.
    """
    misses, hits = cache.split(items, key_field)
    print(cache.describe(len(hits), len(misses)))
    return {"judge": misses,
            "cached": {case_id: v.model_dump() for case_id, v in hits.items()}}


@task
def merge(items: list[dict], misses: list[dict], cached: dict, fresh: list,
          key_field: str = "cache_key", chars_field: str = "prompt_chars") -> dict:
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

    out, hits = [], []
    for item in items:
        case_id = item["case_id"]
        if case_id in judged:
            out.append(judged[case_id].model_dump())
        elif case_id in cached:
            out.append(cached[case_id])
            hits.append(item)
        else:
            raise AirflowFailException(
                f"{case_id} was neither judged nor found in the cache. A replay missing "
                f"a verdict would report a case as unchanged, which is indistinguishable "
                f"from a case the policy agrees with.")
    return {"verdicts": out,
            "ledger": _ledger(misses, chars_field),
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

            cases = store.load_cases(domain_name, until=hi, since=lo,
                                     limit=cap or 10_000, newest_first=manual and bool(cap))
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
            return {**summary, **ledger, "segments": segments,
                    "cache_hits": cache_hits, "estimated_saved_usd": cache_saved,
                    "disparity": [f.model_dump(mode="json") for f in findings]}

        items = prepare()
        read_the_policy() >> items

        # Everything already answered is taken out of the fan-out here, so the
        # judge below - offline or not - only ever sees cases nobody has asked
        # about yet. The second measurement of an edited clause is the one this
        # is for: most of the history is untouched by the edit and its verdicts
        # are still valid, because the key is the prompt and the prompt did not
        # change for those cases.
        candidate_split = to_judge(items)
        baseline_case_items = baseline_items(items)
        baseline_split = to_judge.override(task_id="to_judge_baseline")(
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

            verdicts = judge_offline.expand(item=candidate_split["judge"])
            baseline_verdicts = judge_baseline_offline.expand(item=baseline_split["judge"])
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
            ).expand(prompt=prompts(candidate_split["judge"])).output
            baseline_verdicts = LLMOperator.partial(
                task_id="judge_baseline",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=baseline_prompts(baseline_split["judge"])).output

        candidate = merge(items, candidate_split["judge"], candidate_split["cached"],
                          verdicts)
        baseline_pass = merge.override(task_id="merge_baseline",
                                       trigger_rule="none_failed")(
            baseline_case_items, baseline_split["judge"], baseline_split["cached"],
            baseline_verdicts, "baseline_cache_key", "baseline_prompt_chars")

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
        params={"policy_version": policy_param},
        tags=["policy-time-machine", domain_name, "human-in-the-loop"],
        doc_md="Ask a human to settle only the contested flips, and keep their answers forever.",
    )
    def adjudicate():
        @task
        def contested(**ctx) -> list[dict]:
            """The few flips worth a human's attention, across all replay runs."""
            version = ctx["params"]["policy_version"]
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
            return [f"{f['case_id']}: should this have been '{f['new_outcome']}' rather than '{f['actual_outcome']}'?" for f in flips]

        @task
        def bodies(flips: list[dict]) -> list[str]:
            out = []
            for f in flips:
                clause = f.get("attribution") or (
                    f"clause {f['policy_clause']}" if f.get("policy_clause") else "")
                out.append(
                    f"### The case, as decided on {f['decided_at'][:10]}\n\n"
                    f"```\n{domain.render_case(f['payload'])}```\n\n"
                    f"**What actually happened:** `{f['actual_outcome']}`\n\n"
                    f"**What the proposed policy gives:** `{f['new_outcome']}` "
                    f"(confidence {f['confidence']:.0%}, {domain.impact_unit} {f['impact']:,.2f} at stake)\n\n"
                    # Telling the reviewer which clause drove the change lets them
                    # argue with the rule rather than just the result.
                    + (f"**Driven by:** `{clause}`\n\n" if clause else "")
                    + f"> {f['rationale']}\n\n"
                    f"Pick the outcome that is *actually* correct for this case. Your answer "
                    f"becomes a permanent precedent that every future policy change is tested against."
                )
            return out

        flips = contested()
        held_back = unconfirmed()

        reviews = HITLOperator.partial(
            task_id="review",
            options=domain.outcomes,
            defaults=[domain.outcomes[0]],
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
            saved = []
            for f, resp in zip(flips, responses):
                chosen = (resp or {}).get("chosen_options") or []
                if not chosen:
                    continue
                store.save_precedent(Precedent(
                    case_id=f["case_id"], domain=domain_name, correct_outcome=chosen[0],
                    ruled_by=(resp.get("user_id") or "unknown"),
                    note=(resp.get("params_input") or {}).get("note", ""),
                    established_at=pendulum.now("UTC"), established_by_run=ctx["run_id"],
                ))
                saved.append(f["case_id"])
            store.mark_reviewed(domain_name, ctx["params"]["policy_version"], saved)
            return {"precedents_recorded": len(saved), "case_ids": saved,
                    "held_back_unconfirmed": len(held_back)}

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
        gate_split = to_judge.override(task_id="gate_to_judge")(cases)
        gate_baseline_case_items = gate_baseline_items(cases)
        gate_baseline_split = to_judge.override(task_id="gate_to_judge_baseline")(
            gate_baseline_case_items, "baseline_cache_key")

        if OFFLINE:
            @task
            def gate_judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            @task
            def gate_baseline_offline(item: dict, **ctx) -> dict:
                version = (ctx["params"].get("baseline_version") or "").strip()
                return offline_verdict(_case(item), domain, version).model_dump()

            gate_verdicts = gate_judge_offline.expand(item=gate_split["judge"])
            gate_baseline_verdicts = gate_baseline_offline.expand(
                item=gate_baseline_split["judge"])
        else:
            gate_verdicts = LLMOperator.partial(
                task_id="gate_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=gate_prompts(gate_split["judge"])).output
            gate_baseline_verdicts = LLMOperator.partial(
                task_id="gate_judge_baseline",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=gate_baseline_prompts(gate_baseline_split["judge"])).output

        gate_candidate = merge.override(task_id="gate_merge")(
            cases, gate_split["judge"], gate_split["cached"], gate_verdicts)
        gate_baseline = merge.override(task_id="gate_merge_baseline",
                                       trigger_rule="none_failed")(
            gate_baseline_case_items, gate_baseline_split["judge"],
            gate_baseline_split["cached"], gate_baseline_verdicts,
            "baseline_cache_key", "baseline_prompt_chars")

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
            # Stored so the dashboard can show the gate's answer without paying
            # to judge these cases all over again.
            store.save_verdicts(ctx["run_id"], domain_name, version, by_case)

            # The gate asks whether the policy agrees with the humans. The same
            # verdicts answer a question nothing else in this pipeline asks:
            # whether the *judge* does. Stability says the judge repeats itself;
            # only this says whether repeating itself is worth anything - and the
            # confidence it reports is what routes the review budget.
            print(calibration.describe(
                calibration.score(domain, version, by_case, precedents)))

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
                    "precedent_conflicts": len(found_conflicts)}

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

        rows = units()
        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def stability_judge_offline(row: dict, **ctx) -> dict:
                return offline_verdict(_case(row), domain, ctx["params"]["policy_version"]).model_dump()

            sampled = stability_judge_offline.expand(row=rows)
        else:
            sampled = LLMOperator.partial(
                task_id="stability_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=stability_prompts(rows)).output

        @task
        def report(rows: list[dict], verdicts: list, **ctx) -> dict:
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
            ledger = _ledger(rows)
            store.save_stability(ctx["run_id"], domain_name, version,
                                 samples, result.model_dump(), ledger)

            confirmed = 0
            if (params.get("target") or "sample").strip() == "flips":
                recorded = {r["case_id"]: r.get("recorded_outcome", "") for r in rows}
                confirmations = stability.confirm(samples, recorded)
                confirmed = store.save_flip_stability(domain_name, version, confirmations,
                                                      run_id=ctx["run_id"])
                print(stability.describe_confirmations(confirmations))

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
                    "flips_confirmed": confirmed}

        report(rows, sampled)

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
            """
            draft_version = composed["draft_version"]
            raw = list(rulesets or [])
            first = raw[0] if raw else {}
            candidate = (first.get("rules") if isinstance(first, dict)
                         else [r.model_dump() for r in first.rules])
            candidate = [r if isinstance(r, dict) else r.model_dump() for r in (candidate or [])]

            problems = rules.validate(candidate, domain, composed["version"]) if candidate else []
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
                             evidence={k: v for k, v in found.items() if k != "curves"},
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

        record(written, vitems, vverdicts, found)

    propose()


def _version_from_context(param: str = "policy_version") -> str:
    """The policy version for the running task.

    Read from the task context rather than passed in, so the prompt-rendering
    tasks stay one-liners and cannot drift from the judge's own version.
    """
    from airflow.sdk import get_current_context

    return (get_current_context()["params"].get(param) or "").strip()


for _name in available_domains():
    build(_name)
