"""What every DAG in this package needs.

Split out of the single module these five DAGs used to share, unchanged: the
cost ledger, the cache merge that puts a cached verdict back exactly where the
case it answers sits, the case payload that travels through XCom without the
prompt in it, and the rails a review queue is raised with.

The underscored helpers are package-private rather than module-private: the
underscore says they are no part of any interface outside :mod:`ptm_dags`, and
the builders import them because they were written for exactly these five
callers. They kept their names through the split so that the code that moved
is, line for line, the code that was here before.

Nothing here builds a DAG. :class:`DomainDags` is the one piece of state the
builders share - the domain, its two assets and the policy parameter - built
once per domain by :func:`context` so that the asset URIs are written down in
exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.sdk import Asset, Param, task

from ptm import cache, cost
from ptm.config import JUDGE_MODEL, LLM_CONN_ID, OFFLINE, DomainConfig, load_domain
from ptm.judge import SYSTEM_PROMPT, build_prompt
from ptm.models import Case, Verdict

if not OFFLINE:
    from pydantic_ai.usage import UsageLimits

    from ptm.metered import USAGE_KEY, metered_operator

    # The provider's LLMOperator with the vendor's own token counts kept on a
    # second XCom key. Same operator, same return value; see ptm/metered.py for
    # why it wraps the hook rather than re-implementing execute.
    LLMOperator = metered_operator()

__all__ = [
    "AirflowFailException", "DEFAULTS", "DomainDags", "JUDGE_ID", "LLMOperator",
    "LLM_CONN_ID", "OFFLINE", "START", "SYSTEM_PROMPT", "UsageLimits", "context",
    "merge", "to_judge",
]

START = pendulum.datetime(2024, 9, 1, tz="UTC")
DEFAULTS = {"owner": "policy-time-machine", "retries": 1}
#: What answered a prompt, for the cache key and the ledger. The offline judge
#: is a different answerer from any model, and serving one's verdict as the
#: other's would make a comparison between them agree with itself perfectly.
JUDGE_ID = JUDGE_MODEL if not OFFLINE else "offline"


def judge_configuration() -> dict:
    """Resolve the actual connection at task execution; never record credentials."""
    if OFFLINE:
        return {"model": "offline"}
    from airflow.sdk import BaseHook

    from ptm.provenance import digest
    connection = BaseHook.get_connection(LLM_CONN_ID)
    model = connection.extra_dejson.get("model")
    if not model:
        raise AirflowFailException("Set the model in the AI connection's extra.model field")
    return {"model": model, "connection_id": LLM_CONN_ID,
            "connection_type": connection.conn_type, "endpoint": connection.host or "",
            "configuration_hash": digest(connection.extra_dejson)}


def _review_timeout(domain) -> timedelta | None:
    """How long a review waits before the task gives up. None waits forever."""
    hours = float(domain.review.response_timeout_hours or 0)
    return timedelta(hours=hours) if hours > 0 else None


def _assigned_users(domain) -> list[dict] | None:
    """The reviewers a queue is addressed to, in the shape HITLOperator wants.

    ``HITLUser`` is a TypedDict of ``id`` and ``name``, so plain dicts are the
    whole contract and nothing has to be imported to build one. None means the
    domain named nobody, and the queue is then open to anybody who can reach the
    UI - which is the demo's setting and should not be a deployment's, because
    ``ruled_by`` is what makes a precedent a fact about a person.
    """
    users = [u.strip() for u in domain.review.assigned_users if u and u.strip()]
    return [{"id": user, "name": user} for user in users] or None


def _notifiers(domain) -> list:
    """Notifier instances named by the domain, skipping any that will not import.

    Resolved at parse time and defensively: a Slack webhook that has been
    removed, or a provider that is not installed in this image, must cost the
    notification and never the adjudication. A DAG that fails to parse because
    nobody could be told about a queue is strictly worse than a queue nobody is
    told about.
    """
    import importlib

    out = []
    for path in domain.review.notifiers:
        module, _, name = str(path).rpartition(".")
        if not module or not name:
            print(f"review.notifiers: {path!r} is not a dotted import path; skipped")
            continue
        try:
            resolved = getattr(importlib.import_module(module), name)
        except Exception as exc:  # any import problem here is the same problem
            print(f"review.notifiers: cannot import {path!r} ({exc}); reviews for "
                  f"{domain.name} will be raised without notifying anybody")
            continue
        out.append(resolved() if isinstance(resolved, type) else resolved)
    return out


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
    judge = judge_configuration()
    item = {
        "case_id": case.case_id,
        "domain": case.domain,
        "decided_at": case.decided_at.isoformat(),
        "payload": case.payload,
        "actual_outcome": case.actual_outcome,
        "actual_rationale": case.actual_rationale,
        "prompt_chars": len(prompt),
        "baseline_prompt_chars": 0,
        "judge": judge,
    }
    if cacheable:
        item["cache_key"] = cache.judgment_key(case, domain, version, judge)
    if baseline_version:
        # A baseline pass judges every case twice. Its prompt is sized and keyed
        # separately rather than added to the candidate's, because the two
        # passes hit the cache independently: editing the candidate policy does
        # not change the in-force one, so its half is served from cache and the
        # ledger has to be able to say so.
        baseline_prompt = build_prompt(case, domain, baseline_version)
        item["baseline_prompt_chars"] = len(baseline_prompt)
        if cacheable:
            item["baseline_cache_key"] = cache.judgment_key(case, domain, baseline_version, judge)
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
            items[0]["domain"] if items else "", version,
            items[0].get("judge", {}).get("model", JUDGE_ID) if items else JUDGE_ID,
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


def _version_from_context(param: str = "policy_version") -> str:
    """The policy version for the running task.

    Read from the task context rather than passed in, so the prompt-rendering
    tasks stay one-liners and cannot drift from the judge's own version.
    """
    from airflow.sdk import get_current_context

    return (get_current_context()["params"].get(param) or "").strip()


@dataclass(frozen=True)
class DomainDags:
    """One domain, as the five builders need it.

    A frozen record rather than five parameters, because every builder wants a
    different subset of it and a positional signature would have to be edited
    in six places the first time a sixth DAG wants the flips asset.
    """

    #: The domain's name, which is also the suffix of every DAG id built from it.
    name: str
    #: The loaded domain config - outcomes, policies, review policy, segments.
    domain: DomainConfig
    #: Emitted by the replay, consumed by adjudication.
    flips: Asset
    #: Emitted by adjudication, consumed by the gate.
    precedents: Asset
    #: The version under test. One Param instance, shared by the DAGs that take
    #: it, so the enum of versions it describes is assembled once.
    policy_param: Param


def context(domain_name: str) -> DomainDags:
    """Everything the builders share about one domain.

    The asset URIs are here and nowhere else: a replay that emitted
    ``ptm://<domain>/flips`` while adjudication waited on a URI spelled even
    slightly differently would not fail, it would simply never trigger.
    """
    domain = load_domain(domain_name)
    return DomainDags(
        name=domain_name,
        domain=domain,
        flips=Asset(f"ptm://{domain_name}/flips"),
        precedents=Asset(f"ptm://{domain_name}/precedents"),
        policy_param=Param("v2", type="string", title="Policy version to test",
                           description=f"One of: {', '.join(sorted(domain.policies))}"),
    )

