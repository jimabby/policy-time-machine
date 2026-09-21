"""Read the policy before paying to replay it.

A full LLM-backed replay of the shipped fixture is about USD 4, and twice that
with the baseline pass. The failure worth avoiding is not an expensive run - it
is an expensive run whose output cannot be used: a third of the cases coming
back attributed to ``(no clause applies)`` because half the policy's rules were
written as prose with no clause number on them, or two different rules sharing
the number 3.1 so attribution merges them into one bucket and names the wrong
sentence to edit.

Every one of those is visible by reading the policy. This reads it.

**The division of labour with the lint.** :mod:`ptm.lint` checks the *offline
fixtures* against the policy - a rule naming a field that does not exist, a rule
citing a clause the policy does not contain. This checks the **policy markdown
itself**, which is the artefact the real judge is shown and the one the lint
never looks inside. Neither subsumes the other, and running the pair costs
nothing.

**The structural pass is free and offline.** It is the half that runs in CI, at
the head of every replay, and on a laptop with no key. The optional model pass
on top of it - :func:`build_prompt` - is for what structure cannot see:
two clauses that contradict each other, a threshold stated in two units, a
sentence that three readers would apply three ways. One call, cents, before the
four dollars.
"""

from __future__ import annotations

import functools
import json
import re
import sys
from collections import Counter

from . import cli
from .config import DomainConfig, load_domain
from .models import PolicyFinding

#: A clause: ``1.1 text...`` at the start of a line. The same pattern
#: :meth:`ptm.config.DomainConfig.clauses` uses, kept compatible on purpose -
#: reporting a clause the rest of the project cannot see would be worse than
#: not reporting it.
CLAUSE = re.compile(r"^\s*(\d+\.\d+)\s+(.*)$", re.MULTILINE)

#: A reference from one clause to another, e.g. "exempt from the receipt
#: requirement in clause 1.1". Only two-part numbers: policies legitimately say
#: "unless clause 3 applies", meaning a whole section.
CROSS_REFERENCE = re.compile(r"\bclauses?\s+(\d+\.\d+)")

#: Deontic vocabulary - the words that make a sentence a rule rather than
#: narration. Deliberately not domain vocabulary: this module must stay as
#: ignorant of expenses and refunds as everything else outside the YAML.
DEONTIC = re.compile(
    r"\b(must|shall|may|required?|requires|prohibited|permitted|exempt|entitled|"
    r"eligible|ineligible|obliged|obligated|forbidden)\b", re.IGNORECASE)

#: A clause defined by pointing at a *different policy version* - "unchanged
#: from v1". The judge is shown exactly one policy, so such a clause is empty at
#: judging time. The version reference is required rather than optional:
#: "Unchanged - full refund on proper notice" says what it means and is fine.
BY_REFERENCE = re.compile(
    r"\b(?:unchanged|as (?:in|per)|see|refer to)\b[^.]{0,40}?"
    r"\b(?:v\d+|(?:previous|prior|earlier) version)\b", re.IGNORECASE)


def structural(domain: DomainConfig, version: str) -> list[PolicyFinding]:
    """Findings readable from the policy text alone. No model, no network, no cost."""
    text = domain.policy_text(version)
    found: list[PolicyFinding] = []
    clauses = CLAUSE.findall(text)
    numbers = [number for number, _ in clauses]
    bodies = {number: body.strip() for number, body in clauses}

    if not clauses:
        return [PolicyFinding(
            kind="unnumbered", severity="error",
            detail=f"policy {version} declares no numbered clauses at all. Attribution "
                   f"reports against clause numbers, so every change this policy causes "
                   f"would come back as unattributed.")]

    for number, count in sorted(Counter(numbers).items()):
        if count > 1:
            found.append(PolicyFinding(
                clause=number, kind="duplicate", severity="error",
                detail=f"clause {number} is defined {count} times. Attribution groups on "
                       f"the clause identifier, so these merge into one bucket and the "
                       f"breakdown names the wrong sentence to edit."))

    for number in sorted(set(CROSS_REFERENCE.findall(text)) - set(numbers)):
        found.append(PolicyFinding(
            clause=number, kind="unreachable", severity="error",
            detail=f"the policy refers to clause {number}, which it does not define. A "
                   f"judge asked to apply it has nothing to read."))

    for number, body in sorted(bodies.items()):
        if BY_REFERENCE.search(body):
            found.append(PolicyFinding(
                clause=number, kind="unreachable", severity="warning",
                detail=f"clause {number} is defined only by reference to another version "
                       f"({body!r}). The judge is shown one policy at a time, so this "
                       f"clause is empty at judging time - inline the text if it is meant "
                       f"to decide anything."))

    for line in _uncounted_rule_lines(text):
        found.append(PolicyFinding(
            kind="unnumbered", severity="warning",
            detail=f"reads like a rule but carries no clause number: {line!r}. The judge "
                   f"is asked to cite a clause, so anything this decides is attributed to "
                   f"'(no clause applies)'."))

    words = set(re.findall(r"[a-z]+", text.lower()))
    for outcome in domain.outcomes:
        if not _mentions(words, outcome):
            found.append(PolicyFinding(
                kind="undefined_outcome", severity="warning",
                detail=f"the outcome {outcome!r} is never mentioned in policy {version}. "
                       f"The judge may only return outcomes from this domain's list, so it "
                       f"can reach this one by inference or not at all."))

    order = {"error": 0, "warning": 1}
    found.sort(key=lambda f: (order.get(f.severity, 2), f.clause, f.kind))
    return found


