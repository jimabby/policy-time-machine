"""``adjudicate_<domain>`` - which changes does a person have to rule on?

Triggered by the flips asset. Puts the handful of genuinely contested flips in
front of a human via HITL, and turns their rulings into precedent. Emits the
precedents asset.

The two queues it can raise are the same job from both ends: ``flips`` is what
the replay produced, and ``stale`` is the rulings made about wording that has
since been rewritten - which the gate has always warned about and never had
anywhere to send.
"""

from __future__ import annotations

import pendulum
from airflow.exceptions import AirflowFailException
from airflow.providers.standard.operators.hitl import HITLOperator
from airflow.sdk import Param, dag, task

from ptm import diff, store
from ptm.models import Precedent, Verdict
from ptm_dags.common import (
    DEFAULTS,
    START,
    DomainDags,
    _assigned_users,
    _notifiers,
    _review_timeout,
)


def build(ctx: DomainDags) -> None:
    """Build ``adjudicate_{domain}``: queue the contested flips, record rulings.

    Triggered by the flips asset ``replay`` emits, and emits the precedents
    asset the gate waits on - which is the whole wiring between the three.
    """
    domain_name = ctx.name
    domain = ctx.domain
    flips_asset = ctx.flips
    precedents_asset = ctx.precedents
    policy_param = ctx.policy_param

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
            # Who the queue is addressed to, how long it waits, and who is told
            # it exists. All three were available on this operator from the
            # start and none of them was set, so a contested case sat in the UI
            # indefinitely, answerable by anybody who happened to find it.
            #
            # The timeout is safe to add only because `record` below refuses a
            # response that came from the clock: Airflow answers a timed-out
            # HITL task with `defaults`, which here is the most generous
            # outcome, and writing that into permanent precedent because nobody
            # looked would be the worst failure this pipeline has.
            assigned_users=_assigned_users(domain),
            response_timeout=_review_timeout(domain),
            notifiers=_notifiers(domain),
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
            saved, reconfirmed, revised, expired = [], [], [], []
            for f, resp in zip(flips, responses):
                chosen = (resp or {}).get("chosen_options") or []
                if not chosen:
                    continue
                # Who answered. The payload key is `responded_by_user`, a
                # HITLUser of id and name - never `user_id`, which is what this
                # read and which meant every precedent on file was attributed to
                # 'unknown'. That is the one field a permanent record cannot
                # afford to lose: a ruling nobody is named for is not a fact
                # about a person, and the gate enforces it forever either way.
                responder = (resp or {}).get("responded_by_user") or {}
                ruled_by = responder.get("id") or responder.get("name") or ""
                # A response the clock produced, not a person. Airflow answers a
                # timed-out HITL task with `defaults` and leaves
                # `responded_by_user` empty, so this is the whole difference
                # between a ruling and a shrug - and the shrug is the most
                # generous outcome in the domain.
                if (resp or {}).get("timedout") or not ruled_by:
                    expired.append(f["case_id"])
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
                    ruled_by=ruled_by,
                    note=(resp.get("params_input") or {}).get("note", ""),
                    established_at=pendulum.now("UTC"), established_by_run=f"{ctx['dag'].dag_id}::{ctx['run_id']}",
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
            if expired:
                print(f"{len(expired)} review(s) timed out and were NOT recorded: "
                      f"{expired[:10]}. Airflow answers an expired review with the "
                      f"default option, which is the most generous outcome here, and "
                      f"that is not a ruling. They stay in the queue.")
            if reconfirmed or revised:
                print(f"{len(reconfirmed)} ruling(s) re-confirmed against {version}, "
                      f"{len(revised)} changed. Each earlier ruling is archived, not "
                      f"replaced - store.precedent_history has what it said.")
                for case_id in revised:
                    print(f"  {case_id}: the ruling on file has been superseded")
            return {"precedents_recorded": len(saved), "case_ids": saved,
                    "held_back_unconfirmed": len(held_back),
                    "expired_unanswered": expired,
                    "reconfirmed": reconfirmed, "revised": revised}

        record(flips, reviews.output, held_back)

    adjudicate()
