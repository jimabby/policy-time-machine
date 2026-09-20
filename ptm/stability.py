"""How much of a measured flip rate is the judge disagreeing with itself.

The headline number this project produces is *"147 of 600 outcomes change"*. The
obvious objection is: **is that the policy, or is that the model?** A language
model asked the same question twice does not always answer the same way, and a
flip rate quoted without that number is quoted without an error bar.

So measure it. Take a sample of cases, judge each one several times under the
*same* policy, and count how often the judge contradicts itself. Every
disagreement found here is a flip that might not be real.

    disagreement_rate = cases where repeated judging disagreed / cases sampled

The offline judge is deterministic and scores exactly zero, which is worth
stating rather than hiding: offline, this measures nothing, and the number only
becomes meaningful with ``PTM_OFFLINE=0``. That honesty is the point - a
stability check that cannot fail is not a check.
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime

from . import cli
from .config import OFFLINE, load_domain
from .models import Case, Flip, FlipConfirmation, StabilityReport


def sample_cases(cases: list[Case], n: int, seed: int = 7) -> list[Case]:
    """A deterministic, chronologically spread subset of cases.

    Deterministic so two stability runs are comparable, and spread across the
    period rather than drawn from one end, because a judge's consistency can
    itself vary with the kind of case - and the kinds of case are not evenly
    distributed through history.
    """
    if n <= 0 or n >= len(cases):
        return list(cases)
    rng = random.Random(seed)
    ordered = sorted(cases, key=lambda c: c.decided_at)
    # Take one case from each of n equal slices of the period, so the sample
    # cannot collapse onto a single month.
    step = len(ordered) / n
    picked = []
    for i in range(n):
        lo, hi = int(i * step), max(int(i * step) + 1, int((i + 1) * step))
        picked.append(ordered[rng.randrange(lo, min(hi, len(ordered)))])
    return picked


def flips_to_confirm(flips_: list[Flip], n: int) -> list[Flip]:
    """The flips worth paying to re-judge: the ones that will be acted on.

    Ordered by money at stake, because a flip nobody will ever look at does not
    need an error bar. ``n <= 0`` means all of them.
    """
    ordered = sorted(flips_, key=lambda f: (-f.impact, f.case_id))
    return ordered if n <= 0 else ordered[:n]


def confirm(samples: list[dict], recorded: dict[str, str]) -> list[FlipConfirmation]:
    """Did re-judging reproduce each recorded flip?

    ``samples`` are the same ``case_id``/``outcome``/``confidence`` dicts
    :func:`analyse` consumes; ``recorded`` maps a case to the outcome the replay
    filed for it.

    A flip counts as stable only when every sample agrees *and* agrees with what
    was recorded. The second half matters: a judge can be perfectly
    self-consistent on re-judging and still land somewhere other than the run
    that produced the flip, and treating that as confirmation would launder a
    contradiction into a precedent.
    """
    by_case: dict[str, list[dict]] = {}
    for s in samples:
        by_case.setdefault(s["case_id"], []).append(s)

    out: list[FlipConfirmation] = []
    for case_id, group in sorted(by_case.items()):
        outcomes = Counter(s["outcome"] for s in group)
        modal, modal_n = outcomes.most_common(1)[0]
        was = recorded.get(case_id, "")
        out.append(FlipConfirmation(
            case_id=case_id,
            samples=len(group),
            outcomes=dict(outcomes),
            modal_outcome=modal,
            agreement=round(modal_n / len(group), 3),
            stable=len(outcomes) == 1 and (not was or modal == was),
            recorded_outcome=was,
        ))
    # Least agreement first: the flips you can trust least, first.
    out.sort(key=lambda c: (c.stable, c.agreement, c.case_id))
    return out


def describe_confirmations(confirmations: list[FlipConfirmation]) -> str:
    """A reading of a confirmation pass, for logs and the DAG's return."""
    if not confirmations:
        return "no flips re-judged; nothing to confirm."
    bad = [c for c in confirmations if not c.stable]
    lines = [f"re-judged {len(confirmations)} flips "
             f"{confirmations[0].samples}x each under the same policy",
             f"  {len(confirmations) - len(bad)} reproduced, {len(bad)} did not"]
    if bad:
        lines.append("  the ones that did not are the judge changing its mind, not the "
                     "policy moving, and are held back from the human queue:")
        for c in bad[:10]:
            detail = ", ".join(f"{o}x{n}" for o, n in sorted(c.outcomes.items()))
            recorded = f", recorded '{c.recorded_outcome}'" if c.recorded_outcome else ""
            lines.append(f"    {c.case_id}: {detail}{recorded}")
    else:
        lines.append("  every flip reproduced; the queue is safe to act on.")
    return "\n".join(lines)