def _mentions(words: set[str], outcome: str) -> bool:
    """Whether a policy plausibly talks about an outcome, matched on word stems.

    Outcomes are identifiers (``no_refund``, ``deny``) and policies are prose
    (``no refund is payable``, ``the claim is denied``), so exact matching finds
    nothing and reports every domain as broken. Three characters of each part is
    a crude stem, and crude in the safe direction: a false match means no
    finding, where a false finding means the panel stops being read.
    """
    for part in (p for p in outcome.lower().split("_") if len(p) >= 3):
        stem = part[:3]
        if not any(word.startswith(stem) for word in words):
            return False
    return True


def _uncounted_rule_lines(text: str) -> list[str]:
    """Lines at the left margin that state an obligation without a clause number.

    Indented lines are continuations of the clause above, headings are
    navigation, and blank lines are blank. What is left is prose standing on its
    own - fine when it is a rationale, a problem when it is a rule.
    """
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        if CLAUSE.match(raw):
            continue
        if DEONTIC.search(raw):
            out.append(raw.strip()[:120])
    return out


REVIEW_SYSTEM_PROMPT = (
    "You are reviewing a written policy for whether it can be applied "
    "mechanically and consistently by someone who has only this document. You "
    "are not asked whether the policy is wise, fair or well-drafted, and you "
    "must not suggest improvements to its substance. You are looking for places "
    "where two careful readers would reach different outcomes on the same case: "
    "clauses that contradict each other, thresholds stated twice with different "
    "values or units, terms used without definition, and cases the policy "
    "plainly does not cover. Report nothing when there is nothing to report."
)

PROMPT = """Review this {label} policy for ambiguity and contradiction.

# The policy (version {version})
{policy}

# The only outcomes an adjudicator may return
{outcomes}

# What to report
For each problem, give the clause it concerns, one of these kinds, and one or
two sentences of detail:

  contradiction  two clauses that cannot both be satisfied, or that give
                 different answers to the same case
  ambiguous      a clause two careful readers would apply differently - an
                 undefined term, an unstated unit, a threshold with no
                 tie-break at the boundary
  unreachable    a clause no case could ever trigger, given the others
  undefined_outcome
                 a case the policy covers but whose result is not one of the
                 outcomes listed above

Mark severity 'error' only where a replay would produce results that are wrong
or cannot be attributed to a clause. Everything else is 'warning'.

Do not report clause numbering, formatting or cross-reference problems: those
are checked mechanically before you see this and repeating them buries the
findings only you can make."""


def build_prompt(domain: DomainConfig, version: str) -> str:
    """The prompt for the optional model pass. Structural findings are excluded.

    The model is told not to repeat what the free check already found. Anything
    it says about numbering is noise that pushes the ambiguity findings - the
    only ones worth paying for - off the bottom of the panel.
    """
    return PROMPT.format(
        label=domain.label,
        version=version,
        policy=domain.policy_text(version),
        outcomes=", ".join(domain.outcomes),
    )


def blocking(findings: list[PolicyFinding]) -> list[PolicyFinding]:
    """The findings worth refusing to spend a backfill on."""
    return [f for f in findings if f.severity == "error"]


def describe(findings: list[PolicyFinding], version: str) -> str:
    if not findings:
        return f"preflight: policy {version} is structurally sound"
    errors = blocking(findings)
    lines = [f"preflight: {len(findings)} finding(s) in policy {version}, "
             f"{len(errors)} of them blocking"]
    for f in findings:
        where = f"clause {f.clause}" if f.clause else "policy"
        lines.append(f"  {f.severity.upper():7} {where:<12} [{f.kind}] {f.detail}")
    return "\n".join(lines)


def check(domain_name: str, version: str) -> list[PolicyFinding]:
    """Structural findings for one domain and version."""
    return structural(load_domain(domain_name), version)


USAGE = """usage:
  python -m ptm.preflight [domain] [version] [--json]

Read a policy for problems before paying to replay it. Structural only: clause
numbering, cross-references, outcomes the policy never mentions. No model, no
network, milliseconds.

  domain    defaults to 'expenses'
  version   defaults to every version the domain declares
  --json    every finding on stdout, with its clause, severity and the exit
            code, and the prose on stderr. This runs before the money is spent,
            so it is the one most likely to be read by something other than a
            person - a pre-commit hook, or the step that decides whether the
            backfill is worth starting.

Exits non-zero when a finding is blocking - a replay would run and its results
would not be attributable to any sentence."""


def main(argv: list[str] | None = None) -> int:
    """``python -m ptm.preflight [domain] [version]``; non-zero on a blocking finding."""
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

    def refuse(message: str) -> int:
        if as_json:
            json.dump({"error": message, "domain": domain_name, "code": 2,
                       "ran": False}, sys.stdout, indent=2)
            print()
        print(f"ERROR {message}", file=sys.stderr)
        return 2

    try:
        domain = load_domain(domain_name)
    except FileNotFoundError as exc:
        return refuse(str(exc))
    versions = [positional[1]] if len(positional) > 1 else sorted(domain.policies)

    blocked = 0
    checked = []
    for version in versions:
        if version not in domain.policies:
            return refuse(f"unknown policy version {version!r}; have "
                          f"{sorted(domain.policies)}")
        findings = structural(domain, version)
        blocked += len(blocking(findings))
        say(describe(findings, version))
        checked.append({
            "version": version,
            "findings": [f.model_dump(mode="json") for f in findings],
            "blocking": len(blocking(findings)),
            "summary": describe(findings, version),
        })
    say(f"\n{len(versions)} version(s) checked: {blocked} blocking finding(s)")
    code = 1 if blocked else 0
    if as_json:
        json.dump({"domain": domain_name, "versions": checked, "blocking": blocked,
                   "code": code, "passed": not blocked, "ran": True},
                  sys.stdout, indent=2, default=str)
        print()
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
