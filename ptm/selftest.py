"""Run the entire Policy Time Machine loop without Airflow.

Same code the DAGs call, driven by a plain loop instead of the scheduler. Use
it to rehearse the demo, to check a new domain YAML, or in CI.

    python -m ptm.selftest              # expenses under v2
    python -m ptm.selftest refunds v2   # any domain, any candidate version
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from . import cache, calibration, cost, diff, disparity, preflight, proposal
from . import rules as rules_engine
from . import stability, store
from .config import JUDGE_MODEL, load_domain
from .judge import build_prompt, offline_verdict
from .models import Precedent
from .seed import seed_domain


def month_windows(start: datetime, end: datetime):
    """The data intervals the scheduler would hand each backfilled run."""
    cur = start
    while cur < end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield cur, min(nxt, end)
        cur = nxt


def main(domain_name: str = "expenses", version: str = "v2") -> None:
    print(f"seeding ... {seed_domain(domain_name, force=True)}")
    domain = load_domain(domain_name)
    unit = domain.impact_unit
    baseline_version = domain.in_force

    # --- read the policy before spending anything on it --------------------
    # Free, offline, and the only check here that can run before a single case
    # has been judged. What it catches is not an expensive run but an
    # unusable one: a rule with no clause number decides cases that then
    # cannot be attributed to any sentence.
    for readable in (baseline_version, version):
        print(preflight.describe(preflight.structural(domain, readable), readable))

    # --- what backfilling replay_<domain> does, one run per month -----------
    total_cases = 0
    all_flips = []
    all_cases = []
    all_verdicts = {}
    for lo, hi in month_windows(datetime(2024, 9, 1), datetime(2026, 9, 1)):
        cases = store.load_cases(domain_name, until=hi, since=lo)
        if not cases:
            continue
        verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
        # The baseline pass: the same cases under the policy already in force.
        # Without it a flip can be seen but not explained - see ptm.diff.attribute.
        baseline = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}
        # The domain belongs in the run id. save_replay clears prior rows by
        # run id - safe in Airflow, where run ids are per-DAG and therefore
        # already per-domain - so sharing one across domains would make a
        # refunds run silently delete the expenses results.
        run_id = f"selftest__{domain_name}__{lo:%Y-%m}"
        found = diff.flips(cases, verdicts, domain, baseline=baseline)
        s = diff.summarise(found, len(cases), domain)
        store.save_replay(run_id, domain_name, version, "actual", len(cases), found,
                          s["net_impact"], verdicts,
                          segments=diff.segment_stats(cases, found, domain),
                          case_segments=diff.case_segment_rows(cases, domain),
                          ledger=cost.zero(), baseline_version=baseline_version,
                          baseline_verdicts=baseline)
        total_cases += len(cases)
        all_flips += found
        all_cases += cases
        all_verdicts.update(verdicts)

    s = diff.summarise(all_flips, total_cases, domain)
    print(f"\nreplayed {s['cases_replayed']} decisions under policy {version}")
    print(f"  {s['flips']} outcomes change ({s['flip_rate']:.1%}, "
          f"{s['flip_rate_lo']:.1%}-{s['flip_rate_hi']:.1%} at 95% on {total_cases} cases)")
    print(f"  {s['loosening']} more generous  {unit} {s['impact_loosening']:,.0f}")
    print(f"  {s['tightening']} more strict    {unit} {s['impact_tightening']:,.0f}")
    print(f"  net {unit} {s['net_impact']:,.0f}")

    # --- which clause is doing it ------------------------------------------
    print(f"\nwhat in policy {version} causes the change "
          f"(baseline: policy {baseline_version}):")
    for row in s["by_clause"]:
        print(f"  {row['clause']:<34} {row['flips']:>4} flips ({row['share']:>5.1%})  "
              f"net {unit} {row['net_impact']:>9,.0f}  conf {row['mean_confidence']:.0%}")
    print(f"\n  {s['policy_driven_flips']} of {s['flips']} changes are caused by policy "
          f"{version} (net {unit} {s['policy_driven_net_impact']:,.0f}).")
    print(f"  {s['deviation_flips']} are cases the recorded outcome got wrong under policy "
          f"{baseline_version} too, so they are not this proposal's doing.")

    # --- and who it lands on ------------------------------------------------
    segments = diff.segment_stats(all_cases, all_flips, domain)
    if segments:
        print("\nblast radius:")
        current = None
        for row in segments:
            if row["field"] != current:
                current = row["field"]
                print(f"  by {current}:")
            print(f"    {row['value']:<22} {row['flips']:>4} of {row['cases']:>4} cases "
                  f"({row['flip_rate']:>5.1%})  net {unit} "
                  f"{row['impact_loosening'] - row['impact_tightening']:>9,.0f}")

    # --- and whether it lands evenly ---------------------------------------
    # The blast radius above is a table, and a table of twelve percentages is
    # the shape of information a room skims. This asks the question underneath
    # it out loud, pools the comparison, and refuses to make a finding out of a
    # segment too small to support one.
    print()
    print(disparity.describe(disparity.analyse(segments, domain), domain))

    # --- what it would cost to judge this for real -------------------------
    forecast = cost.estimate(
        sum(len(build_prompt(c, domain, version)) for c in all_cases),
        len(all_cases), JUDGE_MODEL)
    print(f"\noffline judge cost nothing. The same replay against {JUDGE_MODEL}:")
    print(f"  ~{forecast['estimated_input_tokens']:,} in + "
          f"{forecast['estimated_output_tokens']:,} out tokens"
          f"  = USD {forecast['estimated_cost_usd']:,.2f} (estimated)")
    print(f"  a baseline pass doubles that to USD "
          f"{forecast['estimated_cost_usd'] * 2:,.2f}, which is what buys the "
          f"attribution above.")

    # --- confirm the flips before anyone rules on them ---------------------
    # What judge_stability_<domain> does with target=flips. An error bar on the
    # whole replay does not tell you whether *this* flip is real, and precedent
    # is permanent - a verdict the judge will not repeat must not become one.
    by_id = {c.case_id: c for c in all_cases}
    top = stability.flips_to_confirm(all_flips, 25)
    repeats = 3
    confirm_samples = [
        {"case_id": f.case_id, "sample_idx": i,
         **offline_verdict(by_id[f.case_id], domain, version)
         .model_dump(include={"outcome", "confidence"})}
        for f in top for i in range(repeats)
    ]
    confirmations = stability.confirm(
        confirm_samples, {f.case_id: f.new_outcome for f in top})
    store.save_flip_stability(domain_name, version, confirmations)
    tags = {c.case_id: ("stable" if c.stable else "unstable") for c in confirmations}
    for f in all_flips:
        f.stability = tags.get(f.case_id, "")
    print()
    print(stability.describe_confirmations(confirmations))

    # --- what adjudicate_<domain> does, with a human at the keyboard --------
    contested = diff.select_for_review(all_flips, domain)
    sent_dev = [f for f in contested if f.attribution == diff.DEVIATION]
    print(f"\n{len(contested)} flips routed to a human out of {len(all_flips)} "
          f"({len(contested) - len(sent_dev)} caused by {version}, "
          f"{len(sent_dev)} pre-existing deviations):")
    for f in contested:
        print(f"  {f.case_id}  {f.actual_outcome} -> {f.new_outcome}  "
              f"({f.direction}, {f.attribution or '-'}, {unit} {f.impact:,.0f}, "
              f"conf {f.confidence:.0%})")

    # Stand in for the reviewer: they side with history on the big tightenings.
    for f in contested:
        store.save_precedent(Precedent(
            case_id=f.case_id, domain=domain_name,
            correct_outcome=f.actual_outcome if f.direction == "tightening" else f.new_outcome,
            ruled_by="finance.lead", note="Adjudicated during selftest.",
            established_at=datetime.now(), established_by_run="selftest",
        ))
    store.mark_reviewed(domain_name, version, [f.case_id for f in contested])
    print(f"\n{len(contested)} precedents established")

    # --- the deviations, reported separately from the proposal -------------
    dev = diff.deviations(all_flips)
    if dev:
        print(f"\nseparately: {len(dev)} recorded outcomes disagree with policy "
              f"{baseline_version}, which is in force today. Not caused by {version}, "
              f"but worth someone's attention:")
        for f in dev[:5]:
            print(f"  {f.case_id}  recorded {f.actual_outcome}, both policies say "
                  f"{f.new_outcome}  ({unit} {f.impact:,.0f})")
        if len(dev) > 5:
            print(f"  ... and {len(dev) - 5} more, worth {unit} "
                  f"{sum(f.impact for f in dev[5:]):,.0f} between them")

    # --- is the precedent set consistent with itself? ----------------------
    conflicts = diff.precedent_conflicts(store.precedents_with_payload(domain_name), domain)
    print(f"\nprecedent self-consistency: {len(conflicts)} conflict(s)")
    for c in conflicts:
        where = ", ".join(f"{k}={v}" for k, v in c.signature)
        print(f"  [{where}] -> "
              + "; ".join(f"{o} ({', '.join(ids)})" for o, ids in c.outcomes.items()))

    # --- what precedent_gate_<domain> does ---------------------------------
    precedents = store.load_precedents(domain_name)
    ids = [p.case_id for p in precedents]
    # By id, not "load everything and filter": a default limit truncating the
    # set would make the gate check fewer precedents than exist and still pass.
    cases = store.load_cases(domain_name, until=datetime.now(), case_ids=ids)
    assert len(cases) == len(ids), "the gate would silently skip a precedent"
    verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
    violations = diff.precedent_violations(verdicts, precedents)
    # The same question asked of the policy already in force: three violations
    # means something very different when the status quo already has three.
    in_force = {c.case_id: offline_verdict(c, domain, baseline_version) for c in cases}
    pre_existing = {v["case_id"] for v in diff.precedent_violations(in_force, precedents)}
    introduced = [v for v in violations if v["case_id"] not in pre_existing]
    print(f"\ngate: policy {version} vs {len(precedents)} precedents -> {len(violations)} violation(s)")
    for v in violations:
        also = f"  (so does {baseline_version})" if v["case_id"] in pre_existing else ""
        print(f"  {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}', "
              f"{version} gives '{v['proposed_outcome']}'{also}")
    print(f"  {len(introduced)} introduced by {version}; "
          f"{len(violations) - len(introduced)} the policy in force ({baseline_version}) "
          f"already reverses")
    if not violations:
        print("\nGATE PASSES.")
    elif introduced:
        print(f"\nGATE FAILS - {version} reverses {len(introduced)} human ruling(s) that "
              f"{baseline_version} does not.")
    else:
        # Worth distinguishing. The proposal broke nothing the status quo had
        # not broken already, and charging it for these would be the same
        # mistake as charging it for the deviations above.
        print(f"\nGATE FAILS - but every reversal is one policy {baseline_version} already "
              f"makes. These rulings contradict the status quo, not {version} specifically.")

    # --- is the judge right, not merely consistent? ------------------------
    # The gate above judged every case a human ruled on, which makes this free:
    # the same verdicts, scored against the answers. Nothing else in this
    # pipeline compares the judge to anything but itself, and a judge can
    # reproduce its own verdicts perfectly while being reliably wrong.
    print()
    print(calibration.describe(
        calibration.score(domain, version, verdicts, precedents)))

    # --- what judge_stability_<domain> does --------------------------------
    picked = stability.sample_cases(all_cases, 25)
    repeats = 3
    samples = [
        {"case_id": c.case_id, "sample_idx": i,
         **offline_verdict(c, domain, version).model_dump(include={"outcome", "confidence"})}
        for c in picked for i in range(repeats)
    ]
    result = stability.analyse(samples, repeats)
    print(f"\n{stability.describe(result, s['flip_rate'])}")
    print("  (the offline judge is deterministic, so this is inert until "
          "PTM_OFFLINE=0 - see ptm/stability.py)")

    # --- can the threshold sweep be trusted? -------------------------------
    # The sweep is computed from offline_rules, so it is worth exactly what
    # those rules are worth. This is the number that says what that is.
    stored = {case_id: offline_verdict(by_id[case_id], domain, version)
              for case_id in all_verdicts}
    agreement = rules_engine.agreement(
        domain, version, domain.offline_rules.get(version, []), all_cases, stored)
    print()
    print(rules_engine.describe(agreement))
    print("  (offline these verdicts came from these same rules, so this is 100% by "
          "construction - like the stability figure, it is inert until PTM_OFFLINE=0)")

    # --- what the second measurement costs ---------------------------------
    # The loop this whole project is built around is *edit a clause, measure
    # again*, and without a cache the second measurement costs exactly what the
    # first did - including for the hundreds of cases the edit cannot reach.
    prompts = {c.case_id: build_prompt(c, domain, version) for c in all_cases}
    items = [{"case_id": c.case_id, "domain": domain_name,
              "cache_key": cache.key(prompts[c.case_id], JUDGE_MODEL),
              "prompt_chars": len(prompts[c.case_id])} for c in all_cases]
    misses, _ = cache.split(items)
    cache.remember(domain_name, version, JUDGE_MODEL,
                   {i["case_id"]: (i["cache_key"], i["prompt_chars"],
                                   all_verdicts[i["case_id"]]) for i in misses})
    second_misses, second_hits = cache.split(items)
    saved = cache.saving([i for i in items if i["case_id"] in second_hits], JUDGE_MODEL)
    print(f"\nre-running the same replay: {len(second_hits)} of {len(items)} verdicts "
          f"come from cache, {len(second_misses)} would be judged again")
    print(f"  against {JUDGE_MODEL} that is USD {saved['estimated_saved_usd']:,.2f} of the "
          f"USD {forecast['estimated_cost_usd']:,.2f} not spent twice.")
    print("  the key is the prompt, so editing one clause invalidates exactly the cases "
          "whose prompt changed - and nothing else.")

    # --- and finally: write the next version of the policy -----------------
    # Everything above narrows the question to a sentence and a number. This is
    # the step that writes the sentence, and the only reason a machine is
    # allowed to is the gate below: the draft is re-judged against every ruling
    # a human has made, and one that reverses a ruling is reported as such
    # however well it argues for itself.
    found = proposal.evidence(domain, version)
    patch = proposal.offline_patch(domain, version, found)
    print()
    print(proposal.describe(patch))
    if patch.edits:
        amended = rules_engine.with_rules(
            domain, version, proposal.rules_for(domain, version, patch, found) or [])
        after = diff.precedent_violations(
            {c.case_id: offline_verdict(c, amended, version) for c in cases}, precedents)
        print(f"  against the {len(precedents)} rulings on file: "
              f"{len(violations)} reversed today, {len(after)} under the amendment")
        print(f"  nothing was written - `python -m ptm.proposal {domain_name} {version} "
              f"--write` drafts it as {proposal.next_version(domain, version)}, which every "
              f"DAG then treats as an ordinary policy version.")


if __name__ == "__main__":
    main(*sys.argv[1:3])
