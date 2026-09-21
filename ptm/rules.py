"""The offline rules, written from the policy and then checked against the judge.

``offline_rules`` are the deterministic stand-in for the model: a small ordered
list of conditions in the domain YAML, maintained by hand next to - but
separate from - the markdown policy they are supposed to implement. Two things
follow from "separate", and the README says both out loud:

- the threshold sweep is computed from the rules, so it answers *what the rule
  evaluator would do*, not what a model reading the policy would do;
- the rules can drift from the policy, and :mod:`ptm.lint` catches only the
  mechanical half of that drift - a rule naming a field that does not exist, a
  rule citing a clause the policy does not contain. Nothing catches a rule that
  parses cleanly, cites a real clause, and is simply **wrong about what the
  policy says**.

This module closes both. :func:`build_prompt` asks a model to write the rules
*from the policy text*, which is the one job in this project where a model
reading prose and emitting structure is exactly the right tool. And
:func:`agreement` refuses to take its word for it: the rules are run over real
cases and scored against what the judge said about those same cases.

The agreement number is the honest part. A synthesised rule set that agrees
with the judge on 96% of six hundred cases has earned the sweep's credibility;
one that agrees on 70% has told you the sweep was never measuring the policy.

**Offline this is inert, and says so.** With ``PTM_OFFLINE=1`` the stored
verdicts were produced by these very rules, so agreement is 100% by
construction - the same caveat, for the same reason, as the stability figure.
:func:`ptm.report.rule_agreement` reports which judge produced the verdicts it
scored against, so the 100% cannot be quoted as if it meant something.
"""

from __future__ import annotations

import functools
import json
import sys

from . import cli, stats
from .config import DomainConfig, clauses_in, load_domain
from .judge import NO_RULE_RATIONALE, offline_verdict
from .lint import payload_fields, probe, probe_cases
from .models import Case, RuleSet, Verdict
from .safe_eval import check_expression, shadowed

SYNTHESIS_SYSTEM_PROMPT = (
    "You translate written policy into a small ordered list of mechanical "
    "rules. You are not interpreting the policy generously or filling gaps: "
    "where the policy is silent you leave a gap, and say so in the notes. Every "
    "rule you write must be traceable to a specific numbered clause, and must "
    "read only the case fields you are given - a rule that mentions anything "
    "else can never match, and a rule that never matches makes the replay "
    "silently wrong rather than visibly broken."
)

PROMPT = """Write the mechanical rules that implement this {label} policy.

# The policy (version {version})
{policy}

# The only fields a rule may read
{fields}

# The only outcomes a rule may return
{outcomes}

# How the rules are evaluated
They are tried in order and the **first one that matches decides the case**. A
case matching no rule at all gets {default!r}, the most generous outcome, so
that is the behaviour you get for free and should not write a rule for.

Order therefore matters: put the specific exemptions before the general
restrictions they carve out of, or the restriction fires first and the exemption
never runs.

Each `when` is a single Python boolean expression over the fields above -
comparisons, `and`, `or`, `not`, and the helpers abs, len, min, max, float, int,
str, round, sum. Nothing else: no imports, no attribute access, no function
definitions.

Cite the clause each rule implements. A rule that cites nothing produces
changes nobody can attribute to a sentence, which is the whole output this
pipeline exists to produce."""


def build_prompt(domain: DomainConfig, version: str, policy_text: str | None = None) -> str:
    """The prompt that asks a model to write ``offline_rules`` for one version.

    ``policy_text`` overrides what is on disk, which is how the proposer asks
    for rules covering a draft it has written but not yet published - the rules
    have to exist before the draft is written, or an offline replay of it would
    match nothing and report a policy that approves everything.
    """
    return PROMPT.format(
        label=domain.label,
        version=version,
        policy=domain.policy_text(version) if policy_text is None else policy_text,
        fields="\n".join(f"  {name}" for name in sorted(payload_fields(domain))),
        outcomes=", ".join(domain.outcomes),
        default=domain.outcomes[0],
    )


def as_offline_rules(ruleset: RuleSet) -> list[dict]:
    """A :class:`~ptm.models.RuleSet` in the shape the YAML and the judge expect."""
    return [
        {"when": r.when, "outcome": r.outcome, "clause": r.clause,
         "because": r.because, "confidence": r.confidence}
        for r in ruleset.rules
    ]


