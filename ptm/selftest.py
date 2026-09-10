"""Run the entire Policy Time Machine loop without Airflow.

Same code the DAGs call, driven by a plain loop instead of the scheduler. Use
it to rehearse the demo, to check a new domain YAML, or in CI.

    python -m ptm.selftest              # expenses under v2
    python -m ptm.selftest refunds v2   # any domain, any candidate version
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from . import cost, diff, stability, store
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
    print(f"seeding ... {seed_domain(domain_name)}")
    domain = load_domain(domain_name)
    unit = domain.impact_unit
    baseline_version = domain.in_force

    # --- what backfilling replay_<domain> does, one run per month -----------
    total_cases = 0
    all_flips = []
    all_cases = []
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
                          ledger=cost.zero(), baseline_version=baseline_version,
                          baseline_verdicts=baseline)
        total_cases += len(cases)
        all_flips += found
        all_cases += cases

    s = diff.summarise(all_flips, total_cases, domain)
    print(f"\nreplayed {s['cases_replayed']} decisions under policy {version}")
    print(f"  {s['flips']} outcomes change ({s['flip_rate']:.1%})")
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

    # --- what adjudicate_<domain> does, with a human at the keyboard --------
    contested = diff.select_for_review(all_flips, domain)
    print(f"\n{len(contested)} flips routed to a human out of {len(all_flips)}:")
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

    # --- is the precedent set consistent with itself? ----------------------
    conflicts = diff.precedent_conflicts(store.precedents_with_payload(domain_name), domain)
    print(f"\nprecedent self-consistency: {len(conflicts)} conflict(s)")
    for c in conflicts:
        where = ", ".join(f"{k}={v}" for k, v in c.signature)
        print(f"  [{where}] -> "
              + "; ".join(f"{o} ({', '.join(ids)})" for o, ids in c.outcomes.items()))

    # --- what precedent_gate_<domain> does ---------------------------------
    precedents = store.load_precedents(domain_name)
    ids = {p.case_id for p in precedents}
    cases = [c for c in store.load_cases(domain_name, until=datetime.now()) if c.case_id in ids]
    verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
    violations = diff.precedent_violations(verdicts, precedents)
    print(f"\ngate: policy {version} vs {len(precedents)} precedents -> {len(violations)} violation(s)")
    for v in violations:
        print(f"  {v['case_id']}: {v['ruled_by']} ruled '{v['established_outcome']}', "
              f"{version} gives '{v['proposed_outcome']}'")
    print("\nGATE FAILS - policy would reverse a human ruling." if violations
          else "\nGATE PASSES.")

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



if __name__ == "__main__":
    main(*sys.argv[1:3])