def analyse(samples: list[dict], samples_per_case: int) -> StabilityReport:
    """Turn repeated verdicts into a disagreement rate.

    ``samples`` are dicts of ``case_id``, ``outcome`` and ``confidence`` - one
    per (case, repeat). Cases with a single sample are counted as sampled but can
    never be unstable, which keeps the rate honest when a judge task failed.
    """
    by_case: dict[str, list[dict]] = {}
    for s in samples:
        by_case.setdefault(s["case_id"], []).append(s)

    unstable = []
    for case_id, group in by_case.items():
        outcomes = Counter(s["outcome"] for s in group)
        if len(outcomes) < 2:
            continue
        modal, modal_n = outcomes.most_common(1)[0]
        confidences = [s["confidence"] for s in group]
        unstable.append({
            "case_id": case_id,
            "outcomes": dict(outcomes),
            "modal_outcome": modal,
            # 1.0 means every repeat agreed; 0.5 means a coin flip between two.
            "agreement": round(modal_n / len(group), 3),
            "samples": len(group),
            "confidence_spread": round(max(confidences) - min(confidences), 3),
            "mean_confidence": round(sum(confidences) / len(confidences), 3),
        })
    # Least agreement first: the case the judge is most confused by, first.
    unstable.sort(key=lambda r: (r["agreement"], -r["confidence_spread"]))

    sampled = len(by_case)
    return StabilityReport(
        cases_sampled=sampled,
        samples_per_case=samples_per_case,
        unstable_cases=len(unstable),
        disagreement_rate=round(len(unstable) / sampled, 4) if sampled else 0.0,
        unstable=unstable,
    )


def describe(report: StabilityReport, flip_rate: float | None = None) -> str:
    """A one-paragraph reading of the result, for logs and the DAG's return."""
    if report.cases_sampled == 0:
        return "no cases sampled; nothing to say about judge stability."
    if report.samples_per_case < 2:
        return (f"{report.cases_sampled} cases sampled once each - at least 2 samples "
                f"per case are needed to detect disagreement.")
    lines = [
        f"judged {report.cases_sampled} cases {report.samples_per_case}x each under the same policy",
        f"  {report.unstable_cases} disagreed with themselves "
        f"({report.disagreement_rate:.1%} disagreement rate)",
    ]
    if report.unstable_cases == 0:
        lines.append("  the judge is self-consistent on this sample; the measured flip "
                     "rate is the policy, not the model.")
    elif flip_rate:
        share = report.disagreement_rate / flip_rate if flip_rate else 0.0
        lines.append(f"  against a {flip_rate:.1%} flip rate, up to {share:.0%} of the "
                     f"measured change could be judge noise rather than policy.")
    return "\n".join(lines)


# ------------------------------------------------------------------- the CLI

#: What the offline judge makes this measurement worth, said once so the CLI and
#: any caller reading :func:`run` report it in the same words.
INERT_NOTE = (
    "PTM_OFFLINE=1: the offline judge is deterministic, so it agrees with itself by "
    "construction and this figure is 0% whatever a real judge would do. That is not a "
    "clean bill of health - it means the instrument is switched off. The sampling, the "
    "fan-out, the confirmation and the storage are exactly the ones a paid run uses; "
    "only the answer is free. Set PTM_OFFLINE=0 for a number that means something.")


