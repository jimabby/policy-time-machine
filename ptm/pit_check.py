"""Demonstrate that point-in-time replay is not a technicality.

Replays every case twice: once as the world knew it on the decision date
(what the DAGs do), and once with today's facts joined on (what a hand-rolled
backtest does). Prints how many cases the naive version gets wrong.

    python -m ptm.pit_check
    python -m ptm.pit_check refunds v2
"""

from __future__ import annotations

import sys
from datetime import datetime

from . import cli
from .config import available_domains, load_domain
from .diff import flips
from .judge import offline_verdict
from .store import load_cases, query

USAGE = """usage:
  python -m ptm.pit_check [domain] [version]

Replay every case twice - once as the world knew it on the decision date, once
with today's facts joined on - and print how many cases the naive version gets
wrong. The naive join is what every hand-rolled backtest does; Airflow's data
intervals are what stop this engine doing it.

  domain    defaults to 'expenses'
  version   defaults to 'v2'

Needs a domain that declares a pit_field: a slowly-changing fact about the
subject of a case, which is the only thing a naive join can get wrong."""


def run(domain_name: str = "expenses", version: str = "v2") -> None:
    domain = load_domain(domain_name)
    if not domain.pit_field:
        raise SystemExit(f"domain {domain_name!r} declares no pit_field")
    if version not in domain.policies:
        raise SystemExit(f"unknown policy version {version!r} for {domain_name}; "
                         f"have {sorted(domain.policies)}")
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


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.pit_check [domain] [version]``.

    Kept as a wrapper around :func:`run` rather than folded into it, because
    this is the entry point and the other one is the demonstration. It was the
    last ``python -m ptm.*`` command that read ``--help`` as work to do - it ran
    the whole comparison - and the last that could not be pointed at a second
    domain from a shell, despite taking the arguments for it.
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    domain_name = args[0] if args else "expenses"
    version = args[1] if len(args) > 1 else "v2"
    try:
        run(domain_name, version)
    except FileNotFoundError:
        print(f"ERROR no domain config {domain_name!r}; have {available_domains()}",
              file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