def validate(rules: list[dict], domain: DomainConfig, version: str,
             policy_text: str | None = None,
             cases: list[Case] | None = None) -> list[str]:
    """Reject a rule set before it is allowed anywhere near a replay.

    The same checks :mod:`ptm.lint` runs over hand-written rules, applied to
    generated ones at the moment they arrive. A generated rule reading a field
    that does not exist is exactly the failure the lint was written for, and a
    model is a far more prolific source of it than a person editing YAML.

    ``policy_text`` overrides what is on disk, and matters for exactly the same
    reason it does on :func:`build_prompt`: the proposer asks for rules covering
    a draft it has written but not yet published, so there is no version to
    resolve. Validating them against the *base* version instead - which is what
    this did - rejects every rule implementing a clause the draft introduces,
    and since a rule set is ordered and first-match-wins, one rejection drops
    the whole set. The draft then ships with no offline rules at all, and an
    offline replay of it returns the most generous outcome for every case:
    a policy nobody wrote, looking wildly permissive. Adding a clause is the
    normal case for a drafter - :func:`ptm.proposal.apply_to_markdown` appends
    one under its own heading on purpose - so this had to read the draft.

    ``cases`` are what the rules are *run* against, because the checks above
    are all static and there is one failure none of them can see. A model
    writing ``amount_gbp > '75'`` - the threshold quoted, the likeliest single
    mistake here - produces a rule that parses, reads a real field, cites a real
    clause and raises the moment it meets a numeric field. Offline that refusal
    reads as "does not match", so the rule decides nothing and the replay is
    quietly wrong rather than visibly broken. Default None loads them; an empty
    list skips the probe, and no history on file skips it too.

    Only a rule refused by **every** case probed is reported. A rule that simply
    matches nothing is a warning :mod:`ptm.lint` makes and not grounds for
    rejection: a rule set is ordered and first-match-wins, so one entry dropped
    takes the whole set with it, and a fixture with no case for a legitimate
    rule is an ordinary thing for a fixture to be.

    This is a report, not the containment. :mod:`ptm.safe_eval` computes a rule
    by walking it rather than by calling ``eval``, so an expression that gets
    past this one still cannot do anything but arithmetic and comparison. The
    two are separate on purpose: a validator is the kind of thing that acquires
    a gap, and a gap in this one used to mean handing over the interpreter.
    """
    known = payload_fields(domain)
    declared = set(clauses_in(policy_text) if policy_text is not None
                   else domain.clauses(version))
    problems: list[str] = []
    for i, rule in enumerate(rules):
        at = f"rule[{i}]"
        expression = str(rule.get("when") or "")
        if not expression:
            problems.append(f"{at}: has no 'when' expression")
            continue
        # The whole check, not a name scan. A generated expression made of
        # attribute access reads no bare names at all, so the scan this replaced
        # reported nothing wrong with one and let it through to be evaluated.
        problems += [f"{at}: {problem}" for problem in check_expression(expression, known)]
        if rule.get("outcome") not in domain.outcomes:
            problems.append(f"{at}: outcome {rule.get('outcome')!r} is not one of {domain.outcomes}")
        clause = str(rule.get("clause") or "").strip()
        if clause and declared and clause not in declared:
            problems.append(f"{at}: cites clause {clause!r}, absent from policy {version}")
    # The rules, run. Last of the per-rule checks because it is the only one
    # that needs a database, and the only one that can be silent for a reason
    # that is about the deployment rather than about the rules.
    for row in probe(rules, probe_cases(domain) if cases is None else cases):
        if row["always_refused"]:
            problems.append(
                f"rule[{row['rule_index']}]: is refused by every one of the "
                f"{row['probed']} case(s) probed, so it can never fire: {row['reason']}. "
                f"Nothing above catches this - the rule parses, reads real fields and "
                f"cites a real clause - and offline the refusal reads as 'does not "
                f"match', so clause {row['clause'] or '-'} would simply never be cited.")

    # Unreachable rules, last, because the check is about the list rather than
    # about any one entry. A generated set is ordered by a model that was told
    # order matters, and the way that goes wrong is a general restriction
    # written above the exemption it was supposed to carve out of: the
    # exemption then never fires, its clause is never cited, and the replay is
    # confidently wrong about every case it was written for. Nothing else here
    # can see it - the rule parses, reads real fields and cites a real clause.
    for row in shadowed(rules):
        problems.append(
            f"rule[{row['rule_index']}]: can never fire - rule[{row['shadowed_by']}] "
            f"({row['shadowed_by_expression']!r} -> {row['shadowed_by_outcome']}) already "
            f"matches every case {row['expression']!r} would. Put the specific rule first.")
    return problems