def run(domain_name: str, version: str, target: str = "sample", cases: int = 25,
        repeats: int = 3, seed: int = 7, run_id: str = "") -> dict:
    """Judge the same cases repeatedly and record how often the answer moved.

    This is ``judge_stability_<domain>``'s measurement without Airflow, and it
    was the last one in the project that could not be taken from a shell - the
    gap :mod:`ptm.gate` opens by describing about itself. Everything else here
    is reachable from a terminal precisely so it can run in CI and on a laptop
    with no key; this one has to *judge*, so from a shell it judges with the
    offline judge and says what that is worth (:data:`INERT_NOTE`).

    ``target='flips'`` asks the other question the DAG asks: not "how noisy is
    this judge" but "will this particular verdict survive being asked again",
    which is what keeps model noise out of the human queue and therefore out of
    permanent precedent.

    Deliberately **uncached**, like the DAG: :func:`ptm.judge.offline_verdict`
    is called directly rather than through :mod:`ptm.cache`, because repeating
    one prompt *is* the measurement and a cache would answer every repeat with
    the first verdict and report a judge that never contradicts itself.
    """
    from . import cost, store
    from .judge import offline_verdict

    domain = load_domain(domain_name)
    if version not in domain.policies:
        raise LookupError(f"unknown policy version {version!r} for {domain_name}; "
                          f"have {sorted(domain.policies)}")
    repeats = max(2, int(repeats))
    store.init_db()

    recorded: dict[str, str] = {}
    if target == "flips":
        rows = store.flips_for_policy(domain_name, version)
        if not rows:
            raise LookupError(f"no recorded flips for {domain_name}/{version} to "
                              f"confirm; replay it first")
        flips = [Flip(case_id=r["case_id"],
                      decided_at=datetime.fromisoformat(r["decided_at"]),
                      actual_outcome=r["actual_outcome"], new_outcome=r["new_outcome"],
                      rationale=r["rationale"], confidence=r["confidence"],
                      policy_clause=r["policy_clause"] or "", impact=r["impact"],
                      payload=json.loads(r["payload"]), direction=r["direction"])
                 for r in rows]
        chosen = flips_to_confirm(flips, cases)
        ids = [f.case_id for f in chosen]
        picked = store.load_cases(domain_name, until=store.now_utc(), case_ids=ids)
        if len(picked) != len(ids):
            # The DAG refuses here and so does this: a confirmation pass over a
            # subset marks the flips it reached as measured and leaves the rest
            # looking unmeasured for a reason that is about the loader rather
            # than about the judge.
            raise LookupError(
                f"{len(ids) - len(picked)} flipped case(s) could not be loaded; "
                f"refusing to report a confirmation pass over a subset")
        recorded = {f.case_id: f.new_outcome for f in chosen}
    else:
        loaded = store.load_cases(domain_name, until=store.now_utc())
        if not loaded:
            raise LookupError(f"no {domain_name} cases on file; seed the history first")
        picked = sample_cases(loaded, cases, seed=seed)

    samples = [
        {"case_id": c.case_id, "sample_idx": i,
         **offline_verdict(c, domain, version).model_dump(
             include={"outcome", "confidence", "policy_clause"})}
        for c in picked for i in range(repeats)
    ]
    result = analyse(samples, repeats)
    run_id = run_id or f"cli__{domain_name}__{version}__{store.now_utc():%Y%m%dT%H%M%S}"
    store.save_stability(run_id, domain_name, version, samples, result.model_dump(),
                         cost.zero() if OFFLINE else {})

    confirmations: list[FlipConfirmation] = []
    if target == "flips":
        confirmations = confirm(samples, recorded)
        store.save_flip_stability(domain_name, version, confirmations, run_id=run_id)

    # The flip rate this noise floor is the error bar *on*, so describe() can
    # say what share of the measured change the judge might be responsible for.
    totals = store.query(
        """SELECT COALESCE(SUM(flips),0) f, COALESCE(SUM(cases_replayed),0) c
           FROM runs WHERE domain=? AND policy_version=?""", (domain_name, version))[0]
    flip_rate = (totals["f"] / totals["c"]) if totals["c"] else None

    return {
        "domain": domain_name, "policy_version": version, "run_id": run_id,
        "target": target, "inert": OFFLINE,
        "report": result.model_dump(mode="json"),
        "confirmations": [c.model_dump(mode="json") for c in confirmations],
        "flips_confirmed": len(confirmations),
        "flips_unconfirmed": [c.case_id for c in confirmations if not c.stable],
        "flip_rate": flip_rate,
        "summary": describe(result, flip_rate),
        "confirmation_summary": (describe_confirmations(confirmations)
                                 if confirmations else ""),
        # Named rather than left to the reader, exactly as ptm.rules and
        # ptm.calibration name theirs. A band that is zero because the check is
        # switched off is worse than no band.
        "note": INERT_NOTE if OFFLINE else "",
        "note_key": "note.stability_inert" if OFFLINE else "",
    }


