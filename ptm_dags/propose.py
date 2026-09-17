"""``propose_<domain>`` - what should the next version of the policy say?

Manual. Reads everything the pipeline measured and drafts the next version of
the policy, then puts that draft through the regression suite that guards every
other version. The only DAG here where a model writes rather than judges, and
the gate at the end of it is why that is allowed.

Drafting and adopting are separate acts: the draft lands in include/drafts/,
labelled everywhere it appears as written by software and approved by nobody.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import Param, dag, task

from ptm import proposal, rules, store
from ptm.config import LLM_CONN_ID, OFFLINE
from ptm.judge import build_prompt, offline_verdict
from ptm.models import PolicyPatch, RuleSet, Verdict
from ptm_dags.common import (
    DEFAULTS,
    JUDGE_ID,
    START,
    SYSTEM_PROMPT,
    DomainDags,
    _as_verdict,
    _case,
    _item,
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
    """Build ``propose_{domain}``: draft the next version, then gate the draft.

    Writing is optional and off the critical path: the draft lands in
    include/drafts/ and is labelled as written by software everywhere it appears.
    """
    domain_name = ctx.name
    domain = ctx.domain
    policy_param = ctx.policy_param

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