def with_rules(domain: DomainConfig, version: str, rules: list[dict]) -> DomainConfig:
    """A copy of the domain whose ``version`` is evaluated by ``rules``.

    A copy rather than a mutation: the cached :func:`ptm.config.load_domain`
    object is shared by every caller in the process, and a candidate rule set
    scoring itself against the judge must not become the rule set the next
    replay uses.
    """
    merged = {**domain.offline_rules, version: rules}
    return domain.model_copy(update={"offline_rules": merged})


def agreement(domain: DomainConfig, version: str, rules: list[dict],
              cases: list[Case], verdicts: dict[str, Verdict]) -> dict:
    """Score a rule set against what the judge said about the same cases.

    ``verdicts`` is the judge's stored answer per case - normally
    :func:`ptm.store.latest_verdicts` for this version. Cases with no verdict on
    file are skipped rather than scored: the rules are being compared to the
    judge, and a case the judge never saw has no comparison to make.

    Two numbers come back and they are not the same question. **Outcome
    agreement** is whether the rules reach the same decision. **Clause
    agreement** is whether they reach it for the same stated reason - the
    weaker-looking number that actually governs whether the attribution panel,
    and therefore the sweep, is telling the truth.
    """
    candidate = with_rules(domain, version, rules)
    compared = agreed = clause_compared = clause_agreed = unmatched = 0
    disagreements: list[dict] = []
    for case in cases:
        judged = verdicts.get(case.case_id)
        if judged is None:
            continue
        mine = offline_verdict(case, candidate, version)
        compared += 1
        if mine.rationale == NO_RULE_RATIONALE:
            unmatched += 1
        if mine.outcome == judged.outcome:
            agreed += 1
        else:
            disagreements.append({
                "case_id": case.case_id,
                "rule_outcome": mine.outcome,
                "judge_outcome": judged.outcome,
                "rule_clause": mine.policy_clause,
                "judge_clause": judged.policy_clause,
                "judge_rationale": judged.rationale,
            })
        if mine.policy_clause and judged.policy_clause:
            clause_compared += 1
            clause_agreed += int(mine.policy_clause == judged.policy_clause)

    band = stats.rate(agreed, compared)
    disagreements.sort(key=lambda d: d["case_id"])
    return {
        "domain": domain.name,
        "policy_version": version,
        "compared": compared,
        "agreed": agreed,
        **band,
        # Rules that fired on nothing. A high number here with high agreement
        # means the rules agree by defaulting, which is agreement with the
        # judge's easy cases and no evidence about anything else.
        "unmatched": unmatched,
        "clause_compared": clause_compared,
        "clause_agreed": clause_agreed,
        "clause_agreement": round(clause_agreed / clause_compared, 4) if clause_compared else 0.0,
        "disagreements": disagreements[:50],
    }


def gate(result: dict, domain: DomainConfig) -> list[str]:
    """Where the rules have drifted further from the judge than the domain allows.

    Returns the reasons, or an empty list. Two things deliberately do not fire
    it, and both would otherwise make it worse than useless:

    **An inert measurement.** Offline the verdicts being scored against were
    produced by these same rules, so agreement is 1.0 by construction. A gate
    that passes because the check is switched off is a gate that teaches people
    to trust a number that means nothing.

    **Nothing measured.** No verdicts on file is not 0% agreement; it is no
    evidence. Failing there would make the gate fire loudest on a project that
    has not run yet.
    """
    policy = domain.rules
    if not result.get("compared") or result.get("inert"):
        return []
    problems = []
    outcome_floor = policy.min_outcome_agreement
    clause_floor = policy.min_clause_agreement
    if outcome_floor and result.get("rate", 0.0) < outcome_floor:
        problems.append(
            f"the offline rules reach the judge's outcome on {result['rate']:.1%} of "
            f"{result['compared']} case(s), below the {outcome_floor:.1%} this domain "
            f"requires. Every threshold curve is computed from these rules, so a sweep "
            f"drawn now describes the rules rather than the policy.")
    if clause_floor and result.get("clause_agreement", 0.0) < clause_floor:
        problems.append(
            f"they cite the same clause on {result['clause_agreement']:.1%} of the "
            f"{result['clause_compared']} case(s) where both named one, below the "
            f"{clause_floor:.1%} required. Right answer, wrong sentence - which is what "
            f"the attribution panel reports and what the sweep moves.")
    return problems


