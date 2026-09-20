"""Reproducible replay inputs and coverage, without requiring Airflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta

from . import store
from .config import load_domain
from .judge import PROMPT, SYSTEM_PROMPT
from .models import Verdict


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def policy_inputs(domain, version: str, judge: dict | None = None) -> dict:
    return {"policy_text": domain.policy_text(version),
            "domain_config": domain.model_dump(mode="json", exclude={"policies", "draft_versions", "offline_rules"}),
            "offline_rules": domain.offline_rules.get(version, []),
            "case_template": domain.case_template, "outcomes": domain.outcomes,
            "judge_instructions": domain.judge_instructions,
            "prompt_template": PROMPT, "system_prompt": SYSTEM_PROMPT,
            "output_schema": Verdict.model_json_schema(),
            "judge": judge or {"model": "offline"}}


def capture(domain, version: str, cases: list, baseline: str = "",
            since: datetime | None = None, until: datetime | None = None,
            eligible: int | None = None, judge: dict | None = None) -> dict:
    policy = policy_inputs(domain, version, judge)
    inputs = [c.model_dump(mode="json") for c in cases]
    base = policy_inputs(domain, baseline, judge) if baseline else None
    return {"format": "ptm-replay/1", "policy": policy, "policy_hash": digest(policy),
            "baseline_version": baseline, "baseline": base,
            "baseline_hash": digest(base), "inputs": inputs, "input_hash": digest(inputs),
            "case_ids": [c.case_id for c in cases], "selected": len(cases),
            "eligible": len(cases) if eligible is None else eligible,
            "since": store._bound(since) if since else None,
            "until": store._bound(until) if until else None}


def begin(run_id: str, domain: str, version: str, snapshot: dict) -> None:
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO replay_snapshots VALUES (?,?,?,?,?,?)",
                  (store.scoped_run_id(domain, run_id), domain, version, store._stamp(),
                   "pending", json.dumps(snapshot)))


def prepared(run_id: str, domain: str) -> dict | None:
    rows = store.query("SELECT snapshot FROM replay_snapshots WHERE run_id=? AND domain=?",
                       (store.scoped_run_id(domain, run_id), domain))
    return json.loads(rows[0]["snapshot"]) if rows else None


def failed(run_id: str, domain: str) -> None:
    with store.conn() as c:
        c.execute("UPDATE replay_snapshots SET status='failed' WHERE run_id=? AND status='pending'",
                  (store.scoped_run_id(domain, run_id),))


def resolve_pending(domain: str, version: str | None = None,
                    older_than_hours: float = 0.0) -> list[dict]:
    """Mark stranded ``pending`` runs failed, and say which ones went.

    A run is recorded before judging starts so that a crash is visible rather
    than invisible, and :func:`coverage` refuses to call a version complete
    while one is outstanding. That is the right default and it had no way out:
    ``replay_<domain>`` resolves its own failures through an
    ``on_failure_callback`` and the offline CLI did not resolve its at all, so a
    single Ctrl-C left a row that nothing ages out, nothing prunes and nothing
    can clear - and the version read as incomplete from then on.

    ``older_than_hours`` is the guard that makes this safe to run against a live
    deployment: a replay genuinely in flight is pending too, and sweeping it
    would mark a running job failed. Default 0 resolves everything pending,
    which is what you want on a laptop and not what you want on a scheduler.
    """
    cutoff = store._bound(store.now_utc() - timedelta(hours=max(older_than_hours, 0.0)))
    where = "domain=? AND status='pending' AND created_at<=?"
    params: tuple = (domain, cutoff)
    if version:
        where += " AND policy_version=?"
        params += (version,)
    rows = store.query(
        f"SELECT run_id, policy_version, created_at FROM replay_snapshots WHERE {where}",
        params)
    if rows:
        with store.conn() as c:
            c.execute(f"UPDATE replay_snapshots SET status='failed' WHERE {where}", params)
    return rows


def describe_resolved(rows: list[dict], domain: str) -> str:
    """What :func:`resolve_pending` did, as the CLI prints it."""
    if not rows:
        return (f"no pending replay run for {domain}; nothing was holding coverage "
                f"open and nothing was changed")
    lines = [f"marked {len(rows)} stranded run(s) failed for {domain}:"]
    lines += [f"  {r['policy_version']}  started {r['created_at'][:19]}  {r['run_id']}"
              for r in sorted(rows, key=lambda r: (r["policy_version"], r["created_at"]))]
    lines.append("  their results were never published, so nothing was lost; coverage "
                 "stops counting them against completeness.")
    return "\n".join(lines)


def snapshots(domain_name: str, version: str, include_inputs: bool = False) -> list[dict]:
    domain = load_domain(domain_name)
    rows = store.query("""SELECT * FROM replay_snapshots WHERE domain=? AND policy_version=?
                          ORDER BY created_at DESC, run_id""", (domain_name, version))
    if not rows and version not in domain.policies:
        raise LookupError(f"unknown policy version {version!r}")

    # Every run's case ids at once, loaded once. This used to be a load_cases
    # call *inside* the loop, so a version with N runs paid N full hydrations of
    # overlapping case sets - and coverage() calls this on every dashboard load.
    # Twenty-four backfill runs over the shipped fixture is already 180ms; a
    # deployment with a few hundred runs turns the panel into a spinner. The
    # runs overlap heavily by construction (a manual replay re-reads history a
    # backfill already covered), which is exactly what makes one load cheaper
    # than N.
    wanted = {case_id for row in rows
              for case_id in json.loads(row["snapshot"]).get("case_ids", [])}
    loaded = (store.load_cases(domain_name, until=datetime.max, case_ids=sorted(wanted))
              if wanted else [])
    by_id = {c.case_id: c.model_dump(mode="json") for c in loaded}
    # Also once: the policy digest depends on the judge configuration recorded in
    # the snapshot, and runs judged the same way share an answer.
    policy_cache: dict[str, str | None] = {}

    def policy_digest(version_name: str, judge: dict) -> str | None:
        """The current digest for this (version, judge), or None if unreadable."""
        key = f"{version_name}\0{digest(judge)}"
        if key not in policy_cache:
            try:
                policy_cache[key] = digest(policy_inputs(domain, version_name, judge))
            except (KeyError, FileNotFoundError):
                policy_cache[key] = None
        return policy_cache[key]

    result = []
    for row in rows:
        snap = json.loads(row.pop("snapshot"))
        reasons = []
        judge = (snap.get("policy") or {}).get("judge")
        if judge is None:
            reasons.append("candidate policy is unavailable; archived text is preserved")
        else:
            current = policy_digest(version, judge)
            if current is None:
                reasons.append("candidate policy is unavailable; archived text is preserved")
            elif current != snap["policy_hash"]:
                reasons.append("policy or judge instructions changed")
            base = snap.get("baseline_version")
            if base:
                current_base = policy_digest(base, judge)
                if current_base is None:
                    reasons.append("baseline policy is unavailable; archived text is preserved")
                elif current_base != snap["baseline_hash"]:
                    reasons.append("baseline policy changed")
        current_inputs = [by_id.get(i) for i in snap["case_ids"]]
        if digest(current_inputs) != snap["input_hash"]:
            reasons.append("historical inputs changed or are missing")
        if not include_inputs:
            snap = {k: v for k, v in snap.items()
                    if k not in {"inputs", "policy", "baseline", "verdicts", "baseline_verdicts"}}
        result.append({**row, **snap, "stale": bool(reasons), "stale_reasons": reasons})
    return result


#: Stamped into a snapshot export and checked on the way back in, the way
#: :data:`ptm.precedents.FORMAT` is. A file this cannot read is refused by name
#: rather than half-imported.
SNAPSHOT_FORMAT = "ptm-snapshots/1"


def export_snapshots(domain_name: str, version: str) -> dict:
    """Every archived replay input for one version, in a file another database can read.

    ``--snapshots`` has always been able to get this evidence *out*; nothing
    could put it back. That is the same gap the precedent export had before
    :mod:`ptm.precedents` gained ``--import``, and CI makes the same argument
    about it: an export nothing can read back is a backup nobody has tested.

    It matters more here than it looks. A snapshot is what makes a replay
    checkable after the fact - the policy text as judged, the case inputs as
    hydrated, both verdict sets, and the hashes tying them together - so being
    unable to move it means a regression suite can travel between environments
    while the evidence for how it was established cannot.
    """
    rows = store.query(
        """SELECT run_id, domain, policy_version, created_at, status, snapshot
           FROM replay_snapshots WHERE domain=? AND policy_version=?
           ORDER BY created_at, run_id""", (domain_name, version))
    return {
        "format": SNAPSHOT_FORMAT,
        "domain": domain_name,
        "policy_version": version,
        "exported_at": store.now_utc().isoformat(timespec="seconds"),
        "snapshots": [{**row, "snapshot": json.loads(row["snapshot"])} for row in rows],
        "note": "What each replay was actually given: the policy text as judged, the "
                "case inputs as hydrated on their decision dates, both sets of verdicts "
                "and the hashes tying them together. Snapshots contain case data and "
                "carry the same access restrictions as the source history. A trimmed "
                "snapshot has kept its hashes and dropped its bodies - see "
                "ptm.store.trim_snapshots.",
    }


def import_snapshots(domain_name: str, payload: dict) -> dict:
    """Merge an exported snapshot set in. Never replaces one already on file."""
    if payload.get("format") != SNAPSHOT_FORMAT:
        raise LookupError(
            f"this file says its format is {payload.get('format')!r}, not "
            f"{SNAPSHOT_FORMAT!r}. Refusing to guess: evidence read wrongly is worse "
            f"than evidence missing.")
    came_from = payload.get("domain", "")
    if came_from and came_from != domain_name:
        raise LookupError(
            f"this file holds {came_from!r} snapshots and you asked to import them into "
            f"{domain_name!r}. Case ids are per domain, so these would archive inputs "
            f"for cases that are not the ones they were captured from.")
    rows = [{**row, "domain": domain_name} for row in payload.get("snapshots", [])]
    missing = [r.get("run_id", "?") for r in rows
               if not all(k in r for k in ("run_id", "policy_version", "created_at",
                                           "status", "snapshot"))]
    if missing:
        raise LookupError(f"{len(missing)} snapshot(s) are missing required fields: "
                          f"{missing[:5]}")
    result = store.import_snapshots(rows)
    return {"domain": domain_name, "read": len(rows), **result,
            "note": "A run id names the run that made it, so a snapshot already on file "
                    "is the same snapshot; nothing is overwritten. Importing an export "
                    "twice adds nothing."}


def describe_snapshot_import(result: dict) -> str:
    """What a snapshot import did, as the CLI prints it."""
    if not result["read"]:
        return f"that file holds no snapshots for {result['domain']}; nothing to import"
    lines = [f"read {result['read']} snapshot(s) for {result['domain']}: "
             f"{result['added']} added, {result['already_here']} already on file"]
    if not result["added"]:
        lines.append("  a no-op, which is what re-importing an export is meant to be")
    return "\n".join(lines)


def coverage(domain_name: str, version: str) -> dict:
    domain = load_domain(domain_name)
    if version not in domain.policies:
        raise LookupError(f"unknown version {version!r}")
    cases = store.query("SELECT case_id, decided_at FROM cases WHERE domain=?", (domain_name,))
    pointers = store.query("SELECT case_id,run_id FROM replay_cases WHERE domain=? AND policy_version=?",
                           (domain_name, version))
    replayed = {r["case_id"] for r in pointers}
    active_ids = {r["run_id"] for r in pointers}
    missing = sorted(r["case_id"] for r in cases if r["case_id"] not in replayed)
    runs = snapshots(domain_name, version)
    for run in runs:
        run["active"] = run["run_id"] in active_ids
    unverified = sorted(active_ids - {r["run_id"] for r in runs})
    dates = sorted(r["decided_at"] for r in cases)
    return {"domain": domain_name, "version": version, "eligible": len(cases),
            "replayed": len(cases) - len(missing), "missing": len(missing),
            "missing_case_ids": missing, "first_case": dates[0] if dates else None,
            "last_case": dates[-1] if dates else None,
            "unverified_run_ids": unverified,
            "complete": bool(cases) and not missing and bool(runs)
                        and not unverified and not any(r["status"] == "pending" for r in runs)
                        and all(r["status"] == "complete" and not r["stale"] for r in runs if r["active"]),
            "runs": runs,
            "note": "Coverage spans all imported cases. Pending runs have not published results; "
                    "legacy runs without snapshots have unverified provenance. "
                    "Model configuration is the configuration recorded at execution."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ptm.provenance", description=__doc__)
    parser.add_argument("domain")
    # Optional, because --resolve is about a domain rather than about one
    # version: a run stranded under a draft nobody remembers is exactly the one
    # you cannot name.
    parser.add_argument("version", nargs="?")
    parser.add_argument("--snapshots", action="store_true", help="include complete archived inputs")
    parser.add_argument("--export", action="store_true",
                        help="archived inputs in a form another database can import")
    parser.add_argument("--import", dest="import_path", metavar="FILE",
                        help="merge an exported snapshot set in; never overwrites one on file")
    parser.add_argument("--resolve", action="store_true",
                        help="mark stranded pending runs failed so they stop holding "
                             "coverage open; nothing that published results is touched")
    parser.add_argument("--older-than", type=float, default=0.0, metavar="HOURS",
                        help="with --resolve, only runs that started at least this "
                             "many hours ago - so a replay still in flight survives")
    parser.add_argument("-o", "--out")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    store.init_db()
    try:
        if args.resolve:
            # Prose, not JSON: this one changes something, and what it changed
            # is the answer. The others are read models a script pipes onward.
            resolved = resolve_pending(args.domain, args.version, args.older_than)
            print(describe_resolved(resolved, args.domain))
            return 0
        if not args.version:
            parser.exit(2, "error: a policy version is required unless --resolve is given\n")
        if args.import_path:
            from pathlib import Path
            payload = json.loads(Path(args.import_path).read_text(encoding="utf-8"))
            print(describe_snapshot_import(import_snapshots(args.domain, payload)))
            return 0
        result = (export_snapshots(args.domain, args.version) if args.export
                  else snapshots(args.domain, args.version, True) if args.snapshots
                  else coverage(args.domain, args.version))
        body = json.dumps(result, indent=2)
        if args.out:
            from pathlib import Path
            Path(args.out).write_text(body + "\n", encoding="utf-8")
        else:
            print(body)
    except (LookupError, ValueError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
