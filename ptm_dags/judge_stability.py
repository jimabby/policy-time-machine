"""``judge_stability_<domain>`` - how much of that was the judge's noise?

Manual. Judges the same cases repeatedly under the same policy to measure how
often the judge contradicts itself - the error bar on every flip rate the other
DAGs report - and runs a second, independent judge over the same cases to catch
the failure self-consistency cannot see: a judge that misreads a clause the
same way every time.

Deliberately the one DAG that never reads the verdict cache. Serving a repeat
judgement from cache would report a judge that never contradicts itself, which
is not a clean bill of health but a broken instrument.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Param, dag, task

from ptm import crosscheck, diff, stability, store
from ptm.config import LLM_CONN_ID, OFFLINE
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Verdict
from ptm_dags.common import (
    DEFAULTS,
    JUDGE_ID,
    START,
    SYSTEM_PROMPT,
    DomainDags,
    _as_verdict,
    _case,
    _item,
    _ledger,
    _measured_usage,
    _version_from_context,
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
    """Build ``judge_stability_{domain}``: measure the judge, not the policy.

    The one builder that asks for an uncached verdict on purpose.
    """
    domain_name = ctx.name
    domain = ctx.domain
    policy_param = ctx.policy_param

    # --------------------------------------------------------- judge stability
    @dag(
        dag_id=f"judge_stability_{domain_name}",
        schedule=None,
        start_date=START,
        catchup=False,
        # Manual, but two of these at once is two fan-outs of paid judging at
        # the same model endpoint writing to one SQLite file, and the second
        # would overwrite the first's flip_stability rows for the same cases.
        max_active_runs=1,
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
