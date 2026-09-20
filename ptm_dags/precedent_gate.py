"""``precedent_gate_<domain>`` - does this policy overturn a human ruling?

Triggered by the precedents asset. Re-judges every established precedent under
the candidate policy and FAILS if the policy would reverse one. This is the
regression suite for organisational judgment.

It judges the policy *in force* alongside the candidate, so a reversal the
status quo already makes is reported as the status quo's rather than charged to
whoever is proposing a change. It also warns when two human rulings contradict
each other, which would make the suite unsatisfiable by any policy at all.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Param, dag, task

from ptm import calibration, diff, store
from ptm.config import LLM_CONN_ID, OFFLINE
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Verdict
from ptm_dags.common import (
    DEFAULTS,
    START,
    SYSTEM_PROMPT,
    DomainDags,
    _as_verdict,
    _case,
    _item,
    _version_from_context,
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
    """Build ``precedent_gate_{domain}``: re-judge every ruling, fail on a reversal.

    Judges the policy in force alongside the candidate, so a reversal the status
    quo already makes is reported as the status quo's.
    """
    domain_name = ctx.name
    domain = ctx.domain
    precedents_asset = ctx.precedents
    policy_param = ctx.policy_param

    # ----------------------------------------------------------- precedent gate
    @dag(
        dag_id=f"precedent_gate_{domain_name}",
        schedule=[precedents_asset],
        start_date=START,
        catchup=False,
        # One at a time, like every other DAG here. This one is asset-triggered
        # and re-fires on every adjudication run, so it is the likeliest in the
        # file to be asked to run twice at once - against a single SQLite file
        # that each concurrent run would take a write lock on, to re-judge the
        # same handful of precedent cases and reach the same answer.
        max_active_runs=1,
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
            store.save_verdicts(f"{ctx['dag'].dag_id}::{ctx['run_id']}", domain_name, version, by_case)

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
                store.save_verdicts(f"{ctx['dag'].dag_id}::{ctx['run_id']}" + store.BASELINE_RUN_SUFFIX,
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
