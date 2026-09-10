"""Run the entire Policy Time Machine loop without Airflow.

Same code the DAGs call, driven by a plain loop instead of the scheduler. Use
it to rehearse the demo, to check a new domain YAML, or in CI.

    python -m ptm.selftest
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import diff, store
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


def main(domain_name: str = "expenses", version: str = "v2") -> None:
    print(f"seeding … {seed_expenses()}")
    domain = load_domain(domain_name)

    # --- what backfilling replay_<domain> does, one run per month -----------
    total_cases = 0
    all_flips = []
    for i, (lo, hi) in enumerate(month_windows(datetime(2024, 9, 1), datetime(2026, 9, 1))):
        cases = store.load_cases(domain_name, until=hi, since=lo)
        if not cases:
            continue
        verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
        run_id = f"selftest__{lo:%Y-%m}"
        store.save_verdicts(run_id, domain_name, version, verdicts)
        found = diff.flips(cases, verdicts, domain)
        store.save_flips(run_id, domain_name, version, found)
        s = diff.summarise(found, len(cases), domain)
        store.record_run(run_id, domain_name, version, "actual", len(cases), len(found), s["net_impact"])
        total_cases += len(cases)
        all_flips += found

    s = diff.summarise(all_flips, total_cases, domain)
    print(f"\nreplayed {s['cases_replayed']} decisions under policy {version}")
    print(f"  {s['flips']} outcomes change ({s['flip_rate']:.1%})")
    print(f"  {s['loosening']} more generous  {domain.impact_unit} {s['impact_loosening']:,.0f}")
    print(f"  {s['tightening']} more strict    {domain.impact_unit} {s['impact_tightening']:,.0f}")
    print(f"  net {domain.impact_unit} {s['net_impact']:,.0f}")

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
    print("\nGATE FAILS - policy would reverse a human ruling." if violations
          else "\nGATE PASSES.")


if __name__ == "__main__":
    main()
