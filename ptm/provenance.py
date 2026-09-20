"""Reproducible replay inputs and coverage, without requiring Airflow."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime

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


def snapshots(domain_name: str, version: str, include_inputs: bool = False) -> list[dict]:
    domain = load_domain(domain_name)
    rows = store.query("""SELECT * FROM replay_snapshots WHERE domain=? AND policy_version=?
                          ORDER BY created_at DESC, run_id""", (domain_name, version))
    if not rows and version not in domain.policies:
        raise LookupError(f"unknown policy version {version!r}")
    result = []
    for row in rows:
        snap = json.loads(row.pop("snapshot"))
        reasons = []
        try:
            current = policy_inputs(domain, version, snap["policy"]["judge"])
            if digest(current) != snap["policy_hash"]:
                reasons.append("policy or judge instructions changed")
        except (KeyError, FileNotFoundError):
            reasons.append("candidate policy is unavailable; archived text is preserved")
        base = snap.get("baseline_version")
        if base:
            try:
                if digest(policy_inputs(domain, base, snap["policy"]["judge"])) != snap["baseline_hash"]:
                    reasons.append("baseline policy changed")
            except (KeyError, FileNotFoundError):
                reasons.append("baseline policy is unavailable; archived text is preserved")
        cases = store.load_cases(domain_name, until=datetime.max, case_ids=snap["case_ids"])
        by_id = {c.case_id: c.model_dump(mode="json") for c in cases}
        current_inputs = [by_id.get(i) for i in snap["case_ids"]]
        if digest(current_inputs) != snap["input_hash"]:
            reasons.append("historical inputs changed or are missing")
        if not include_inputs:
            snap = {k: v for k, v in snap.items()
                    if k not in {"inputs", "policy", "baseline", "verdicts", "baseline_verdicts"}}
        result.append({**row, **snap, "stale": bool(reasons), "stale_reasons": reasons})
    return result


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
    parser.add_argument("version")
    parser.add_argument("--snapshots", action="store_true", help="include complete archived inputs")
    parser.add_argument("-o", "--out")
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    store.init_db()
    try:
        result = (snapshots(args.domain, args.version, True) if args.snapshots
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
