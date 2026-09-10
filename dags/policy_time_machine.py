"""Policy Time Machine - three DAGs per domain, generated from include/domains/*.yaml.

Drop a new YAML in and Airflow grows a new set of DAGs on the next parse. The
DAG code below contains no domain knowledge at all.

    replay_<domain>          @monthly, backfilled across history.
                             Replays every real decision in its data interval
                             under a proposed policy, point-in-time correct.
                             Emits the flips asset.

    adjudicate_<domain>      Triggered by the flips asset. Puts the handful of
                             genuinely contested flips in front of a human via
                             HITL, and turns their rulings into precedent.
                             Emits the precedents asset.

    precedent_gate_<domain>  Triggered by the precedents asset. Re-judges every
                             established precedent under the candidate policy
                             and FAILS if the policy would reverse one. This is
                             the regression suite for organisational judgment.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Asset, Param, dag, task

from ptm import diff, store
from ptm.config import LLM_CONN_ID, OFFLINE, available_domains, load_domain
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
            capped by the max_cases param. Point-in-time hydration is
            unaffected either way - it is per-case, not per-run.
            """
            store.init_db()
            version = ctx["params"]["policy_version"]
            lo, hi = ctx["data_interval_start"], ctx["data_interval_end"]
            cap = int(ctx["params"].get("max_cases") or 0)

            if lo >= hi:  # manual run
                lo, hi = pendulum.datetime(1970, 1, 1), pendulum.now("UTC")
                print(f"manual run: replaying all history up to {hi:%Y-%m-%d}"
                      f"{f', capped at {cap} cases' if cap else ''}")
            else:
                print(f"scheduled run: replaying {lo:%Y-%m-%d} to {hi:%Y-%m-%d}")

            cases = store.load_cases(domain_name, until=hi, since=lo,
                                     limit=cap or 10_000)
            print(f"{len(cases)} cases to replay under policy {version}")
            return [
                {
                    "case_id": c.case_id,
                    "domain": c.domain,
                    "decided_at": c.decided_at.isoformat(),
                    "payload": c.payload,
                    "actual_outcome": c.actual_outcome,
                    "actual_rationale": c.actual_rationale,
                    "prompt": build_prompt(c, domain, version),
                }
                for c in cases
            ]

        @task
        def prompts(items: list[dict]) -> list[str]:
            return [i["prompt"] for i in items]

        @task
        def reconcile(items: list[dict], verdicts: list, **ctx) -> dict:
            version = ctx["params"]["policy_version"]
            run_id = ctx["run_id"]
            cases = [_case(i) for i in items]
            by_case = {c.case_id: _as_verdict(v) for c, v in zip(cases, verdicts)}

            store.save_verdicts(run_id, domain_name, version, by_case)
            found = diff.flips(cases, by_case, domain)
            store.save_flips(run_id, domain_name, version, found)
            summary = diff.summarise(found, len(cases), domain)
            store.record_run(run_id, domain_name, version, "actual",
                             len(cases), len(found), summary["net_impact"])
            return summary

        items = prepare()
        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def judge_offline(item: dict, **ctx) -> dict:
                return offline_verdict(_case(item), domain, ctx["params"]["policy_version"]).model_dump()

            verdicts = judge_offline.expand(item=items)
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

        @task(outlets=[flips_asset])
        def publish(summary: dict) -> dict:
            """Emitting the asset is what wakes the adjudication DAG."""
            return summary

        publish(reconcile(items, verdicts))

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
                    policy_clause="", impact=r["impact"],
                    payload=_json.loads(r["payload"]), direction=r["direction"],
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
                out.append(
                    f"### The case, as decided on {f['decided_at'][:10]}\n\n"
                    f"```\n{domain.render_case(f['payload'])}```\n\n"
                    f"**What actually happened:** `{f['actual_outcome']}`\n\n"
                    f"**What the proposed policy gives:** `{f['new_outcome']}` "
                    f"(confidence {f['confidence']:.0%}, {domain.impact_unit} {f['impact']:,.2f} at stake)\n\n"
                    f"> {f['rationale']}\n\n"
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
            store.mark_reviewed(saved)
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
            return [
                {
                    "case_id": c.case_id, "domain": c.domain,
                    "decided_at": c.decided_at.isoformat(), "payload": c.payload,
                    "actual_outcome": c.actual_outcome,
                    "prompt": build_prompt(c, domain, version),
                }
                for c in cases
            ]

        @task
        def gate_prompts(items: list[dict]) -> list[str]:
            return [i["prompt"] for i in items]

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
        def enforce(items: list[dict], verdicts: list, **ctx) -> dict:
            """Fail the run if the candidate policy reverses a human ruling."""
            version = ctx["params"]["policy_version"]
            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            violations = diff.precedent_violations(by_case, store.load_precedents(domain_name))
            if violations:
                lines = "\n".join(
                    f"  - {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}' on "
                    f"{v['established_at']}, policy {version} gives '{v['proposed_outcome']}'"
                    for v in violations
                )
                raise AirflowFailException(
                    f"Policy {version} reverses {len(violations)} established precedent(s):\n{lines}"
                )
            return {"precedents_checked": len(by_case), "violations": 0}

        enforce(cases, gate_verdicts)

    precedent_gate()


for _name in available_domains():
    build(_name)
