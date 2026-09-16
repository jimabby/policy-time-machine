"""The regression suite for organisational judgment, without Airflow.

``precedent_gate_<domain>`` is the point of this project: every ruling a human
made is re-judged under the candidate policy, and a policy that reverses one
fails. It was also the one measurement here with no way to run it from a shell.
Everything else - the lint, the preflight, the sweep, the calibration score, the
export bundle - has a ``python -m ptm.*`` entry point precisely so it can run in
CI and on a laptop with no key. The gate, the thing the README calls "the
point", could only be reached by starting Airflow and triggering a DAG.

So this is that DAG's ``enforce`` task, reading rather than judging. Verdicts
come from what has already been stored - whatever the last gate or replay put on
file - so it costs nothing and it is scoring the same numbers the dashboard
shows. ``--offline-judge`` fills gaps with the deterministic judge, which is
free and exact but is the *fixture* answering, so it says so.

    python -m ptm.gate expenses v2                 # every reversal fails
    python -m ptm.gate expenses v2 --introduced-only
    python -m ptm.gate expenses v2 --offline-judge

**A precedent with no verdict on file is a refusal, not a pass.** That is the
one thing this must get right. The DAG re-judges every precedent case and fails
when one cannot be loaded, on the grounds that a regression suite silently
checking less than it reports is worse than no regression suite; a CLI reading
stored verdicts has the same obligation and an easier way to get it wrong, so
an unchecked precedent exits 2 and names itself rather than being counted as
agreement.
"""

from __future__ import annotations

import sys
from datetime import datetime

from . import cli, diff, store
from .config import load_domain
from .judge import offline_verdict
from .models import Verdict

#: Exit codes. 1 is "the gate failed", which is the answer; 2 is "the gate could
#: not be run", which is not an answer and must not be confused with one by a
#: shell that only checks for zero.
FAILED = 1
CANNOT_RUN = 2


def _stored(domain_name: str, version: str) -> dict[str, Verdict]:
    return {
        case_id: Verdict(outcome=row["outcome"], rationale=row["rationale"],
                         confidence=row["confidence"],
                         policy_clause=row["policy_clause"] or "")
        for case_id, row in store.latest_verdicts(domain_name, version).items()
    }


def check(domain_name: str, version: str, baseline_version: str | None = None,
          offline_judge: bool = False) -> dict:
    """Which precedents this policy reverses, and which the status quo already does.

    ``baseline_version`` defaults to the domain's ``in_force``. Judging it too is
    what stops "this proposal reverses 3 rulings" reading as the proposal's fault
    when the policy already in force reverses the same 3 - the same separation
    the DAG makes, from the same stored verdicts.
    """
    domain = load_domain(domain_name)
    if version not in domain.policies:
        raise LookupError(f"unknown policy version {version!r} for {domain_name}; "
                          f"have {sorted(domain.policies)}")
    baseline_version = domain.in_force if baseline_version is None else baseline_version
    if baseline_version and baseline_version not in domain.policies:
        raise LookupError(f"unknown baseline version {baseline_version!r} for "
                          f"{domain_name}; have {sorted(domain.policies)}")

    precedents = store.load_precedents(domain_name)
    wanted = {p.case_id for p in precedents}
    cases = store.load_cases(domain_name, until=datetime.now(),
                             case_ids=sorted(wanted)) if wanted else []
    on_file = {c.case_id: c for c in cases}

    candidate = {k: v for k, v in _stored(domain_name, version).items() if k in wanted}
    baseline = ({k: v for k, v in _stored(domain_name, baseline_version).items()
                 if k in wanted} if baseline_version else {})

    judged_offline: list[str] = []
    if offline_judge:
        for case_id in sorted(wanted):
            case = on_file.get(case_id)
            if case is None:
                continue
            if case_id not in candidate:
                candidate[case_id] = offline_verdict(case, domain, version)
                judged_offline.append(case_id)
            if baseline_version and case_id not in baseline:
                baseline[case_id] = offline_verdict(case, domain, baseline_version)

    unchecked = sorted(wanted - set(candidate))
    missing_cases = sorted(wanted - set(on_file))
    violations = diff.precedent_violations(candidate, precedents)
    pre_existing = ({v["case_id"] for v in diff.precedent_violations(baseline, precedents)}
                    if baseline else set())
    stale = diff.stale_precedents(precedents, domain, version)
    stale_ids = {r["case_id"] for r in stale}
    conflicts = diff.precedent_conflicts(
        store.precedents_with_payload(domain_name), domain)
    return {
        "domain": domain_name,
        "version": version,
        "baseline_version": baseline_version or "",
        "precedents": len(precedents),
        "checked": len(candidate),
        "unchecked": unchecked,
        # A ruling whose case is gone cannot be re-judged by anything, which is
        # a different problem from one nothing has judged yet.
        "cases_missing": missing_cases,
        "judged_offline": judged_offline,
        "violations": [{**v, "stale": v["case_id"] in stale_ids,
                        "pre_existing": v["case_id"] in pre_existing}
                       for v in violations],
        "introduced": [v["case_id"] for v in violations
                       if v["case_id"] not in pre_existing],
        "in_force_violations": sorted(pre_existing),
        "stale": stale,
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
    }


