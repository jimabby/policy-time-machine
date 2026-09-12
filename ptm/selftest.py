"""Run the entire Policy Time Machine loop without Airflow.

Same code the DAGs call, driven by a plain loop instead of the scheduler. Use
it to rehearse the demo, to check a new domain YAML, or in CI.

    python -m ptm.selftest
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import ai, amend, analysis, diff, store
from .config import load_domain
from .judge import offline_verdict
from .models import Precedent
from .seed import seed_expenses


def month_windows(start: datetime, end: datetime):
    """The data intervals the scheduler would hand each backfilled run."""
    cur = start
    while cur < end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield cur, min(nxt, end)
        cur = nxt


def replay(domain, domain_name: str, version: str):
    """What backfilling replay_<domain> does: one run per month of history."""
    total_cases, all_flips = 0, []
    for lo, hi in month_windows(datetime(2024, 9, 1), datetime(2026, 9, 1)):
        cases = store.load_cases(domain_name, until=hi, since=lo)
        if not cases:
            continue
        verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
        run_id = f"selftest__{version}__{lo:%Y-%m}"
        store.save_verdicts(run_id, domain_name, version, verdicts)
        found = diff.flips(cases, verdicts, domain)
        store.save_flips(run_id, domain_name, version, found)
        s = diff.summarise(found, len(cases), domain)
        store.record_run(run_id, domain_name, version, "actual", len(cases), len(found), s["net_impact"])
        total_cases += len(cases)
        all_flips += found
    return total_cases, all_flips


def main(domain_name: str = "expenses", version: str = "v2") -> None:
    print(f"seeding … {seed_expenses()}")
    domain = load_domain(domain_name)

    # Replay the policy actually in force first. It is the control: history was
    # decided under v1, so the only flips it can find are the cases where a
    # reviewer departed from the rulebook. Anything else means the harness is
    # measuring itself rather than the change.
    baseline_cases, baseline_flips = replay(domain, domain_name, "v1")
    b = diff.summarise(baseline_flips, baseline_cases, domain)
    print(f"\ncontrol: replaying the policy already in force (v1)")
    print(f"  {b['flips']} of {b['cases_replayed']} differ ({b['flip_rate']:.1%}) "
          f"- reviewer discretion, not the proposal")

    total_cases, all_flips = replay(domain, domain_name, version)
    s = diff.summarise(all_flips, total_cases, domain)
    print(f"\nreplayed {s['cases_replayed']} decisions under policy {version}")
    print(f"  {s['flips']} outcomes change ({s['flip_rate']:.1%})")
    print(f"  {s['loosening']} more generous  {domain.impact_unit} {s['impact_loosening']:,.0f}")
    print(f"  {s['tightening']} more strict    {domain.impact_unit} {s['impact_tightening']:,.0f}")
    print(f"  net {domain.impact_unit} {s['net_impact']:,.0f}")

    # --- coverage and cohorts, which no flip list can show ------------------
    cov = analysis.clause_coverage(domain, version)
    print(f"\nclause coverage: {cov['exercised']}/{cov['declared']} exercised"
          + (f", never reached: {', '.join(cov['unexercised'])}" if cov['unexercised'] else ""))
    print(f"  {cov['decided_by_no_clause']} cases ({cov['no_clause_share']:.1%}) "
          f"decided by no clause at all")

    report = analysis.cohort_report(domain, version)
    hardest = analysis.disproportionate(report)
    print(f"\nwho bears it:")
    for c in hardest[:4]:
        print(f"  {c['field']}={c['cohort']:<18} {c['flip_rate']:>6.1%} flip rate, "
              f"{c['disproportion']}x the population, net {domain.impact_unit} {c['net_impact']:,.0f}")
    if not hardest:
        print("  no cohort is disproportionately affected")

    cmp_ = analysis.compare(domain, "v1", version)
    print(f"\nv1 vs {version}: {cmp_['disagreements']} of {cmp_['compared_cases']} cases decided differently")
    print(f"  {cmp_['verdict']}")

    # --- what the analysis tasks do ----------------------------------------
    top = sorted(all_flips, key=lambda f: -f.impact)[:25]
    brief = ai.offline_brief(s, top, domain, version, cov, report)
    themes = ai.offline_themes(all_flips, domain)
    store.save_insight(domain_name, version, "brief", brief.model_dump(), "offline", "selftest")
    store.save_insight(domain_name, version, "themes", themes.model_dump(), "offline", "selftest")
    store.save_insight(domain_name, version, "coverage", cov, "computed", "selftest")
    store.save_insight(domain_name, version, "cohorts", {"breakdowns": report}, "computed", "selftest")
    print(f"\nbrief [{brief.verdict}]: {brief.headline}")
    for spot in brief.blind_spots:
        print(f"  blind spot: {spot}")
    for t in themes.themes:
        print(f"  theme: {t.name:<24} {t.case_count:>4} cases  ({t.direction})")

    # --- what adjudicate_<domain> does, with a human at the keyboard --------
    contested = diff.select_for_review(all_flips, domain)
    print(f"\n{len(contested)} flips routed to a human out of {len(all_flips)}:")
    for f in contested:
        print(f"  {f.case_id}  {f.actual_outcome} -> {f.new_outcome}  "
              f"({f.direction}, {domain.impact_unit} {f.impact:,.0f}, conf {f.confidence:.0%})")

    # Stand in for the reviewer: they side with history on the big tightenings.
    for f in contested:
        store.save_precedent(Precedent(
            case_id=f.case_id, domain=domain_name,
            correct_outcome=f.actual_outcome if f.direction == "tightening" else f.new_outcome,
            ruled_by="finance.lead", note="Adjudicated during selftest.",
            established_at=datetime.now(), established_by_run="selftest",
        ))
    store.mark_reviewed([f.case_id for f in contested])
    print(f"\n{len(contested)} precedents established")

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

    if not violations:
        print("\nGATE PASSES.")
        return

    amendment = ai.offline_amendment(violations, domain, version)
    store.save_insight(domain_name, version, "amendment", amendment.model_dump(),
                       "offline", "selftest")
    print("\nGATE FAILS - policy would reverse a human ruling.")
    print(f"amendment drafted: {len(amendment.edits)} edit(s) proposed")

    # --- what amend_<domain> does: test the fix rather than trusting it -----
    candidate = amend.materialise(domain, version, amendment.model_dump(), violations, "selftest")
    prec_verdicts = {c.case_id: offline_verdict(c, domain, candidate) for c in cases}
    flipped = [r["case_id"] for r in store.flips_for_policy(domain_name, version)]
    collateral_cases = [c for c in store.load_cases(domain_name, until=datetime.now())
                        if c.case_id in set(flipped)]
    coll_verdicts = {c.case_id: offline_verdict(c, domain, candidate) for c in collateral_cases}

    result = amend.verify(domain, candidate, version, prec_verdicts, coll_verdicts)
    store.save_insight(domain_name, version, "amendment_verification", result,
                       "offline", "selftest")
    print(f"\nverifying candidate {candidate}:")
    print(f"  {result['summary']}")
    print("  GATE NOW PASSES." if result["clears_gate"] else "  FIX DOES NOT WORK.")


if __name__ == "__main__":
    main()
