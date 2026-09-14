"""Demonstrate that point-in-time replay is not a technicality.

Replays every case twice: once as the world knew it on the decision date
(what the DAGs do), and once with today's facts joined on (what a hand-rolled
backtest does). Prints how many cases the naive version gets wrong.

    python -m ptm.pit_check
"""

from __future__ import annotations

from datetime import datetime

from .config import load_domain
from .diff import flips
from .judge import offline_verdict
from .store import load_cases, query


def main(domain_name: str = "expenses", version: str = "v2") -> None:
    domain = load_domain(domain_name)
    if not domain.pit_field:
        raise SystemExit(f"domain {domain_name!r} declares no pit_field")
    cases = load_cases(domain_name, until=datetime.now())
    if not cases:
        raise SystemExit("no cases - run `python -m ptm.seed` first")

    correct = {c.case_id: offline_verdict(c, domain, version) for c in cases}

    # The naive join: every case sees the subject's *current* attributes.
    latest = {
        r["subject_id"]: r["value"]
        for r in query(
            """SELECT f.subject_id, f.value FROM subject_facts f
               WHERE f.key = ? AND f.known_from =
                 (SELECT MAX(g.known_from) FROM subject_facts g
                   WHERE g.subject_id = f.subject_id AND g.key = f.key)""",
            (domain.pit_field,),
        )
    }
    subject = {r["case_id"]: r["subject_id"]
               for r in query("SELECT case_id, subject_id FROM cases WHERE domain = ?",
                              (domain_name,))}
    # Count the point-in-time flips before the payloads are overwritten below.
    pit_flips = len(flips(cases, correct, domain))

    # A subject with no recorded fact has nothing to join on, naively or
    # otherwise. Skipping is right and silence is not: the comparison is only
    # meaningful over the cases both replays actually saw.
    ungrounded = [c.case_id for c in cases
                  if subject.get(c.case_id) not in latest]
    comparable = [c for c in cases if c.case_id not in set(ungrounded)]
    for c in comparable:
        c.payload[domain.pit_field] = latest[subject[c.case_id]]
    naive = {c.case_id: offline_verdict(c, domain, version) for c in comparable}

    wrong = [c.case_id for c in comparable
             if correct[c.case_id].outcome != naive[c.case_id].outcome]
    print(f"point-in-time replay : {pit_flips} flips")
    print(f"naive replay         : wrong on {len(wrong)} / {len(comparable)} cases")
    if ungrounded:
        print(f"  ({len(ungrounded)} case(s) skipped: no {domain.pit_field!r} fact on file "
              f"for the subject, so there is nothing for a naive join to get wrong)")
    for k in wrong[:5]:
        print(f"  {k}: correct '{correct[k].outcome}', naive '{naive[k].outcome}'")
    if len(wrong) > 5:
        print(f"  ... and {len(wrong) - 5} more")


if __name__ == "__main__":
    main()
