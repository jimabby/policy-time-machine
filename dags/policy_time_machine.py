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

Each of the three also asks the model for the thing a *human* needs out of the
numbers: an executive brief and a thematic clustering on the replay, and a
drafted policy amendment when the gate fails. Those run through the same
Common AI provider with their own ``output_type``, so they are typed too.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Asset, Param, dag, task

from ptm import ai, amend, analysis, diff, store
from ptm.config import LLM_CONN_ID, OFFLINE, available_domains, load_domain
from ptm.judge import build_prompt, offline_verdict
from ptm.models import Case, Flip, Precedent, Verdict

if not OFFLINE:
    from airflow.providers.common.ai.operators.llm import LLMOperator
    from pydantic_ai.usage import UsageLimits
from airflow.providers.standard.operators.hitl import HITLOperator

#: Analysis prompts are single-shot and longer than a judge prompt, so they get
#: their own, slightly looser request budget.
ANALYSIS_REQUEST_LIMIT = 4

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
    """Coerce whatever XCom handed back into a Verdict.

    LLMOperator pushes the pydantic model, the offline task pushes a dict, and a
    serialising XCom backend can return either as a JSON string.
    """
    return ai.as_model(raw, Verdict)


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

    def _flips_from_store(version: str, unreviewed_only: bool = False) -> list[Flip]:
        """Rehydrate this policy's flips from the store, across every replay run.

        Analysis and adjudication both want the whole picture rather than one
        monthly interval's slice, and a backfill produces 24 of those.
        """
        import json as _json

        return [
            Flip(
                case_id=r["case_id"], decided_at=pendulum.parse(r["decided_at"]),
                actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
                rationale=r["rationale"], confidence=r["confidence"],
                policy_clause=r["policy_clause"] or "", impact=r["impact"],
                payload=_json.loads(r["payload"]), direction=r["direction"],
            )
            for r in store.flips_for_policy(domain_name, version)
            if not (unreviewed_only and r["reviewed"])
        ]

    flips_asset = Asset(f"ptm://{domain_name}/flips")
    precedents_asset = Asset(f"ptm://{domain_name}/precedents")
    amendments_asset = Asset(f"ptm://{domain_name}/amendments")
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

            manual = lo >= hi
            if manual:
                lo, hi = pendulum.datetime(1970, 1, 1), pendulum.now("UTC")
                print(f"manual run: replaying all history up to {hi:%Y-%m-%d}"
                      f"{f', sampled evenly down to {cap} cases' if cap else ''}")
            else:
                print(f"scheduled run: replaying {lo:%Y-%m-%d} to {hi:%Y-%m-%d}")

            cases = store.load_cases(domain_name, until=hi, since=lo,
                                     limit=cap or 10_000, spread=manual)
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

        summary = reconcile(items, verdicts)

        # ---------------------------------------------------------- analysis
        # 147 changed outcomes is data, not an answer. These two tasks ask the
        # model for what a decision-maker actually needs from them: the brief
        # they can forward, and the handful of mechanisms doing the work.
        @task
        def analysis_prompts(_run_summary: dict, **ctx) -> list[str]:
            """Build both analysis prompts from the policy's cumulative picture.

            Deliberately ignores the run summary it is triggered by: one run is
            one month of a 24-month backfill, and a brief describing a single
            month is not the question anyone asked. ``policy_summary`` counts
            per case across every run, so the brief, the themes and the
            dashboard tiles all quote the same figures.

            Returned as a list because the analysis operators are mapped over
            it - a one-element expansion per prompt. That keeps the LLM call
            using exactly the same mechanism as the judge above rather than
            relying on XCom rendering into an unmapped template field.
            """
            version = ctx["params"]["policy_version"]
            summary = store.policy_summary(domain_name, version)
            summary["impact_unit"] = domain.impact_unit
            flips_ = _flips_from_store(version)
            top = sorted(flips_, key=lambda f: -f.impact)[:25]
            coverage = analysis.clause_coverage(domain, version)
            cohorts = analysis.cohort_report(domain, version)
            return [
                ai.build_brief_prompt(summary, top, domain, version, coverage, cohorts),
                ai.build_themes_prompt(top, domain, version),
            ]

        @task
        def brief_prompt(both: list[str]) -> list[str]:
            return both[:1]

        @task
        def themes_prompt(both: list[str]) -> list[str]:
            return both[1:]

        if OFFLINE:
            @task
            def analyse_offline(_run_summary: dict, **ctx) -> dict:
                """Deterministic stand-in, so the panel is populated with no API key.

                Reads the cumulative picture for the same reason the prompt
                builder above does - see its docstring.
                """
                version = ctx["params"]["policy_version"]
                summary = store.policy_summary(domain_name, version)
                summary["impact_unit"] = domain.impact_unit
                flips_ = _flips_from_store(version)
                top = sorted(flips_, key=lambda f: -f.impact)[:25]
                coverage = analysis.clause_coverage(domain, version)
                cohorts = analysis.cohort_report(domain, version)
                return {
                    "brief": ai.offline_brief(summary, top, domain, version,
                                              coverage, cohorts).model_dump(),
                    "themes": ai.offline_themes(flips_, domain).model_dump(),
                }

            analysis = analyse_offline(summary)
        else:
            # Built here rather than above so the offline DAG does not carry a
            # task producing prompts nothing consumes.
            both = analysis_prompts(summary)
            brief = LLMOperator.partial(
                task_id="brief",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=ai.BRIEF_SYSTEM,
                output_type=ai.PolicyBrief,
                usage_limits=UsageLimits(request_limit=ANALYSIS_REQUEST_LIMIT),
            ).expand(prompt=brief_prompt(both)).output

            themes = LLMOperator.partial(
                task_id="themes",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=ai.THEMES_SYSTEM,
                output_type=ai.FlipThemes,
                usage_limits=UsageLimits(request_limit=ANALYSIS_REQUEST_LIMIT),
            ).expand(prompt=themes_prompt(both)).output

            @task
            def collect(brief: list, themes: list) -> dict:
                return {
                    "brief": ai.as_model(brief[0], ai.PolicyBrief).model_dump(),
                    "themes": ai.as_model(themes[0], ai.FlipThemes).model_dump(),
                }

            analysis = collect(brief, themes)

        @task
        def save_analysis(generated: dict, **ctx) -> dict:
            """Persist the model's reading, and the arithmetic behind it."""
            version = ctx["params"]["policy_version"]
            source = "offline" if OFFLINE else LLM_CONN_ID
            for kind, payload in generated.items():
                store.save_insight(domain_name, version, kind, payload, source, ctx["run_id"])

            # Coverage and cohorts are computed, not generated: stored as their
            # own kinds so the dashboard never presents arithmetic as AI output.
            store.save_insight(domain_name, version, "coverage",
                               analysis.clause_coverage(domain, version),
                               "computed", ctx["run_id"])
            store.save_insight(domain_name, version, "cohorts",
                               {"breakdowns": analysis.cohort_report(domain, version)},
                               "computed", ctx["run_id"])

            brief = generated.get("brief", {})
            print(f"brief: [{brief.get('verdict', '?')}] {brief.get('headline', '')}")
            for spot in brief.get("blind_spots", []):
                print(f"  blind spot: {spot}")
            return {"kinds": sorted(generated) + ["coverage", "cohorts"]}

        @task(outlets=[flips_asset], trigger_rule="all_done")
        def publish(summary: dict) -> dict:
            """Emitting the asset is what wakes the adjudication DAG.

            ``all_done`` because the analysis above is commentary: a model
            outage must not stop the adjudication it is commentary on.
            """
            return summary

        save_analysis(analysis) >> publish(summary)

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
            flips = _flips_from_store(version, unreviewed_only=True)
            chosen = diff.select_for_review(flips, domain)
            print(f"{len(chosen)} of {len(flips)} unreviewed flips routed to a human")
            return [f.model_dump(mode="json") for f in chosen]

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
            # Without this the reviewer has no way to say *why*, and record()
            # below stores an empty note on every precedent. The reasoning is
            # the part a future policy author needs most.
            params={"note": Param("", type="string", title="Why is this the correct outcome?",
                                  description="Kept with the precedent forever. One or two sentences.")},
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
        def check(items: list[dict], verdicts: list, **ctx) -> dict:
            """Find precedent reversals. Deliberately does NOT fail the run yet.

            A failing gate is the most useful moment to ask what to do about
            it, so the violations are handed to the drafter below and the
            failure is raised afterwards.
            """
            version = ctx["params"]["policy_version"]
            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            violations = diff.precedent_violations(by_case, store.load_precedents(domain_name))
            print(f"{len(violations)} violation(s) across {len(by_case)} precedent(s)")
            return {"precedents_checked": len(by_case), "violations": violations}

        result = check(cases, gate_verdicts)

        @task
        def amendment_prompt(result: dict, **ctx) -> list[str]:
            """One prompt if the gate failed, none if it passed.

            An empty list means the drafter below is mapped over nothing and is
            skipped, which is exactly the wanted behaviour on a passing gate.
            """
            if not result["violations"]:
                return []
            return [ai.build_amendment_prompt(result["violations"], domain,
                                              ctx["params"]["policy_version"])]

        if OFFLINE:
            @task
            def draft_offline(result: dict, **ctx) -> dict | None:
                if not result["violations"]:
                    return None
                return ai.offline_amendment(result["violations"], domain,
                                            ctx["params"]["policy_version"]).model_dump()

            drafts = draft_offline(result)
        else:
            drafts = LLMOperator.partial(
                task_id="draft_amendment",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=ai.AMENDMENT_SYSTEM,
                output_type=ai.Amendment,
                usage_limits=UsageLimits(request_limit=ANALYSIS_REQUEST_LIMIT),
            ).expand(prompt=amendment_prompt(result)).output

        @task(outlets=[amendments_asset], trigger_rule="all_done")
        def record_amendment(result: dict, drafts, **ctx) -> dict:
            """Persist the drafted amendment and register it as a candidate policy.

            Runs *before* the gate fails, and emits the amendments asset, so the
            verification DAG can test the fix rather than leaving it as advice.
            ``all_done`` so a model outage cannot stop the gate below.
            """
            version = ctx["params"]["policy_version"]
            violations = result["violations"]
            if not violations:
                return {"drafted": False}

            raw = drafts[0] if isinstance(drafts, list) and drafts else drafts
            if not raw:
                print("no amendment drafted; the gate will fail without a proposed fix")
                return {"drafted": False}

            amendment = ai.as_model(raw, ai.Amendment)
            store.save_insight(domain_name, version, "amendment", amendment.model_dump(),
                               "offline" if OFFLINE else LLM_CONN_ID, ctx["run_id"])
            if not amendment.feasible:
                print(f"drafter reports the precedents cannot all be satisfied: {amendment.rationale}")
                return {"drafted": False, "feasible": False}

            candidate = amend.materialise(domain, version, amendment.model_dump(),
                                          violations, ctx["run_id"])
            print(f"drafted {len(amendment.edits)} edit(s) -> candidate policy {candidate}")
            return {"drafted": True, "candidate": candidate, "parent": version}

        @task(trigger_rule="all_done")
        def enforce(result: dict, **ctx) -> dict:
            """Fail the run if the candidate policy reverses a human ruling.

            The gate is the contract, so this runs whatever the drafter did.
            """
            version = ctx["params"]["policy_version"]
            violations = result["violations"]
            if not violations:
                return {"precedents_checked": result["precedents_checked"], "violations": 0}

            lines = "\n".join(
                f"  - {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}' on "
                f"{v['established_at']}, policy {version} gives '{v['proposed_outcome']}'"
                for v in violations
            )
            raise AirflowFailException(
                f"Policy {version} reverses {len(violations)} established precedent(s):\n{lines}\n"
                f"A proposed amendment has been drafted and is being verified by amend_{domain_name}."
            )

        record_amendment(result, drafts) >> enforce(result)

    precedent_gate()

    # --------------------------------------------------------- amendment gate
    @dag(
        dag_id=f"amend_{domain_name}",
        schedule=[amendments_asset],
        start_date=START,
        catchup=False,
        max_active_runs=1,
        default_args=DEFAULTS,
        params={"policy_version": policy_param},
        tags=["policy-time-machine", domain_name, "regression"],
        doc_md="Tests the amendment the precedent gate drafted. An untested fix is an opinion.",
    )
    def amend_gate():
        @task
        def candidate_cases(**ctx) -> dict:
            """Everything the candidate has to be judged on.

            Two populations, for two different questions. The precedents ask
            "does the fix work?". The parent's own flips ask "what did the fix
            cost?" - a narrowing amendment should barely disturb them, and if
            it does, it was not narrow.

            Cases the parent never moved are not re-judged: an amendment only
            adds exceptions to its parent, so a case the parent left alone is
            decided by the same clause either way. That keeps the LLM bill
            proportional to the change rather than to the archive.
            """
            parent = ctx["params"]["policy_version"]
            candidate = amend.candidate_name(parent)
            if not store.load_policy_version(domain_name, candidate):
                print(f"no candidate {candidate} registered; nothing to verify")
                return {"candidate": candidate, "parent": parent, "precedents": [], "collateral": []}

            precedent_ids = {p.case_id for p in store.load_precedents(domain_name)}
            flip_ids = {r["case_id"] for r in store.flips_for_policy(domain_name, parent)}
            wanted = precedent_ids | flip_ids
            cases = [c for c in store.load_cases(domain_name, until=pendulum.now("UTC"))
                     if c.case_id in wanted]

            def item(c):
                return {
                    "case_id": c.case_id, "domain": c.domain,
                    "decided_at": c.decided_at.isoformat(), "payload": c.payload,
                    "actual_outcome": c.actual_outcome,
                    "prompt": build_prompt(c, domain, candidate),
                }

            print(f"verifying {candidate}: {len(precedent_ids)} precedent(s), "
                  f"{len(flip_ids)} case(s) the parent moved")
            return {
                "candidate": candidate, "parent": parent,
                "precedents": [item(c) for c in cases if c.case_id in precedent_ids],
                "collateral": [item(c) for c in cases if c.case_id in flip_ids],
            }

        @task
        def all_prompts(work: dict) -> list[str]:
            return [i["prompt"] for i in work["precedents"] + work["collateral"]]

        work = candidate_cases()

        if OFFLINE:
            @task(max_active_tis_per_dag=8)
            def verify_offline(item: dict, **ctx) -> dict:
                candidate = amend.candidate_name(ctx["params"]["policy_version"])
                return offline_verdict(_case(item), domain, candidate).model_dump()

            # One flat expansion over both populations, split again in report().
            @task
            def flatten(work: dict) -> list[dict]:
                return work["precedents"] + work["collateral"]

            verdicts = verify_offline.expand(item=flatten(work))
        else:
            verdicts = LLMOperator.partial(
                task_id="verify_judge",
                llm_conn_id=LLM_CONN_ID,
                system_prompt=SYSTEM_PROMPT,
                output_type=Verdict,
                usage_limits=UsageLimits(request_limit=3),
                max_active_tis_per_dag=8,
            ).expand(prompt=all_prompts(work)).output

        @task
        def report(work: dict, verdicts: list, **ctx) -> dict:
            """Did the fix work, and what did it disturb?"""
            items = work["precedents"] + work["collateral"]
            if not items:
                return {"verified": False, "reason": "no candidate registered"}

            by_case = {i["case_id"]: _as_verdict(v) for i, v in zip(items, verdicts)}
            n_prec = len(work["precedents"])
            precedent_verdicts = {i["case_id"]: by_case[i["case_id"]] for i in work["precedents"]}
            collateral_verdicts = {i["case_id"]: by_case[i["case_id"]] for i in work["collateral"]}

            result = amend.verify(domain, work["candidate"], work["parent"],
                                  precedent_verdicts, collateral_verdicts)
            store.save_insight(domain_name, work["parent"], "amendment_verification",
                               result, "offline" if OFFLINE else LLM_CONN_ID, ctx["run_id"])
            print(f"checked {n_prec} precedent(s) under {work['candidate']}")
            print(result["summary"])

            if not result["clears_gate"]:
                raise AirflowFailException(
                    f"The drafted amendment does not work: {work['candidate']} still reverses "
                    f"{len(result['violations'])} ruling(s). "
                    f"This policy needs a human, not another draft."
                )
            return result

        report(work, verdicts)

    amend_gate()


for _name in available_domains():
    build(_name)
