"""Policy Time Machine - four DAGs per domain, generated from include/domains/*.yaml.

Drop a new YAML in and Airflow grows a new set of DAGs on the next parse. The
DAG code below contains no domain knowledge at all.

    replay_<domain>          @monthly, backfilled across history.
                             Replays every real decision in its data interval
                             under a proposed policy, point-in-time correct.
                             Attributes every change to the clause that caused
                             it, breaks the blast radius down by segment, and
                             records what the judging cost. Emits the flips asset.

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
                             rate the other three DAGs report.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Asset, Param, dag, task

from ptm import cost, diff, stability, store
from ptm.config import JUDGE_MODEL, LLM_CONN_ID, OFFLINE, available_domains, load_domain
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Case, Precedent, Verdict

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


def _item(case: Case, domain, version: str, baseline_version: str = "") -> dict:
    """One case as it travels through XCom.

    Note what is *absent*: the rendered prompt. Every prompt embeds the whole
    policy text, so carrying prompts here would duplicate the policy once per
    case through every downstream task - and the offline judge never reads them
    at all. Only ``prompt_chars`` survives, because the cost ledger needs the
    size and nothing needs the bytes.
    """
    chars = len(build_prompt(case, domain, version))
    if baseline_version:
        # A baseline pass judges every case twice, and the ledger has to say so
        # rather than quietly under-reporting half the bill.
        chars += len(build_prompt(case, domain, baseline_version))
    return {
        "case_id": case.case_id,
        "domain": case.domain,
        "decided_at": case.decided_at.isoformat(),
        "payload": case.payload,
        "actual_outcome": case.actual_outcome,
        "actual_rationale": case.actual_rationale,
        "prompt_chars": chars,
    }


def _ledger(items: list[dict], version: str, baseline: bool = False) -> dict:
    """Price the judging this run performed.

    Offline the answer is genuinely zero, and the ledger says so rather than
    quoting a counterfactual as though money had moved. The forecast of what a
    real judge *would* have cost lives on the plugin's /api/cost endpoint, where
    it is clearly labelled as a forecast.
    """
    if OFFLINE:
        return cost.zero()
    prompt_chars = sum(int(i.get("prompt_chars") or 0) for i in items)
    return cost.estimate(prompt_chars, len(items) * (2 if baseline else 1), JUDGE_MODEL)


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
        },
        tags=["policy-time-machine", domain_name, "replay"],
        doc_md=f"Replay historical {domain.label} decisions under a proposed policy. "
               f"Backfill this DAG to simulate the whole of history.",
    )
    def replay():
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

        @task
        def reconcile(items: list[dict], verdicts: list, baseline_verdicts: list | None = None,
                      **ctx) -> dict:
            version = ctx["params"]["policy_version"]
            baseline_version = (ctx["params"].get("baseline_version") or "").strip()
            run_id = ctx["run_id"]
            cases = [_case(i) for i in items]
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
            ledger = _ledger(items, version, baseline=baseline is not None)
            store.save_replay(run_id, domain_name, version, "actual", len(cases),
                              found, summary["net_impact"], by_case,
                              segments=segments, ledger=ledger,
                              baseline_version=baseline_version if baseline else "",
                              baseline_verdicts=baseline)

            for row in summary["by_clause"]:
                print(f"{row['clause']:>34}  {row['flips']:>4} flips "
                      f"({row['share']:>5.1%})  net {domain.impact_unit} {row['net_impact']:>10,.0f}")
            if summary["deviation_flips"]:
                print(f"of {summary['flips']} changes, {summary['policy_driven_flips']} are caused "
                      f"by policy {version}; {summary['deviation_flips']} are cases where the "
                      f"recorded outcome never matched policy {baseline_version} either")
            if ledger["estimated_cost_usd"]:
                print(f"judged by {ledger['judge_model']} for an estimated "
                      f"USD {ledger['estimated_cost_usd']:.4f}")
            return {**summary, **ledger, "segments": segments}

        items = prepare()
        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            @task(max_active_tis_per_dag=8)
            def judge_baseline_offline(item: dict, **ctx) -> dict:
                """The same case under the policy already in force."""
                version = (ctx["params"]["baseline_version"] or "").strip()
                return offline_verdict(_case(item), domain, version).model_dump()

            verdicts = judge_offline.expand(item=items)
            baseline_verdicts = judge_baseline_offline.expand(item=baseline_items(items))
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
            ).expand(prompt=prompts(items)).output
            baseline_verdicts = LLMOperator.partial(
                task_id="judge_baseline",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=baseline_prompts(baseline_items(items))).output

        @task(outlets=[flips_asset])
        def publish(summary: dict) -> dict:
            """Emitting the asset is what wakes the adjudication DAG."""
            return summary

        publish(reconcile(items, verdicts, baseline_verdicts))

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
                )
                for r in rows if not r["reviewed"]
            ]
            return [f.model_dump(mode="json") for f in diff.select_for_review(flips, domain)]

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

        reviews = HITLOperator.partial(
            task_id="review",
            options=domain.outcomes,
            defaults=[domain.outcomes[0]],
            task_display_name="Adjudicate contested case",
        ).expand(subject=subjects(flips), body=bodies(flips))

        @task(outlets=[precedents_asset], trigger_rule="all_done")
        def record(flips: list[dict], responses: list, **ctx) -> dict:
            """Turn human answers into precedent. This is the only durable output."""
            saved = []
            for f, resp in zip(flips, responses or []):
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
            return {"precedents_recorded": len(saved), "case_ids": saved}

        record(flips, reviews.output)

    adjudicate()

    # ----------------------------------------------------------- precedent gate
    @dag(
        dag_id=f"precedent_gate_{domain_name}",
        schedule=[precedents_asset],
        start_date=START,
        catchup=False,
        default_args=DEFAULTS,
        params={"policy_version": policy_param},
        tags=["policy-time-machine", domain_name, "regression"],
        doc_md="Fails if the candidate policy would reverse a ruling a human already made.",
    )
    def precedent_gate():
        @task
        def precedent_cases(**ctx) -> list[dict]:
            version = ctx["params"]["policy_version"]
            precedents = store.load_precedents(domain_name)
            ids = {p.case_id for p in precedents}
            cases = [c for c in store.load_cases(domain_name, until=pendulum.now("UTC")) if c.case_id in ids]
            return [_item(c, domain, version) for c in cases]

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
        if OFFLINE:
            @task
            def gate_judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            gate_verdicts = gate_judge_offline.expand(item=cases)
        else:
            gate_verdicts = LLMOperator.partial(
                task_id="gate_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
            ).expand(prompt=gate_prompts(cases)).output

        @task
        def enforce(items: list[dict], verdicts: list, found_conflicts: list[dict], **ctx) -> dict:
            """Fail the run if the candidate policy reverses a human ruling."""
            version = ctx["params"]["policy_version"]
            if len(verdicts) != len(items):
                raise AirflowFailException(
                    f"Judge returned {len(verdicts)} verdicts for {len(items)} precedents; refusing partial gate."
                )
            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            for verdict in by_case.values():
                domain.validate_outcome(verdict.outcome)
            violations = diff.precedent_violations(by_case, store.load_precedents(domain_name))
            if violations:
                lines = "\n".join(
                    f"  - {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}' on "
                    f"{v['established_at']}, policy {version} gives '{v['proposed_outcome']}'"
                    for v in violations
                )
                hint = ("\nNote: the precedent set also contains "
                        f"{len(found_conflicts)} internal conflict(s), so some violation here may "
                        "be unavoidable until two humans agree with each other."
                        ) if found_conflicts else ""
                raise AirflowFailException(
                    f"Policy {version} reverses {len(violations)} established precedent(s):\n{lines}{hint}"
                )
            return {"precedents_checked": len(by_case), "violations": 0,
                    "precedent_conflicts": len(found_conflicts)}

        enforce(cases, gate_verdicts, conflicts())

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
            "sample_cases": Param(25, type="integer", title="Cases to sample",
                                  description="Spread across the whole period, deterministically."),
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
            """One row per (case, repeat): the fan-out this DAG measures."""
            store.init_db()
            params = ctx["params"]
            version = params["policy_version"]
            repeats = max(2, int(params["samples_per_case"]))
            cases = store.load_cases(domain_name, until=pendulum.now("UTC"))
            if not cases:
                raise AirflowFailException(
                    f"no {domain_name} cases to sample; seed the history first")
            picked = stability.sample_cases(cases, int(params["sample_cases"]),
                                            seed=int(params["seed"]))
            print(f"sampling {len(picked)} cases x {repeats} judgements under policy {version}")
            return [
                {**_item(c, domain, version), "sample_idx": i}
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
            ledger = _ledger(rows, version)
            store.save_stability(ctx["run_id"], domain_name, version,
                                 samples, result.model_dump(), ledger)

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
            return {**result.model_dump(exclude={"unstable"}), **ledger}

        report(rows, sampled)

    judge_stability()


def _version_from_context(param: str = "policy_version") -> str:
    """The policy version for the running task.

    Read from the task context rather than passed in, so the prompt-rendering
    tasks stay one-liners and cannot drift from the judge's own version.
    """
    from airflow.sdk import get_current_context

    return (get_current_context()["params"].get(param) or "").strip()


for _name in available_domains():
    build(_name)