def describe(result: dict) -> str:
    if not result.get("compared"):
        return (f"no stored verdicts for {result.get('domain')}/{result.get('policy_version')} "
                f"to score the rules against; run the replay first")
    lines = [
        f"offline rules vs the judge, {result['compared']} case(s) under policy "
        f"{result['policy_version']}",
        f"  same outcome on {result['agreed']}  -  "
        f"{stats.describe_rate(result)}",
        f"  same clause cited on {result['clause_agreed']} of {result['clause_compared']} "
        f"({result['clause_agreement']:.1%}) - this is the number the sweep rests on",
    ]
    if result["unmatched"]:
        lines.append(f"  {result['unmatched']} case(s) matched no rule and took the default "
                     f"outcome; agreement on those is agreement by accident")
    for d in result["disagreements"][:5]:
        lines.append(f"    {d['case_id']}: rules say '{d['rule_outcome']}' "
                     f"(clause {d['rule_clause'] or '-'}), judge says '{d['judge_outcome']}' "
                     f"(clause {d['judge_clause'] or '-'})")
    return "\n".join(lines)


USAGE = """usage:
  python -m ptm.rules [domain] [version] [--json]

Do the offline rules agree with the judge they stand in for? Every threshold
curve is computed from these rules, so this is the number the sweep rests on.

  domain    defaults to 'expenses'
  version   defaults to 'v2'
  --json    the agreement, clause by clause, on stdout with the gate's verdict
            in it, and the prose on stderr. The same argument ptm.gate makes:
            a gate that can only say pass or fail leaves a CI step reporting
            that something was below its floor rather than which clause.

Exits non-zero when the domain's rules.gate is 'fail' and agreement is below
its floor. Inert with PTM_OFFLINE=1, where the verdicts being scored against
were produced by these same rules - it says so and does not gate."""


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.rules [domain] [version]`` - score the shipped rules."""
    args = list(argv if argv is not None else sys.argv[1:])
    if cli.wants_help(args):
        print(USAGE)
        return 0
    as_json = "--json" in args
    say = functools.partial(print, file=sys.stderr if as_json else sys.stdout)
    positional = [a for a in args if not a.startswith("-")]
    # A misspelled flag would otherwise be dropped on the floor rather than
    # refused - see the note in ptm.calibration.main, which had the same shape.
    unknown = [a for a in args if a.startswith("-") and a != "--json"]
    if unknown:
        print(f"ERROR unknown option {unknown[0]!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    domain_name = positional[0] if positional else "expenses"
    version = positional[1] if len(positional) > 1 else "v2"

    from . import report as report_module

    def answer(result: dict, code: int, problems: list[str]) -> int:
        if as_json:
            json.dump({**result, "code": code, "passed": not problems,
                       "gate_problems": problems}, sys.stdout, indent=2, default=str)
            print()
        return code

    try:
        result = report_module.rule_agreement(domain_name, version)
    except LookupError as exc:
        if as_json:
            json.dump({"error": str(exc), "domain": domain_name, "version": version,
                       "code": 2, "compared": 0, "passed": False},
                      sys.stdout, indent=2)
            print()
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    say(describe(result))
    if result.get("inert"):
        say(f"  measured against verdicts produced by {result['judged_by'] or ['the offline judge']}, "
            f"which is these same rules - so this figure is 100% by construction and "
            f"means nothing until PTM_OFFLINE=0")
        return answer(result, 0, [])
    problems = gate(result, load_domain(domain_name))
    for problem in problems:
        print(f"GATE  {problem}", file=sys.stderr)
    code = 1 if problems and load_domain(domain_name).rules.gate == "fail" else 0
    return answer(result, code, problems)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


# Re-exported so callers need one import to go from a policy to scored rules.
__all__ = ["SYNTHESIS_SYSTEM_PROMPT", "build_prompt", "as_offline_rules", "validate",
           "with_rules", "agreement", "gate", "describe", "load_domain", "RuleSet"]