def describe(result: dict) -> str:
    """The gate's answer, in the words the DAG logs it in."""
    version, baseline = result["version"], result["baseline_version"]
    lines = [f"gate: policy {version} vs {result['checked']} of {result['precedents']} "
             f"precedent(s)"
             + (f", against {baseline} in force" if baseline else "")]
    if result["judged_offline"]:
        lines.append(f"  {len(result['judged_offline'])} of them judged here by the "
                     f"offline rules, which were written from the same policy the "
                     f"reviewer was shown - that half of this is the fixture agreeing "
                     f"with itself, not a judge being checked")
    for row in result["violations"]:
        lines.append(f"  {row['case_id']}: {row['ruled_by']} ruled "
                     f"'{row['established_outcome']}' on {row['established_at']}, "
                     f"{version} gives '{row['proposed_outcome']}'"
                     + (f"  (so does {baseline})" if row["pre_existing"] else ""))
    if result["violations"]:
        introduced = len(result["introduced"])
        lines.append(f"  {introduced} introduced by {version}; "
                     f"{len(result['violations']) - introduced} the policy in force "
                     f"already reverses, so fixing those is a separate job from this "
                     f"proposal")
        shaky = [v["case_id"] for v in result["violations"] if v["stale"]]
        if shaky:
            lines.append(f"  {len(shaky)} of them rest on a ruling made about a clause "
                         f"that has since changed ({shaky[:5]}); re-adjudicating those is "
                         f"a different fix from editing the policy")
    else:
        lines.append(f"  no established ruling is reversed by {version}")
    if result["conflicts"]:
        lines.append(f"  the precedent set contains {len(result['conflicts'])} internal "
                     f"conflict(s), so some reversal here may be unavoidable until two "
                     f"humans agree with each other")
    if result["unchecked"]:
        lines.append(f"  {len(result['unchecked'])} precedent(s) have no verdict on file "
                     f"under {version} and were NOT checked: {result['unchecked'][:10]}")
    if result["cases_missing"]:
        lines.append(f"  {len(result['cases_missing'])} precedent(s) have no case on file "
                     f"and can never be re-judged: {result['cases_missing'][:10]}")
    lines.append(diff.describe_stale(result["stale"], version))
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.gate [domain] [version] [--introduced-only] [--offline-judge]
                     [--baseline VERSION | --no-baseline]

Re-check every human ruling against a policy version and exit non-zero if one is
reversed. This is precedent_gate_<domain> without Airflow, reading the verdicts
already on file rather than paying to judge them again.

  domain              defaults to 'expenses'
  version             defaults to 'v2'
  --introduced-only   fail only on reversals this version introduces, not on the
                      ones the policy in force already makes. The right setting
                      for a repository whose status quo reverses a ruling and
                      whose candidate is not to blame for it.
  --offline-judge     judge any precedent with no verdict on file using the
                      offline rules. Free and deterministic, and it is the
                      fixture answering rather than a judge - reported as such.
  --baseline VERSION  compare against this version instead of the domain's
                      in_force; --no-baseline skips the comparison entirely.

Exit 0 the gate passed, 1 it failed, 2 it could not be run - an unknown version,
or a precedent nothing has judged. The third is not a pass and is kept apart
from one on purpose."""


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0

    introduced_only = "--introduced-only" in args
    offline_judge = "--offline-judge" in args
    no_baseline = "--no-baseline" in args
    baseline: str | None = None
    if "--baseline" in args:
        index = args.index("--baseline")
        baseline = args[index + 1] if index + 1 < len(args) else ""
        if not baseline or baseline.startswith("-"):
            print(f"ERROR --baseline needs a version\n\n{USAGE}", file=sys.stderr)
            return CANNOT_RUN
        args.pop(index + 1)
    if no_baseline:
        baseline = ""

    positional = [a for a in args if not a.startswith("-")]
    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    store.init_db()
    try:
        result = check(domain_name, version, baseline, offline_judge)
    except (LookupError, FileNotFoundError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return CANNOT_RUN

    print(describe(result))

    if not result["precedents"]:
        print(f"\nno human ruling has been recorded for {domain_name} yet, so there is "
              f"no regression suite to run. Adjudicate some flips first - this passing "
              f"means nothing until it does.")
        return 0
    if result["unchecked"]:
        print(f"\nERROR {len(result['unchecked'])} precedent(s) have never been judged "
              f"under {version}, so this gate would be reporting a pass it did not "
              f"check. Run precedent_gate_{domain_name}, or --offline-judge to settle "
              f"them from the offline rules.", file=sys.stderr)
        return CANNOT_RUN

    failing = (result["introduced"] if introduced_only
               else [v["case_id"] for v in result["violations"]])
    if failing:
        scope = ("introduced by " + version if introduced_only else "reversed by " + version)
        print(f"\nGATE FAILS: {len(failing)} established ruling(s) {scope}.",
              file=sys.stderr)
        return FAILED
    if result["violations"] and introduced_only:
        print(f"\nGATE PASSES: {len(result['violations'])} ruling(s) are reversed here, "
              f"and every one of them is a reversal policy "
              f"{result['baseline_version'] or 'in force'} already makes. None is this "
              f"proposal's doing.")
        return 0
    print(f"\nGATE PASSES: {version} reverses none of the "
          f"{result['checked']} ruling(s) on file.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
