"""Replay imported history offline, without seeding or replacing any cases."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from uuid import uuid4

from . import cost, diff, provenance, store
from .config import load_domain
from .judge import offline_verdict


def replay(domain_name: str, version: str, since: datetime | None = None,
           until: datetime | None = None, limit: int | None = None) -> dict:
    domain = load_domain(domain_name)
    if version not in domain.policies:
        raise ValueError(f"unknown policy {version!r}")
    if not domain.offline_rules.get(version) or not domain.offline_rules.get(domain.in_force):
        raise ValueError("offline replay requires rules for both candidate and baseline")
    since = since or datetime.min
    until = until or datetime.now(timezone.utc)
    if store._bound(since) >= store._bound(until):
        raise ValueError("--since must precede --until (exclusive)")
    if limit is not None and limit < 1:
        raise ValueError("--limit must be positive")
    cases = store.load_cases(domain_name, until=until, since=since, limit=limit,
                             newest_first=bool(limit))
    eligible = store.query("SELECT COUNT(*) n FROM cases WHERE domain=? AND decided_at>=? AND decided_at<?",
                           (domain_name, store._bound(since), store._bound(until)))[0]["n"]
    snapshot = provenance.capture(domain, version, cases, domain.in_force, since, until, eligible)
    run_id = f"cli::{uuid4()}"
    provenance.begin(run_id, domain_name, version, snapshot)
    verdicts = {c.case_id: offline_verdict(c, domain, version) for c in cases}
    baseline = {c.case_id: offline_verdict(c, domain, domain.in_force) for c in cases}
    flips = diff.flips(cases, verdicts, domain, baseline=baseline)
    summary = diff.summarise(flips, len(cases), domain)
    store.save_replay(run_id, domain_name, version, "actual", len(cases), flips,
                      summary["net_impact"], verdicts, diff.segment_stats(cases, flips, domain),
                      cost.zero(), domain.in_force, baseline, diff.case_segment_rows(cases, domain),
                      snapshot=snapshot)
    return {"run_id": store.scoped_run_id(domain_name, run_id), "eligible": eligible,
            "selected": len(cases), "capped": len(cases) < eligible, **summary}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ptm.replay", description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("version")
    parser.add_argument("--since", type=lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")))
    parser.add_argument("--until", type=lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")))
    parser.add_argument("--limit", type=int)
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    store.init_db()
    try:
        print(json.dumps(replay(args.domain, args.version, args.since, args.until, args.limit), indent=2))
    except (ValueError, LookupError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