USAGE = """usage:
  python -m ptm.stability [domain] [version] [--target sample|flips]
                          [--cases N] [--samples N] [--seed N]
                          [--max-disagreement RATE] [--show] [--json]

How often the judge contradicts itself, asked from a shell. This is the error
bar on every flip rate the rest of the project reports, and it was the last
measurement here that could only be reached by triggering a DAG.

  domain              defaults to 'expenses'
  version             defaults to 'v2'
  --target sample     cases spread across the period, for the judge's overall
                      noise floor. The default.
  --target flips      re-judge the recorded flips instead, to confirm each one
                      before a human is asked to rule on it. Writes the result
                      onto the flip rows, which is what keeps an unconfirmed
                      flip out of the review queue.
  --cases N           how many cases to sample, or with target=flips how many
                      of the highest-impact flips to confirm (0 = all).
  --samples N         how many times to judge each case. Minimum 2.
  --seed N            same seed, same sample, so two runs are comparable.
  --max-disagreement  exit non-zero above this rate. Held off while the
                      measurement is inert, for the reason below.
  --show              print the last measurement on file instead of taking one.
  --json              the whole result as JSON.

With PTM_OFFLINE=1 the judge is deterministic and this necessarily reports 0%.
It says so on its own last line and refuses to gate on it, because a gate that
passes because the check is switched off is worse than no gate."""


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.stability [domain] [version]``; non-zero above the ceiling."""
    from . import store

    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0

    as_json = "--json" in args
    showing = "--show" in args
    values: dict[str, str] = {}
    for name in ("--target", "--cases", "--samples", "--seed", "--max-disagreement"):
        if name not in args:
            continue
        index = args.index(name)
        raw = args[index + 1] if index + 1 < len(args) else ""
        # "Looks like another option" rather than "starts with a dash": a seed
        # is a signed integer, so `--seed -1` is a value and not a missing one.
        # Every number these flags take is rejected on its own terms below, and
        # a range error that says "needs a value" sends the reader to look for
        # the wrong mistake.
        if not raw or raw.startswith("--") or raw in cli.HELP_FLAGS:
            print(f"ERROR {name} needs a value\n\n{USAGE}", file=sys.stderr)
            return 2
        values[name] = raw
        args.pop(index + 1)

    target = values.get("--target", "sample")
    if target not in {"sample", "flips"}:
        print(f"ERROR --target takes 'sample' or 'flips', got {target!r}\n\n{USAGE}",
              file=sys.stderr)
        return 2
    try:
        cases = int(values.get("--cases", 25))
        repeats = int(values.get("--samples", 3))
        seed = int(values.get("--seed", 7))
        ceiling = float(values.get("--max-disagreement", 1.0))
    except ValueError as exc:
        print(f"ERROR --cases, --samples and --seed take whole numbers and "
              f"--max-disagreement a rate: {exc}\n\n{USAGE}", file=sys.stderr)
        return 2
    if repeats < 2:
        # Refused rather than clamped. A case judged once cannot disagree with
        # itself, so this would report a 0% disagreement rate that is a property
        # of the request rather than of the judge - the exact reading this
        # module exists to stop anybody taking.
        print("ERROR --samples must be at least 2; a case judged once cannot disagree "
              "with itself, and reporting 0% from that would be a fact about the "
              "command rather than about the judge", file=sys.stderr)
        return 2
    if not 0.0 <= ceiling <= 1.0:
        print(f"ERROR --max-disagreement is a rate between 0 and 1, got {ceiling}",
              file=sys.stderr)
        return 2

    known = {"--json", "--show", "--target", "--cases", "--samples", "--seed",
             "--max-disagreement"}
    unknown = [a for a in args if a.startswith("--") and a not in known]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    positional = [a for a in args if not a.startswith("-")]
    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    store.init_db()
    if showing:
        from . import report as report_module

        try:
            stored = report_module.stability(domain_name, version)
        except LookupError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 2
        if as_json:
            print(json.dumps(stored, indent=2, default=str))
            return 0
        if not stored["measured"]:
            print(f"nothing measured for {domain_name}/{version}: {stored['hint']}")
            return 0
        print(f"last measured {str(stored['created_at'])[:19]} by "
              f"{stored['judge_model'] or 'an unrecorded judge'}: "
              f"{stored['unstable_cases']} of {stored['cases_sampled']} case(s) "
              f"disagreed with themselves over {stored['samples_per_case']} sample(s) "
              f"each ({stored['disagreement_rate']:.1%})")
        return 0

    try:
        result = run(domain_name, version, target, cases, repeats, seed)
    except (LookupError, FileNotFoundError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(result["summary"])
        if result["confirmation_summary"]:
            print()
            print(result["confirmation_summary"])
        if result["note"]:
            print(f"  {result['note']}")

    rate = result["report"]["disagreement_rate"]
    if rate > ceiling:
        if result["inert"]:
            # Unreachable while the offline judge is deterministic, and kept
            # anyway: this is the branch that fires the day somebody points the
            # offline judge at something that is not, and it must refuse to gate
            # on a measurement it has just called meaningless rather than fail a
            # run on it.
            print(f"\n{rate:.1%} is above the {ceiling:.1%} ceiling, but the judge here "
                  f"is the offline one - the gate is held off while the measurement is "
                  f"inert.", file=sys.stderr)
            return 0
        print(f"\nGATE FAILS: the judge disagreed with itself on {rate:.1%} of sampled "
              f"cases, above the {ceiling:.1%} ceiling. Flip rates measured with this "
              f"judge are not trustworthy enough to act on.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
